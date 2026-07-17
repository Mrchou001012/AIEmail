import hashlib
import smtplib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from email.message import EmailMessage as MIMEMessage
from email.utils import format_datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import stub_analyze
from app.api import dashboard_data
from app.auto_replies import AutomatedReplyType
from app.db import (
    AuditEvent,
    CaseStage,
    CaseStatus,
    Contact,
    Customer,
    DeliveryStatus,
    EmailAddressStatus,
    EmailDomainStatus,
    EmailMessage,
    Handoff,
    Job,
    MailboxCursor,
    MailboxDailyUsage,
    MailboxThrottle,
    Outbox,
    Product,
    Quote,
    SalesCase,
)
from app.deliverability import MXResult, MXStatus
from app.domain import HandoffReason
from app.history import (
    HISTORY_CASE_ASSIGNMENT_SUMMARY,
    HISTORY_REVIEW_SUMMARY,
    reconcile_email_history,
)
from app.imap_poller import poll_folder_once
from app.mail import parse_mime
from app.services import (
    active_policy,
    assign_handoff_case,
    create_case_for_handoff,
    create_case_outreach,
    create_handoff,
    ingest_raw_email,
    notify_handoff,
    process_inbound,
    queue_human_reply,
    seed_demo_data,
    send_one_outbox,
)
from app.settings import Settings, get_settings

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
        "unmatched_history_cases": 0,
        "customer_matched_case_unmatched": 0,
        "ai_failures": 0,
        "bounced_24h": 0,
        "suppressed_addresses": 0,
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


async def test_safe_inline_image_does_not_force_attachment_handoff(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await _seed_case(db_session, with_quote=False)
    email_row = await _add_inbound(
        db_session,
        case,
        "PRODUCT WIDGET-100 Please quote 100 kg.",
        suffix="safe-inline-image",
    )
    email_row.attachment_metadata = [
        {
            "filename": "client-logo.png",
            "content_type": "application/octet-stream",
            "disposition": None,
            "content_id": "<client-logo.png>",
            "size": 20_135,
            "sha256": hashlib.sha256(b"client-logo").hexdigest(),
        }
    ]
    await db_session.commit()

    async def overcautious_analyze(self, subject, body, attachments):
        analysis = stub_analyze(subject, body, attachments).model_copy(
            update={"risky_attachment": True}
        )
        return analysis, {
            "provider": "stub",
            "model": "stub-overcautious-attachment",
            "request_hash": hashlib.sha256(f"{subject}\n{body}".encode()).hexdigest(),
        }

    monkeypatch.setattr("app.services.AIClient.analyze", overcautious_analyze)

    await process_inbound(db_session, email_row.id)

    handoff = await db_session.scalar(select(Handoff).where(Handoff.source_email_id == email_row.id))
    outbox = await db_session.scalar(
        select(Outbox).where(Outbox.business_key == f"inbound-reply:{email_row.id}")
    )
    assert handoff is None
    assert outbox is not None
    assert outbox.status == DeliveryStatus.PENDING


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


async def _unassigned_handoff(db_session: AsyncSession, *, suffix: str) -> Handoff:
    email_row = EmailMessage(
        case_id=None,
        direction="INBOUND",
        mailbox="integration-test",
        message_id=f"<{suffix}@example.com>",
        from_address="internal@example.com",
        to_addresses=["sales-agent@example.com"],
        subject="New customer inquiry",
        body_text="Please review this inquiry.",
        attachment_metadata=[],
        raw_sha256=hashlib.sha256(suffix.encode()).hexdigest(),
    )
    db_session.add(email_row)
    await db_session.commit()
    return await create_handoff(
        db_session,
        case=None,
        reason=HandoffReason.THREAD_AMBIGUOUS,
        summary="Manual association required",
        source_email_id=email_row.id,
    )


async def test_human_can_assign_unmatched_email_to_existing_case(
    db_session: AsyncSession,
) -> None:
    case = await _seed_case(db_session, with_quote=True)
    handoff = await _unassigned_handoff(db_session, suffix="human-assign")

    assigned = await assign_handoff_case(
        db_session,
        handoff_id=handoff.id,
        case_id=case.id,
        actor="reviewer",
    )

    email_row = await db_session.get(EmailMessage, handoff.source_email_id)
    await db_session.refresh(case)
    assert assigned.case_id == case.id
    assert email_row is not None and email_row.case_id == case.id
    assert case.status == CaseStatus.WAITING_HUMAN
    audit_event = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.event_type == "handoff.case_assigned")
    )
    assert audit_event is not None and audit_event.actor == "reviewer"


async def test_human_can_create_case_for_unmatched_email(db_session: AsyncSession) -> None:
    ids = await seed_demo_data(db_session)
    handoff = await _unassigned_handoff(db_session, suffix="human-create-case")

    case = await create_case_for_handoff(
        db_session,
        handoff_id=handoff.id,
        contact_id=ids["contact_id"],
        product_id=ids["product_id"],
        currency="usd",
        actor="reviewer",
    )

    email_row = await db_session.get(EmailMessage, handoff.source_email_id)
    await db_session.refresh(handoff)
    assert case.currency == "USD"
    assert case.status == CaseStatus.WAITING_HUMAN
    assert email_row is not None and email_row.case_id == case.id
    assert handoff.case_id == case.id


async def test_human_approved_reply_is_audited_and_sends_with_auto_send_disabled(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    sent: list[str] = []

    class RecordingTransport:
        def send(self, raw_message, message_id, recipient):
            sent.append(recipient)

    case = await _seed_case(db_session, with_quote=True)
    await db_session.refresh(case, ["customer"])
    case.customer.auto_send_allowed = False
    await db_session.commit()
    handoff = await _unassigned_handoff(db_session, suffix="human-approved-send")
    await assign_handoff_case(
        db_session,
        handoff_id=handoff.id,
        case_id=case.id,
        actor="reviewer",
    )

    outbox = await queue_human_reply(
        db_session,
        handoff_id=handoff.id,
        subject="Re: New customer inquiry",
        body_text="Dear Customer,\n\nWe have reviewed your request.",
        actor="reviewer",
        note="Reviewed and approved",
        resume_automation=False,
    )

    await db_session.refresh(case)
    await db_session.refresh(handoff)
    assert outbox.approval_handoff_id == handoff.id
    assert outbox.human_approved_by == "reviewer"
    assert outbox.human_approved_at is not None
    assert "Shreya Saxena" in outbox.raw_message
    assert case.status == CaseStatus.HUMAN_TAKEOVER
    assert handoff.status == "RESOLVED"
    approval = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.event_type == "handoff.reply_approved")
    )
    assert approval is not None and approval.actor == "reviewer"

    monkeypatch.setattr("app.services.transport_for", lambda settings: RecordingTransport())
    settings = Settings(
        _env_file=None,
        mail_transport="smtp",
        auto_send_enabled=False,
        safe_mode=True,
        recipient_allowlist=["internal@example.com"],
        gmail_address="sales-agent@example.com",
        gmail_app_password="test-only",
        email_preflight_enabled=False,
        min_send_interval_seconds=0,
        send_interval_jitter_seconds=0,
        max_sends_per_hour=10,
        max_sends_per_day=20,
    )

    assert await send_one_outbox(db_session, settings) is True
    await db_session.refresh(outbox)
    assert sent == ["internal@example.com"]
    assert outbox.status == DeliveryStatus.SENT


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


async def test_new_subject_creates_independent_case_without_inheriting_quote_history(
    db_session: AsyncSession,
) -> None:
    existing_case = await _seed_case(db_session, with_quote=True)
    expired_quote = await db_session.scalar(select(Quote).where(Quote.case_id == existing_case.id))
    assert expired_quote is not None
    expired_quote.valid_until = date.today() - timedelta(days=1)
    await db_session.commit()
    message = MIMEMessage()
    message["From"] = "internal@example.com"
    message["To"] = "sales-agent@example.com"
    message["Subject"] = "Fresh WIDGET-100 inquiry"
    message["Message-ID"] = "<fresh-widget-inquiry@example.com>"
    message.set_content("Please quote PRODUCT WIDGET-100 quantity 100 kg.")

    email_row = await ingest_raw_email(db_session, message.as_bytes(), mailbox="integration-test")

    assert email_row is not None
    assert email_row.case_id is not None
    assert email_row.case_id != existing_case.id
    new_case = await db_session.get(SalesCase, email_row.case_id)
    assert new_case is not None
    assert new_case.stage == CaseStage.QUOTING
    assert new_case.negotiation_round == 0
    await process_inbound(db_session, email_row.id)

    quote = await db_session.scalar(select(Quote).where(Quote.case_id == new_case.id))
    outbox = await db_session.scalar(
        select(Outbox).where(Outbox.business_key == f"inbound-reply:{email_row.id}")
    )
    audit_event = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.case_id == new_case.id,
            AuditEvent.event_type == "case.created_from_new_inquiry",
        )
    )
    assert quote is not None and quote.round_number == 0
    assert quote.unit_price == Decimal("100.0000")
    assert outbox is not None
    assert audit_event is not None
    assert audit_event.data["possible_related_case_ids"] == [existing_case.id]


async def test_new_subject_with_recent_same_product_case_requires_manual_linking(
    db_session: AsyncSession,
) -> None:
    existing_case = await _seed_case(db_session, with_quote=True)
    message = MIMEMessage()
    message["From"] = "internal@example.com"
    message["To"] = "sales-agent@example.com"
    message["Subject"] = "Fresh WIDGET-100 inquiry"
    message["Message-ID"] = "<fresh-but-possibly-related@example.com>"
    message.set_content("Please quote PRODUCT WIDGET-100 quantity 100 kg.")

    email_row = await ingest_raw_email(db_session, message.as_bytes(), mailbox="integration-test")

    assert email_row is not None and email_row.case_id is None
    assert email_row.customer_id == existing_case.customer_id
    assert email_row.contact_id == existing_case.contact_id
    handoff = await db_session.scalar(select(Handoff).where(Handoff.source_email_id == email_row.id))
    assert handoff is not None
    assert handoff.reason_code == HandoffReason.THREAD_AMBIGUOUS.value
    assert handoff.extracted_facts["recent_related_case_ids"] == [existing_case.id]
    assert await db_session.scalar(select(func.count()).select_from(SalesCase)) == 1
    assert await db_session.scalar(select(func.count()).select_from(Outbox)) == 0


async def test_localized_reply_subject_recovers_unique_recent_case_without_thread_headers(
    db_session: AsyncSession,
) -> None:
    existing_case = await _seed_case(db_session, with_quote=True)
    message = MIMEMessage()
    message["From"] = "internal@example.com"
    message["To"] = "sales-agent@example.com"
    message["Subject"] = "回复：Re: Industrial Widget 100 quotation"
    message["Message-ID"] = "<localized-reply-without-thread-headers@example.com>"
    message.set_content("Please quote PRODUCT WIDGET-100 quantity 100 kg.")

    email_row = await ingest_raw_email(db_session, message.as_bytes(), mailbox="integration-test")

    assert email_row is not None and email_row.case_id == existing_case.id
    assert await db_session.scalar(select(func.count()).select_from(SalesCase)) == 1
    assert await db_session.scalar(select(func.count()).select_from(Handoff)) == 0
    audit_event = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.case_id == existing_case.id,
            AuditEvent.event_type == "email.thread_recovered",
        )
    )
    assert audit_event is not None
    assert audit_event.data["recovered_thread"] is True
    assert audit_event.data["match_basis"] == "unique_recent_contact_product_currency_subject"


async def test_log_only_dingtalk_notification_is_not_marked_sent(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff = await create_handoff(
        db_session,
        case=None,
        reason=HandoffReason.THREAD_AMBIGUOUS,
        summary="Integration test",
        facts={},
    )

    class LogNotifier:
        async def notify(self, handoff: Handoff, case: SalesCase | None) -> str:
            return "LOGGED"

    monkeypatch.setattr("app.services.DingTalkNotifier", LogNotifier)
    await notify_handoff(db_session, handoff.id)

    await db_session.refresh(handoff)
    assert handoff.dingtalk_status == "LOGGED"


async def test_new_subject_referring_to_previous_quote_requires_manual_linking(
    db_session: AsyncSession,
) -> None:
    existing_case = await _seed_case(db_session, with_quote=True)
    message = MIMEMessage()
    message["From"] = "internal@example.com"
    message["To"] = "sales-agent@example.com"
    message["Subject"] = "Price discussion"
    message["Message-ID"] = "<lost-thread-context@example.com>"
    message.set_content(
        "Regarding your previous quote for PRODUCT WIDGET-100 quantity 100 kg, please review it."
    )

    email_row = await ingest_raw_email(db_session, message.as_bytes(), mailbox="integration-test")

    assert email_row is not None and email_row.case_id is None
    handoff = await db_session.scalar(select(Handoff).where(Handoff.source_email_id == email_row.id))
    assert handoff is not None
    assert handoff.reason_code == HandoffReason.THREAD_AMBIGUOUS.value
    assert handoff.extracted_facts["prior_context_marker"] == "previous quote"
    assert await db_session.scalar(select(func.count()).select_from(SalesCase)) == 1
    assert existing_case.id == 1


async def test_new_subject_with_multiple_products_requires_human_selection(
    db_session: AsyncSession,
) -> None:
    existing_case = await _seed_case(db_session, with_quote=False)
    message = MIMEMessage()
    message["From"] = "internal@example.com"
    message["To"] = "sales-agent@example.com"
    message["Subject"] = "New quotation request"
    message["Message-ID"] = "<multi-product-new-thread@example.com>"
    message.set_content("Please quote YAC-TES and YAC-TMCS, quantity 100 kg each.")

    email_row = await ingest_raw_email(db_session, message.as_bytes(), mailbox="integration-test")

    assert email_row is not None and email_row.case_id is None
    assert email_row.customer_id == existing_case.customer_id
    assert email_row.contact_id == existing_case.contact_id
    handoff = await db_session.scalar(select(Handoff).where(Handoff.source_email_id == email_row.id))
    assert handoff is not None
    assert handoff.reason_code == HandoffReason.NEW_INQUIRY_REVIEW.value
    assert handoff.extracted_facts["product_codes"] == ["YAC-TES", "YAC-TMCS"]


async def test_unmatched_reply_headers_do_not_fall_back_to_subject(db_session: AsyncSession) -> None:
    await _seed_case(db_session, with_quote=True)
    message = MIMEMessage()
    message["From"] = "internal@example.com"
    message["To"] = "sales-agent@example.com"
    message["Subject"] = "Re: Industrial Widget 100 quotation"
    message["Message-ID"] = "<orphan-reply@example.com>"
    message["In-Reply-To"] = "<unknown-message@example.com>"
    message["References"] = "<unknown-message@example.com>"
    message.set_content("Please quote PRODUCT WIDGET-100 quantity 100 kg.")

    email_row = await ingest_raw_email(db_session, message.as_bytes(), mailbox="integration-test")

    assert email_row is not None and email_row.case_id is None
    handoff = await db_session.scalar(select(Handoff).where(Handoff.source_email_id == email_row.id))
    assert handoff is not None
    assert handoff.reason_code == HandoffReason.THREAD_AMBIGUOUS.value
    assert handoff.extracted_facts["in_reply_to"] == "<unknown-message@example.com>"


async def test_counteroffer_hands_off_without_changing_quote(
    db_session: AsyncSession,
) -> None:
    case = await _seed_case(db_session, with_quote=True)
    email_row = await _add_inbound(
        db_session,
        case,
        "PRODUCT WIDGET-100 quantity 100. Your price is too high; can you do USD 92?",
        suffix="counteroffer",
    )
    await process_inbound(db_session, email_row.id)

    latest_quote = await db_session.scalar(select(Quote).where(Quote.case_id == case.id).order_by(Quote.round_number.desc()))
    outbox = await db_session.scalar(select(Outbox).where(Outbox.business_key == f"inbound-reply:{email_row.id}"))
    await db_session.refresh(case)
    assert case.stage == CaseStage.QUOTING
    assert latest_quote is not None
    assert latest_quote.round_number == 0
    assert latest_quote.unit_price == Decimal("100.0000")
    assert outbox is None
    handoff = await db_session.scalar(select(Handoff).where(Handoff.source_email_id == email_row.id))
    assert handoff is not None
    assert handoff.reason_code == HandoffReason.PRICE_NEGOTIATION.value


async def test_ready_stock_lead_time_gets_bounded_reply(db_session: AsyncSession) -> None:
    case = await _seed_case(db_session, with_quote=True)
    email_row = await _add_inbound(
        db_session,
        case,
        "PRODUCT WIDGET-100 quantity 100. Please quote and confirm the lead time.",
        suffix="ready-stock-lead-time",
    )

    await process_inbound(db_session, email_row.id)

    outbox = await db_session.scalar(
        select(Outbox).where(Outbox.business_key == f"inbound-reply:{email_row.id}")
    )
    assert outbox is not None
    parsed = parse_mime(outbox.raw_message.encode())
    assert "Availability: Ready stock" in parsed.body_text
    assert await db_session.scalar(
        select(func.count()).select_from(Handoff).where(Handoff.source_email_id == email_row.id)
    ) == 0


async def test_out_of_office_reply_is_recorded_and_silently_handled(db_session: AsyncSession) -> None:
    case = await _seed_case(db_session, with_quote=True)
    message = MIMEMessage()
    message["From"] = "internal@example.com"
    message["To"] = "sales-agent@example.com"
    message["Subject"] = "Re: Industrial Widget 100 quotation"
    message["Message-ID"] = "<out-of-office@example.com>"
    message["Auto-Submitted"] = "auto-replied"
    message.set_content(
        "I am currently out of the office and will return on 22 July 2026. "
        "For urgent matters please contact backup@example.com."
    )

    email_row = await ingest_raw_email(db_session, message.as_bytes(), mailbox="integration-test")
    assert email_row is not None
    assert email_row.case_id == case.id
    assert email_row.automated_reply_type == AutomatedReplyType.OUT_OF_OFFICE.value

    await process_inbound(db_session, email_row.id)

    await db_session.refresh(email_row)
    await db_session.refresh(case)
    assert email_row.automated_reply_handled_at is not None
    assert email_row.automated_reply_metadata["return_hint"] == "22 July 2026"
    assert email_row.automated_reply_metadata["replacement_emails"] == ["backup@example.com"]
    assert case.status == CaseStatus.ACTIVE
    assert await db_session.scalar(
        select(func.count()).select_from(Handoff).where(Handoff.source_email_id == email_row.id)
    ) == 0
    assert await db_session.scalar(
        select(func.count()).select_from(Outbox).where(Outbox.business_key == f"inbound-reply:{email_row.id}")
    ) == 0
    assert await db_session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.event_type == "inbound.automated_reply_handled")
    ) == 1


async def test_departed_contact_is_suppressed_and_handed_off(db_session: AsyncSession) -> None:
    case = await _seed_case(db_session, with_quote=True)
    message = MIMEMessage()
    message["From"] = "internal@example.com"
    message["To"] = "sales-agent@example.com"
    message["Subject"] = "Re: Industrial Widget 100 quotation"
    message["Message-ID"] = "<departed-contact@example.com>"
    message["Auto-Submitted"] = "auto-replied"
    message.set_content(
        "I no longer work with Example Ltd. Going forward, please contact newbuyer@example.com."
    )

    email_row = await ingest_raw_email(db_session, message.as_bytes(), mailbox="integration-test")
    assert email_row is not None
    await process_inbound(db_session, email_row.id)

    handoff = await db_session.scalar(select(Handoff).where(Handoff.source_email_id == email_row.id))
    contact = await db_session.get(Contact, case.contact_id)
    await db_session.refresh(case)
    await db_session.refresh(email_row)
    assert handoff is not None
    assert handoff.reason_code == HandoffReason.PERSONNEL_CHANGE.value
    assert handoff.extracted_facts["replacement_emails"] == ["newbuyer@example.com"]
    assert contact is not None and contact.suppressed
    assert case.status == CaseStatus.WAITING_HUMAN
    assert email_row.automated_reply_handled_at is not None
    assert await db_session.scalar(
        select(func.count()).select_from(Outbox).where(Outbox.business_key == f"inbound-reply:{email_row.id}")
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
    assert sent_row.customer_id == case.customer_id
    assert sent_row.contact_id == case.contact_id
    assert reply_row.case_id == case.id
    assert reply_row.customer_id == case.customer_id
    assert reply_row.contact_id == case.contact_id
    assert case.status == CaseStatus.WAITING_HUMAN
    assert result.replies_waiting_review == 1
    assert await db_session.scalar(
        select(func.count()).select_from(Job).where(Job.idempotency_key == f"process-inbound:{reply_row.id}")
    ) == 0
    review = await db_session.scalar(
        select(Handoff).where(Handoff.case_id == case.id, Handoff.summary == HISTORY_REVIEW_SUMMARY)
    )
    assert review is not None


async def test_history_links_unique_contact_without_guessing_case(
    db_session: AsyncSession,
) -> None:
    ids = await seed_demo_data(db_session)
    row = EmailMessage(
        direction="INBOUND",
        mailbox="sales-agent@example.com",
        mailbox_folder="INBOX",
        message_id="<contact-only-history@example.com>",
        from_address="internal@example.com",
        to_addresses=["sales-agent@example.com"],
        subject="General introduction",
        body_text="Hello",
        attachment_metadata=[],
        raw_sha256=hashlib.sha256(b"contact-only-history").hexdigest(),
        is_history=True,
        received_at=datetime(2025, 4, 1, 8, 0, tzinfo=UTC),
    )
    db_session.add(row)
    await db_session.commit()

    result = await reconcile_email_history(db_session)
    second_result = await reconcile_email_history(db_session)
    await db_session.refresh(row)

    assert row.customer_id == ids["customer_id"]
    assert row.contact_id == ids["contact_id"]
    assert row.case_id is None
    assert result.customer_matched_messages == 1
    assert result.customer_unmatched_messages == 0
    assert result.customer_matched_case_unmatched_messages == 1
    assert result.replies_waiting_review == 1
    assert second_result.replies_waiting_review == 0
    review = await db_session.scalar(
        select(Handoff).where(Handoff.source_email_id == row.id)
    )
    assert review is not None
    assert review.case_id is None
    assert review.reason_code == HandoffReason.THREAD_AMBIGUOUS.value
    assert review.summary == HISTORY_CASE_ASSIGNMENT_SUMMARY
    assert review.extracted_facts["contact_id"] == ids["contact_id"]


async def test_history_uses_explicit_product_to_choose_one_of_multiple_cases(
    db_session: AsyncSession,
) -> None:
    first_case = await _seed_case(db_session, with_quote=False)
    product = Product(
        code="YAC-TES",
        name="YAC-TES",
        unit="kg",
        approved_text_key="yac_tes",
    )
    db_session.add(product)
    await db_session.flush()
    second_case = SalesCase(
        customer_id=first_case.customer_id,
        contact_id=first_case.contact_id,
        product_id=product.id,
        currency="USD",
        stage=CaseStage.QUOTING,
        status=CaseStatus.ACTIVE,
        subject_key="yac-tes quotation",
    )
    db_session.add(second_case)
    await db_session.flush()
    row = EmailMessage(
        direction="INBOUND",
        mailbox="sales-agent@example.com",
        mailbox_folder="INBOX",
        message_id="<explicit-product-history@example.com>",
        from_address="internal@example.com",
        to_addresses=["sales-agent@example.com"],
        subject="Please quote YAC-TES",
        body_text="We need YAC-TES pricing.",
        attachment_metadata=[],
        raw_sha256=hashlib.sha256(b"explicit-product-history").hexdigest(),
        is_history=True,
        received_at=datetime(2025, 4, 2, 8, 0, tzinfo=UTC),
    )
    db_session.add(row)
    await db_session.commit()

    await reconcile_email_history(db_session)
    await db_session.refresh(row)

    assert row.case_id == second_case.id
    assert row.customer_id == second_case.customer_id
    assert row.contact_id == second_case.contact_id


async def test_history_does_not_link_email_shared_by_multiple_customers(
    db_session: AsyncSession,
) -> None:
    await seed_demo_data(db_session)
    second_customer = Customer(company_name="Second Company")
    db_session.add(second_customer)
    await db_session.flush()
    db_session.add(
        Contact(
            customer_id=second_customer.id,
            name="Second Buyer",
            email="internal@example.com",
        )
    )
    row = EmailMessage(
        direction="INBOUND",
        mailbox="sales-agent@example.com",
        mailbox_folder="INBOX",
        message_id="<ambiguous-contact-history@example.com>",
        from_address="internal@example.com",
        to_addresses=["sales-agent@example.com"],
        subject="Hello",
        body_text="Hello",
        attachment_metadata=[],
        raw_sha256=hashlib.sha256(b"ambiguous-contact-history").hexdigest(),
        is_history=True,
        received_at=datetime(2025, 4, 3, 8, 0, tzinfo=UTC),
    )
    db_session.add(row)
    await db_session.commit()

    result = await reconcile_email_history(db_session)
    await db_session.refresh(row)

    assert row.customer_id is None
    assert row.contact_id is None
    assert row.case_id is None
    assert result.customer_unmatched_messages == 1


async def test_contact_linked_history_blocks_initial_outreach_without_case_guess(
    db_session: AsyncSession,
) -> None:
    case = await _seed_case(db_session, with_quote=False)
    row = EmailMessage(
        customer_id=case.customer_id,
        contact_id=case.contact_id,
        direction="OUTBOUND",
        mailbox="sales-agent@example.com",
        mailbox_folder="[Gmail]/Sent Mail",
        message_id="<contact-outreach-history@example.com>",
        from_address="sales-agent@example.com",
        to_addresses=["internal@example.com"],
        subject="Unrelated historical note",
        body_text="Earlier contact",
        attachment_metadata=[],
        raw_sha256=hashlib.sha256(b"contact-outreach-history").hexdigest(),
        is_history=True,
        received_at=datetime(2025, 3, 1, 8, 0, tzinfo=UTC),
    )
    db_session.add(row)
    await db_session.commit()

    await create_case_outreach(db_session, {"case_id": case.id, "quantity": 100})

    assert await db_session.scalar(select(func.count()).select_from(Quote)) == 0
    assert await db_session.scalar(select(func.count()).select_from(Outbox)) == 0
    assert await db_session.scalar(
        select(func.count())
        .select_from(Handoff)
        .where(Handoff.case_id == case.id, Handoff.summary.like("Historical Gmail outreach exists%"))
    ) == 1


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


async def test_old_history_does_not_take_over_case_with_newer_live_activity(
    db_session: AsyncSession,
) -> None:
    case = await _seed_case(db_session, with_quote=False)
    live_received_at = case.last_activity_at + timedelta(minutes=1)
    linked_history = EmailMessage(
        case_id=case.id,
        customer_id=case.customer_id,
        contact_id=case.contact_id,
        direction="OUTBOUND",
        mailbox="sales-agent@example.com",
        mailbox_folder="[Gmail]/Sent Mail",
        message_id="<linked-old-history@example.com>",
        from_address="sales-agent@example.com",
        to_addresses=["internal@example.com"],
        subject="Industrial Widget 100 quotation",
        body_text="Older linked quotation",
        attachment_metadata=[],
        raw_sha256=hashlib.sha256(b"linked-old-history").hexdigest(),
        is_history=True,
        received_at=datetime(2025, 6, 1, 8, 0, tzinfo=UTC),
    )
    unlinked_history = EmailMessage(
        customer_id=case.customer_id,
        contact_id=case.contact_id,
        direction="OUTBOUND",
        mailbox="sales-agent@example.com",
        mailbox_folder="[Gmail]/Sent Mail",
        message_id="<unlinked-old-history@example.com>",
        from_address="sales-agent@example.com",
        to_addresses=["internal@example.com"],
        subject="Industrial Widget 100 quotation",
        body_text="Older unlinked quotation",
        attachment_metadata=[],
        raw_sha256=hashlib.sha256(b"unlinked-old-history").hexdigest(),
        is_history=True,
        received_at=datetime(2025, 5, 1, 8, 0, tzinfo=UTC),
    )
    live_email = EmailMessage(
        case_id=case.id,
        customer_id=case.customer_id,
        contact_id=case.contact_id,
        direction="OUTBOUND",
        mailbox="sales-agent@example.com",
        mailbox_folder="[Gmail]/Sent Mail",
        message_id="<new-live-email@example.com>",
        from_address="sales-agent@example.com",
        to_addresses=["internal@example.com"],
        subject="Industrial Widget 100 quotation",
        body_text="Current live quotation",
        attachment_metadata=[],
        raw_sha256=hashlib.sha256(b"new-live-email").hexdigest(),
        is_history=False,
        received_at=live_received_at,
    )
    db_session.add_all([linked_history, unlinked_history, live_email])
    await db_session.commit()

    result = await reconcile_email_history(db_session)
    await db_session.refresh(case)
    await db_session.refresh(unlinked_history)

    assert unlinked_history.case_id is None
    assert case.status == CaseStatus.ACTIVE
    assert case.last_activity_at == live_email.received_at
    assert result.no_reply_cases_paused == 0


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

        def fetch_after(self, last_uid, expected_uid_validity, *, folder, limit, max_bytes):
            self.calls += 1
            assert folder == "[Gmail]/Sent Mail"
            assert limit == settings.imap_batch_size
            assert max_bytes <= settings.imap_daily_download_limit_mb * 1024 * 1024
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
        usage = await db_session.get(
            MailboxDailyUsage,
            ("sales-agent@example.com", datetime.now(UTC).date()),
        )
        assert usage is not None
        assert usage.imap_download_bytes == sum(len(raw_message(number)) for number in (1, 2, 3))
    finally:
        settings.gmail_address = original_address
        settings.gmail_app_password = original_password


async def test_mailbox_sent_history_enforces_spacing_without_calling_smtp(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    now = datetime.now(UTC)
    db_session.add(
        EmailMessage(
            direction="OUTBOUND",
            mailbox="sales-agent@example.com",
            mailbox_folder="[Gmail]/Sent Mail",
            message_id="<manual-send@example.com>",
            from_address="sales-agent@example.com",
            to_addresses=["buyer@example.com"],
            subject="Manual send",
            body_text="Manual send",
            raw_sha256=hashlib.sha256(b"manual-send").hexdigest(),
            received_at=now - timedelta(seconds=30),
        )
    )
    pending = Outbox(
        business_key="spacing-test",
        message_id="<spacing-test@example.com>",
        recipient="buyer@example.com",
        raw_message="Subject: spacing\n\nbody",
        status=DeliveryStatus.PENDING,
        available_at=now - timedelta(seconds=1),
    )
    db_session.add(pending)
    await db_session.commit()
    monkeypatch.setattr("app.services.transport_for", lambda settings: pytest.fail("SMTP must not be called"))
    settings = Settings(
        _env_file=None,
        mail_transport="smtp",
        auto_send_enabled=True,
        safe_mode=False,
        gmail_address="sales-agent@example.com",
        gmail_app_password="test-only",
        email_preflight_enabled=False,
        min_send_interval_seconds=120,
        send_interval_jitter_seconds=0,
        max_sends_per_hour=5,
        max_sends_per_day=20,
    )

    assert await send_one_outbox(db_session, settings) is True
    await db_session.refresh(pending)

    assert pending.status == DeliveryStatus.PENDING
    assert pending.attempts == 0
    assert pending.last_error == "mailbox-wide send spacing deferred message"
    assert pending.available_at >= now + timedelta(seconds=89)


async def test_gmail_rate_error_sets_durable_mailbox_cooldown(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    calls = 0

    class RateLimitedTransport:
        def send(self, raw_message, message_id, recipient):
            nonlocal calls
            calls += 1
            raise smtplib.SMTPDataError(421, b"4.7.28 Rate limit exceeded")

    monkeypatch.setattr("app.services.transport_for", lambda settings: RateLimitedTransport())
    settings = Settings(
        _env_file=None,
        mail_transport="smtp",
        auto_send_enabled=True,
        safe_mode=False,
        gmail_address="sales-agent@example.com",
        gmail_app_password="test-only",
        email_preflight_enabled=False,
        min_send_interval_seconds=0,
        send_interval_jitter_seconds=0,
        max_sends_per_hour=5,
        max_sends_per_day=20,
        gmail_transient_cooldown_seconds=600,
    )
    first = Outbox(
        business_key="cooldown-first",
        message_id="<cooldown-first@example.com>",
        recipient="buyer@example.com",
        raw_message="Subject: first\n\nbody",
        status=DeliveryStatus.PENDING,
        available_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    db_session.add(first)
    await db_session.commit()

    assert await send_one_outbox(db_session, settings) is True
    await db_session.refresh(first)
    throttle = await db_session.get(MailboxThrottle, "sales-agent@example.com")

    assert calls == 1
    assert first.status == DeliveryStatus.PENDING
    assert first.attempts == 0
    assert throttle is not None
    assert throttle.cooldown_until >= datetime.now(UTC) + timedelta(minutes=9)

    second = Outbox(
        business_key="cooldown-second",
        message_id="<cooldown-second@example.com>",
        recipient="buyer@example.com",
        raw_message="Subject: second\n\nbody",
        status=DeliveryStatus.PENDING,
        available_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    db_session.add(second)
    await db_session.commit()

    assert await send_one_outbox(db_session, settings) is True
    await db_session.refresh(second)
    assert calls == 1
    assert second.status == DeliveryStatus.PENDING
    assert second.available_at == throttle.cooldown_until


async def test_smtp_preflight_uses_cached_mx_and_sends_valid_recipient(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    lookups = 0
    sent: list[str] = []

    def valid_mx(domain: str, *, timeout_seconds: int) -> MXResult:
        nonlocal lookups
        lookups += 1
        return MXResult(MXStatus.VALID, domain, ("10 mx.example.net",))

    class RecordingTransport:
        def send(self, raw_message, message_id, recipient):
            sent.append(recipient)

    monkeypatch.setattr("app.services.lookup_mx", valid_mx)
    monkeypatch.setattr("app.services.transport_for", lambda settings: RecordingTransport())
    settings = Settings(
        _env_file=None,
        mail_transport="smtp",
        auto_send_enabled=True,
        safe_mode=False,
        min_send_interval_seconds=0,
        send_interval_jitter_seconds=0,
        max_sends_per_hour=10,
        max_sends_per_day=20,
    )
    first = Outbox(
        business_key="preflight-valid-1",
        message_id="<preflight-valid-1@example.com>",
        recipient="buyer@example.com",
        raw_message="Subject: valid\n\nbody",
        status=DeliveryStatus.PENDING,
        available_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    second = Outbox(
        business_key="preflight-valid-2",
        message_id="<preflight-valid-2@example.com>",
        recipient="other@example.com",
        raw_message="Subject: valid\n\nbody",
        status=DeliveryStatus.PENDING,
        available_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    db_session.add_all([first, second])
    await db_session.commit()

    assert await send_one_outbox(db_session, settings) is True
    assert await send_one_outbox(db_session, settings) is True
    await db_session.refresh(first)
    await db_session.refresh(second)

    assert first.status == DeliveryStatus.SENT
    assert second.status == DeliveryStatus.SENT
    assert sent == ["buyer@example.com", "other@example.com"]
    assert lookups == 1
    domain = await db_session.get(EmailDomainStatus, "example.com")
    assert domain is not None and domain.mx_status == MXStatus.VALID.value


async def test_smtp_preflight_blocks_missing_mx_without_permanent_suppression(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    case = await _seed_case(db_session, with_quote=False)
    await db_session.refresh(case, ["contact"])
    contact_email = case.contact.email
    monkeypatch.setattr(
        "app.services.lookup_mx",
        lambda domain, *, timeout_seconds: MXResult(MXStatus.NO_MX, domain, error="domain has no MX record"),
    )
    monkeypatch.setattr("app.services.transport_for", lambda settings: pytest.fail("SMTP must not be called"))
    settings = Settings(
        _env_file=None,
        mail_transport="smtp",
        auto_send_enabled=True,
        safe_mode=False,
        min_send_interval_seconds=0,
        send_interval_jitter_seconds=0,
    )
    pending = Outbox(
        case_id=case.id,
        business_key="preflight-no-mx",
        message_id="<preflight-no-mx@example.com>",
        recipient=contact_email,
        raw_message="Subject: no mx\n\nbody",
        status=DeliveryStatus.PENDING,
        available_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    db_session.add(pending)
    await db_session.commit()

    assert await send_one_outbox(db_session, settings) is True
    await db_session.refresh(pending)
    await db_session.refresh(case)
    address = await db_session.get(EmailAddressStatus, contact_email)
    handoff = await db_session.scalar(select(Handoff).where(Handoff.case_id == case.id))

    assert pending.status == DeliveryStatus.CANCELLED
    assert "no MX" in (pending.last_error or "")
    assert address is not None and address.preflight_status == MXStatus.NO_MX.value
    assert address.suppressed is False
    assert case.status == CaseStatus.WAITING_HUMAN
    assert handoff is not None and handoff.reason_code == HandoffReason.EMAIL_DELIVERABILITY.value


async def test_smtp_preflight_defers_temporary_dns_failure_without_attempt(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.services.lookup_mx",
        lambda domain, *, timeout_seconds: MXResult(MXStatus.TEMPORARY_ERROR, domain, error="DNS timeout"),
    )
    monkeypatch.setattr("app.services.transport_for", lambda settings: pytest.fail("SMTP must not be called"))
    settings = Settings(
        _env_file=None,
        mail_transport="smtp",
        auto_send_enabled=True,
        safe_mode=False,
        min_send_interval_seconds=0,
        send_interval_jitter_seconds=0,
        mx_temporary_retry_minutes=30,
    )
    pending = Outbox(
        business_key="preflight-dns-timeout",
        message_id="<preflight-dns-timeout@example.com>",
        recipient="buyer@example.com",
        raw_message="Subject: dns timeout\n\nbody",
        status=DeliveryStatus.PENDING,
        available_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    db_session.add(pending)
    await db_session.commit()
    before = datetime.now(UTC)

    assert await send_one_outbox(db_session, settings) is True
    await db_session.refresh(pending)

    assert pending.status == DeliveryStatus.PENDING
    assert pending.attempts == 0
    assert pending.available_at >= before + timedelta(minutes=29)


def _integration_dsn(*, original_message_id: str, recipient: str, status: str, diagnostic: str) -> bytes:
    return f"""From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>
To: sales-agent@example.com
Subject: Delivery Status Notification (Failure)
Message-ID: <bounce-{original_message_id.strip('<>')}@googlemail.com>
MIME-Version: 1.0
Content-Type: multipart/report; report-type=delivery-status; boundary=dsn

--dsn
Content-Type: text/plain; charset=utf-8

Your message wasn't delivered to {recipient}. {diagnostic}
--dsn
Content-Type: message/delivery-status

Final-Recipient: rfc822; {recipient}
Action: failed
Status: {status}
Diagnostic-Code: smtp; {diagnostic}

--dsn
Content-Type: message/rfc822

Message-ID: {original_message_id}
From: sales-agent@example.com
To: {recipient}
Subject: Quote

Hello
--dsn--
""".replace("\n", "\r\n").encode()


async def test_correlated_hard_bounce_permanently_suppresses_recipient(
    db_session: AsyncSession,
) -> None:
    case = await _seed_case(db_session, with_quote=False)
    await db_session.refresh(case, ["contact"])
    contact_id = case.contact.id
    contact_email = case.contact.email
    original_message_id = "<hard-bounce-original@example.com>"
    sent = Outbox(
        case_id=case.id,
        business_key="hard-bounce-original",
        message_id=original_message_id,
        recipient=contact_email,
        raw_message="Subject: Quote\n\nHello",
        status=DeliveryStatus.SENT,
        sent_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    db_session.add(sent)
    await db_session.commit()
    raw = _integration_dsn(
        original_message_id=original_message_id,
        recipient=contact_email,
        status="5.1.1",
        diagnostic="550 5.1.1 The email account does not exist",
    )

    email_row = await ingest_raw_email(db_session, raw, mailbox="integration-test")
    assert email_row is not None
    await process_inbound(db_session, email_row.id)
    await db_session.refresh(case)
    await db_session.refresh(email_row)
    contact = await db_session.get(Contact, contact_id)
    address = await db_session.get(EmailAddressStatus, contact_email)
    handoff_count = await db_session.scalar(select(func.count()).select_from(Handoff))

    assert email_row.is_bounce is True
    assert email_row.bounce_type == "HARD"
    assert email_row.bounce_handled_at is not None
    assert address is not None and address.suppressed is True
    assert address.suppression_reason == "HARD_BOUNCE"
    assert address.suppression_source_email_id == email_row.id
    assert contact is not None and contact.suppressed is True
    assert case.status == CaseStatus.PAUSED
    assert handoff_count == 0


async def test_uncorrelated_hard_bounce_goes_to_review_without_suppression(
    db_session: AsyncSession,
) -> None:
    recipient = "unknown-buyer@example.com"
    raw = _integration_dsn(
        original_message_id="<not-sent-by-us@example.com>",
        recipient=recipient,
        status="5.1.1",
        diagnostic="550 5.1.1 User unknown",
    )

    email_row = await ingest_raw_email(db_session, raw, mailbox="integration-test")
    assert email_row is not None
    await process_inbound(db_session, email_row.id)
    address = await db_session.get(EmailAddressStatus, recipient)
    handoff = await db_session.scalar(select(Handoff).where(Handoff.source_email_id == email_row.id))

    assert address is not None and address.suppressed is False
    assert address.last_bounce_type == "HARD"
    assert handoff is not None and handoff.reason_code == HandoffReason.BOUNCE_REVIEW.value
