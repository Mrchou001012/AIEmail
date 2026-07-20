from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

import app.commercial as commercial
from app.commercial import (
    LocalDatabaseCommercialDataProvider,
    QuoteContextStatus,
    business_week_bounds,
    commercial_update_link,
    get_commercial_data_provider,
    get_or_create_current_cycle,
    is_business_day,
    next_business_open,
    review_link,
)
from app.db import CommercialDataCycle, InventorySnapshot, PricePolicy
from app.settings import Settings


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "business_timezone": "Asia/Kolkata",
        "business_open_hour": 9,
        "commercial_scope": "india",
        "commercial_retry_minutes": 15,
        "public_base_url": "https://aiemail.example.com",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _cycle(**overrides: object) -> CommercialDataCycle:
    values: dict[str, object] = {
        "id": 17,
        "scope": "india",
        "week_start": date(2026, 7, 20),
        "week_end": date(2026, 7, 24),
        "price_status": "CONFIRMED",
        "inventory_status": "CONFIRMED",
        "reminder_status": "SENT",
        "metadata_json": {},
    }
    values.update(overrides)
    return CommercialDataCycle(**values)


def _policy() -> PricePolicy:
    return PricePolicy(
        id=31,
        commercial_cycle_id=17,
        product_id=4,
        currency="INR",
        standard_price=Decimal("100"),
        absolute_floor=Decimal("90"),
        valid_from=date(2026, 7, 20),
        valid_to=date(2026, 7, 24),
        source_hash="test",
        active=True,
    )


def _inventory(availability: str = "AVAILABLE", quantity: Decimal | None = None) -> InventorySnapshot:
    return InventorySnapshot(
        id=41,
        cycle_id=17,
        product_id=4,
        availability=availability,
        quantity=quantity,
        source_system="manual",
        metadata_json={},
    )


class _ScalarSession:
    def __init__(self, *responses: object):
        self.responses = list(responses)

    async def scalar(self, _statement: object) -> object:
        return self.responses.pop(0)


def test_business_week_uses_business_timezone_across_utc_date_boundary() -> None:
    settings = _settings()
    # Sunday UTC is already Monday in India.
    observed_at = datetime(2026, 7, 19, 20, 0, tzinfo=UTC)

    assert business_week_bounds(settings, observed_at) == (
        date(2026, 7, 20),
        date(2026, 7, 24),
    )
    assert is_business_day(settings, observed_at)


@pytest.mark.parametrize(
    ("observed_at", "expected"),
    [
        (
            datetime(2026, 7, 18, 4, 30, tzinfo=UTC),  # Saturday 10:00 IST
            datetime(2026, 7, 20, 3, 30, tzinfo=UTC),
        ),
        (
            datetime(2026, 7, 19, 18, 30, tzinfo=UTC),  # Monday 00:00 IST
            datetime(2026, 7, 20, 3, 30, tzinfo=UTC),
        ),
    ],
)
def test_next_business_open_skips_weekends(
    observed_at: datetime,
    expected: datetime,
) -> None:
    settings = _settings()

    assert next_business_open(settings, observed_at) == expected


def test_business_clock_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        business_week_bounds(_settings(), datetime(2026, 7, 20, 9, 0))


@pytest.mark.asyncio
async def test_get_or_create_current_cycle_contains_concurrent_insert_in_savepoint() -> None:
    winner = _cycle()

    class RaceSession:
        def __init__(self) -> None:
            self.scalar_results = [None, winner]
            self.added: list[CommercialDataCycle] = []

        async def scalar(self, _statement: object) -> object:
            return self.scalar_results.pop(0)

        def add(self, value: CommercialDataCycle) -> None:
            self.added.append(value)

        def begin_nested(self):
            class Savepoint:
                async def __aenter__(self) -> None:
                    return None

                async def __aexit__(self, *_args: object) -> bool:
                    return False

            return Savepoint()

        async def flush(self) -> None:
            raise IntegrityError("insert", {}, Exception("duplicate"))

    session = RaceSession()
    observed_at = datetime(2026, 7, 20, 4, 0, tzinfo=UTC)

    result = await get_or_create_current_cycle(  # type: ignore[arg-type]
        session,
        _settings(),
        at=observed_at,
    )

    assert result is winner
    assert len(session.added) == 1


@pytest.mark.asyncio
async def test_local_provider_returns_available_context(monkeypatch: pytest.MonkeyPatch) -> None:
    cycle = _cycle()

    async def current_cycle(*_args: object, **_kwargs: object) -> CommercialDataCycle:
        return cycle

    monkeypatch.setattr(commercial, "get_or_create_current_cycle", current_cycle)
    provider = LocalDatabaseCommercialDataProvider(_settings())

    result = await provider.get_quote_context(  # type: ignore[arg-type]
        _ScalarSession(_policy(), _inventory(quantity=Decimal("600"))),
        product_id=4,
        currency="inr",
        at=datetime(2026, 7, 20, 4, 0, tzinfo=UTC),  # Monday 09:30 IST
    )

    assert result.status is QuoteContextStatus.AVAILABLE
    assert result.ready_stock_available
    assert result.policy is not None
    assert result.inventory is not None
    assert result.next_check_at is None


@pytest.mark.asyncio
async def test_local_provider_waits_for_cycle_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    cycle = _cycle(price_status="PENDING")

    async def current_cycle(*_args: object, **_kwargs: object) -> CommercialDataCycle:
        return cycle

    monkeypatch.setattr(commercial, "get_or_create_current_cycle", current_cycle)
    provider = LocalDatabaseCommercialDataProvider(_settings())

    result = await provider.get_quote_context(  # type: ignore[arg-type]
        _ScalarSession(_policy(), _inventory()),
        product_id=4,
        currency="INR",
        at=datetime(2026, 7, 20, 4, 0, tzinfo=UTC),
    )

    assert result.status is QuoteContextStatus.WAITING
    assert result.reason == "PRICE_CONFIRMATION_PENDING"
    assert result.next_check_at == datetime(2026, 7, 20, 4, 15, tzinfo=UTC)


@pytest.mark.asyncio
async def test_local_provider_distinguishes_unavailable_and_unknown_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle = _cycle()

    async def current_cycle(*_args: object, **_kwargs: object) -> CommercialDataCycle:
        return cycle

    monkeypatch.setattr(commercial, "get_or_create_current_cycle", current_cycle)
    provider = LocalDatabaseCommercialDataProvider(_settings())
    observed_at = datetime(2026, 7, 20, 4, 0, tzinfo=UTC)

    unavailable = await provider.get_quote_context(  # type: ignore[arg-type]
        _ScalarSession(_policy(), _inventory("OUT_OF_STOCK")),
        product_id=4,
        currency="INR",
        at=observed_at,
    )
    unknown = await provider.get_quote_context(  # type: ignore[arg-type]
        _ScalarSession(_policy(), _inventory("UNKNOWN")),
        product_id=4,
        currency="INR",
        at=observed_at,
    )

    assert unavailable.status is QuoteContextStatus.UNAVAILABLE
    assert unavailable.reason == "INVENTORY_UNAVAILABLE"
    assert unknown.status is QuoteContextStatus.WAITING
    assert unknown.reason == "INVENTORY_STATUS_UNKNOWN"


@pytest.mark.asyncio
async def test_local_provider_rejects_quantity_above_confirmed_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle = _cycle()

    async def current_cycle(*_args: object, **_kwargs: object) -> CommercialDataCycle:
        return cycle

    monkeypatch.setattr(commercial, "get_or_create_current_cycle", current_cycle)
    provider = LocalDatabaseCommercialDataProvider(_settings())

    result = await provider.get_quote_context(  # type: ignore[arg-type]
        _ScalarSession(_policy(), _inventory(quantity=Decimal("599"))),
        product_id=4,
        currency="INR",
        requested_quantity=Decimal("600"),
        at=datetime(2026, 7, 20, 4, 0, tzinfo=UTC),
    )

    assert result.status is QuoteContextStatus.UNAVAILABLE
    assert result.reason == "INVENTORY_INSUFFICIENT"


def test_commercial_provider_factory_resolves_configured_boundary() -> None:
    assert isinstance(
        get_commercial_data_provider(_settings(commercial_data_provider="database")),
        LocalDatabaseCommercialDataProvider,
    )


def test_review_link_defaults_locally_and_supports_crm_template() -> None:
    assert review_link(_settings(), 91, None) == (
        "https://aiemail.example.com/admin/handoffs/91/review"
    )
    settings = _settings(
        crm_review_url_template="https://crm.example.com/cases/{case_id}?handoff={handoff_id}"
    )

    assert review_link(settings, 91, 22) == (
        "https://crm.example.com/cases/22?handoff=91"
    )


@pytest.mark.parametrize(
    "template",
    [
        "ftp://crm.example.com/{handoff_id}",
        "https://crm.example.com/{unknown}",
        "https://crm.example.com/{handoff_id!r}",
    ],
)
def test_review_link_rejects_unsafe_or_unknown_templates(template: str) -> None:
    with pytest.raises(ValueError):
        review_link(_settings(crm_review_url_template=template), 1, 2)


def test_commercial_update_link_supports_cycle_placeholders() -> None:
    cycle = _cycle()
    settings = _settings(
        commercial_update_url=(
            "https://crm.example.com/commercial/{cycle_id}"
            "?scope={scope}&from={week_start}&to={week_end}"
        )
    )

    assert commercial_update_link(settings, cycle) == (
        "https://crm.example.com/commercial/17"
        "?scope=india&from=2026-07-20&to=2026-07-24"
    )
    assert commercial_update_link(_settings(), cycle) == (
        "https://aiemail.example.com/admin/commercial/current/update"
    )
