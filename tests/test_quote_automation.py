import hashlib
from datetime import date
from decimal import Decimal
from email.message import EmailMessage as MIMEMessage

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.commercial import get_or_create_current_cycle
from app.db import (
    CaseStage,
    CaseStatus,
    EmailMessage,
    Handoff,
    InventorySnapshot,
    Outbox,
    PricePolicy,
    Product,
    Quote,
    SalesCase,
)
from app.domain import HandoffReason
from app.services import (
    active_policy,
    ingest_raw_email,
    process_inbound,
    quote_with_manual_price,
    seed_demo_data,
)
from app.settings import Settings

pytestmark = pytest.mark.integration


async def _seed_case(session: AsyncSession, *, currency: str = "USD") -> SalesCase:
    ids = await seed_demo_data(session)
    case = SalesCase(
        customer_id=ids["customer_id"],
        contact_id=ids["contact_id"],
        product_id=ids["product_id"],
        currency=currency,
        stage=CaseStage.QUOTING,
        status=CaseStatus.ACTIVE,
        subject_key="widget quotation",
    )
    session.add(case)
    await session.commit()
    return case


async def _add_inbound(session: AsyncSession, case: SalesCase, body: str, *, suffix: str) -> EmailMessage:
    raw_sha256 = hashlib.sha256(f"{suffix}:{body}".encode()).hexdigest()
    row = EmailMessage(
        case_id=case.id,
        direction="INBOUND",
        mailbox="integration-test",
        message_id=f"<{suffix}@example.com>",
        from_address="internal@example.com",
        to_addresses=["sales-agent@example.com"],
        subject="Re: widget quotation",
        body_text=body,
        attachment_metadata=[],
        raw_sha256=raw_sha256,
    )
    session.add(row)
    await session.commit()
    return row


def _mime(body: str, *, message_id: str, subject: str = "Re: widget quotation") -> bytes:
    message = MIMEMessage()
    message["From"] = "Alex Buyer <internal@example.com>"
    message["To"] = "sales-agent@example.com"
    message["Subject"] = subject
    message["Message-ID"] = f"<{message_id}@example.com>"
    message.set_content(body)
    return message.as_bytes()


async def _add_quoteable_product(
    session: AsyncSession,
    *,
    code: str,
    name: str,
    approved_text_key: str,
    cycle_id: int | None = None,
) -> Product:
    product = Product(
        code=code,
        name=name,
        unit="kg",
        approved_text_key=approved_text_key,
    )
    session.add(product)
    await session.flush()
    session.add(
        PricePolicy(
            commercial_cycle_id=cycle_id,
            product_id=product.id,
            currency="USD",
            standard_price=Decimal("200.0000"),
            absolute_floor=Decimal("150.0000"),
            max_discount_pct=Decimal("0.1000"),
            max_negotiation_rounds=2,
            concession_step_pct=Decimal("0.0200"),
            min_quantity=1,
            max_quantity=10000,
            quote_valid_days=30,
            standard_incoterm="EXW",
            allowed_incoterms=["EXW"],
            standard_payment_term="100% before shipment",
            allowed_payment_terms=["100% before shipment"],
            valid_from=date.today(),
            source_hash=f"test-{code}",
        )
    )
    await session.commit()
    return product


async def test_quote_missing_quantity_asks_then_quotes(db_session: AsyncSession) -> None:
    case = await _seed_case(db_session)
    email_row = await _add_inbound(
        db_session,
        case,
        "PRODUCT WIDGET-100 Please quote.",
        suffix="missing-qty-first",
    )

    await process_inbound(db_session, email_row.id)

    clarification = await db_session.scalar(
        select(Outbox).where(Outbox.message_kind == "QUOTE_CLARIFICATION")
    )
    assert clarification is not None
    assert "required quantity" in clarification.raw_message
    assert await db_session.scalar(select(func.count()).select_from(Handoff)) == 0

    reply = await _add_inbound(
        db_session,
        case,
        "PRODUCT WIDGET-100 Please quote 100 kg.",
        suffix="missing-qty-answer",
    )
    await process_inbound(db_session, reply.id)

    quote_outbox = await db_session.scalar(
        select(Outbox).where(Outbox.message_kind == "AUTO_QUOTE")
    )
    assert quote_outbox is not None
    assert "WIDGET-100" in quote_outbox.raw_message or "Industrial Widget 100" in quote_outbox.raw_message


async def test_quote_ignore_inventory_quotes_out_of_stock_product(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    ids = await seed_demo_data(db_session)
    settings = Settings(
        _env_file=None,
        demo_mode=False,
        commercial_gate_enabled=True,
        commercial_scope="ignore-inventory-test",
        business_timezone="Asia/Kolkata",
        commercial_open_hour=0,
        quote_ignore_inventory=True,
    )
    monkeypatch.setattr("app.services.get_settings", lambda: settings)
    cycle = await get_or_create_current_cycle(db_session, settings)
    cycle.price_status = "CONFIRMED"
    cycle.inventory_status = "CONFIRMED"
    product = await db_session.get(Product, ids["product_id"])
    policy = await active_policy(db_session, product.id, "USD")
    assert policy is not None
    policy.commercial_cycle_id = cycle.id
    db_session.add(
        InventorySnapshot(
            cycle_id=cycle.id,
            product_id=product.id,
            availability="OUT_OF_STOCK",
            quantity=Decimal("0"),
        )
    )
    await db_session.commit()

    case = SalesCase(
        customer_id=ids["customer_id"],
        contact_id=ids["contact_id"],
        product_id=product.id,
        currency="USD",
        stage=CaseStage.QUOTING,
        status=CaseStatus.ACTIVE,
        subject_key="widget quotation",
    )
    db_session.add(case)
    await db_session.commit()
    email_row = await _add_inbound(
        db_session,
        case,
        "PRODUCT WIDGET-100 Please quote 100 kg.",
        suffix="ignore-inventory",
    )

    await process_inbound(db_session, email_row.id)

    quote_outbox = await db_session.scalar(
        select(Outbox).where(Outbox.message_kind == "AUTO_QUOTE")
    )
    assert quote_outbox is not None
    assert "Subject to confirmation at order placement" in quote_outbox.raw_message
    assert "Ready stock" not in quote_outbox.raw_message
    assert await db_session.scalar(select(func.count()).select_from(Handoff)) == 0


async def test_quote_without_ignore_inventory_still_blocks_out_of_stock(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    ids = await seed_demo_data(db_session)
    settings = Settings(
        _env_file=None,
        demo_mode=False,
        commercial_gate_enabled=True,
        commercial_scope="inventory-block-test",
        business_timezone="Asia/Kolkata",
        commercial_open_hour=0,
        quote_ignore_inventory=False,
    )
    monkeypatch.setattr("app.services.get_settings", lambda: settings)
    cycle = await get_or_create_current_cycle(db_session, settings)
    cycle.price_status = "CONFIRMED"
    cycle.inventory_status = "CONFIRMED"
    product = await db_session.get(Product, ids["product_id"])
    policy = await active_policy(db_session, product.id, "USD")
    assert policy is not None
    policy.commercial_cycle_id = cycle.id
    db_session.add(
        InventorySnapshot(
            cycle_id=cycle.id,
            product_id=product.id,
            availability="OUT_OF_STOCK",
            quantity=Decimal("0"),
        )
    )
    await db_session.commit()

    case = SalesCase(
        customer_id=ids["customer_id"],
        contact_id=ids["contact_id"],
        product_id=product.id,
        currency="USD",
        stage=CaseStage.QUOTING,
        status=CaseStatus.ACTIVE,
        subject_key="widget quotation",
    )
    db_session.add(case)
    await db_session.commit()
    email_row = await _add_inbound(
        db_session,
        case,
        "PRODUCT WIDGET-100 Please quote 100 kg.",
        suffix="inventory-block",
    )

    await process_inbound(db_session, email_row.id)

    handoff = await db_session.scalar(
        select(Handoff).where(Handoff.source_email_id == email_row.id)
    )
    assert handoff is not None
    assert handoff.reason_code == HandoffReason.INVENTORY_UNAVAILABLE.value


async def test_multi_product_quote_sends_one_email_with_two_quotes(
    db_session: AsyncSession,
) -> None:
    await seed_demo_data(db_session)
    await _add_quoteable_product(
        db_session,
        code="WIDGET-200",
        name="Industrial Widget 200",
        approved_text_key="widget_200",
    )
    email_row = await ingest_raw_email(
        db_session,
        _mime(
            "Please quote PRODUCT WIDGET-100 100 kg and PRODUCT WIDGET-200 200 kg.",
            message_id="multi-quote-success",
        ),
        mailbox="integration-test",
    )
    assert email_row is not None and email_row.case_id is not None
    case = await db_session.get(SalesCase, email_row.case_id)
    assert case is not None and case.product_id is None

    await process_inbound(db_session, email_row.id)

    outbox = await db_session.scalar(
        select(Outbox).where(Outbox.message_kind == "AUTO_QUOTE")
    )
    assert outbox is not None
    assert "Industrial Widget 100" in outbox.raw_message
    assert "Industrial Widget 200" in outbox.raw_message
    quotes = (
        (
            await db_session.execute(
                select(Quote).where(Quote.case_id == case.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(quotes) == 2
    assert {quote.product_id for quote in quotes} == {
        (await db_session.scalar(select(Product).where(Product.code == "WIDGET-100"))).id,
        (await db_session.scalar(select(Product).where(Product.code == "WIDGET-200"))).id,
    }
    assert len({quote.round_number for quote in quotes}) == 1
    assert await db_session.scalar(select(func.count()).select_from(Handoff)) == 0


async def test_multi_product_quote_with_unknown_or_unpriced_product_goes_human(
    db_session: AsyncSession,
) -> None:
    await seed_demo_data(db_session)
    await _add_quoteable_product(
        db_session,
        code="WIDGET-200",
        name="Industrial Widget 200",
        approved_text_key="widget_200",
    )
    unknown = await db_session.scalar(
        select(Product).where(Product.code == "WIDGET-999")
    )
    if unknown is None:
        db_session.add(
            Product(
                code="WIDGET-999",
                name="Unpriced Widget",
                unit="kg",
                approved_text_key="widget_999",
            )
        )
        await db_session.commit()
    email_row = await ingest_raw_email(
        db_session,
        _mime(
            (
                "Please quote PRODUCT WIDGET-100 100 kg, "
                "PRODUCT WIDGET-200 200 kg and PRODUCT WIDGET-999 50 kg."
            ),
            message_id="multi-quote-unpriced",
        ),
        mailbox="integration-test",
    )
    assert email_row is not None and email_row.case_id is not None

    await process_inbound(db_session, email_row.id)

    handoff = await db_session.scalar(
        select(Handoff).where(Handoff.source_email_id == email_row.id)
    )
    assert handoff is not None
    assert handoff.reason_code == HandoffReason.NONSTANDARD.value
    assert await db_session.scalar(select(func.count()).select_from(Quote)) == 0


async def test_multi_product_quote_missing_quantity_clarifies(
    db_session: AsyncSession,
) -> None:
    await seed_demo_data(db_session)
    await _add_quoteable_product(
        db_session,
        code="WIDGET-200",
        name="Industrial Widget 200",
        approved_text_key="widget_200",
    )
    email_row = await ingest_raw_email(
        db_session,
        _mime(
            "Please quote PRODUCT WIDGET-100 100 kg and PRODUCT WIDGET-200.",
            message_id="multi-quote-missing-qty",
        ),
        mailbox="integration-test",
    )
    assert email_row is not None and email_row.case_id is not None

    await process_inbound(db_session, email_row.id)

    clarification = await db_session.scalar(
        select(Outbox).where(Outbox.message_kind == "QUOTE_CLARIFICATION")
    )
    assert clarification is not None
    assert "WIDGET-200" in clarification.raw_message
    assert await db_session.scalar(select(func.count()).select_from(Quote)) == 0


async def test_manual_price_quote_sends_now_and_keeps_price_as_history_only(
    db_session: AsyncSession,
) -> None:
    await seed_demo_data(db_session)
    product = Product(
        code="WIDGET-300",
        name="Industrial Widget 300",
        unit="kg",
        approved_text_key="widget_300",
    )
    db_session.add(product)
    await db_session.commit()
    email_row = await ingest_raw_email(
        db_session,
        _mime(
            "Please quote PRODUCT WIDGET-300 50 kg.",
            message_id="manual-price-inquiry",
        ),
        mailbox="integration-test",
    )
    assert email_row is not None and email_row.case_id is not None
    await process_inbound(db_session, email_row.id)
    handoff = await db_session.scalar(
        select(Handoff).where(Handoff.source_email_id == email_row.id)
    )
    assert handoff is not None
    assert handoff.reason_code == HandoffReason.NONSTANDARD.value
    assert handoff.case_id is not None

    outbox = await quote_with_manual_price(
        db_session,
        handoff_id=handoff.id,
        product_id=product.id,
        standard_price=Decimal("450.0000"),
        currency="USD",
        quantity=50,
        actor="admin",
    )
    assert outbox.message_kind == "AUTO_QUOTE"
    assert "Industrial Widget 300" in outbox.raw_message

    policy = await db_session.scalar(
        select(PricePolicy).where(
            PricePolicy.product_id == product.id,
        )
        .order_by(PricePolicy.id.desc())
        .limit(1)
    )
    assert policy is not None
    assert policy.standard_price == Decimal("450.0000")
    assert policy.absolute_floor == Decimal("450.0000")
    # History only: the human-set price must not enable the next automatic
    # quotation because prices change frequently.
    assert policy.active is False
    quote = await db_session.scalar(
        select(Quote).where(Quote.case_id == handoff.case_id)
    )
    assert quote is not None and quote.product_id == product.id
    await db_session.refresh(handoff)
    assert handoff.status == "RESOLVED"

    # A later inquiry for the same product still routes to a human, and the
    # review screen shows the stored historical price for reference.
    second = await ingest_raw_email(
        db_session,
        _mime(
            "Please quote PRODUCT WIDGET-300 100 kg.",
            message_id="manual-price-followup",
        ),
        mailbox="integration-test",
    )
    assert second is not None and second.case_id is not None
    await process_inbound(db_session, second.id)
    second_handoff = await db_session.scalar(
        select(Handoff).where(Handoff.source_email_id == second.id)
    )
    assert second_handoff is not None
    assert second_handoff.reason_code == HandoffReason.NONSTANDARD.value
    history_rows = (
        (
            await db_session.execute(
                select(PricePolicy)
                .where(PricePolicy.product_id == product.id)
                .order_by(PricePolicy.valid_from.desc(), PricePolicy.id.desc())
            )
        )
        .scalars()
        .all()
    )
    assert any(
        row.standard_price == Decimal("450.0000") and row.active is False
        for row in history_rows
    )
