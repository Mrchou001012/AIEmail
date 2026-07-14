import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from email.message import EmailMessage as MIMEMessage
from email.utils import format_datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import dashboard_data
from app.db import (
    AuditEvent,
    CaseStage,
    CaseStatus,
    EmailMessage,
    Handoff,
    Job,
    MailboxCursor,
    Outbox,
    Quote,
    SalesCase,
)
from app.domain import HandoffReason
from app.domain import transition as domain_transition
from app.history import HISTORY_REVIEW_SUMMARY, reconcile_email_history
from app.imap_poller import poll_folder_once
from app.mail import parse_mime
from app.services import (
    active_policy,
    create_case_outreach,
    create_handoff,
    ingest_raw_email,
    process_inbound,
    seed_demo_data,
)
from app.settings import get_settings

pytestmark = pytest.mark.integration


async def test_dashboard_data_supports_an_empty_database(db_session: AsyncSession) -> None:
    payload = await dashboard_data("admin", db_session, get_settings())

    assert payload["metrics"] == {
        "inbound_24h": 0,
        "sent_24h": 0,
        "pending_outbox": 0,
        "open_handoffs": 0,
        "failed_jobs": 0,
        "unmatched_history": 0,
        "ai_failures": 0,
    }
    assert payload["emails"] == []
    assert payload["outbox"] == []
    assert payload["handoffs"] == []


async def _seed_case(session: AsyncSession, *, with_quote: bool) -> SalesCase:
    ids = await seed_demo_data(session)
    case = SalesCase(
        customer_id=ids["customer_id"],
        contact_id=ids["contact_id"],
        product_id=ids["product_id"],
        currency="USD",
        stage=CaseStage.QUOTING,
        status=CaseStatus.ACTIVE,
        subject_key="industrial widget 100 quotation",
    )
    session.add(case)
    await session.flush()
    if with_quote:
        policy = await active_policy(session, ids["product_id"], "USD")
        assert policy is not None
        session.add(
            Quote(
                case_id=case.id,
                price_policy_id=policy.id,
                round_number=0,
                unit_price=Decimal("100.0000"),
                currency="USD",
                quantity=100,
                incoterm=policy.standard_incoterm,
                payment_term=policy.standard_payment_term,
                valid_until=date.today() + timedelta(days=policy.quote_valid_days),
                pricing_snapshot={"hard_minimum": "85.0000"},
            )
        )
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
        subject="Re: Industrial Widget 100 quotation",
        body_text=body,
        attachment_metadata=[],
        raw_sha256=raw_sha256,
    )
    session.add(row)
    await session.commit()
    return row


@pytest.mark.parametrize(
    ("body", "reason", "intent", "fact_name"),
    [
        ("PRODUCT WIDGET-100 Please send a sample.", HandoffReason.SAMPLE_REQUEST, "sample_request", "sample_requested"),
        ("PRODUCT WIDGET-100 We want to place an order.", HandoffReason.ORDER_COMMITMENT, "order", "order_requested"),
        ("PRODUCT WIDGET-100 Please provide shipping tracking.", HandoffReason.SHIPPING_REQUEST, "shipping", "shipping_requested"),
        ("PRODUCT WIDGET-100 We need the technical datasheet.", HandoffReason.TECHNICAL_REQUEST, "technical", "technical_requested"),
        ("PRODUCT WIDGET-100 Complaint: the unit arrived damaged.", HandoffReason.COMPLAINT, "complaint", "complaint"),
    ],
)
async def test_risky_intents_create_specific_handoffs(
    db_session: AsyncSession,
    body: str,
    reason: HandoffReason,
    intent: str,
    fact_name: str,
) -> None:
    case = await _seed_case(db_session, with_quote=False)
    email_row = await _add_inbound(db_session, case, body, suffix=reason.value.lower())

    await process_inbound(db_session, email_row.id)

    handoff = await db_session.scalar(select(Handoff).where(Handoff.source_email_id == email_row.id))
    assert handoff is not None
    assert handoff.reason_code == reason.value
    assert handoff.extracted_facts["intent"] == intent
    assert handoff.extracted_facts[fact_name] is True
    assert await db_session.scalar(select(func.count()).select_from(Quote)) == 0
    assert await db_session.scalar(select(func.count()).select_from(Outbox)) == 0
    await db_session.refresh(case)
    assert case.status == CaseStatus.WAITING_HUMAN


async def test_recovered_processing_does_not_duplicate_handoff(db_session: AsyncSession) -> None:
    case = await _seed_case(db_session, with_quote=False)
    email_row = await _add_inbound(
        db_session,
        case,
        "PRODUCT WIDGET-100 Please send a sample.",
        suffix="handoff-idempotency",
    )

    await process_inbound(db_session, email_row.id)
    original = await db_session.scalar(select(Handoff).where(Handoff.source_email_id == email_row.id))
    assert original is not None
    duplicate = await create_handoff(
        db_session,
        case=case,
        reason=HandoffReason.SAMPLE_REQUEST,
        summary="Recovered processing attempt",
        source_email_id=email_row.id,
    )
    await process_inbound(db_session, email_row.id)

    assert duplicate.id == original.id
    assert await db_session.scalar(
        select(func.count()).select_from(Handoff).where(Handoff.source_email_id == email_row.id)
    ) == 1
    assert await db_session.scalar(
        select(func.count()).select_from(AuditEvent).where(AuditEvent.event_type == "handoff.created")
    ) == 1
    assert await db_session.scalar(
        select(func.count()).select_from(Job).where(Job.idempotency_key == f"handoff-notify:{original.id}")
    ) == 1


async def test_duplicate_raw_ingestion_repairs_missing_job(db_session: AsyncSession) -> None:
    case = await _seed_case(db_session, with_quote=False)
    message = MIMEMessage()
    message["From"] = "internal@example.com"
    message["To"] = "sales-agent@example.com"
    message["Subject"] = "Re: Industrial Widget 100 quotation"
    message["Message-ID"] = "<duplicate-ingestion@example.com>"
    message.set_content("PRODUCT WIDGET-100 Please send a sample.")
    raw = message.as_bytes()
    parsed = parse_mime(raw)
    stranded = EmailMessage(
        case_id=case.id,
        direction="INBOUND",
        mailbox="integration-test",
        message_id=parsed.message_id,
        in_reply_to=parsed.in_reply_to,
        references_json=parsed.references,
        from_address=parsed.from_address,
        to_addresses=parsed.to_addresses,
        subject=parsed.subject,
        body_text=parsed.body_text,
        body_html=parsed.body_html,
        attachment_metadata=parsed.attachments,
        raw_sha256=parsed.raw_sha256,
    )
    db_session.add(stranded)
    await db_session.commit()

    repaired = await ingest_raw_email(db_session, raw, mailbox="integration-test")
    repeated = await ingest_raw_email(db_session, raw, mailbox="integration-test")

    assert repaired is not None and repeated is not None
    assert repaired.id == stranded.id == repeated.id
    assert await db_session.scalar(
        select(func.count()).select_from(EmailMessage).where(EmailMessage.raw_sha256 == stranded.raw_sha256)
    ) == 1
    assert await db_session.scalar(
        select(func.count()).select_from(Job).where(Job.idempotency_key == f"process-inbound:{stranded.id}")
    ) == 1


async def test_counteroffer_uses_transition_and_freezes_reply(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await _seed_case(db_session, with_quote=True)
    email_row = await _add_inbound(
        db_session,
        case,
        "PRODUCT WIDGET-100 quantity 100. Your price is too high; can you do USD 92?",
        suffix="counteroffer",
    )
    transitions: list[tuple[CaseStage, CaseStage]] = []

    def record_transition(current: CaseStage, target: CaseStage) -> CaseStage:
        transitions.append((current, target))
        return domain_transition(current, target)

    monkeypatch.setattr("app.services.transition", record_transition)

    await process_inbound(db_session, email_row.id)

    latest_quote = await db_session.scalar(select(Quote).where(Quote.case_id == case.id).order_by(Quote.round_number.desc()))
    outbox = await db_session.scalar(select(Outbox).where(Outbox.business_key == f"inbound-reply:{email_row.id}"))
    await db_session.refresh(case)
    assert transitions == [(CaseStage.QUOTING, CaseStage.NEGOTIATING)]
    assert case.stage == CaseStage.NEGOTIATING
    assert latest_quote is not None
    assert latest_quote.round_number == 1
    assert latest_quote.unit_price == Decimal("97.0000")
    assert outbox is not None
    assert await db_session.scalar(
        select(func.count()).select_from(Handoff).where(Handoff.source_email_id == email_row.id)
    ) == 0


async def test_gmail_history_links_sent_and_inbox_without_processing_old_reply(db_session: AsyncSession) -> None:
    case = await _seed_case(db_session, with_quote=False)
    sent = MIMEMessage()
    sent["From"] = "sales-agent@example.com"
    sent["To"] = "internal@example.com"
    sent["Subject"] = "Industrial Widget 100 quotation"
    sent["Message-ID"] = "<legacy-sent@example.com>"
    sent["Date"] = format_datetime(datetime(2025, 5, 1, 8, 0, tzinfo=UTC))
    sent.set_content("Our earlier quotation")
    sent_row = await ingest_raw_email(
        db_session,
        sent.as_bytes(),
        mailbox="sales-agent@example.com",
        mailbox_folder="[Gmail]/Sent Mail",
        uid_validity=1,
        imap_uid=42,
        direction="OUTBOUND",
        is_history=True,
    )
    assert sent_row is not None

    reply = MIMEMessage()
    reply["From"] = "internal@example.com"
    reply["To"] = "sales-agent@example.com"
    reply["Subject"] = "Re: Industrial Widget 100 quotation"
    reply["Message-ID"] = "<legacy-reply@example.com>"
    reply["In-Reply-To"] = "<legacy-sent@example.com>"
    reply["References"] = "<legacy-sent@example.com>"
    reply["Date"] = format_datetime(datetime(2025, 5, 2, 8, 0, tzinfo=UTC))
    reply.set_content("We are interested. Please follow up.")
    reply_row = await ingest_raw_email(
        db_session,
        reply.as_bytes(),
        mailbox="sales-agent@example.com",
        mailbox_folder="INBOX",
        uid_validity=1,
        imap_uid=42,
        direction="INBOUND",
        is_history=True,
    )
    assert reply_row is not None

    result = await reconcile_email_history(db_session)
    await db_session.refresh(case)

    assert sent_row.case_id == case.id
    assert reply_row.case_id == case.id
    assert case.status == CaseStatus.WAITING_HUMAN
    assert result.replies_waiting_review == 1
    assert await db_session.scalar(
        select(func.count()).select_from(Job).where(Job.idempotency_key == f"process-inbound:{reply_row.id}")
    ) == 0
    review = await db_session.scalar(
        select(Handoff).where(Handoff.case_id == case.id, Handoff.summary == HISTORY_REVIEW_SUMMARY)
    )
    assert review is not None


async def test_historical_sent_mail_pauses_case_and_blocks_new_initial_outreach(db_session: AsyncSession) -> None:
    case = await _seed_case(db_session, with_quote=False)
    sent = MIMEMessage()
    sent["From"] = "sales-agent@example.com"
    sent["To"] = "internal@example.com"
    sent["Subject"] = "Industrial Widget 100 quotation"
    sent["Message-ID"] = "<legacy-no-reply@example.com>"
    sent["Date"] = format_datetime(datetime(2025, 6, 1, 8, 0, tzinfo=UTC))
    sent.set_content("Our earlier quotation")
    sent_row = await ingest_raw_email(
        db_session,
        sent.as_bytes(),
        mailbox="sales-agent@example.com",
        mailbox_folder="[Gmail]/Sent Mail",
        uid_validity=1,
        imap_uid=7,
        direction="OUTBOUND",
        is_history=True,
    )
    assert sent_row is not None

    result = await reconcile_email_history(db_session)
    await db_session.refresh(case)
    assert result.no_reply_cases_paused == 1
    assert case.status == CaseStatus.PAUSED

    await create_case_outreach(db_session, {"case_id": case.id, "quantity": 100})

    assert await db_session.scalar(select(func.count()).select_from(Quote).where(Quote.case_id == case.id)) == 0
    assert await db_session.scalar(select(func.count()).select_from(Outbox).where(Outbox.case_id == case.id)) == 0
    assert await db_session.scalar(
        select(func.count())
        .select_from(Handoff)
        .where(Handoff.case_id == case.id, Handoff.summary.like("Historical Gmail outreach exists%"))
    ) == 1


async def test_imap_cursor_marks_initial_batch_as_history_and_later_mail_as_live(db_session: AsyncSession) -> None:
    settings = get_settings()
    original_address = settings.gmail_address
    original_password = settings.gmail_app_password
    settings.gmail_address = "sales-agent@example.com"
    settings.gmail_app_password = "test-only"

    def raw_message(number: int) -> bytes:
        message = MIMEMessage()
        message["From"] = "sales-agent@example.com"
        message["To"] = "internal@example.com"
        message["Subject"] = f"History {number}"
        message["Message-ID"] = f"<history-{number}@example.com>"
        message.set_content(f"Message {number}")
        return message.as_bytes()

    class FakeClient:
        calls = 0

        def fetch_after(self, last_uid, expected_uid_validity, *, folder, limit):
            self.calls += 1
            assert folder == "[Gmail]/Sent Mail"
            assert limit == settings.imap_batch_size
            if self.calls == 1:
                assert last_uid == 0
                return 99, 2, [(1, raw_message(1)), (2, raw_message(2))]
            assert last_uid == 2
            assert expected_uid_validity == 99
            return 99, 3, [(3, raw_message(3))]

    try:
        client = FakeClient()
        assert await poll_folder_once(client, "[Gmail]/Sent Mail", "OUTBOUND") == 2
        assert await poll_folder_once(client, "[Gmail]/Sent Mail", "OUTBOUND") == 1
        rows = (
            (
                await db_session.execute(
                    select(EmailMessage).order_by(EmailMessage.imap_uid)
                )
            )
            .scalars()
            .all()
        )
        assert [(row.imap_uid, row.is_history) for row in rows] == [(1, True), (2, True), (3, False)]
        cursor = await db_session.get(MailboxCursor, ("sales-agent@example.com", "[Gmail]/Sent Mail"))
        assert cursor is not None
        assert cursor.last_uid == 3
        assert cursor.history_cutoff_uid == 2
        assert cursor.history_complete is True
    finally:
        settings.gmail_address = original_address
        settings.gmail_app_password = original_password
