import asyncio
import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from email import policy
from email.message import EmailMessage as MIMEMessage
from email.parser import BytesParser

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services as services
from app.ai import InboundAnalysis, ProductLine
from app.api import _admin_latest_quote_rows, _price_history_by_product
from app.commercial import get_or_create_current_cycle
from app.db import (
    AIInvocation,
    AuditEvent,
    CaseStage,
    CaseStatus,
    Contact,
    Customer,
    DeliveryStatus,
    EmailAddressStatus,
    EmailMessage,
    Handoff,
    InventorySnapshot,
    Job,
    JobStatus,
    Outbox,
    PricePolicy,
    Product,
    Quote,
    SalesCase,
)
from app.domain import HandoffReason, Intent
from app.services import (
    active_policy,
    claim_and_run_job,
    enqueue_job,
    ingest_raw_email,
    process_inbound,
    quote_with_manual_price,
    seed_demo_data,
    send_one_outbox,
    standard_quote_valid_until,
)
from app.settings import Settings, get_settings

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


async def _manual_quote_fixture(
    session: AsyncSession,
) -> tuple[SalesCase, Handoff, Outbox]:
    case = await _seed_case(session)
    source = await _add_inbound(
        session,
        case,
        "Please send the reviewed quotation for 25 kg.",
        suffix="manual-delivery-policy",
    )
    case.status = CaseStatus.WAITING_HUMAN
    handoff = Handoff(
        case_id=case.id,
        source_email_id=source.id,
        reason_code=HandoffReason.NONSTANDARD.value,
        summary="manual delivery policy fixture",
        extracted_facts={},
        status="OPEN",
    )
    session.add(handoff)
    await session.commit()
    assert case.product_id is not None
    outbox = await quote_with_manual_price(
        session,
        handoff_id=handoff.id,
        lines=[(case.product_id, Decimal("325.0000"), 25)],
        currency="USD",
        actor="reviewer",
    )
    return case, handoff, outbox


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
    mime = BytesParser(policy=policy.default).parsebytes(
        clarification.raw_message.encode("utf-8")
    )
    body_text = mime.get_body(preferencelist=("plain",)).get_content()
    assert "quantity you require" in body_text
    assert (
        "Our minimum order quantity is 10 pieces at USD 100.00 per piece"
        in body_text
    )
    assert (
        "Could you please let us know the quantity you require? "
        "Better pricing is available for larger volumes."
    ) in body_text
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
    expected_valid_until = standard_quote_valid_until(get_settings())
    assert expected_valid_until.weekday() == 0
    assert 1 <= (expected_valid_until - date.today()).days <= 7
    mime = BytesParser(policy=policy.default).parsebytes(
        outbox.raw_message.encode("utf-8")
    )
    body_text = mime.get_body(preferencelist=("plain",)).get_content()
    assert (
        f"Quote valid until: {expected_valid_until.isoformat()} "
        f"({expected_valid_until.strftime('%A')})"
    ) in body_text
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
    assert {quote.valid_until for quote in quotes} == {expected_valid_until}
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
    mime = BytesParser(policy=policy.default).parsebytes(
        clarification.raw_message.encode("utf-8")
    )
    body_text = mime.get_body(preferencelist=("plain",)).get_content()
    assert (
        "WIDGET-200: MOQ 1 kg at USD 200.00" in body_text
    )
    assert (
        "Could you please let us know the quantity you require for WIDGET-200? "
        "Better pricing is available for larger volumes."
    ) in body_text
    assert await db_session.scalar(select(func.count()).select_from(Quote)) == 0


async def test_multi_product_quote_duplicate_mentions_with_conflicting_quantities_clarifies(
    db_session: AsyncSession,
    monkeypatch,
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
            (
                "Please quote PRODUCT WIDGET-100 100 kg, "
                "PRODUCT WIDGET-100 200 kg and PRODUCT WIDGET-200 200 kg."
            ),
            message_id="multi-quote-duplicate-conflict",
        ),
        mailbox="integration-test",
    )
    assert email_row is not None and email_row.case_id is not None

    async def fake_analyze(self, subject, body, attachments):
        return (
            InboundAnalysis(
                intent=Intent.QUOTE_REQUEST,
                intent_confidence=0.99,
                product_code="WIDGET-100",
                product_confidence=0.99,
                product_requests=[
                    ProductLine(product_code="WIDGET-100", quantity=100),
                    ProductLine(product_code="WIDGET-100", quantity=200),
                    ProductLine(product_code="WIDGET-200", quantity=200),
                ],
                quantity=100,
                numeric_confidence=0.99,
            ),
            {
                "provider": "stub",
                "model": "stub",
                "request_hash": "duplicate-conflict",
                "input_tokens": 1,
                "output_tokens": 1,
            },
        )

    monkeypatch.setattr("app.services.AIClient.analyze", fake_analyze)
    await process_inbound(db_session, email_row.id)

    clarification = await db_session.scalar(
        select(Outbox).where(Outbox.message_kind == "QUOTE_CLARIFICATION")
    )
    assert clarification is not None
    mime = BytesParser(policy=policy.default).parsebytes(
        clarification.raw_message.encode("utf-8")
    )
    body_text = mime.get_body(preferencelist=("plain",)).get_content()
    assert "WIDGET-100" in body_text
    assert await db_session.scalar(select(func.count()).select_from(Quote)) == 0
    assert await db_session.scalar(select(func.count()).select_from(Handoff)) == 0


async def test_multi_product_quote_duplicate_mentions_with_same_quantity_dedupe(
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
            (
                "Please quote PRODUCT WIDGET-100 100 kg, "
                "PRODUCT WIDGET-100 100 kg and PRODUCT WIDGET-200 200 kg."
            ),
            message_id="multi-quote-duplicate-same",
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
    assert len({quote.product_id for quote in quotes}) == 2
    assert await db_session.scalar(select(func.count()).select_from(Handoff)) == 0


async def test_multi_product_quote_resolves_separator_variants(
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
            "Please quote PRODUCT WIDGET_100 100 kg and PRODUCT WIDGET-200 200 kg.",
            message_id="multi-quote-separator-variant",
        ),
        mailbox="integration-test",
    )
    assert email_row is not None and email_row.case_id is not None

    await process_inbound(db_session, email_row.id)

    outbox = await db_session.scalar(
        select(Outbox).where(Outbox.message_kind == "AUTO_QUOTE")
    )
    assert outbox is not None
    quotes = (
        (
            await db_session.execute(
                select(Quote).join(SalesCase, Quote.case_id == SalesCase.id)
                .where(SalesCase.id == email_row.case_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(quotes) == 2
    product_codes = set()
    for quote in quotes:
        product = await db_session.get(Product, quote.product_id)
        assert product is not None
        product_codes.add(product.code)
    assert product_codes == {"WIDGET-100", "WIDGET-200"}
    assert await db_session.scalar(select(func.count()).select_from(Handoff)) == 0


async def test_manual_price_rejects_product_mismatch_with_case(
    db_session: AsyncSession,
) -> None:
    case = await _seed_case(db_session)
    other = await _add_quoteable_product(
        db_session,
        code="WIDGET-200",
        name="Industrial Widget 200",
        approved_text_key="widget_200",
    )
    email_row = await _add_inbound(
        db_session,
        case,
        "Please quote PRODUCT WIDGET-200.",
        suffix="manual-price-mismatch",
    )
    handoff = Handoff(
        case_id=case.id,
        source_email_id=email_row.id,
        reason_code=HandoffReason.NONSTANDARD.value,
        summary="manual price mismatch fixture",
        extracted_facts={},
        status="OPEN",
    )
    db_session.add(handoff)
    await db_session.commit()

    with pytest.raises(ValueError, match="does not match the case product"):
        await quote_with_manual_price(
            db_session,
            handoff_id=handoff.id,
            lines=[(other.id, Decimal("300.0000"), 50)],
            currency="USD",
            actor="admin",
        )


async def test_admin_latest_quote_rows_include_multi_product_quotes(
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
            message_id="admin-multi-quote",
        ),
        mailbox="integration-test",
    )
    assert email_row is not None and email_row.case_id is not None
    await process_inbound(db_session, email_row.id)

    rows = await _admin_latest_quote_rows(db_session)
    codes = {row[2] for row in rows}
    assert {"WIDGET-100", "WIDGET-200"} <= codes


async def test_multi_product_quote_missing_quantity_without_policy_clarifies_without_price(
    db_session: AsyncSession,
) -> None:
    await seed_demo_data(db_session)
    unpriced = Product(
        code="WIDGET-999",
        name="Unpriced Widget",
        unit="kg",
        approved_text_key="widget_999",
    )
    db_session.add(unpriced)
    await db_session.commit()
    email_row = await ingest_raw_email(
        db_session,
        _mime(
            "Please quote PRODUCT WIDGET-100 100 kg and PRODUCT WIDGET-999.",
            message_id="multi-quote-missing-qty-unpriced",
        ),
        mailbox="integration-test",
    )
    assert email_row is not None and email_row.case_id is not None

    await process_inbound(db_session, email_row.id)

    clarification = await db_session.scalar(
        select(Outbox).where(Outbox.message_kind == "QUOTE_CLARIFICATION")
    )
    assert clarification is not None
    assert "WIDGET-999" in clarification.raw_message
    mime = BytesParser(policy=policy.default).parsebytes(
        clarification.raw_message.encode("utf-8")
    )
    body_text = mime.get_body(preferencelist=("plain",)).get_content()
    assert "MOQ" not in body_text
    assert "Could you please confirm the required quantity for WIDGET-999?" in body_text
    assert await db_session.scalar(select(func.count()).select_from(Quote)) == 0
    assert await db_session.scalar(select(func.count()).select_from(Handoff)) == 0


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
        lines=[(product.id, Decimal("450.0000"), 50)],
        currency="USD",
        actor="admin",
    )
    assert outbox.message_kind == "HUMAN_QUOTE"
    assert outbox.approval_handoff_id == handoff.id
    assert outbox.human_approved_by == "admin"
    assert outbox.human_approved_at is not None
    assert "Industrial Widget 300" in outbox.raw_message

    case = await db_session.get(SalesCase, handoff.case_id)
    assert case is not None and case.status == CaseStatus.WAITING_HUMAN
    assert await send_one_outbox(
        db_session,
        get_settings(),
        at=datetime.now(UTC),
    ) is True
    await db_session.refresh(outbox)
    assert outbox.status == DeliveryStatus.SENT
    await db_session.refresh(case)
    assert case.status == CaseStatus.WAITING_HUMAN

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
    with pytest.raises(ValueError, match="already resolved"):
        await quote_with_manual_price(
            db_session,
            handoff_id=handoff.id,
            lines=[(product.id, Decimal("450.0000"), 50)],
            currency="USD",
            actor="admin",
        )

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


async def test_manual_price_quote_multiple_products_sends_one_email(
    db_session: AsyncSession,
) -> None:
    await seed_demo_data(db_session)
    first = Product(
        code="WIDGET-400",
        name="Industrial Widget 400",
        unit="kg",
        approved_text_key="widget_400",
    )
    second = Product(
        code="WIDGET-500",
        name="Industrial Widget 500",
        unit="kg",
        approved_text_key="widget_500",
    )
    db_session.add_all([first, second])
    await db_session.commit()
    email_row = await ingest_raw_email(
        db_session,
        _mime(
            (
                "Please quote PRODUCT WIDGET-400 50 kg and "
                "PRODUCT WIDGET-500 60 kg."
            ),
            message_id="manual-price-multi-inquiry",
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
    case = await db_session.get(SalesCase, handoff.case_id)
    assert case is not None and case.product_id is None

    outbox = await quote_with_manual_price(
        db_session,
        handoff_id=handoff.id,
        lines=[
            (first.id, Decimal("400.0000"), 50),
            (second.id, Decimal("500.0000"), 60),
        ],
        currency="INR",
        actor="admin",
    )
    assert outbox.message_kind == "HUMAN_QUOTE"
    assert outbox.approval_handoff_id == handoff.id
    assert "Industrial Widget 400" in outbox.raw_message
    assert "Industrial Widget 500" in outbox.raw_message
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
    assert {quote.product_id for quote in quotes} == {first.id, second.id}
    assert len({quote.round_number for quote in quotes}) == 1
    policies = (
        (
            await db_session.execute(
                select(PricePolicy).where(
                    PricePolicy.product_id.in_([first.id, second.id])
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(policies) == 2
    assert all(policy.active is False for policy in policies)
    assert {policy.standard_price for policy in policies} == {
        Decimal("400.0000"),
        Decimal("500.0000"),
    }
    await db_session.refresh(handoff)
    assert handoff.status == "RESOLVED"
    await db_session.refresh(case)
    assert case.product_id is None


async def test_price_history_query_returns_only_recent_rows_per_product(
    db_session: AsyncSession,
) -> None:
    products = [
        Product(
            code=f"HISTORY-{index}",
            name=f"History Product {index}",
            unit="kg",
            approved_text_key=f"history_{index}",
        )
        for index in range(3)
    ]
    db_session.add_all(products)
    await db_session.flush()
    for product in products:
        for version in range(25):
            price = Decimal(100 + version)
            db_session.add(
                PricePolicy(
                    product_id=product.id,
                    currency="USD",
                    standard_price=price,
                    absolute_floor=price,
                    valid_from=date.today(),
                    source_hash=f"history-{product.id}-{version}",
                    active=False,
                )
            )
    await db_session.commit()

    payload = await _price_history_by_product(
        db_session,
        [product.id for product in products],
    )

    assert set(payload) == {str(product.id) for product in products}
    assert all(len(rows) == 10 for rows in payload.values())
    assert all(
        [Decimal(row["price"]) for row in rows]
        == [Decimal(value) for value in range(124, 114, -1)]
        for rows in payload.values()
    )


@pytest.mark.parametrize(
    ("policy_case", "expected_status"),
    [
        ("allowed", DeliveryStatus.SENT),
        ("do_not_contact", DeliveryStatus.CANCELLED),
        ("contact_suppressed", DeliveryStatus.CANCELLED),
        ("address_suppressed", DeliveryStatus.CANCELLED),
        ("closed", DeliveryStatus.CANCELLED),
        ("wrong_recipient", DeliveryStatus.CANCELLED),
        ("safe_mode", DeliveryStatus.CANCELLED),
    ],
)
async def test_manual_quote_delivery_policy_matrix(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    policy_case: str,
    expected_status: DeliveryStatus,
) -> None:
    case, _handoff, outbox = await _manual_quote_fixture(db_session)
    customer = await db_session.get(Customer, case.customer_id)
    contact = await db_session.get(Contact, case.contact_id)
    assert customer is not None and contact is not None
    if policy_case == "do_not_contact":
        customer.do_not_contact = True
    elif policy_case == "contact_suppressed":
        contact.suppressed = True
    elif policy_case == "address_suppressed":
        db_session.add(
            EmailAddressStatus(
                email=contact.email.casefold(),
                domain="example.com",
                suppressed=True,
                suppression_reason="TEST",
                suppressed_at=datetime.now(UTC),
            )
        )
    elif policy_case == "closed":
        case.status = CaseStatus.CLOSED_LOST
    elif policy_case == "wrong_recipient":
        outbox.recipient = "wrong-recipient@example.com"
    await db_session.commit()

    settings = Settings(
        mail_transport="smtp",
        safe_mode=True,
        auto_send_enabled=False,
        recipient_allowlist=(
            [] if policy_case == "safe_mode" else ["internal@example.com"]
        ),
        forward_recipient_allowlist=[],
        commercial_gate_enabled=False,
        email_preflight_enabled=True,
        mx_check_enabled=False,
        min_send_interval_seconds=0,
        send_interval_jitter_seconds=0,
    )

    async def allow_preflight(
        _session: AsyncSession,
        recipient: str,
        _settings: Settings,
    ) -> tuple[str, str, dict[str, object]]:
        return "ALLOW", "test preflight passed", {"recipient": recipient}

    sent: list[str] = []

    class CapturingTransport:
        def send(self, raw_message: str, message_id: str, recipient: str) -> None:
            sent.append(recipient)

    monkeypatch.setattr(services, "_recipient_preflight", allow_preflight)
    monkeypatch.setattr(services, "transport_for", lambda _settings: CapturingTransport())

    assert await send_one_outbox(db_session, settings, at=datetime.now(UTC)) is True
    await db_session.refresh(outbox)
    assert outbox.status == expected_status
    assert sent == (["internal@example.com"] if expected_status == DeliveryStatus.SENT else [])


async def test_manual_quote_failure_during_staging_is_fully_atomic(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await _seed_case(db_session)
    source = await _add_inbound(
        db_session,
        case,
        "Please quote 25 kg.",
        suffix="manual-atomic-failure",
    )
    case.status = CaseStatus.WAITING_HUMAN
    handoff = Handoff(
        case_id=case.id,
        source_email_id=source.id,
        reason_code=HandoffReason.NONSTANDARD.value,
        summary="manual atomicity fixture",
        extracted_facts={},
        status="OPEN",
    )
    db_session.add(handoff)
    await db_session.commit()
    assert case.product_id is not None
    case_id = case.id
    handoff_id = handoff.id
    product_id = case.product_id

    models = (Quote, Outbox, EmailMessage, PricePolicy, AuditEvent)
    before = {
        model.__name__: await db_session.scalar(select(func.count()).select_from(model))
        for model in models
    }

    async def fail_audit(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(services, "audit", fail_audit)
    with pytest.raises(RuntimeError, match="injected audit failure"):
        await quote_with_manual_price(
            db_session,
            handoff_id=handoff_id,
            lines=[(product_id, Decimal("325.0000"), 25)],
            currency="USD",
            actor="reviewer",
        )

    after = {
        model.__name__: await db_session.scalar(select(func.count()).select_from(model))
        for model in models
    }
    assert after == before
    stored_handoff = await db_session.get(Handoff, handoff_id)
    stored_case = await db_session.get(SalesCase, case_id)
    assert stored_handoff is not None and stored_handoff.status == "OPEN"
    assert stored_case is not None
    assert stored_case.status == CaseStatus.WAITING_HUMAN
    assert stored_case.negotiation_round == 0


async def _fail_after_outbox_staging(
    monkeypatch: pytest.MonkeyPatch,
    *,
    expected_kind: str,
    error: BaseException,
) -> None:
    real_stage = services.stage_outbox

    async def staged_then_failed(*args: object, **kwargs: object) -> Outbox | None:
        row = await real_stage(*args, **kwargs)  # type: ignore[arg-type]
        assert kwargs.get("message_kind", "AUTO_QUOTE") == expected_kind
        assert row is not None
        raise error

    monkeypatch.setattr(services, "stage_outbox", staged_then_failed)


async def _business_counts(session: AsyncSession) -> dict[str, int]:
    models = (Quote, Outbox, EmailMessage, AuditEvent, AIInvocation)
    return {
        model.__name__: int(
            await session.scalar(select(func.count()).select_from(model)) or 0
        )
        for model in models
    }


async def test_auto_quote_cancelled_after_staging_rolls_back_and_session_recovers(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await _seed_case(db_session)
    email_row = await _add_inbound(
        db_session,
        case,
        "PRODUCT WIDGET-100 Please quote 100 kg.",
        suffix="auto-quote-cancelled-after-stage",
    )
    case_id = case.id
    before = await _business_counts(db_session)
    await _fail_after_outbox_staging(
        monkeypatch,
        expected_kind="AUTO_QUOTE",
        error=asyncio.CancelledError(),
    )

    with pytest.raises(asyncio.CancelledError):
        await process_inbound(db_session, email_row.id)

    assert await _business_counts(db_session) == before
    assert await db_session.scalar(
        select(SalesCase.id).where(SalesCase.id == case_id)
    ) == case_id


async def test_cancelled_job_releases_lease_without_consuming_retry(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = await enqueue_job(
        db_session,
        "cancelled-atomic-probe",
        {},
        "cancelled-atomic-probe",
    )
    assert job is not None
    job_id = job.id

    async def cancel_handler(_session: AsyncSession, _payload: dict[str, object]) -> None:
        raise asyncio.CancelledError

    monkeypatch.setitem(
        services.JOB_HANDLERS,
        "cancelled-atomic-probe",
        cancel_handler,
    )
    with pytest.raises(asyncio.CancelledError):
        await claim_and_run_job(db_session, "cancel-test-worker")

    stored = await db_session.get(Job, job_id)
    assert stored is not None
    assert stored.status == JobStatus.PENDING
    assert stored.attempts == 0
    assert stored.locked_at is None
    assert stored.locked_by is None


async def test_quote_clarification_failure_after_staging_rolls_back_everything(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await _seed_case(db_session)
    email_row = await _add_inbound(
        db_session,
        case,
        "PRODUCT WIDGET-100 Please quote.",
        suffix="clarification-failure-after-stage",
    )
    before = await _business_counts(db_session)
    await _fail_after_outbox_staging(
        monkeypatch,
        expected_kind="QUOTE_CLARIFICATION",
        error=RuntimeError("injected clarification post-stage failure"),
    )

    with pytest.raises(RuntimeError, match="clarification post-stage"):
        await process_inbound(db_session, email_row.id)

    assert await _business_counts(db_session) == before


async def test_initial_outreach_failure_after_staging_rolls_back_quote_and_case_state(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await _seed_case(db_session)
    case_id = case.id
    before = await _business_counts(db_session)
    before_round = case.negotiation_round
    before_subject = case.subject_key
    await _fail_after_outbox_staging(
        monkeypatch,
        expected_kind="AUTO_QUOTE",
        error=RuntimeError("injected initial outreach post-stage failure"),
    )

    with pytest.raises(RuntimeError, match="initial outreach post-stage"):
        await services.create_case_outreach(
            db_session,
            {"case_id": case_id, "quantity": 100},
        )

    assert await _business_counts(db_session) == before
    stored_case = await db_session.get(SalesCase, case_id)
    assert stored_case is not None
    assert stored_case.negotiation_round == before_round
    assert stored_case.subject_key == before_subject
