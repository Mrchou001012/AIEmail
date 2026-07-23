import hashlib
import smtplib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from email import policy
from email.message import EmailMessage as MIMEMessage
from email.parser import BytesParser
from email.utils import format_datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import stub_analyze
from app.api import dashboard_data
from app.auto_replies import AutomatedReplyType
from app.bounces import BounceType
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
    JobStatus,
    MailboxCursor,
    MailboxDailyUsage,
    MailboxThrottle,
    Outbox,
    Product,
    Quote,
    ReactivationCampaign,
    ReactivationRecipient,
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
from app.mail import OutboundAttachment, extract_full_message_bodies, parse_mime
from app.recovery import (
    FalseCounterofferDuplicateSource,
    FalseCounterofferRecoveryRequest,
    recover_false_counteroffer,
)
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
    reconcile_permanent_bounce_handoffs,
    resolve_deliverability_handoff,
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


async def test_pending_weekly_prices_do_not_hide_non_quote_handoffs(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await _seed_case(db_session, with_quote=False)
    email_row = await _add_inbound(
        db_session,
        case,
        "PRODUCT WIDGET-100 Please send a sample.",
        suffix="pending-weekly-price-sample",
    )
    settings = Settings(
        _env_file=None,
        demo_mode=False,
        commercial_gate_enabled=True,
        commercial_scope="pending-non-quote-test",
        business_timezone="Asia/Kolkata",
        business_open_hour=0,
    )
    monkeypatch.setattr("app.services.get_settings", lambda: settings)

    await process_inbound(db_session, email_row.id)

    handoff = await db_session.scalar(
        select(Handoff).where(Handoff.source_email_id == email_row.id)
    )
    assert handoff is not None
    assert handoff.reason_code == HandoffReason.SAMPLE_REQUEST.value


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
    source = MIMEMessage()
    source["From"] = email_row.from_address
    source["To"] = "sales-agent@example.com"
    source["Subject"] = email_row.subject
    source["Message-ID"] = email_row.message_id
    source.set_content(email_row.body_text)
    source.add_alternative(
        '<p>PRODUCT WIDGET-100 Please quote 100 kg.</p><img src="cid:client-logo.png">',
        subtype="html",
    )
    source.get_payload()[-1].add_related(
        b"\x89PNG\r\n\x1a\n" + (b"client-logo" * 1_700),
        maintype="application",
        subtype="octet-stream",
        cid="<client-logo.png>",
        filename="client-logo.png",
        disposition="inline",
    )
    source_raw = source.as_bytes()
    source_parsed = parse_mime(source_raw)
    email_row.body_html = source_parsed.body_html
    email_row.attachment_metadata = source_parsed.attachments
    email_row.raw_sha256 = source_parsed.raw_sha256
    archive = (
        get_settings().runtime_dir
        / "inbound_archive"
        / f"{source_parsed.raw_sha256}.eml"
    )
    archive.write_bytes(source_raw)
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


async def test_autonomous_reply_preserves_complete_reference_chain(
    db_session: AsyncSession,
) -> None:
    case = await _seed_case(db_session, with_quote=False)
    email_row = await _add_inbound(
        db_session,
        case,
        "PRODUCT WIDGET-100 Please quote 100 kg. CURRENT REQUEST TOKEN",
        suffix="complete-autonomous-references",
    )
    source = MIMEMessage()
    source["From"] = "internal@example.com"
    source["To"] = "sales-agent@example.com"
    source["Subject"] = email_row.subject
    source["Message-ID"] = email_row.message_id
    source.set_content(
        "PRODUCT WIDGET-100 Please quote 100 kg. CURRENT REQUEST TOKEN\n\n"
        "On Thu, 16 Jul 2026, sales-agent@example.com wrote:\n"
        "> PREVIOUS QUOTE TOKEN\n"
        ">\n"
        "> On Wed, 15 Jul 2026, internal@example.com wrote:\n"
        "> > ROOT INQUIRY TOKEN"
    )
    source.add_alternative(
        "<p>PRODUCT WIDGET-100 Please quote 100 kg. CURRENT REQUEST TOKEN</p>"
        '<img src="cid:buyer-signature@example.com" alt="Buyer signature">'
        "<div>On Thu, 16 Jul 2026, sales-agent@example.com wrote:"
        "<blockquote><p>PREVIOUS QUOTE TOKEN</p>"
        "<div>On Wed, 15 Jul 2026, internal@example.com wrote:"
        "<blockquote><p>ROOT INQUIRY TOKEN</p></blockquote></div>"
        "</blockquote></div>",
        subtype="html",
    )
    source.get_payload()[-1].add_related(
        b"\x89PNG\r\n\x1a\ncomplete-reference-chain-logo",
        maintype="application",
        subtype="octet-stream",
        cid="<buyer-signature@example.com>",
        filename="buyer-signature.png",
        disposition="inline",
    )
    source_raw = source.as_bytes()
    source_parsed = parse_mime(source_raw)
    email_row.body_text = source_parsed.body_text
    email_row.body_html = source_parsed.body_html
    email_row.attachment_metadata = source_parsed.attachments
    email_row.raw_sha256 = source_parsed.raw_sha256
    email_row.references_json = ["<thread-root@example.com>"]
    email_row.in_reply_to = "<parent-only@example.com>"
    archive = (
        get_settings().runtime_dir
        / "inbound_archive"
        / f"{source_parsed.raw_sha256}.eml"
    )
    archive.write_bytes(source_raw)
    await db_session.commit()

    await process_inbound(db_session, email_row.id)

    outbox = await db_session.scalar(
        select(Outbox).where(Outbox.business_key == f"inbound-reply:{email_row.id}")
    )
    assert outbox is not None
    parsed = parse_mime(outbox.raw_message.encode("utf-8"))
    assert parsed.in_reply_to == email_row.message_id
    assert parsed.references == [
        "<thread-root@example.com>",
        "<parent-only@example.com>",
        email_row.message_id,
    ]
    mime = BytesParser(policy=policy.default).parsebytes(
        outbox.raw_message.encode("utf-8")
    )
    plain_body = mime.get_body(preferencelist=("plain",)).get_content()
    html_body = mime.get_body(preferencelist=("html",)).get_content()
    assert "> PRODUCT WIDGET-100 Please quote 100 kg. CURRENT REQUEST TOKEN" in plain_body
    assert "> > PREVIOUS QUOTE TOKEN" in plain_body
    assert "> > > ROOT INQUIRY TOKEN" in plain_body
    assert plain_body.count("CURRENT REQUEST TOKEN") == 1
    assert plain_body.count("PREVIOUS QUOTE TOKEN") == 1
    assert plain_body.count("ROOT INQUIRY TOKEN") == 1
    assert '<div class="aiemail-quoted-reply gmail_quote"' in mime.get_body(
        preferencelist=("html",)
    ).get_content()
    assert html_body.count("CURRENT REQUEST TOKEN") == 1
    assert html_body.count("PREVIOUS QUOTE TOKEN") == 1
    assert html_body.count("ROOT INQUIRY TOKEN") == 1
    quoted_image = next(
        part
        for part in mime.walk()
        if str(part.get("Content-ID") or "").startswith("<quoted-")
    )
    assert quoted_image.get_payload(decode=True) == (
        b"\x89PNG\r\n\x1a\ncomplete-reference-chain-logo"
    )
    full_text, full_html = extract_full_message_bodies(outbox.raw_message.encode("utf-8"))
    assert "CURRENT REQUEST TOKEN" in full_text
    assert full_html is not None
    assert "PREVIOUS QUOTE TOKEN" in full_html
    assert "ROOT INQUIRY TOKEN" in full_html


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
    source_email = await db_session.get(EmailMessage, handoff.source_email_id)
    assert source_email is not None
    source = MIMEMessage()
    source["From"] = source_email.from_address
    source["To"] = "sales-agent@example.com"
    source["Subject"] = source_email.subject
    source["Message-ID"] = source_email.message_id
    source.set_content(source_email.body_text)
    source.add_alternative(
        '<p>Please review this inquiry.</p><img src="cid:review-signature@example.com">',
        subtype="html",
    )
    source.get_payload()[-1].add_related(
        b"\x89PNG\r\n\x1a\nhuman-review-signature",
        maintype="image",
        subtype="png",
        cid="<review-signature@example.com>",
        filename="review-signature.png",
        disposition="inline",
    )
    source_raw = source.as_bytes()
    source_parsed = parse_mime(source_raw)
    source_email.body_html = source_parsed.body_html
    source_email.attachment_metadata = source_parsed.attachments
    source_email.raw_sha256 = source_parsed.raw_sha256
    source_email.references_json = ["<thread-root@example.com>"]
    source_email.in_reply_to = "<parent-only@example.com>"
    archive = (
        get_settings().runtime_dir
        / "inbound_archive"
        / f"{source_parsed.raw_sha256}.eml"
    )
    archive.write_bytes(source_raw)
    await db_session.commit()

    outbox = await queue_human_reply(
        db_session,
        handoff_id=handoff.id,
        subject="Re: New customer inquiry",
        body_text="Dear Customer,\n\nWe have reviewed your request.\n\nBest regards,",
        actor="reviewer",
        note="Reviewed and approved",
        resume_automation=False,
        attachments=(
            OutboundAttachment(
                filename="reviewed-quotation.pdf",
                content_type="application/pdf",
                payload=b"%PDF-1.7\nreviewed quotation",
            ),
        ),
    )

    await db_session.refresh(case)
    await db_session.refresh(handoff)
    assert outbox.approval_handoff_id == handoff.id
    assert outbox.human_approved_by == "reviewer"
    assert outbox.human_approved_at is not None
    assert "Shreya Saxena" in outbox.raw_message
    parsed = parse_mime(outbox.raw_message.encode("utf-8"))
    assert parsed.body_text.count("Best regards,") == 1
    assert parsed.in_reply_to == source_email.message_id
    assert parsed.references == [
        "<thread-root@example.com>",
        "<parent-only@example.com>",
        source_email.message_id,
    ]
    mime = BytesParser(policy=policy.default).parsebytes(
        outbox.raw_message.encode("utf-8")
    )
    assert "> Please review this inquiry." in mime.get_body(
        preferencelist=("plain",)
    ).get_content()
    assert '<div class="aiemail-quoted-reply gmail_quote"' in mime.get_body(
        preferencelist=("html",)
    ).get_content()
    historical_image = next(
        part
        for part in mime.walk()
        if str(part.get("Content-ID") or "").startswith("<quoted-")
    )
    uploaded_attachment = next(
        part
        for part in mime.walk()
        if part.get_content_disposition() == "attachment"
    )
    assert historical_image.get_payload(decode=True) == (
        b"\x89PNG\r\n\x1a\nhuman-review-signature"
    )
    assert uploaded_attachment.get_filename() == "reviewed-quotation.pdf"
    assert uploaded_attachment.get_payload(decode=True) == b"%PDF-1.7\nreviewed quotation"
    outbound_email = await db_session.scalar(
        select(EmailMessage).where(EmailMessage.message_id == outbox.message_id)
    )
    assert outbound_email is not None
    assert outbound_email.attachment_metadata[0]["filename"] == "reviewed-quotation.pdf"
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


async def test_reply_to_case_less_reactivation_can_create_product_case(
    db_session: AsyncSession,
) -> None:
    ids = await seed_demo_data(db_session)
    parent = Outbox(
        case_id=None,
        quote_id=None,
        message_kind="REACTIVATION",
        business_key="reactivation:test:case-less",
        message_id="<case-less-reactivation@example.com>",
        recipient="internal@example.com",
        raw_message="From: sales-agent@example.com\nTo: internal@example.com\n\nChecking in",
        status=DeliveryStatus.SENT,
        sent_at=datetime.now(UTC) - timedelta(minutes=5),
        sent_via="smtp",
    )
    db_session.add(parent)
    await db_session.flush()
    campaign = ReactivationCampaign(
        name="Case-less reply test",
        status="RUNNING",
        subject_template="Checking in",
        body_template="Hello",
        start_date=date.today(),
        created_by="test",
    )
    db_session.add(campaign)
    await db_session.flush()
    recipient = ReactivationRecipient(
        campaign_id=campaign.id,
        customer_id=ids["customer_id"],
        contact_id=ids["contact_id"],
        outbox_id=parent.id,
        status="SENT",
        eligible=True,
        selected=True,
        sent_at=parent.sent_at,
    )
    outbound_email = EmailMessage(
        case_id=None,
        customer_id=ids["customer_id"],
        contact_id=ids["contact_id"],
        direction="OUTBOUND",
        message_id=parent.message_id,
        from_address="sales-agent@example.com",
        to_addresses=[parent.recipient],
        subject="Checking in from Lanya Chem",
        body_text="Checking in",
        raw_sha256="c" * 64,
        received_at=parent.sent_at,
    )
    db_session.add_all([recipient, outbound_email])
    await db_session.commit()

    message = MIMEMessage()
    message["From"] = "internal@example.com"
    message["To"] = "sales-agent@example.com"
    message["Subject"] = "Re: Checking in from Lanya Chem"
    message["Message-ID"] = "<case-less-reactivation-reply@example.com>"
    message["In-Reply-To"] = parent.message_id
    message["References"] = parent.message_id
    message.set_content(
        "Please quote PRODUCT WIDGET-100 quantity 100 kg.\n\n"
        "On Monday, sales-agent@example.com wrote:\nChecking in about our products."
    )

    email_row = await ingest_raw_email(db_session, message.as_bytes(), mailbox="integration-test")

    assert email_row is not None and email_row.case_id is not None
    sales_case = await db_session.get(SalesCase, email_row.case_id)
    assert sales_case is not None
    assert sales_case.customer_id == ids["customer_id"]
    assert sales_case.contact_id == ids["contact_id"]
    assert sales_case.product_id == ids["product_id"]
    await db_session.refresh(parent)
    await db_session.refresh(recipient)
    await db_session.refresh(outbound_email)
    assert parent.case_id == sales_case.id
    assert recipient.case_id == sales_case.id
    assert recipient.status == "REPLIED"
    assert outbound_email.case_id == sales_case.id
    assert await db_session.scalar(
        select(func.count()).select_from(Handoff).where(Handoff.source_email_id == email_row.id)
    ) == 0
    audit_event = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.case_id == sales_case.id,
            AuditEvent.event_type == "case.created_from_new_inquiry",
        )
    )
    assert audit_event is not None
    assert audit_event.data["new_thread"] is False
    assert audit_event.data["reactivation_outbox_id"] == parent.id

    second = MIMEMessage()
    second["From"] = "internal@example.com"
    second["To"] = "sales-agent@example.com"
    second["Subject"] = "Re: Checking in from Lanya Chem"
    second["Message-ID"] = "<case-less-reactivation-second-reply@example.com>"
    second["In-Reply-To"] = parent.message_id
    second["References"] = parent.message_id
    second.set_content("Please quote PRODUCT WIDGET-100 quantity 200 kg.")
    second_row = await ingest_raw_email(
        db_session,
        second.as_bytes(),
        mailbox="integration-test",
    )
    assert second_row is not None and second_row.case_id == sales_case.id
    assert await db_session.scalar(select(func.count()).select_from(SalesCase)) == 1


async def test_case_less_reactivation_reply_with_prior_terms_requires_human_review(
    db_session: AsyncSession,
) -> None:
    ids = await seed_demo_data(db_session)
    sent_at = datetime.now(UTC) - timedelta(minutes=5)
    parent = Outbox(
        case_id=None,
        quote_id=None,
        message_kind="REACTIVATION",
        business_key="reactivation:test:prior-terms",
        message_id="<case-less-prior-terms@example.com>",
        recipient="internal@example.com",
        raw_message="From: sales-agent@example.com\nTo: internal@example.com\n\nChecking in",
        status=DeliveryStatus.SENT,
        sent_at=sent_at,
        sent_via="smtp",
    )
    db_session.add(parent)
    await db_session.flush()
    campaign = ReactivationCampaign(
        name="Prior terms safety test",
        status="RUNNING",
        subject_template="Checking in",
        body_template="Hello",
        start_date=date.today(),
        created_by="test",
    )
    db_session.add(campaign)
    await db_session.flush()
    recipient = ReactivationRecipient(
        campaign_id=campaign.id,
        customer_id=ids["customer_id"],
        contact_id=ids["contact_id"],
        outbox_id=parent.id,
        status="SENT",
        eligible=True,
        selected=True,
        sent_at=sent_at,
    )
    db_session.add(recipient)
    await db_session.commit()

    message = MIMEMessage()
    message["From"] = "internal@example.com"
    message["To"] = "sales-agent@example.com"
    message["Subject"] = "Re: Checking in"
    message["Message-ID"] = "<case-less-prior-terms-reply@example.com>"
    message["In-Reply-To"] = parent.message_id
    message["References"] = parent.message_id
    message.set_content(
        "Please quote PRODUCT WIDGET-100 quantity 100 kg, same as before."
    )

    email_row = await ingest_raw_email(db_session, message.as_bytes(), mailbox="integration-test")

    assert email_row is not None and email_row.case_id is None
    handoff = await db_session.scalar(
        select(Handoff).where(Handoff.source_email_id == email_row.id)
    )
    assert handoff is not None
    assert handoff.reason_code == HandoffReason.THREAD_AMBIGUOUS.value
    assert handoff.extracted_facts["prior_context_marker"] == "same as before"
    assert handoff.extracted_facts["reactivation_outbox_id"] == parent.id
    await db_session.refresh(recipient)
    assert recipient.status == "REPLIED"
    assert await db_session.scalar(select(func.count()).select_from(SalesCase)) == 0


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


async def _add_false_counteroffer_recovery_source(
    db_session: AsyncSession,
    case: SalesCase,
    *,
    message_id: str,
    requested_quantity: int,
    received_at: datetime,
    imap_uid: int,
    include_thread_headers: bool = True,
) -> tuple[EmailMessage, Handoff, str]:
    expected_body = f"Please quote {requested_quantity} kg PRODUCT WIDGET-100 instead."
    message = MIMEMessage()
    message["From"] = "internal@example.com"
    message["To"] = "sales-agent@example.com"
    message["Subject"] = "Re: Industrial Widget 100 quotation"
    message["Message-ID"] = message_id
    if include_thread_headers:
        message["In-Reply-To"] = "<original-quote@example.com>"
        message["References"] = "<root@example.com> <original-quote@example.com>"
    message.set_content(
        f"{expected_body}\n\n"
        "On Friday, July 17, 2026 Seller <sales-agent@example.com> wrote:\n"
        "Quantity: 100 kg\n"
        "Unit price: USD 100.0000 per kg"
    )
    raw = message.as_bytes()
    parsed = parse_mime(raw)
    assert parsed.body_text == expected_body

    polluted_body = (
        f"{expected_body}\n\n"
        "Seller wrote:\n"
        "Quantity: 100 kg\n"
        "Unit price: USD 100.0000 per kg"
    )
    email_row = EmailMessage(
        case_id=case.id,
        customer_id=case.customer_id,
        contact_id=case.contact_id,
        direction="INBOUND",
        mailbox="integration-test",
        mailbox_folder="INBOX",
        uid_validity=42,
        imap_uid=imap_uid,
        message_id=parsed.message_id,
        in_reply_to=parsed.in_reply_to,
        references_json=parsed.references,
        from_address=parsed.from_address,
        to_addresses=parsed.to_addresses,
        subject=parsed.subject,
        body_text=polluted_body,
        body_html=parsed.body_html,
        attachment_metadata=parsed.attachments,
        raw_sha256=parsed.raw_sha256,
        received_at=received_at,
    )
    db_session.add(email_row)
    await db_session.flush()

    handoff = Handoff(
        case_id=case.id,
        source_email_id=email_row.id,
        reason_code=HandoffReason.PRICE_NEGOTIATION.value,
        summary="Inbound counteroffer requires human review",
        extracted_facts={"intent": "counteroffer", "quantity": requested_quantity},
        status="OPEN",
        dingtalk_status="SENT",
    )
    db_session.add(handoff)
    await db_session.flush()
    db_session.add_all(
        [
            Job(
                kind="process_inbound",
                payload={"email_id": email_row.id},
                status=JobStatus.DONE,
                idempotency_key=f"process-inbound:{email_row.id}",
            ),
            Job(
                kind="notify_handoff",
                payload={"handoff_id": handoff.id},
                status=JobStatus.DONE,
                idempotency_key=f"handoff-notify:{handoff.id}",
            ),
        ]
    )
    archive = get_settings().runtime_dir / "inbound_archive" / f"{parsed.raw_sha256}.eml"
    archive.write_bytes(raw)
    return email_row, handoff, polluted_body


async def test_false_counteroffer_recovery_reparses_and_reprocesses(
    db_session: AsyncSession,
) -> None:
    case = await _seed_case(db_session, with_quote=True)
    case.status = CaseStatus.WAITING_HUMAN

    message = MIMEMessage()
    message["From"] = "internal@example.com"
    message["To"] = "sales-agent@example.com"
    message["Subject"] = "Re: Industrial Widget 100 quotation"
    message["Message-ID"] = "<false-counteroffer-recovery@example.com>"
    message.set_content(
        "Please quote 200 kg PRODUCT WIDGET-100 instead.\n\n"
        "Seller <sales-agent@example.com&gt;&nbsp;"
        "在 2026年7月17日 周五 10:02 写道：\n"
        "Quantity: 100 kg\n"
        "Unit price: USD 100.0000 per kg"
    )
    raw = message.as_bytes()
    parsed = parse_mime(raw)
    email_row = EmailMessage(
        case_id=case.id,
        customer_id=case.customer_id,
        contact_id=case.contact_id,
        direction="INBOUND",
        mailbox="integration-test",
        message_id=parsed.message_id,
        from_address=parsed.from_address,
        to_addresses=parsed.to_addresses,
        subject=parsed.subject,
        body_text=(
            "Please quote 200 kg PRODUCT WIDGET-100 instead.\n\n"
            "Seller wrote:\nQuantity: 100 kg\nUnit price: USD 100.0000 per kg"
        ),
        body_html=parsed.body_html,
        attachment_metadata=parsed.attachments,
        raw_sha256=parsed.raw_sha256,
    )
    db_session.add(email_row)
    await db_session.flush()

    handoff = Handoff(
        case_id=case.id,
        source_email_id=email_row.id,
        reason_code=HandoffReason.PRICE_NEGOTIATION.value,
        summary="Inbound counteroffer requires human review",
        extracted_facts={"intent": "counteroffer", "quantity": 200},
        status="OPEN",
        dingtalk_status="SENT",
    )
    db_session.add(handoff)
    await db_session.flush()
    db_session.add_all(
        [
            Job(
                kind="process_inbound",
                payload={"email_id": email_row.id},
                status=JobStatus.DONE,
                idempotency_key=f"process-inbound:{email_row.id}",
            ),
            Job(
                kind="notify_handoff",
                payload={"handoff_id": handoff.id},
                status=JobStatus.DONE,
                idempotency_key=f"handoff-notify:{handoff.id}",
            ),
        ]
    )
    await db_session.commit()
    email_id = email_row.id
    case_id = case.id
    handoff_id = handoff.id

    archive = get_settings().runtime_dir / "inbound_archive" / f"{parsed.raw_sha256}.eml"
    archive.write_bytes(raw)

    with pytest.raises(RuntimeError, match="expected second-round quantity quote"):
        await recover_false_counteroffer(
            FalseCounterofferRecoveryRequest(
                email_id=email_id,
                case_id=case_id,
                handoff_id=handoff_id,
                expected_body="Please quote 200 kg PRODUCT WIDGET-100 instead.",
                expected_existing_quantity=100,
                expected_new_quantity=201,
                expected_recipient="internal@example.com",
                recovery_commit="test-rollback-commit",
            )
        )

    db_session.expire_all()
    rolled_back_email = await db_session.get(EmailMessage, email_id)
    rolled_back_case = await db_session.get(SalesCase, case_id)
    rolled_back_handoff = await db_session.get(Handoff, handoff_id)
    assert rolled_back_email is not None
    assert rolled_back_case is not None
    assert rolled_back_handoff is not None
    assert "Seller wrote:" in rolled_back_email.body_text
    assert rolled_back_case.status == CaseStatus.WAITING_HUMAN
    assert rolled_back_case.negotiation_round == 0
    assert rolled_back_handoff.status == "OPEN"
    assert rolled_back_handoff.source_email_id == email_id
    assert await db_session.scalar(select(func.count()).select_from(Quote)) == 1
    assert await db_session.scalar(select(func.count()).select_from(Outbox)) == 0
    assert await db_session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.event_type.in_(
                ["email.reparsed_for_recovery", "handoff.false_positive_resolved"]
            )
        )
    ) == 0

    result = await recover_false_counteroffer(
        FalseCounterofferRecoveryRequest(
            email_id=email_id,
            case_id=case_id,
            handoff_id=handoff_id,
            expected_body="Please quote 200 kg PRODUCT WIDGET-100 instead.",
            expected_existing_quantity=100,
            expected_new_quantity=200,
            expected_recipient="internal@example.com",
            recovery_commit="test-recovery-commit",
        ),
    )

    db_session.expire_all()
    recovered_email = await db_session.get(EmailMessage, email_id)
    recovered_case = await db_session.get(SalesCase, case_id)
    recovered_handoff = await db_session.get(Handoff, handoff_id)
    assert recovered_email is not None
    assert recovered_case is not None
    assert recovered_handoff is not None
    assert recovered_email.body_text == "Please quote 200 kg PRODUCT WIDGET-100 instead."
    assert recovered_case.status == CaseStatus.ACTIVE
    assert recovered_case.negotiation_round == 1
    assert recovered_handoff.status == "RESOLVED"
    assert recovered_handoff.source_email_id is None
    assert recovered_handoff.dingtalk_status == "SENT"
    assert recovered_handoff.extracted_facts["recovery_source_email_id"] == email_id
    assert result.quote_quantity == 200
    assert result.outbox_status == DeliveryStatus.PENDING.value
    assert result.recipient == "internal@example.com"
    assert await db_session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.case_id == case_id,
            AuditEvent.event_type.in_(
                ["email.reparsed_for_recovery", "handoff.false_positive_resolved"]
            ),
        )
    ) == 2


@pytest.mark.parametrize(
    ("include_thread_headers", "expected_match_basis", "expected_max_gap_seconds"),
    [
        pytest.param(True, "thread_headers", 300, id="threaded"),
        pytest.param(False, "headerless_exact_120s", 120, id="headerless"),
    ],
)
async def test_false_counteroffer_recovery_collapses_duplicate_sources(
    db_session: AsyncSession,
    include_thread_headers: bool,
    expected_match_basis: str,
    expected_max_gap_seconds: int,
) -> None:
    case = await _seed_case(db_session, with_quote=True)
    case.status = CaseStatus.WAITING_HUMAN
    canonical_received_at = datetime(2026, 7, 17, 2, 20, 12, tzinfo=UTC)
    duplicate_email, duplicate_handoff, _ = await _add_false_counteroffer_recovery_source(
        db_session,
        case,
        message_id="<false-counteroffer-duplicate-earlier@example.com>",
        requested_quantity=200,
        received_at=canonical_received_at - timedelta(seconds=55),
        imap_uid=100,
        include_thread_headers=include_thread_headers,
    )
    canonical_email, canonical_handoff, _ = await _add_false_counteroffer_recovery_source(
        db_session,
        case,
        message_id="<false-counteroffer-duplicate-canonical@example.com>",
        requested_quantity=200,
        received_at=canonical_received_at,
        imap_uid=101,
        include_thread_headers=include_thread_headers,
    )
    await db_session.commit()
    case_id = case.id
    canonical_email_id = canonical_email.id
    canonical_handoff_id = canonical_handoff.id
    duplicate_email_id = duplicate_email.id
    duplicate_handoff_id = duplicate_handoff.id

    result = await recover_false_counteroffer(
        FalseCounterofferRecoveryRequest(
            email_id=canonical_email_id,
            case_id=case_id,
            handoff_id=canonical_handoff_id,
            expected_body="Please quote 200 kg PRODUCT WIDGET-100 instead.",
            expected_existing_quantity=100,
            expected_new_quantity=200,
            expected_recipient="internal@example.com",
            recovery_commit="test-duplicate-recovery-commit",
            duplicate_sources=(
                FalseCounterofferDuplicateSource(
                    email_id=duplicate_email_id,
                    handoff_id=duplicate_handoff_id,
                ),
            ),
        )
    )

    db_session.expire_all()
    recovered_case = await db_session.get(SalesCase, case_id)
    recovered_canonical_email = await db_session.get(EmailMessage, canonical_email_id)
    recovered_duplicate_email = await db_session.get(EmailMessage, duplicate_email_id)
    recovered_canonical_handoff = await db_session.get(Handoff, canonical_handoff_id)
    recovered_duplicate_handoff = await db_session.get(Handoff, duplicate_handoff_id)
    assert recovered_case is not None
    assert recovered_canonical_email is not None
    assert recovered_duplicate_email is not None
    assert recovered_canonical_handoff is not None
    assert recovered_duplicate_handoff is not None

    assert result.canonical_email_id == canonical_email_id
    assert result.duplicate_email_ids == (duplicate_email_id,)
    assert result.resolved_handoff_ids == (canonical_handoff_id, duplicate_handoff_id)
    assert recovered_case.status == CaseStatus.ACTIVE
    assert recovered_case.negotiation_round == 1
    assert recovered_canonical_email.body_text == "Please quote 200 kg PRODUCT WIDGET-100 instead."
    assert recovered_duplicate_email.body_text == "Please quote 200 kg PRODUCT WIDGET-100 instead."
    assert recovered_canonical_handoff.status == "RESOLVED"
    assert recovered_canonical_handoff.source_email_id is None
    assert recovered_canonical_handoff.dingtalk_status == "SENT"
    assert recovered_canonical_handoff.extracted_facts["recovery_role"] == "canonical"
    assert (
        recovered_canonical_handoff.extracted_facts["recovery_match_basis"]
        == expected_match_basis
    )
    assert recovered_duplicate_handoff.status == "RESOLVED"
    assert recovered_duplicate_handoff.source_email_id == duplicate_email_id
    assert recovered_duplicate_handoff.dingtalk_status == "SENT"
    assert recovered_duplicate_handoff.extracted_facts["recovery_role"] == "duplicate"
    assert recovered_duplicate_handoff.extracted_facts["duplicate_of_email_id"] == canonical_email_id
    assert (
        recovered_duplicate_handoff.extracted_facts["recovery_match_basis"]
        == expected_match_basis
    )
    assert recovered_duplicate_handoff.extracted_facts["duplicate_gap_seconds"] == 55
    assert (
        recovered_duplicate_handoff.extracted_facts["duplicate_max_gap_seconds"]
        == expected_max_gap_seconds
    )

    quotes = list(
        (
            await db_session.scalars(
                select(Quote).where(Quote.case_id == case_id).order_by(Quote.round_number)
            )
        ).all()
    )
    assert [(quote.round_number, quote.quantity) for quote in quotes] == [(0, 100), (1, 200)]
    outboxes = list((await db_session.scalars(select(Outbox))).all())
    assert len(outboxes) == 1
    outbox = outboxes[0]
    assert outbox.id == result.outbox_id
    assert outbox.business_key == f"inbound-reply:{canonical_email_id}"
    assert outbox.status == DeliveryStatus.PENDING
    assert outbox.recipient == "internal@example.com"
    parsed_outbound = parse_mime(outbox.raw_message.encode("utf-8"))
    assert parsed_outbound.in_reply_to == recovered_canonical_email.message_id
    assert await db_session.scalar(
        select(func.count())
        .select_from(Outbox)
        .where(Outbox.business_key.like(f"inbound-reply:{duplicate_email_id}%"))
    ) == 0

    reparse_audits = list(
        (
            await db_session.scalars(
                select(AuditEvent).where(
                    AuditEvent.case_id == case_id,
                    AuditEvent.event_type == "email.reparsed_for_recovery",
                )
            )
        ).all()
    )
    resolved_audits = list(
        (
            await db_session.scalars(
                select(AuditEvent).where(
                    AuditEvent.case_id == case_id,
                    AuditEvent.event_type == "handoff.false_positive_resolved",
                )
            )
        ).all()
    )
    duplicate_audits = list(
        (
            await db_session.scalars(
                select(AuditEvent).where(
                    AuditEvent.case_id == case_id,
                    AuditEvent.event_type == "email.duplicate_suppressed",
                )
            )
        ).all()
    )
    assert {(event.data["email_id"], event.data["role"]) for event in reparse_audits} == {
        (canonical_email_id, "canonical"),
        (duplicate_email_id, "duplicate"),
    }
    assert {(event.data["handoff_id"], event.data["role"]) for event in resolved_audits} == {
        (canonical_handoff_id, "canonical"),
        (duplicate_handoff_id, "duplicate"),
    }
    assert len(duplicate_audits) == 1
    assert duplicate_audits[0].data == {
        "email_id": duplicate_email_id,
        "handoff_id": duplicate_handoff_id,
        "canonical_email_id": canonical_email_id,
        "canonical_message_id": recovered_canonical_email.message_id,
        "duplicate_message_id": recovered_duplicate_email.message_id,
        "canonical_imap_uid": 101,
        "duplicate_imap_uid": 100,
        "duplicate_gap_seconds": 55,
        "duplicate_max_gap_seconds": expected_max_gap_seconds,
        "commit": "test-duplicate-recovery-commit",
        "match_basis": expected_match_basis,
    }

    counts_before_replay = (
        await db_session.scalar(select(func.count()).select_from(Quote)),
        await db_session.scalar(select(func.count()).select_from(Outbox)),
        await db_session.scalar(select(func.count()).select_from(Handoff)),
        await db_session.scalar(select(func.count()).select_from(AuditEvent)),
        await db_session.scalar(select(func.count()).select_from(Job)),
    )
    await process_inbound(db_session, duplicate_email_id)
    counts_after_replay = (
        await db_session.scalar(select(func.count()).select_from(Quote)),
        await db_session.scalar(select(func.count()).select_from(Outbox)),
        await db_session.scalar(select(func.count()).select_from(Handoff)),
        await db_session.scalar(select(func.count()).select_from(AuditEvent)),
        await db_session.scalar(select(func.count()).select_from(Job)),
    )
    assert counts_after_replay == counts_before_replay


@pytest.mark.parametrize(
    (
        "duplicate_quantity",
        "duplicate_time_offset",
        "duplicate_has_thread_headers",
        "canonical_has_thread_headers",
        "error_match",
    ),
    [
        (201, -55, True, True, "unexpected cleaned body"),
        (200, 1, True, True, "canonical email must be the latest request"),
        (200, -301, True, True, "not within 300s"),
        (200, -55, True, False, "thread-header presence differs"),
        (200, -55, False, True, "thread-header presence differs"),
        (200, -121, False, False, "not within 120s"),
    ],
)
async def test_false_counteroffer_duplicate_guard_rolls_back_everything(
    db_session: AsyncSession,
    duplicate_quantity: int,
    duplicate_time_offset: int,
    duplicate_has_thread_headers: bool,
    canonical_has_thread_headers: bool,
    error_match: str,
) -> None:
    case = await _seed_case(db_session, with_quote=True)
    case.status = CaseStatus.WAITING_HUMAN
    canonical_received_at = datetime(2026, 7, 17, 2, 20, 12, tzinfo=UTC)
    duplicate_email, duplicate_handoff, duplicate_polluted_body = (
        await _add_false_counteroffer_recovery_source(
            db_session,
            case,
            message_id=f"<false-counteroffer-guard-duplicate-{duplicate_quantity}-{duplicate_time_offset}@example.com>",
            requested_quantity=duplicate_quantity,
            received_at=canonical_received_at + timedelta(seconds=duplicate_time_offset),
            imap_uid=100,
            include_thread_headers=duplicate_has_thread_headers,
        )
    )
    canonical_email, canonical_handoff, canonical_polluted_body = (
        await _add_false_counteroffer_recovery_source(
            db_session,
            case,
            message_id=f"<false-counteroffer-guard-canonical-{duplicate_quantity}-{duplicate_time_offset}@example.com>",
            requested_quantity=200,
            received_at=canonical_received_at,
            imap_uid=101,
            include_thread_headers=canonical_has_thread_headers,
        )
    )
    await db_session.commit()
    case_id = case.id
    canonical_email_id = canonical_email.id
    canonical_handoff_id = canonical_handoff.id
    duplicate_email_id = duplicate_email.id
    duplicate_handoff_id = duplicate_handoff.id

    with pytest.raises(RuntimeError, match=error_match):
        await recover_false_counteroffer(
            FalseCounterofferRecoveryRequest(
                email_id=canonical_email_id,
                case_id=case_id,
                handoff_id=canonical_handoff_id,
                expected_body="Please quote 200 kg PRODUCT WIDGET-100 instead.",
                expected_existing_quantity=100,
                expected_new_quantity=200,
                expected_recipient="internal@example.com",
                recovery_commit="test-duplicate-guard-rollback",
                duplicate_sources=(
                    FalseCounterofferDuplicateSource(
                        email_id=duplicate_email_id,
                        handoff_id=duplicate_handoff_id,
                    ),
                ),
            )
        )

    db_session.expire_all()
    rolled_back_case = await db_session.get(SalesCase, case_id)
    rolled_back_canonical_email = await db_session.get(EmailMessage, canonical_email_id)
    rolled_back_duplicate_email = await db_session.get(EmailMessage, duplicate_email_id)
    rolled_back_canonical_handoff = await db_session.get(Handoff, canonical_handoff_id)
    rolled_back_duplicate_handoff = await db_session.get(Handoff, duplicate_handoff_id)
    assert rolled_back_case is not None
    assert rolled_back_canonical_email is not None
    assert rolled_back_duplicate_email is not None
    assert rolled_back_canonical_handoff is not None
    assert rolled_back_duplicate_handoff is not None
    assert rolled_back_case.status == CaseStatus.WAITING_HUMAN
    assert rolled_back_case.negotiation_round == 0
    assert rolled_back_canonical_email.body_text == canonical_polluted_body
    assert rolled_back_duplicate_email.body_text == duplicate_polluted_body
    assert rolled_back_canonical_handoff.status == "OPEN"
    assert rolled_back_canonical_handoff.source_email_id == canonical_email_id
    assert rolled_back_canonical_handoff.dingtalk_status == "SENT"
    assert "recovery_role" not in rolled_back_canonical_handoff.extracted_facts
    assert rolled_back_duplicate_handoff.status == "OPEN"
    assert rolled_back_duplicate_handoff.source_email_id == duplicate_email_id
    assert rolled_back_duplicate_handoff.dingtalk_status == "SENT"
    assert "recovery_role" not in rolled_back_duplicate_handoff.extracted_facts
    assert await db_session.scalar(select(func.count()).select_from(Quote)) == 1
    assert await db_session.scalar(select(func.count()).select_from(Outbox)) == 0
    assert await db_session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.event_type.in_(
                [
                    "email.reparsed_for_recovery",
                    "handoff.false_positive_resolved",
                    "email.duplicate_suppressed",
                ]
            )
        )
    ) == 0
    jobs = list((await db_session.scalars(select(Job).order_by(Job.id))).all())
    assert len(jobs) == 4
    assert all(job.status == JobStatus.DONE for job in jobs)


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


async def test_google_security_notification_is_archived_without_sales_workflow(
    db_session: AsyncSession,
) -> None:
    message = MIMEMessage()
    message["From"] = "Google <no-reply@accounts.google.com>"
    message["To"] = "sales-agent@example.com"
    message["Subject"] = "Security alert"
    message["Message-ID"] = "<google-security-alert@accounts.google.com>"
    message.set_content(
        "We noticed a new sign-in to your Google Account on an Apple iPhone 17 device."
    )

    email_row = await ingest_raw_email(db_session, message.as_bytes(), mailbox="integration-test")

    assert email_row is not None
    assert email_row.case_id is None
    assert email_row.customer_id is None
    assert email_row.contact_id is None
    assert email_row.is_automated_reply is True
    assert email_row.automated_reply_type == AutomatedReplyType.SYSTEM_NOTIFICATION.value
    assert email_row.automated_reply_handled_at is not None
    assert await db_session.scalar(select(func.count()).select_from(SalesCase)) == 0
    assert await db_session.scalar(
        select(func.count()).select_from(Handoff).where(Handoff.source_email_id == email_row.id)
    ) == 0
    assert await db_session.scalar(select(func.count()).select_from(Job)) == 0
    assert await db_session.scalar(select(func.count()).select_from(Outbox)) == 0
    assert await db_session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.event_type == "inbound.system_notification_ignored")
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


@pytest.mark.parametrize(
    ("mx_status", "detail"),
    [
        (MXStatus.NO_DOMAIN, "domain does not exist"),
        (MXStatus.NULL_MX, "domain explicitly accepts no email"),
    ],
)
async def test_smtp_preflight_auto_suppresses_permanent_domain_failure_without_handoff(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    mx_status: MXStatus,
    detail: str,
) -> None:
    case = await _seed_case(db_session, with_quote=False)
    await db_session.refresh(case, ["contact"])
    contact_email = case.contact.email
    monkeypatch.setattr(
        "app.services.lookup_mx",
        lambda domain, *, timeout_seconds: MXResult(mx_status, domain, error=detail),
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
        business_key=f"preflight-{mx_status.value.lower()}",
        message_id=f"<preflight-{mx_status.value.lower()}@example.com>",
        recipient=contact_email,
        raw_message="Subject: permanent domain failure\n\nbody",
        status=DeliveryStatus.PENDING,
        available_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    db_session.add(pending)
    await db_session.commit()

    assert await send_one_outbox(db_session, settings) is True
    await db_session.refresh(pending)
    await db_session.refresh(case)
    await db_session.refresh(case.contact)
    address = await db_session.get(EmailAddressStatus, contact_email)
    handoff_count = await db_session.scalar(select(func.count()).select_from(Handoff))

    assert pending.status == DeliveryStatus.CANCELLED
    assert pending.attempts == 0
    assert address is not None and address.preflight_status == mx_status.value
    assert address.suppressed is True
    assert address.suppression_reason == f"PREFLIGHT_{mx_status.value}"
    assert case.contact.suppressed is True
    assert case.status == CaseStatus.PAUSED
    assert handoff_count == 0


async def test_operator_can_resolve_legacy_deliverability_handoff_by_suppressing_recipient(
    db_session: AsyncSession,
) -> None:
    case = await _seed_case(db_session, with_quote=False)
    await db_session.refresh(case, ["contact"])
    contact_email = case.contact.email
    case.status = CaseStatus.WAITING_HUMAN
    pending = Outbox(
        case_id=case.id,
        business_key="legacy-deliverability",
        message_id="<legacy-deliverability@example.com>",
        recipient=contact_email,
        raw_message="Subject: legacy deliverability\n\nbody",
        status=DeliveryStatus.CANCELLED,
        last_error="recipient preflight blocked: domain does not exist",
    )
    db_session.add(pending)
    await db_session.flush()
    address = EmailAddressStatus(
        email=contact_email,
        domain=contact_email.rsplit("@", 1)[1],
        format_valid=True,
        preflight_status=MXStatus.NO_DOMAIN.value,
        suppressed=False,
    )
    handoff = Handoff(
        case_id=case.id,
        reason_code=HandoffReason.EMAIL_DELIVERABILITY.value,
        summary="Recipient preflight blocked",
        extracted_facts={
            "outbox_id": pending.id,
            "recipient": contact_email,
            "preflight_status": MXStatus.NO_DOMAIN.value,
        },
    )
    db_session.add_all([address, handoff])
    await db_session.commit()

    resolved = await resolve_deliverability_handoff(
        db_session,
        handoff_id=handoff.id,
        actor="integration-admin",
    )
    await db_session.refresh(case)
    await db_session.refresh(case.contact)
    await db_session.refresh(address)

    assert resolved.status == "RESOLVED"
    assert address.suppressed is True
    assert address.suppression_reason == "PREFLIGHT_NO_DOMAIN"
    assert case.contact.suppressed is True
    assert case.status == CaseStatus.PAUSED
    assert pending.status == DeliveryStatus.CANCELLED
    audit_row = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.event_type == "handoff.deliverability_recipient_suppressed")
    )
    assert audit_row is not None and audit_row.actor == "integration-admin"


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


async def test_correlated_nxdomain_bounce_does_not_create_human_review(
    db_session: AsyncSession,
) -> None:
    case = await _seed_case(db_session, with_quote=False)
    await db_session.refresh(case, ["contact"])
    contact_email = case.contact.email
    original_message_id = "<nxdomain-bounce-original@example.com>"
    sent = Outbox(
        case_id=case.id,
        business_key="nxdomain-bounce-original",
        message_id=original_message_id,
        recipient=contact_email,
        raw_message="Subject: Quote\n\nHello",
        status=DeliveryStatus.SENT,
        sent_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    db_session.add(sent)
    await db_session.commit()
    diagnostic = (
        "DNS Error: DNS type 'mx' lookup of aptuitlaurus.com responded with code NXDOMAIN. "
        "Domain name not found: aptuitlaurus.com. Check the address and try again."
    )
    raw = _integration_dsn(
        original_message_id=original_message_id,
        recipient=contact_email,
        status="4.4.1",
        diagnostic=diagnostic,
    )

    email_row = await ingest_raw_email(db_session, raw, mailbox="integration-test")
    assert email_row is not None
    await process_inbound(db_session, email_row.id)
    await db_session.refresh(case)
    address = await db_session.get(EmailAddressStatus, contact_email)
    handoff = await db_session.scalar(select(Handoff).where(Handoff.source_email_id == email_row.id))

    assert email_row.bounce_type == BounceType.HARD.value
    assert address is not None and address.suppressed is True
    assert address.suppression_reason == "HARD_BOUNCE"
    assert case.status == CaseStatus.PAUSED
    assert handoff is None


async def test_legacy_nxdomain_soft_review_is_resolved_automatically(
    db_session: AsyncSession,
) -> None:
    case = await _seed_case(db_session, with_quote=False)
    await db_session.refresh(case, ["contact"])
    contact_email = case.contact.email
    case.status = CaseStatus.WAITING_HUMAN
    sent = Outbox(
        case_id=case.id,
        business_key="legacy-nxdomain-original",
        message_id="<legacy-nxdomain-original@example.com>",
        recipient=contact_email,
        raw_message="Subject: Quote\n\nHello",
        status=DeliveryStatus.SENT,
        sent_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    db_session.add(sent)
    await db_session.flush()
    diagnostic = (
        "DNS Error: DNS type 'mx' lookup responded with code NXDOMAIN. "
        "Domain name not found. Check the address and try again."
    )
    email_row = EmailMessage(
        case_id=case.id,
        customer_id=case.customer_id,
        contact_id=case.contact_id,
        direction="INBOUND",
        mailbox="integration-test",
        mailbox_folder="INBOX",
        message_id="<legacy-nxdomain-bounce@googlemail.com>",
        from_address="mailer-daemon@googlemail.com",
        to_addresses=["sales@example.com"],
        subject="Delivery Status Notification (Failure)",
        body_text=f"Address not found. {diagnostic}",
        raw_sha256=hashlib.sha256(b"legacy-nxdomain-bounce").hexdigest(),
        is_history=False,
        is_bounce=True,
        bounce_type=BounceType.SOFT.value,
        bounce_metadata={
            "recipient": contact_email,
            "original_message_id": sent.message_id,
            "matched_outbox_id": sent.id,
            "status_code": "4.4.1",
            "diagnostic": diagnostic,
            "permanent": False,
        },
        bounce_handled_at=datetime.now(UTC),
    )
    db_session.add(email_row)
    await db_session.flush()
    handoff = Handoff(
        case_id=case.id,
        source_email_id=email_row.id,
        reason_code=HandoffReason.BOUNCE_REVIEW.value,
        summary=f"Review SOFT delivery failure for {contact_email}",
        extracted_facts={
            "recipient": contact_email,
            "outbox_id": sent.id,
            "status_code": "4.4.1",
            "diagnostic": diagnostic,
        },
        status="OPEN",
        dingtalk_status="PENDING",
    )
    db_session.add(handoff)
    await db_session.flush()
    notify_job = Job(
        kind="notify_handoff",
        payload={"handoff_id": handoff.id},
        idempotency_key=f"handoff-notify:{handoff.id}",
        status=JobStatus.PENDING,
    )
    db_session.add(notify_job)
    await db_session.commit()

    assert await reconcile_permanent_bounce_handoffs(db_session) == 1
    await db_session.refresh(case)
    await db_session.refresh(email_row)
    await db_session.refresh(handoff)
    await db_session.refresh(notify_job)
    address = await db_session.get(EmailAddressStatus, contact_email)

    assert email_row.bounce_type == BounceType.HARD.value
    assert email_row.bounce_metadata["permanent"] is True
    assert handoff.status == "RESOLVED"
    assert handoff.dingtalk_status == "CANCELLED"
    assert notify_job.status == JobStatus.DONE
    assert address is not None and address.suppression_reason == "HARD_BOUNCE"
    assert case.status == CaseStatus.PAUSED


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
