"""Commercial data refresh and quote-context coordination."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs import JobDeferred, enqueue_job
from app.quotations.commercial import (
    QuoteContext,
    QuoteContextStatus,
    get_commercial_data_provider,
    get_or_create_current_cycle,
    is_commercial_day,
    is_commercial_open,
)
from app.settings import Settings, get_settings


async def ensure_weekly_commercial_refresh(
    session: AsyncSession,
    settings: Settings | None = None,
    *,
    at: datetime | None = None,
) -> bool:
    """Durably request one DingTalk price/inventory reminder per business week."""

    settings = settings or get_settings()
    observed_at = at or datetime.now(UTC)
    if (
        settings.demo_mode
        or not settings.commercial_gate_enabled
        or not is_commercial_day(settings, observed_at)
        or not is_commercial_open(settings, observed_at)
    ):
        return False
    cycle = await get_or_create_current_cycle(session, settings, at=observed_at)
    if cycle.price_status == "CONFIRMED" and cycle.inventory_status == "CONFIRMED":
        await session.commit()
        return False
    job = await enqueue_job(
        session,
        "notify_commercial_refresh",
        {"cycle_id": cycle.id},
        f"weekly-commercial-refresh:{cycle.scope}:{cycle.week_start.isoformat()}",
    )
    return job is not None


async def _commercial_quote_context(
    session: AsyncSession,
    *,
    product_id: int,
    currency: str,
    settings: Settings,
    requested_quantity: Decimal | int | None = None,
    at: datetime | None = None,
) -> QuoteContext | None:
    if settings.demo_mode or not settings.commercial_gate_enabled:
        return None
    context = await get_commercial_data_provider(settings).get_quote_context(
        session,
        product_id=product_id,
        currency=currency,
        requested_quantity=requested_quantity,
        at=at,
    )
    if context.status is QuoteContextStatus.WAITING:
        await ensure_weekly_commercial_refresh(session, settings, at=at)
        raise JobDeferred(
            f"commercial data waiting: {context.reason}",
            context.next_check_at or (datetime.now(UTC) + timedelta(minutes=settings.commercial_retry_minutes)),
        )
    return context
