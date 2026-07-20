from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from string import Formatter
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import CommercialDataCycle, InventorySnapshot, PricePolicy
from app.settings import Settings


class QuoteContextStatus(StrEnum):
    WAITING = "WAITING"
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class QuoteContext:
    cycle: CommercialDataCycle
    policy: PricePolicy | None
    inventory: InventorySnapshot | None
    status: QuoteContextStatus
    reason: str
    next_check_at: datetime | None = None

    @property
    def ready_stock_available(self) -> bool:
        return self.status is QuoteContextStatus.AVAILABLE


@runtime_checkable
class CommercialDataProvider(Protocol):
    async def get_quote_context(
        self,
        session: AsyncSession,
        *,
        product_id: int,
        currency: str,
        requested_quantity: Decimal | int | None = None,
        at: datetime | None = None,
    ) -> QuoteContext: ...


def _business_local(settings: Settings, at: datetime | None = None) -> datetime:
    value = at or datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("business clock requires a timezone-aware datetime")
    return value.astimezone(ZoneInfo(settings.business_timezone))


def business_week_bounds(
    settings: Settings,
    at: datetime | None = None,
) -> tuple[date, date]:
    """Return Monday through Friday for the business-local calendar week."""

    local_day = _business_local(settings, at).date()
    week_start = local_day - timedelta(days=local_day.weekday())
    return week_start, week_start + timedelta(days=4)


def is_business_day(settings: Settings, at: datetime | None = None) -> bool:
    return _business_local(settings, at).weekday() < 5


def is_business_open(settings: Settings, at: datetime | None = None) -> bool:
    local = _business_local(settings, at)
    return local.weekday() < 5 and local.time() >= time(settings.business_open_hour)


def next_business_open(settings: Settings, at: datetime | None = None) -> datetime:
    """Return the earliest business opening at or after *at*, normalized to UTC."""

    local = _business_local(settings, at)
    opening = local.replace(
        hour=settings.business_open_hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    if local.weekday() < 5 and local <= opening:
        return opening.astimezone(UTC)

    candidate = opening + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def _next_check(settings: Settings, at: datetime) -> datetime:
    local = _business_local(settings, at)
    candidate = local + timedelta(minutes=settings.commercial_retry_minutes)
    if candidate.weekday() < 5 and candidate.time() >= time(settings.business_open_hour):
        return candidate.astimezone(UTC)
    return next_business_open(settings, candidate)


def _cycle_lookup(scope: str, week_start: date):
    return select(CommercialDataCycle).where(
        CommercialDataCycle.scope == scope,
        CommercialDataCycle.week_start == week_start,
    )


async def get_or_create_current_cycle(
    session: AsyncSession,
    settings: Settings,
    *,
    at: datetime | None = None,
    scope: str | None = None,
) -> CommercialDataCycle:
    """Get this business week's cycle without committing the caller's transaction.

    The unique constraint is the concurrency authority. A savepoint contains a
    losing insert so that the caller's outer transaction remains usable.
    """

    cycle_scope = (scope or settings.commercial_scope).strip() or "default"
    week_start, week_end = business_week_bounds(settings, at)
    existing = await session.scalar(_cycle_lookup(cycle_scope, week_start))
    if existing is not None:
        return existing

    candidate = CommercialDataCycle(
        scope=cycle_scope,
        week_start=week_start,
        week_end=week_end,
        price_status="PENDING",
        inventory_status="PENDING",
        reminder_status="PENDING",
        metadata_json={},
    )
    try:
        async with session.begin_nested():
            session.add(candidate)
            await session.flush()
    except IntegrityError:
        winner = await session.scalar(_cycle_lookup(cycle_scope, week_start))
        if winner is None:
            raise
        return winner
    return candidate


class LocalDatabaseCommercialDataProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def get_quote_context(
        self,
        session: AsyncSession,
        *,
        product_id: int,
        currency: str,
        requested_quantity: Decimal | int | None = None,
        at: datetime | None = None,
    ) -> QuoteContext:
        observed_at = at or datetime.now(UTC)
        local = _business_local(self.settings, observed_at)
        cycle = await get_or_create_current_cycle(
            session,
            self.settings,
            at=observed_at,
        )
        policy = await session.scalar(
            select(PricePolicy)
            .where(
                PricePolicy.commercial_cycle_id == cycle.id,
                PricePolicy.product_id == product_id,
                PricePolicy.currency == currency.strip().upper(),
                PricePolicy.active.is_(True),
                PricePolicy.valid_from <= local.date(),
                or_(PricePolicy.valid_to.is_(None), PricePolicy.valid_to >= local.date()),
            )
            .order_by(PricePolicy.valid_from.desc(), PricePolicy.id.desc())
        )
        inventory = await session.scalar(
            select(InventorySnapshot).where(
                InventorySnapshot.cycle_id == cycle.id,
                InventorySnapshot.product_id == product_id,
            )
        )

        if local.weekday() >= 5:
            return QuoteContext(
                cycle,
                policy,
                inventory,
                QuoteContextStatus.WAITING,
                "NON_BUSINESS_DAY",
                next_business_open(self.settings, observed_at),
            )
        if not is_business_open(self.settings, observed_at):
            return QuoteContext(
                cycle,
                policy,
                inventory,
                QuoteContextStatus.WAITING,
                "BEFORE_BUSINESS_OPEN",
                next_business_open(self.settings, observed_at),
            )
        if cycle.price_status.upper() != "CONFIRMED":
            return QuoteContext(
                cycle,
                policy,
                inventory,
                QuoteContextStatus.WAITING,
                "PRICE_CONFIRMATION_PENDING",
                _next_check(self.settings, observed_at),
            )
        if cycle.inventory_status.upper() != "CONFIRMED":
            return QuoteContext(
                cycle,
                policy,
                inventory,
                QuoteContextStatus.WAITING,
                "INVENTORY_CONFIRMATION_PENDING",
                _next_check(self.settings, observed_at),
            )
        if policy is None:
            return QuoteContext(
                cycle,
                None,
                inventory,
                QuoteContextStatus.UNAVAILABLE,
                "PRICE_POLICY_UNAVAILABLE",
            )
        if inventory is None:
            return QuoteContext(
                cycle,
                policy,
                None,
                QuoteContextStatus.UNAVAILABLE,
                "INVENTORY_SNAPSHOT_UNAVAILABLE",
            )

        availability = inventory.availability.strip().upper()
        if availability in {"UNAVAILABLE", "OUT_OF_STOCK", "NO_STOCK"} or (
            inventory.quantity is not None and inventory.quantity <= 0
        ):
            return QuoteContext(
                cycle,
                policy,
                inventory,
                QuoteContextStatus.UNAVAILABLE,
                "INVENTORY_UNAVAILABLE",
            )
        if (
            requested_quantity is not None
            and inventory.quantity is not None
            and inventory.quantity < Decimal(str(requested_quantity))
        ):
            return QuoteContext(
                cycle,
                policy,
                inventory,
                QuoteContextStatus.UNAVAILABLE,
                "INVENTORY_INSUFFICIENT",
            )
        if availability not in {"AVAILABLE", "IN_STOCK", "READY_STOCK"}:
            return QuoteContext(
                cycle,
                policy,
                inventory,
                QuoteContextStatus.WAITING,
                "INVENTORY_STATUS_UNKNOWN",
                _next_check(self.settings, observed_at),
            )
        return QuoteContext(
            cycle,
            policy,
            inventory,
            QuoteContextStatus.AVAILABLE,
            "COMMERCIAL_DATA_AVAILABLE",
        )


def get_commercial_data_provider(settings: Settings) -> CommercialDataProvider:
    """Resolve the configured commercial-data adapter in one place.

    Mail policy calls this boundary only; a future CRM/WMS implementation can
    be registered here without changing quote generation or send-time guards.
    """

    if settings.commercial_data_provider == "database":
        return LocalDatabaseCommercialDataProvider(settings)
    raise RuntimeError(
        f"unsupported commercial data provider: {settings.commercial_data_provider}"
    )


def commercial_scope_lock_key(scope: str) -> int:
    digest = hashlib.blake2b(
        f"aiemail-commercial:{scope}".encode(),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


async def lock_commercial_scope(session: AsyncSession, scope: str) -> None:
    """Serialize commercial writes and the final auto-quote send claim.

    The transaction-scoped PostgreSQL advisory lock complements the cycle row
    lock. Price replacement refuses to proceed while an auto quote is already
    CLAIMED/UNKNOWN, closing the check-to-SMTP version race without holding a
    database transaction open during the network call.
    """

    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": commercial_scope_lock_key(scope)},
    )


def _render_http_url(
    template: str,
    *,
    allowed_fields: set[str],
    values: dict[str, object],
) -> str:
    try:
        parsed_fields = list(Formatter().parse(template))
    except ValueError as exc:
        raise ValueError("invalid URL template") from exc
    for _, field_name, format_spec, conversion in parsed_fields:
        if field_name is None:
            continue
        if field_name not in allowed_fields:
            raise ValueError(f"unsupported URL template placeholder: {field_name}")
        if format_spec or conversion:
            raise ValueError("URL template formatting and conversions are not supported")
    try:
        rendered = template.format_map(values)
    except (KeyError, ValueError) as exc:
        raise ValueError("invalid URL template") from exc
    if any(character.isspace() or ord(character) < 32 for character in rendered):
        raise ValueError("URL must not contain whitespace or control characters")
    parts = urlsplit(rendered)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc or not parts.hostname:
        raise ValueError("URL must be an absolute http(s) URL")
    return rendered


def review_link(settings: Settings, handoff_id: int, case_id: int | None) -> str:
    template = settings.crm_review_url_template or (
        f"{settings.public_base_url.rstrip('/')}/admin/handoffs/{{handoff_id}}/review"
    )
    return _render_http_url(
        template,
        allowed_fields={"handoff_id", "case_id"},
        values={
            "handoff_id": handoff_id,
            "case_id": case_id if case_id is not None else "unmatched",
        },
    )


def commercial_update_link(
    settings: Settings,
    cycle: CommercialDataCycle | None = None,
    *,
    at: datetime | None = None,
) -> str:
    week_start, week_end = business_week_bounds(settings, at)
    template = settings.commercial_update_url or (
        f"{settings.public_base_url.rstrip('/')}/admin/commercial/current/update"
    )
    return _render_http_url(
        template,
        allowed_fields={"cycle_id", "week_start", "week_end", "scope"},
        values={
            "cycle_id": cycle.id if cycle is not None else "current",
            "week_start": cycle.week_start.isoformat() if cycle is not None else week_start.isoformat(),
            "week_end": cycle.week_end.isoformat() if cycle is not None else week_end.isoformat(),
            "scope": cycle.scope if cycle is not None else settings.commercial_scope,
        },
    )
