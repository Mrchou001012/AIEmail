import asyncio
import hashlib
import html
import logging
import re
import smtplib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from email.utils import parseaddr
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy import case as sa_case
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.ai import AIClient, validate_rendered_email
from app.auto_replies import AutomatedReplyType, classify_automated_reply
from app.bounces import BounceType, classify_bounce, classify_smtp_failure
from app.commercial import (
    QuoteContext,
    QuoteContextStatus,
    get_commercial_data_provider,
    get_or_create_current_cycle,
    is_business_day,
    is_business_open,
    lock_commercial_scope,
    next_business_open,
)
from app.db import (
    AIInvocation,
    AuditEvent,
    CaseStage,
    CaseStatus,
    CommercialDataCycle,
    Contact,
    Customer,
    DeliveryStatus,
    EmailAddressStatus,
    EmailDomainStatus,
    EmailMessage,
    Handoff,
    Job,
    JobStatus,
    MailboxThrottle,
    Outbox,
    PricePolicy,
    Product,
    Quote,
    ReactivationRecipient,
    SalesCase,
)
from app.deliverability import MXResult, MXStatus, lookup_mx, validate_address_format
from app.domain import (
    HandoffReason,
    Intent,
    PricingPolicy,
    SendContext,
    evaluate_send_policy,
    initial_quote,
    quote_valid_until,
    transition,
)
from app.history import resolve_unique_contact
from app.imports import ContentBundle, load_content
from app.integrations import DingTalkNotifier
from app.mail import (
    FullReplySource,
    GmailIMAPClient,
    InlineImageAsset,
    ParsedEmail,
    append_quoted_reply,
    attachments_require_review,
    build_message,
    extract_full_reply_source,
    has_thread_subject_prefix,
    html_requires_mime_resources,
    match_case,
    normalized_subject,
    parse_mime,
    transport_for,
)
from app.products import canonical_product_code, find_product_codes, product_codes_match
from app.reactivation import reactivation_send_guard, record_reactivation_reply
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)

PRIOR_THREAD_MARKERS = (
    "previous quote",
    "previous quotation",
    "earlier quote",
    "earlier quotation",
    "last quote",
    "last price",
    "as discussed",
    "as agreed",
    "same as before",
    "revised quote",
    "revised quotation",
    "revise your quote",
    "follow up on",
    "our previous conversation",
    "your previous offer",
)


@dataclass(frozen=True)
class NewInquiryResolution:
    case: SalesCase | None
    reason: HandoffReason | None = None
    summary: str | None = None
    facts: dict[str, Any] | None = None


@dataclass(frozen=True)
class CaseLessReactivationParent:
    outbox: Outbox
    recipient: ReactivationRecipient


class JobDeferred(RuntimeError):
    """A durable business wait that must not consume the job retry budget."""

    def __init__(self, reason: str, available_at: datetime):
        super().__init__(reason)
        self.reason = reason
        self.available_at = available_at


def _pricing_policy(row: PricePolicy) -> PricingPolicy:
    return PricingPolicy(
        standard_price=Decimal(row.standard_price),
        absolute_floor=Decimal(row.absolute_floor),
        max_discount_pct=Decimal(row.max_discount_pct),
        concession_step_pct=Decimal(row.concession_step_pct),
        max_negotiation_rounds=row.max_negotiation_rounds,
        min_quantity=row.min_quantity,
        max_quantity=row.max_quantity,
        currency=row.currency,
        standard_incoterm=row.standard_incoterm,
        allowed_incoterms=tuple(row.allowed_incoterms),
        standard_payment_term=row.standard_payment_term,
        allowed_payment_terms=tuple(row.allowed_payment_terms),
        tier_1_max_multiple=Decimal(row.tier_1_max_multiple) if row.tier_1_max_multiple is not None else None,
        tier_1_markup_pct=Decimal(row.tier_1_markup_pct),
        tier_2_max_multiple=Decimal(row.tier_2_max_multiple) if row.tier_2_max_multiple is not None else None,
        tier_2_markup_pct=Decimal(row.tier_2_markup_pct),
    )


async def audit(
    session: AsyncSession,
    event_type: str,
    *,
    case_id: int | None,
    actor: str,
    data: dict[str, Any] | None = None,
) -> None:
    session.add(AuditEvent(case_id=case_id, actor=actor, event_type=event_type, data=data or {}))


async def _email_address_status(session: AsyncSession, email_address: str) -> EmailAddressStatus:
    normalized = email_address.strip().casefold()[:320]
    row = await session.get(EmailAddressStatus, normalized)
    if row is None:
        row = EmailAddressStatus(email=normalized, suppressed=False)
        session.add(row)
        await session.flush()
    return row


async def _suppress_email_address(
    session: AsyncSession,
    email_address: str,
    *,
    reason: str,
    source_email_id: int | None = None,
    bounce_type: str | None = None,
    diagnostic: str | None = None,
) -> EmailAddressStatus:
    now = datetime.now(UTC)
    status = await _email_address_status(session, email_address)
    status.suppressed = True
    status.suppression_reason = reason
    status.suppression_source_email_id = source_email_id
    status.suppressed_at = status.suppressed_at or now
    if bounce_type:
        status.last_bounce_at = now
        status.last_bounce_type = bounce_type
        status.last_bounce_diagnostic = diagnostic[:2000] if diagnostic else None
    contacts = (
        (
            await session.execute(
                select(Contact).where(func.lower(Contact.email) == status.email)
            )
        )
        .scalars()
        .all()
    )
    for contact in contacts:
        contact.suppressed = True
    return status


async def _recipient_preflight(
    session: AsyncSession,
    recipient: str,
    settings: Settings,
) -> tuple[str, str, dict[str, Any]]:
    """Return ALLOW, BLOCK, or DEFER plus a stable detail and audit facts."""
    if not settings.email_preflight_enabled:
        return "ALLOW", "recipient preflight disabled", {"preflight_status": "DISABLED"}

    now = datetime.now(UTC)
    format_result = validate_address_format(recipient)
    status = await _email_address_status(session, format_result.normalized)
    status.format_valid = format_result.valid
    status.domain = format_result.domain
    status.last_preflight_at = now
    if status.suppressed:
        status.preflight_status = "SUPPRESSED"
        detail = f"recipient permanently suppressed: {status.suppression_reason or 'unspecified'}"
        status.last_preflight_detail = detail
        return "BLOCK", detail, {
            "recipient": status.email,
            "preflight_status": "SUPPRESSED",
            "suppression_reason": status.suppression_reason,
        }
    if not format_result.valid:
        detail = f"invalid recipient format: {format_result.error or 'invalid address'}"
        status.preflight_status = "INVALID_FORMAT"
        status.last_preflight_detail = detail
        await _suppress_email_address(session, status.email, reason="INVALID_FORMAT")
        return "BLOCK", detail, {
            "recipient": status.email,
            "preflight_status": "INVALID_FORMAT",
            "format_error": format_result.error,
        }
    if not settings.mx_check_enabled:
        status.preflight_status = MXStatus.UNCHECKED.value
        status.last_preflight_detail = "MX checking disabled"
        return "ALLOW", "MX checking disabled", {
            "recipient": status.email,
            "domain": status.domain,
            "preflight_status": MXStatus.UNCHECKED.value,
        }

    assert format_result.domain is not None
    domain_status = await session.get(EmailDomainStatus, format_result.domain)
    cache_ttl = (
        timedelta(minutes=settings.mx_temporary_retry_minutes)
        if domain_status and domain_status.mx_status == MXStatus.TEMPORARY_ERROR.value
        else timedelta(hours=settings.mx_cache_ttl_hours)
    )
    cache_fresh = bool(domain_status and domain_status.checked_at >= now - cache_ttl)
    if cache_fresh and domain_status is not None:
        mx_result = MXResult(
            MXStatus(domain_status.mx_status),
            domain_status.domain,
            tuple(domain_status.mx_records),
            domain_status.last_error,
        )
    else:
        mx_result = await asyncio.to_thread(
            lookup_mx,
            format_result.domain,
            timeout_seconds=settings.mx_lookup_timeout_seconds,
        )
        if domain_status is None:
            domain_status = EmailDomainStatus(
                domain=format_result.domain,
                mx_status=mx_result.status.value,
                mx_records=list(mx_result.records),
                checked_at=now,
                last_error=mx_result.error,
            )
            session.add(domain_status)
        else:
            domain_status.mx_status = mx_result.status.value
            domain_status.mx_records = list(mx_result.records)
            domain_status.checked_at = now
            domain_status.last_error = mx_result.error

    status.preflight_status = mx_result.status.value
    status.last_preflight_detail = mx_result.error
    facts = {
        "recipient": status.email,
        "domain": mx_result.domain,
        "preflight_status": mx_result.status.value,
        "mx_records": list(mx_result.records),
        "cache_hit": cache_fresh,
        "detail": mx_result.error,
    }
    if mx_result.deliverable:
        return "ALLOW", "recipient format and MX checks passed", facts
    if mx_result.temporary:
        return "DEFER", mx_result.error or "temporary DNS lookup failure", facts
    return "BLOCK", mx_result.error or "recipient domain cannot receive email", facts


async def enqueue_job(
    session: AsyncSession,
    kind: str,
    payload: dict[str, Any],
    idempotency_key: str,
    available_at: datetime | None = None,
) -> Job | None:
    try:
        async with session.begin_nested():
            job = Job(
                kind=kind,
                payload=payload,
                idempotency_key=idempotency_key,
                available_at=available_at or datetime.now(UTC),
            )
            session.add(job)
            await session.flush()
        await session.commit()
        return job
    except IntegrityError:
        # The nested transaction already rolled back the conflicting insert.
        # Commit the still-valid outer transaction without expiring unrelated
        # ORM instances that callers may continue to use.
        await session.commit()
        return None


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
        or not is_business_day(settings, observed_at)
        or not is_business_open(settings, observed_at)
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
            context.next_check_at
            or (datetime.now(UTC) + timedelta(minutes=settings.commercial_retry_minutes)),
        )
    return context


async def create_handoff(
    session: AsyncSession,
    *,
    case: SalesCase | None,
    reason: HandoffReason,
    summary: str,
    facts: dict[str, Any] | None = None,
    source_email_id: int | None = None,
) -> Handoff:
    created = False
    try:
        async with session.begin_nested():
            handoff = Handoff(
                case_id=case.id if case else None,
                source_email_id=source_email_id,
                reason_code=reason.value,
                summary=summary,
                extracted_facts=facts or {},
            )
            session.add(handoff)
            await session.flush()
            created = True
    except IntegrityError as exc:
        if source_email_id is None:
            raise
        handoff = await session.scalar(select(Handoff).where(Handoff.source_email_id == source_email_id))
        if handoff is None:
            raise
        expected_case_id = case.id if case else None
        if handoff.case_id != expected_case_id:
            raise RuntimeError(f"email {source_email_id} is already attached to a different case handoff") from exc

    if created:
        if case and case.status == CaseStatus.ACTIVE:
            case.status = CaseStatus.WAITING_HUMAN
        await audit(
            session,
            "handoff.created",
            case_id=case.id if case else None,
            actor="system",
            data={"handoff_id": handoff.id, "reason": reason.value, "source_email_id": source_email_id},
        )
        await session.commit()
    await enqueue_job(
        session,
        "notify_handoff",
        {"handoff_id": handoff.id},
        f"handoff-notify:{handoff.id}",
    )
    return handoff


async def assign_handoff_case(
    session: AsyncSession,
    *,
    handoff_id: int,
    case_id: int,
    actor: str,
) -> Handoff:
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None:
        raise ValueError("handoff not found")
    if handoff.source_email_id is None:
        raise ValueError("handoff has no source email to associate")
    email_row = await session.get(EmailMessage, handoff.source_email_id)
    case = await session.scalar(
        select(SalesCase)
        .options(selectinload(SalesCase.contact))
        .where(SalesCase.id == case_id)
    )
    if email_row is None or case is None:
        raise ValueError("source email or case not found")
    if email_row.direction != "INBOUND":
        raise ValueError("only inbound email can be associated with a handoff case")
    if case.status in {CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST}:
        raise ValueError("closed case cannot accept a new inbound email")
    if email_row.from_address.casefold() != case.contact.email.casefold():
        raise ValueError("source sender does not match the selected case contact")
    if email_row.case_id not in {None, case.id}:
        raise ValueError("source email is already associated with a different case")

    previous_case_id = handoff.case_id
    email_row.case_id = case.id
    email_row.customer_id = case.customer_id
    email_row.contact_id = case.contact_id
    handoff.case_id = case.id
    case.status = CaseStatus.WAITING_HUMAN
    await audit(
        session,
        "handoff.case_assigned",
        case_id=case.id,
        actor=actor,
        data={
            "handoff_id": handoff.id,
            "email_id": email_row.id,
            "previous_case_id": previous_case_id,
        },
    )
    await session.commit()
    return handoff


async def create_case_for_handoff(
    session: AsyncSession,
    *,
    handoff_id: int,
    contact_id: int,
    product_id: int,
    currency: str,
    actor: str,
) -> SalesCase:
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None:
        raise ValueError("handoff not found")
    if handoff.source_email_id is None:
        raise ValueError("handoff has no source email")
    email_row = await session.get(EmailMessage, handoff.source_email_id)
    contact = await session.get(Contact, contact_id)
    product = await session.get(Product, product_id)
    normalized_currency = currency.strip().upper()
    if email_row is None or contact is None or product is None:
        raise ValueError("source email, contact, or product not found")
    if email_row.direction != "INBOUND":
        raise ValueError("only inbound email can create a reviewed case")
    if email_row.from_address.casefold() != contact.email.casefold():
        raise ValueError("source sender does not match the selected contact")
    if not product.active:
        raise ValueError("inactive product cannot be selected")
    if not re.fullmatch(r"[A-Z]{3}", normalized_currency):
        raise ValueError("currency must be a three-letter code")
    if email_row.case_id is not None or handoff.case_id is not None:
        raise ValueError("handoff is already associated with a case")

    sales_case = SalesCase(
        customer_id=contact.customer_id,
        contact_id=contact.id,
        product_id=product.id,
        currency=normalized_currency,
        stage=CaseStage.QUOTING,
        status=CaseStatus.WAITING_HUMAN,
        subject_key=normalized_subject(email_row.subject)[:255],
    )
    session.add(sales_case)
    await session.flush()
    email_row.case_id = sales_case.id
    email_row.customer_id = sales_case.customer_id
    email_row.contact_id = sales_case.contact_id
    handoff.case_id = sales_case.id
    await audit(
        session,
        "handoff.case_created",
        case_id=sales_case.id,
        actor=actor,
        data={
            "handoff_id": handoff.id,
            "email_id": email_row.id,
            "contact_id": contact.id,
            "product_id": product.id,
            "currency": normalized_currency,
        },
    )
    await session.commit()
    return sales_case


async def queue_human_reply(
    session: AsyncSession,
    *,
    handoff_id: int,
    subject: str,
    body_text: str,
    actor: str,
    note: str = "",
    resume_automation: bool = False,
) -> Outbox:
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None:
        raise ValueError("handoff not found")
    existing = await session.scalar(
        select(Outbox).where(
            or_(
                Outbox.approval_handoff_id == handoff.id,
                Outbox.business_key == f"handoff-reply:{handoff.id}",
            )
        )
    )
    if existing is not None:
        return existing
    if handoff.status != "OPEN":
        raise ValueError("handoff is already resolved")
    if handoff.case_id is None or handoff.source_email_id is None:
        raise ValueError("associate the handoff with a case before replying")
    source_email = await session.get(EmailMessage, handoff.source_email_id)
    case = await session.scalar(
        select(SalesCase)
        .options(
            selectinload(SalesCase.customer),
            selectinload(SalesCase.contact),
            selectinload(SalesCase.product),
        )
        .where(SalesCase.id == handoff.case_id)
    )
    if source_email is None or case is None:
        raise ValueError("source email or associated case not found")
    if source_email.direction != "INBOUND":
        raise ValueError("human reply requires an inbound source email")
    if source_email.from_address.casefold() != case.contact.email.casefold():
        raise ValueError("source sender does not match the associated case contact")
    if case.status in {CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST}:
        raise ValueError("closed case cannot send a reviewed reply")
    if case.customer.do_not_contact or case.contact.suppressed:
        raise ValueError("recipient is suppressed or marked do-not-contact")
    address_status = await session.get(EmailAddressStatus, case.contact.email.casefold())
    if address_status is not None and address_status.suppressed:
        raise ValueError("recipient address is permanently suppressed")

    clean_subject = subject.strip()
    clean_body = body_text.strip()
    if not clean_subject or "\r" in clean_subject or "\n" in clean_subject:
        raise ValueError("subject must be a single non-empty line")
    if not clean_body:
        raise ValueError("reply body cannot be empty")
    bundle = load_content(get_settings().content_dir)
    signed_text = "\n".join([clean_body, "", bundle.signature_text.strip()])
    html_lines = [
        f"<p>{html.escape(line) if line else '&nbsp;'}</p>"
        for line in clean_body.splitlines()
    ]
    signed_html = "".join(html_lines) + bundle.signature_html
    source = _reply_source(source_email)
    signed_text, signed_html = append_quoted_reply(
        signed_text,
        signed_html,
        from_address=source_email.from_address,
        source_body=source.body_text,
        source_html=source.body_html,
        occurred_at=source_email.received_at,
    )
    references = _reply_references(source_email)
    business_key = f"handoff-reply:{handoff.id}"
    message_id, raw = build_message(
        from_address=get_settings().mail_from,
        recipient=case.contact.email,
        subject=clean_subject,
        text_body=signed_text,
        html_body=signed_html,
        stable_key=business_key,
        in_reply_to=source_email.message_id,
        references=references,
        inline_images=source.inline_images,
    )
    parsed_outbound = parse_mime(raw.encode("utf-8"))
    now = datetime.now(UTC)
    outbox = Outbox(
        case_id=case.id,
        message_kind="HUMAN_REPLY",
        business_key=business_key,
        message_id=message_id,
        recipient=case.contact.email,
        raw_message=raw,
        approval_handoff_id=handoff.id,
        human_approved_by=actor[:128],
        human_approved_at=now,
    )
    session.add(outbox)
    await session.flush()
    session.add(
        EmailMessage(
            case_id=case.id,
            customer_id=case.customer_id,
            contact_id=case.contact_id,
            direction="OUTBOUND",
            message_id=message_id,
            in_reply_to=source_email.message_id,
            references_json=references,
            from_address=parseaddr(get_settings().mail_from)[1],
            to_addresses=[case.contact.email],
            subject=clean_subject,
            body_text=signed_text,
            body_html=signed_html,
            attachment_metadata=[],
            raw_sha256=parsed_outbound.raw_sha256,
        )
    )
    handoff.status = "RESOLVED"
    handoff.resolution_note = note.strip() or f"Reply approved by {actor}"
    case.status = CaseStatus.ACTIVE if resume_automation else CaseStatus.HUMAN_TAKEOVER
    await audit(
        session,
        "handoff.reply_approved",
        case_id=case.id,
        actor=actor,
        data={
            "handoff_id": handoff.id,
            "outbox_id": outbox.id,
            "message_id": message_id,
            "resume_automation": resume_automation,
        },
    )
    await session.commit()
    return outbox


def _reply_references(source_email: EmailMessage) -> list[str]:
    """Build a complete, ordered RFC reply chain for a response."""
    return list(
        dict.fromkeys(
            item
            for item in [
                *source_email.references_json,
                source_email.in_reply_to,
                source_email.message_id,
            ]
            if item
        )
    )


MAX_REPLY_SOURCE_ARCHIVE_BYTES = 30 * 1024 * 1024


def _reply_source(source_email: EmailMessage) -> FullReplySource:
    """Load the complete direct-parent display body and its inline resources."""
    archive_folder = "mail_archive" if source_email.is_history else "inbound_archive"
    archive_path = (
        get_settings().runtime_dir
        / archive_folder
        / f"{source_email.raw_sha256}.eml"
    )
    try:
        archive_size = archive_path.stat().st_size
        raw = archive_path.read_bytes()
    except OSError as exc:
        if html_requires_mime_resources(source_email.body_html):
            raise RuntimeError(
                f"complete reply source with inline images is unavailable for email_id={source_email.id}"
            ) from exc
        logger.warning(
            "Complete reply archive unavailable for email_id=%s; using stored body without MIME resources",
            source_email.id,
        )
        return FullReplySource(
            body_text=source_email.body_text,
            body_html=source_email.body_html,
        )
    if archive_size > MAX_REPLY_SOURCE_ARCHIVE_BYTES:
        raise RuntimeError(
            f"complete reply source exceeds {MAX_REPLY_SOURCE_ARCHIVE_BYTES} bytes"
        )
    try:
        return extract_full_reply_source(raw)
    except (ValueError, LookupError, RecursionError) as exc:
        raise RuntimeError(
            f"complete reply source could not preserve inline content for email_id={source_email.id}"
        ) from exc


async def active_policy(session: AsyncSession, product_id: int, currency: str) -> PricePolicy | None:
    settings = get_settings()
    today = datetime.now(UTC).astimezone(ZoneInfo(settings.business_timezone)).date()
    return await session.scalar(
        select(PricePolicy)
        .where(
            PricePolicy.product_id == product_id,
            PricePolicy.currency == currency,
            PricePolicy.active.is_(True),
            PricePolicy.valid_from <= today,
            (PricePolicy.valid_to.is_(None) | (PricePolicy.valid_to >= today)),
        )
        .order_by(PricePolicy.valid_from.desc())
    )


async def seed_demo_data(session: AsyncSession) -> dict[str, int]:
    if not get_settings().demo_mode:
        raise RuntimeError("demo mode is disabled")
    product = await session.scalar(select(Product).where(Product.code == "WIDGET-100"))
    if product is None:
        product = Product(
            code="WIDGET-100",
            name="Industrial Widget 100",
            unit="piece",
            approved_text_key="widget_100",
        )
        session.add(product)
        await session.flush()
    policy = await active_policy(session, product.id, "USD")
    if policy is None:
        policy = PricePolicy(
            product_id=product.id,
            currency="USD",
            standard_price=Decimal("100.0000"),
            absolute_floor=Decimal("82.0000"),
            max_discount_pct=Decimal("0.1500"),
            max_negotiation_rounds=2,
            concession_step_pct=Decimal("0.0300"),
            min_quantity=10,
            max_quantity=10000,
            quote_valid_days=30,
            standard_incoterm="EXW",
            allowed_incoterms=["EXW", "FCA", "FOB"],
            standard_payment_term="100% before shipment",
            allowed_payment_terms=[
                "100% before shipment",
                "30% deposit / 70% before shipment",
            ],
            valid_from=date.today(),
            source_hash="demo-seed-v1",
        )
        session.add(policy)
    customer = await session.scalar(select(Customer).where(Customer.company_name == "Demo Industrial Ltd"))
    if customer is None:
        customer = Customer(
            company_name="Demo Industrial Ltd",
            language="en",
            auto_send_allowed=True,
            consent_basis="demo fixture",
        )
        session.add(customer)
        await session.flush()
    contact = await session.scalar(select(Contact).where(Contact.customer_id == customer.id, Contact.email == "internal@example.com"))
    if contact is None:
        contact = Contact(
            customer_id=customer.id,
            name="Alex Buyer",
            email="internal@example.com",
            language="en",
        )
        session.add(contact)
        await session.flush()
    await session.commit()
    return {"product_id": product.id, "customer_id": customer.id, "contact_id": contact.id}


def render_quote(
    *,
    plan: Any,
    bundle: ContentBundle,
    product_key: str,
    product_name: str,
    price: Decimal,
    currency: str,
    quantity: int,
    unit: str,
    incoterm: str,
    payment_term: str,
    valid_until: date,
    taxes_included: bool = False,
    freight_included: bool = False,
    availability: str = "Ready stock",
) -> tuple[str, str]:
    snippet = bundle.product_snippets[product_key]
    # Free-form model prose is deliberately not inserted into a commercial email.
    # The structured plan selects tone/snippet IDs; factual language remains local and reviewed.
    safe_greeting = plan.greeting.lower().startswith("dear ") and not any(ch.isdigit() for ch in plan.greeting)
    greeting = plan.greeting if safe_greeting else "Dear Customer,"
    opening = "Thank you for your inquiry."
    price_lead_in = "Please find our standard quotation details below."
    closing = "Please let us know if you have questions about this non-binding standard quotation."
    body_lines = [
        greeting,
        "",
        opening,
        snippet,
        "",
        price_lead_in,
        f"Product: {product_name}",
        f"Quantity: {quantity} {unit}",
        f"Unit price: {currency} {price:.4f} per {unit}",
        f"Availability: {availability}",
        f"Price basis: {incoterm} (ex-warehouse)",
        f"Taxes: {'included' if taxes_included else 'excluded'}",
        f"Freight: {'included' if freight_included else 'excluded'}",
        f"Payment term: {payment_term}",
        f"Quote valid until: {valid_until.isoformat()} ({valid_until.strftime('%A')})",
        "",
        closing,
    ]
    business_text = "\n".join(body_lines)
    validate_rendered_email(business_text, exact_price=price, currency=currency, approved_fragments=[snippet])
    text = "\n".join([business_text, "", bundle.signature_text.strip()])
    html_body = (
        "<p>"
        + "</p><p>".join(html.escape(line) if line else "&nbsp;" for line in body_lines)
        + "</p>"
        + bundle.signature_html
    )
    return text, html_body


async def freeze_outbox(
    session: AsyncSession,
    *,
    case: SalesCase,
    quote: Quote,
    subject: str,
    text_body: str,
    html_body: str,
    business_key: str,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    inline_images: tuple[InlineImageAsset, ...] = (),
) -> Outbox | None:
    message_id, raw = build_message(
        from_address=get_settings().mail_from,
        recipient=case.contact.email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        stable_key=business_key,
        in_reply_to=in_reply_to,
        references=references,
        inline_images=inline_images,
    )
    parsed_outbound = parse_mime(raw.encode("utf-8"))
    try:
        async with session.begin_nested():
            row = Outbox(
                case_id=case.id,
                quote_id=quote.id,
                message_kind="AUTO_QUOTE",
                business_key=business_key,
                message_id=message_id,
                recipient=case.contact.email,
                raw_message=raw,
            )
            session.add(row)
            await session.flush()
            session.add(
                EmailMessage(
                    case_id=case.id,
                    customer_id=case.customer_id,
                    contact_id=case.contact_id,
                    direction="OUTBOUND",
                    message_id=message_id,
                    in_reply_to=in_reply_to,
                    references_json=references or [],
                    from_address=parseaddr(get_settings().mail_from)[1],
                    to_addresses=[case.contact.email],
                    subject=subject,
                    body_text=text_body,
                    body_html=html_body,
                    attachment_metadata=[],
                    raw_sha256=parsed_outbound.raw_sha256,
                )
            )
        await audit(
            session,
            "outbox.frozen",
            case_id=case.id,
            actor="system",
            data={"outbox_id": row.id, "message_id": message_id, "quote_id": quote.id},
        )
        await session.commit()
        return row
    except IntegrityError:
        await session.rollback()
        return None


async def create_demo_outreach(session: AsyncSession, payload: dict[str, Any]) -> None:
    ids = await seed_demo_data(session)
    customer = await session.get(Customer, ids["customer_id"])
    seed_contact = await session.get(Contact, ids["contact_id"])
    product = await session.get(Product, ids["product_id"])
    assert customer and seed_contact and product
    recipient = str(payload.get("recipient") or seed_contact.email).lower()
    contact = await session.scalar(select(Contact).where(Contact.customer_id == customer.id, Contact.email == recipient))
    if contact is None:
        contact = Contact(
            customer_id=customer.id,
            name="Demo Recipient",
            email=recipient,
            language=customer.language,
        )
        session.add(contact)
        await session.flush()
    quantity = int(payload.get("quantity") or 100)
    business_key = f"demo-outreach:{recipient}:{quantity}"
    if await session.scalar(select(Outbox.id).where(Outbox.business_key == business_key)) is not None:
        return
    policy_row = await active_policy(session, product.id, "USD")
    if policy_row is None:
        raise RuntimeError("no active demo policy")
    decision = initial_quote(_pricing_policy(policy_row), quantity)
    if not decision.approved or decision.unit_price is None:
        raise RuntimeError(decision.reason or "initial quote rejected")
    case = SalesCase(
        customer_id=customer.id,
        contact_id=contact.id,
        product_id=product.id,
        stage=CaseStage.QUOTING,
        status=CaseStatus.ACTIVE,
        subject_key="industrial widget 100 quotation",
    )
    session.add(case)
    await session.flush()
    valid_until = quote_valid_until(
        quote_valid_days=policy_row.quote_valid_days,
        quote_valid_weekday=policy_row.quote_valid_weekday,
    )
    quote = Quote(
        case_id=case.id,
        price_policy_id=policy_row.id,
        round_number=0,
        unit_price=decision.unit_price,
        currency=policy_row.currency,
        quantity=quantity,
        incoterm=policy_row.standard_incoterm,
        payment_term=policy_row.standard_payment_term,
        valid_until=valid_until,
        pricing_snapshot={
            "standard_price": str(policy_row.standard_price),
            "absolute_floor": str(policy_row.absolute_floor),
            "hard_minimum": str(decision.hard_minimum),
            "max_discount_pct": str(policy_row.max_discount_pct),
            "applied_markup_pct": str(decision.applied_markup_pct),
            "pricing_tier": decision.reason,
        },
    )
    session.add(quote)
    await session.flush()
    bundle = load_content(get_settings().content_dir)
    ai = AIClient()
    plan = await ai.draft_plan(
        {
            "subject": "Industrial Widget 100 quotation",
            "contact_name": contact.name,
            "approved_product_key": product.approved_text_key,
        }
    )
    text, html_body = render_quote(
        plan=plan,
        bundle=bundle,
        product_key=product.approved_text_key,
        product_name=product.name,
        price=decision.unit_price,
        currency=policy_row.currency,
        quantity=quantity,
        unit=product.unit,
        incoterm=policy_row.standard_incoterm,
        payment_term=policy_row.standard_payment_term,
        valid_until=valid_until,
        taxes_included=policy_row.taxes_included,
        freight_included=policy_row.freight_included,
    )
    await freeze_outbox(
        session,
        case=case,
        quote=quote,
        subject="Industrial Widget 100 quotation",
        text_body=text,
        html_body=html_body,
        business_key=business_key,
    )


async def create_case_outreach(session: AsyncSession, payload: dict[str, Any]) -> None:
    case_id = int(payload["case_id"])
    quantity = int(payload.get("quantity") or 1)
    reprice = bool(payload.get("reprice"))
    settings = get_settings()
    case = await session.scalar(
        select(SalesCase)
        .options(
            selectinload(SalesCase.customer),
            selectinload(SalesCase.contact),
            selectinload(SalesCase.product),
        )
        .where(SalesCase.id == case_id)
    )
    if case is None:
        raise RuntimeError(f"case {case_id} not found")
    historical_outbound = await session.scalar(
        select(EmailMessage)
        .where(
            or_(
                EmailMessage.case_id == case.id,
                EmailMessage.contact_id == case.contact_id,
            ),
            EmailMessage.direction == "OUTBOUND",
            EmailMessage.is_history.is_(True),
        )
        .order_by(EmailMessage.received_at.desc(), EmailMessage.id.desc())
        .limit(1)
    )
    if historical_outbound is not None:
        summary = "Historical Gmail outreach exists; initial outreach is blocked"
        existing_review = await session.scalar(
            select(Handoff.id).where(
                Handoff.case_id == case.id,
                Handoff.reason_code == HandoffReason.HUMAN_CONTROL.value,
                Handoff.summary == summary,
                Handoff.status == "OPEN",
            )
        )
        if existing_review is None:
            await create_handoff(
                session,
                case=case,
                reason=HandoffReason.HUMAN_CONTROL,
                summary=summary,
                facts={
                    "history_import": True,
                    "latest_outbound_email_id": historical_outbound.id,
                    "latest_outbound_at": historical_outbound.received_at.isoformat(),
                },
            )
        return
    if case.status != CaseStatus.ACTIVE:
        raise RuntimeError(f"case {case_id} is not active")
    if case.customer.do_not_contact or case.contact.suppressed or not case.customer.auto_send_allowed:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.SUPPRESSED,
            summary="Initial outreach blocked by customer/contact send eligibility",
        )
        return
    commercial_context = await _commercial_quote_context(
        session,
        product_id=case.product_id,
        currency=case.currency,
        settings=settings,
        requested_quantity=quantity,
    )
    if commercial_context is not None and commercial_context.status is QuoteContextStatus.UNAVAILABLE:
        unavailable_reason = (
            HandoffReason.INVENTORY_UNAVAILABLE
            if commercial_context.reason.startswith("INVENTORY")
            else HandoffReason.NONSTANDARD
        )
        await create_handoff(
            session,
            case=case,
            reason=unavailable_reason,
            summary=f"Current commercial data cannot quote {case.product.code}: {commercial_context.reason}",
            facts={"commercial_cycle_id": commercial_context.cycle.id},
        )
        return
    cycle_id = commercial_context.cycle.id if commercial_context is not None else None
    business_key = (
        f"initial-quote:case:{case.id}:cycle:{cycle_id}"
        if cycle_id is not None
        else f"initial-quote:case:{case.id}"
    )
    if await session.scalar(select(Outbox.id).where(Outbox.business_key == business_key)) is not None:
        return
    existing_quote = await session.scalar(
        select(Quote).where(Quote.case_id == case.id).order_by(Quote.round_number.desc()).limit(1)
    )
    if existing_quote is not None and not reprice:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.NONSTANDARD,
            summary="Case already has a quotation but no matching initial-outreach outbox record",
        )
        return
    policy_row = (
        commercial_context.policy
        if commercial_context is not None
        else await active_policy(session, case.product_id, case.currency)
    )
    if policy_row is None:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.NONSTANDARD,
            summary=f"No active {case.currency} price policy is available for {case.product.code}",
        )
        return
    decision = initial_quote(_pricing_policy(policy_row), quantity)
    if not decision.approved or decision.unit_price is None:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.NONSTANDARD,
            summary=f"Initial quotation rejected by pricing policy: {decision.reason}",
            facts={"quantity": quantity, "hard_minimum": str(decision.hard_minimum)},
        )
        return
    valid_until = quote_valid_until(
        quote_valid_days=policy_row.quote_valid_days,
        quote_valid_weekday=policy_row.quote_valid_weekday,
        today=datetime.now(UTC).astimezone(ZoneInfo(settings.business_timezone)).date(),
    )
    bundle = load_content(get_settings().content_dir)
    if not str(bundle.product_snippets.get(case.product.approved_text_key) or "").strip():
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.NONSTANDARD,
            summary=f"Approved product text is missing for key {case.product.approved_text_key}",
        )
        return
    try:
        plan = await AIClient().draft_plan(
            {
                "subject": f"{case.product.name} quotation",
                "contact_name": case.contact.name,
                "approved_product_key": case.product.approved_text_key,
            }
        )
        text, html_body = render_quote(
            plan=plan,
            bundle=bundle,
            product_key=case.product.approved_text_key,
            product_name=case.product.name,
            price=decision.unit_price,
            currency=policy_row.currency,
            quantity=quantity,
            unit=case.product.unit,
            incoterm=policy_row.standard_incoterm,
            payment_term=policy_row.standard_payment_term,
            valid_until=valid_until,
            taxes_included=policy_row.taxes_included,
            freight_included=policy_row.freight_included,
        )
    except Exception as exc:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.AI_FAILURE,
            summary=f"Initial outreach drafting failed: {type(exc).__name__}",
        )
        return
    round_number = existing_quote.round_number + 1 if existing_quote is not None else 0
    quote = Quote(
        case_id=case.id,
        price_policy_id=policy_row.id,
        commercial_cycle_id=cycle_id,
        round_number=round_number,
        unit_price=decision.unit_price,
        currency=policy_row.currency,
        quantity=quantity,
        incoterm=policy_row.standard_incoterm,
        payment_term=policy_row.standard_payment_term,
        valid_until=valid_until,
        pricing_snapshot={
            "standard_price": str(policy_row.standard_price),
            "absolute_floor": str(policy_row.absolute_floor),
            "hard_minimum": str(decision.hard_minimum),
            "max_discount_pct": str(policy_row.max_discount_pct),
            "applied_markup_pct": str(decision.applied_markup_pct),
            "pricing_tier": decision.reason,
        },
    )
    session.add(quote)
    case.negotiation_round = round_number
    await session.flush()
    subject = f"{case.product.name} quotation"
    case.subject_key = subject.lower()
    await freeze_outbox(
        session,
        case=case,
        quote=quote,
        subject=subject,
        text_body=text,
        html_body=html_body,
        business_key=business_key,
    )


async def _ensure_inbound_follow_up(
    session: AsyncSession,
    row: EmailMessage,
    *,
    ambiguous: bool = False,
    review_reason: HandoffReason | None = None,
    review_summary: str | None = None,
    review_facts: dict[str, Any] | None = None,
) -> None:
    if row.is_bounce:
        await enqueue_job(
            session,
            "process_inbound",
            {"email_id": row.id},
            f"process-inbound:{row.id}",
        )
        return
    if row.case_id is not None:
        await enqueue_job(
            session,
            "process_inbound",
            {"email_id": row.id},
            f"process-inbound:{row.id}",
        )
        return
    summary_prefix = "Ambiguous thread" if ambiguous else "No case matched inbound email"
    await create_handoff(
        session,
        case=None,
        reason=review_reason or HandoffReason.THREAD_AMBIGUOUS,
        summary=review_summary or f"{summary_prefix} from {row.from_address}: {row.subject}",
        facts=review_facts,
        source_email_id=row.id,
    )


def _explicit_product_codes(text: str) -> list[str]:
    codes = find_product_codes(text)
    explicit = re.findall(
        r"\b(?:SKU|PRODUCT)\s*[:#-]?\s*([A-Z0-9][A-Z0-9_()%.\-]{1,63})",
        text,
        flags=re.IGNORECASE,
    )
    return list(dict.fromkeys([*codes, *(canonical_product_code(value) for value in explicit)]))


def _prior_thread_marker(text: str) -> str | None:
    lowered = text.casefold()
    return next((marker for marker in PRIOR_THREAD_MARKERS if marker in lowered), None)


async def _resolve_new_inquiry_case(
    session: AsyncSession,
    parsed: ParsedEmail,
    *,
    trusted_reactivation_parent: CaseLessReactivationParent | None = None,
) -> NewInquiryResolution:
    sender = parsed.from_address.strip().lower()
    facts: dict[str, Any] = {
        "new_thread": trusted_reactivation_parent is None,
        "sender": sender,
        "subject": parsed.subject,
    }
    if trusted_reactivation_parent is not None:
        facts.update(
            {
                "reactivation_outbox_id": trusted_reactivation_parent.outbox.id,
                "reactivation_recipient_id": trusted_reactivation_parent.recipient.id,
                "match_basis": "exact_case_less_reactivation_thread",
            }
        )
    if not sender:
        return NewInquiryResolution(
            None,
            HandoffReason.NEW_INQUIRY_REVIEW,
            "New inbound thread has no reliable sender address",
            facts,
        )

    combined_text = f"{parsed.subject}\n{parsed.body_text}"
    marker = _prior_thread_marker(combined_text)
    if marker:
        return NewInquiryResolution(
            None,
            HandoffReason.THREAD_AMBIGUOUS,
            "New email thread refers to prior commercial history and requires manual linking",
            {**facts, "prior_context_marker": marker},
        )

    contacts = (
        (
            await session.execute(
                select(Contact)
                .options(selectinload(Contact.customer))
                .where(func.lower(Contact.email) == sender)
            )
        )
        .scalars()
        .all()
    )
    if len(contacts) != 1:
        return NewInquiryResolution(
            None,
            HandoffReason.NEW_INQUIRY_REVIEW,
            (
                "New inbound thread sender is not a known contact"
                if not contacts
                else "New inbound thread sender matches multiple customer records"
            ),
            {**facts, "matching_contact_count": len(contacts)},
        )
    contact = contacts[0]

    product_codes = _explicit_product_codes(combined_text)
    facts.update(
        {
            "contact_id": contact.id,
            "customer_id": contact.customer_id,
            "product_codes": product_codes,
        }
    )
    if len(product_codes) != 1:
        return NewInquiryResolution(
            None,
            HandoffReason.NEW_INQUIRY_REVIEW,
            (
                "New inbound thread does not identify a supported product"
                if not product_codes
                else "New inbound thread mentions multiple products"
            ),
            facts,
        )

    product = await session.scalar(
        select(Product).where(Product.code == product_codes[0], Product.active.is_(True))
    )
    if product is None:
        return NewInquiryResolution(
            None,
            HandoffReason.NEW_INQUIRY_REVIEW,
            "New inbound thread names a product that is not active in the catalog",
            facts,
        )

    today = date.today()
    policy_rows = await session.execute(
        select(PricePolicy.currency).where(
            PricePolicy.product_id == product.id,
            PricePolicy.active.is_(True),
            PricePolicy.valid_from <= today,
            (PricePolicy.valid_to.is_(None) | (PricePolicy.valid_to >= today)),
        )
    )
    policy_currencies = set(policy_rows.scalars().all())
    customer_currency_rows = await session.execute(
        select(SalesCase.currency).where(
            SalesCase.customer_id == contact.customer_id,
            SalesCase.status.not_in([CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST]),
        )
    )
    customer_currencies = set(customer_currency_rows.scalars().all())
    if len(policy_currencies) == 1:
        currency = next(iter(policy_currencies))
    elif len(policy_currencies & customer_currencies) == 1:
        currency = next(iter(policy_currencies & customer_currencies))
    elif not policy_currencies and len(customer_currencies) == 1:
        # Manual-only products can still be represented as a case and routed to
        # a human using the customer's established market currency.
        currency = next(iter(customer_currencies))
    else:
        return NewInquiryResolution(
            None,
            HandoffReason.NEW_INQUIRY_REVIEW,
            "New inbound thread currency cannot be selected unambiguously",
            {
                **facts,
                "policy_currencies": sorted(policy_currencies),
                "customer_currencies": sorted(customer_currencies),
            },
        )

    related_case_ids = (
        (
            await session.execute(
                select(SalesCase.id).where(
                    SalesCase.contact_id == contact.id,
                    SalesCase.product_id == product.id,
                    SalesCase.status.not_in([CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST]),
                )
            )
        )
        .scalars()
        .all()
    )
    recent_cutoff = datetime.now(UTC) - timedelta(days=7)
    recent_related_case_ids = (
        (
            await session.execute(
                select(SalesCase.id).where(
                    SalesCase.id.in_(related_case_ids),
                    or_(
                        SalesCase.id.in_(
                            select(EmailMessage.case_id).where(
                                EmailMessage.case_id.is_not(None),
                                EmailMessage.received_at >= recent_cutoff,
                            )
                        ),
                        SalesCase.id.in_(
                            select(Quote.case_id).where(Quote.valid_until >= today)
                        ),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    incoming_subject_key = normalized_subject(parsed.subject)[:255]
    if recent_related_case_ids and has_thread_subject_prefix(parsed.subject):
        strong_matches = (
            (
                await session.execute(
                    select(SalesCase).where(
                        SalesCase.id.in_(recent_related_case_ids),
                        SalesCase.currency == currency,
                        SalesCase.subject_key == incoming_subject_key,
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(strong_matches) == 1:
            return NewInquiryResolution(
                strong_matches[0],
                facts={
                    **facts,
                    "product_id": product.id,
                    "currency": currency,
                    "possible_related_case_ids": related_case_ids,
                    "recent_related_case_ids": recent_related_case_ids,
                    "recovered_thread": True,
                    "match_basis": "unique_recent_contact_product_currency_subject",
                },
            )
    if recent_related_case_ids:
        return NewInquiryResolution(
            None,
            HandoffReason.THREAD_AMBIGUOUS,
            "New email thread may belong to a recent active case and requires manual linking",
            {
                **facts,
                "product_id": product.id,
                "currency": currency,
                "possible_related_case_ids": related_case_ids,
                "recent_related_case_ids": recent_related_case_ids,
                "recent_activity_cutoff": recent_cutoff.isoformat(),
            },
        )
    sales_case = SalesCase(
        customer_id=contact.customer_id,
        contact_id=contact.id,
        product_id=product.id,
        currency=currency,
        stage=CaseStage.QUOTING,
        status=CaseStatus.ACTIVE,
        subject_key=incoming_subject_key,
    )
    session.add(sales_case)
    await session.flush()
    return NewInquiryResolution(
        sales_case,
        facts={
            **facts,
            "product_id": product.id,
            "currency": currency,
            "possible_related_case_ids": related_case_ids,
        },
    )


async def _case_less_reactivation_parent(
    session: AsyncSession,
    parsed: ParsedEmail,
) -> CaseLessReactivationParent | None:
    """Find a verified case-less reactivation message referenced by this reply."""

    sender = parsed.from_address.strip().casefold()
    ordered_ids = list(dict.fromkeys(item for item in parsed.references if item))
    if parsed.in_reply_to:
        ordered_ids = [parsed.in_reply_to]
    else:
        ordered_ids.reverse()
    if not sender or not ordered_ids:
        return None
    occurred_at = parsed.occurred_at or datetime.now(UTC)
    rows = (
        await session.execute(
            select(Outbox, ReactivationRecipient, Contact)
                .join(ReactivationRecipient, ReactivationRecipient.outbox_id == Outbox.id)
                .join(Contact, Contact.id == ReactivationRecipient.contact_id)
                .where(
                    Outbox.message_id.in_(ordered_ids),
                    Outbox.case_id.is_(None),
                    Outbox.message_kind == "REACTIVATION",
                    Outbox.status == DeliveryStatus.SENT,
                    Outbox.sent_at.is_not(None),
                    Outbox.sent_at <= occurred_at,
                    ReactivationRecipient.status.in_(["QUEUED", "SENT"]),
                    ReactivationRecipient.customer_id == Contact.customer_id,
                )
                .with_for_update()
        )
    ).all()
    matches = [
        CaseLessReactivationParent(outbox=outbox, recipient=recipient)
        for outbox, recipient, contact in rows
        if outbox.recipient.strip().casefold() == sender
        and contact.email.strip().casefold() == sender
    ]
    if len(matches) == 1:
        return matches[0]
    return None


async def ingest_raw_email(
    session: AsyncSession,
    raw: bytes,
    *,
    mailbox: str = "demo",
    mailbox_folder: str | None = None,
    uid_validity: int | None = None,
    imap_uid: int | None = None,
    direction: str = "INBOUND",
    is_history: bool = False,
) -> EmailMessage | None:
    direction = direction.upper()
    if direction not in {"INBOUND", "OUTBOUND"}:
        raise ValueError(f"unsupported email direction: {direction}")
    parsed = parse_mime(raw)
    bounce = classify_bounce(
        raw,
        subject=parsed.subject,
        body=parsed.body_text,
        sender=parsed.from_address,
    ) if direction == "INBOUND" else None
    automated_reply = (
        classify_automated_reply(
            subject=parsed.subject,
            body=parsed.body_text,
            headers=parsed.header_metadata,
            sender=parsed.from_address,
        )
        if direction == "INBOUND" and not (bounce and bounce.is_bounce)
        else None
    )
    duplicate_query = select(EmailMessage).where(
        (EmailMessage.raw_sha256 == parsed.raw_sha256)
        | ((EmailMessage.message_id == parsed.message_id) & EmailMessage.message_id.is_not(None))
    )
    duplicate = await session.scalar(duplicate_query)
    if duplicate:
        if direction == "INBOUND" and duplicate.direction == "INBOUND" and not is_history:
            await _ensure_inbound_follow_up(session, duplicate)
        return duplicate
    live_human_inbound = bool(
        direction == "INBOUND"
        and not is_history
        and not (bounce and bounce.is_bounce)
        and not (automated_reply and automated_reply.is_automated)
    )
    case, ambiguous = await match_case(
        session,
        parsed,
        direction=direction,
        # A live human-authored message may inherit commercial history only
        # through Message-ID/References. Subject-only matching is retained for
        # history reconciliation, outbound mail, and non-sending auto replies.
        allow_subject_fallback=not live_human_inbound,
    )
    new_inquiry = NewInquiryResolution(case)
    reactivation_parent: CaseLessReactivationParent | None = None
    if live_human_inbound and case is None and not ambiguous:
        has_thread_headers = bool(parsed.in_reply_to or parsed.references)
        if has_thread_headers:
            reactivation_parent = await _case_less_reactivation_parent(session, parsed)
            if reactivation_parent is not None:
                new_inquiry = await _resolve_new_inquiry_case(
                    session,
                    parsed,
                    trusted_reactivation_parent=reactivation_parent,
                )
                case = new_inquiry.case
            else:
                # A concurrent first reply can promote a case-less reactivation
                # while this transaction waits for the parent row lock. Re-run
                # the authoritative header match once before escalating.
                case, ambiguous = await match_case(
                    session,
                    parsed,
                    direction=direction,
                    allow_subject_fallback=False,
                )
                if case is not None:
                    new_inquiry = NewInquiryResolution(case)
                else:
                    new_inquiry = NewInquiryResolution(
                        None,
                        HandoffReason.THREAD_AMBIGUOUS,
                        "Inbound reply contains thread references that do not match a known case",
                        {
                            "new_thread": False,
                            "sender": parsed.from_address,
                            "subject": parsed.subject,
                            "in_reply_to": parsed.in_reply_to,
                            "references": parsed.references,
                        },
                    )
        else:
            new_inquiry = await _resolve_new_inquiry_case(session, parsed)
            case = new_inquiry.case
    if reactivation_parent is not None and case is not None:
        reactivation_parent.outbox.case_id = case.id
        reactivation_parent.recipient.case_id = case.id
        await session.execute(
            update(EmailMessage)
            .where(
                EmailMessage.message_id == reactivation_parent.outbox.message_id,
                EmailMessage.direction == "OUTBOUND",
                EmailMessage.case_id.is_(None),
            )
            .values(case_id=case.id)
        )
        session.add(
            AuditEvent(
                case_id=case.id,
                actor="thread_resolver",
                event_type="reactivation.thread_promoted",
                data={
                    "outbox_id": reactivation_parent.outbox.id,
                    "recipient_id": reactivation_parent.recipient.id,
                },
            )
        )
    matched_outbox = None
    if bounce and bounce.is_bounce and bounce.original_message_id:
        matched_outbox = await session.scalar(
            select(Outbox).where(
                Outbox.message_id == bounce.original_message_id,
                Outbox.status == DeliveryStatus.SENT,
            )
        )
        if case is None and matched_outbox and matched_outbox.case_id:
            case = await session.get(SalesCase, matched_outbox.case_id)
            ambiguous = False
    bounce_metadata = bounce.metadata() if bounce and bounce.is_bounce else {}
    if matched_outbox is not None:
        bounce_metadata["matched_outbox_id"] = matched_outbox.id
    identity_contact = None
    if case is None:
        identity_addresses = (
            [parsed.from_address] if direction == "INBOUND" else parsed.to_addresses
        )
        identity_contact = await resolve_unique_contact(session, identity_addresses)
    identity_customer_id = (
        case.customer_id
        if case is not None
        else identity_contact.customer_id if identity_contact is not None else None
    )
    identity_contact_id = (
        case.contact_id
        if case is not None
        else identity_contact.id if identity_contact is not None else None
    )
    try:
        async with session.begin_nested():
            row = EmailMessage(
                case_id=case.id if case else None,
                customer_id=identity_customer_id,
                contact_id=identity_contact_id,
                direction=direction,
                mailbox=mailbox,
                mailbox_folder=mailbox_folder,
                uid_validity=uid_validity,
                imap_uid=imap_uid,
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
                is_history=is_history,
                is_automated_reply=bool(automated_reply and automated_reply.is_automated),
                automated_reply_type=(
                    automated_reply.reply_type.value
                    if automated_reply and automated_reply.reply_type is not None
                    else None
                ),
                automated_reply_metadata=(
                    {**automated_reply.metadata(), "headers": parsed.header_metadata}
                    if automated_reply and automated_reply.is_automated
                    else {}
                ),
                is_bounce=bool(bounce and bounce.is_bounce),
                bounce_type=(
                    bounce.bounce_type.value
                    if bounce and bounce.bounce_type is not None
                    else None
                ),
                bounce_metadata=bounce_metadata,
                received_at=parsed.occurred_at or datetime.now(UTC),
            )
            session.add(row)
            await session.flush()
    except IntegrityError:
        duplicate = await session.scalar(duplicate_query)
        if duplicate is None:
            raise
        if direction == "INBOUND" and duplicate.direction == "INBOUND" and not is_history:
            await _ensure_inbound_follow_up(session, duplicate)
        return duplicate

    archive_dir = "mail_archive" if is_history or direction == "OUTBOUND" else "inbound_archive"
    archive = get_settings().runtime_dir / archive_dir / f"{parsed.raw_sha256}.eml"
    archive.write_bytes(raw)
    await audit(
        session,
        "email.history_ingested" if is_history else "email.ingested",
        case_id=case.id if case else None,
        actor="gmail_history" if is_history else ("imap" if mailbox != "demo" else "demo"),
        data={
            "email_id": row.id,
            "message_id": parsed.message_id,
            "direction": direction,
            "mailbox": mailbox,
            "mailbox_folder": mailbox_folder,
            "automated_reply_type": row.automated_reply_type,
            "bounce_type": row.bounce_type,
        },
    )
    if new_inquiry.case is not None and new_inquiry.facts is not None and live_human_inbound:
        await audit(
            session,
            (
                "email.thread_recovered"
                if new_inquiry.facts.get("recovered_thread")
                else "case.created_from_new_inquiry"
            ),
            case_id=new_inquiry.case.id,
            actor="thread_resolver",
            data=new_inquiry.facts,
        )
    if direction == "INBOUND" and not is_history and reactivation_parent is not None:
        await record_reactivation_reply(
            session,
            row,
            recipient_id=reactivation_parent.recipient.id,
            commit=False,
        )
    await session.commit()
    if direction == "INBOUND" and not is_history:
        await _ensure_inbound_follow_up(
            session,
            row,
            ambiguous=ambiguous,
            review_reason=new_inquiry.reason,
            review_summary=new_inquiry.summary,
            review_facts=new_inquiry.facts,
        )
    return row


async def _match_bounce_outbox(
    session: AsyncSession,
    email_row: EmailMessage,
) -> tuple[Outbox | None, str | None]:
    metadata = email_row.bounce_metadata or {}
    recipient = str(metadata.get("recipient") or "").strip().casefold() or None
    outbox = None
    if metadata.get("matched_outbox_id"):
        outbox = await session.get(Outbox, int(metadata["matched_outbox_id"]))
    if outbox is None and metadata.get("original_message_id"):
        outbox = await session.scalar(
            select(Outbox).where(
                Outbox.message_id == str(metadata["original_message_id"]),
                Outbox.status == DeliveryStatus.SENT,
            )
        )
    if outbox is None and recipient:
        outbox = await session.scalar(
            select(Outbox)
            .where(
                func.lower(Outbox.recipient) == recipient,
                Outbox.status == DeliveryStatus.SENT,
            )
            .order_by(Outbox.sent_at.desc(), Outbox.id.desc())
        )
    if outbox is not None:
        recipient = recipient or outbox.recipient.casefold()
        if recipient != outbox.recipient.casefold():
            return None, recipient
    return outbox, recipient


async def _handle_bounce(session: AsyncSession, email_row: EmailMessage) -> None:
    if email_row.bounce_handled_at is not None:
        return
    outbox, recipient = await _match_bounce_outbox(session, email_row)
    case = await session.get(SalesCase, outbox.case_id) if outbox and outbox.case_id else None
    if case is None and email_row.case_id:
        case = await session.get(SalesCase, email_row.case_id)
    if case and email_row.case_id is None:
        email_row.case_id = case.id
    if case:
        email_row.customer_id = case.customer_id
        email_row.contact_id = case.contact_id

    metadata = dict(email_row.bounce_metadata or {})
    if outbox is not None:
        metadata["matched_outbox_id"] = outbox.id
    metadata["recipient"] = recipient
    email_row.bounce_metadata = metadata
    email_row.bounce_handled_at = datetime.now(UTC)
    diagnostic = str(metadata.get("diagnostic") or "")[:2000] or None

    if email_row.bounce_type == BounceType.HARD.value and outbox is not None and recipient:
        await _suppress_email_address(
            session,
            recipient,
            reason="HARD_BOUNCE",
            source_email_id=email_row.id,
            bounce_type=email_row.bounce_type,
            diagnostic=diagnostic,
        )
        if case and case.status == CaseStatus.ACTIVE:
            case.status = CaseStatus.PAUSED
        await audit(
            session,
            "inbound.hard_bounce_suppressed",
            case_id=case.id if case else None,
            actor="system",
            data={"email_id": email_row.id, "outbox_id": outbox.id, **metadata},
        )
        await session.commit()
        return

    if recipient:
        status = await _email_address_status(session, recipient)
        status.last_bounce_at = datetime.now(UTC)
        status.last_bounce_type = email_row.bounce_type
        status.last_bounce_diagnostic = diagnostic
    await audit(
        session,
        "inbound.bounce_review_required",
        case_id=case.id if case else None,
        actor="system",
        data={"email_id": email_row.id, "outbox_id": outbox.id if outbox else None, **metadata},
    )
    await create_handoff(
        session,
        case=case,
        reason=HandoffReason.BOUNCE_REVIEW,
        summary=(
            f"Review {email_row.bounce_type or 'unknown'} delivery failure for "
            f"{recipient or 'an unidentified recipient'}"
        ),
        facts={"email_id": email_row.id, "outbox_id": outbox.id if outbox else None, **metadata},
        source_email_id=email_row.id,
    )


async def _handle_automated_reply(
    session: AsyncSession,
    *,
    case: SalesCase,
    email_row: EmailMessage,
) -> bool:
    if not email_row.is_automated_reply:
        return False
    if email_row.automated_reply_handled_at is not None:
        return True

    reply_type = email_row.automated_reply_type
    facts = {
        "automated_reply_type": reply_type,
        **(email_row.automated_reply_metadata or {}),
    }
    email_row.automated_reply_handled_at = datetime.now(UTC)
    if reply_type in {
        AutomatedReplyType.OUT_OF_OFFICE.value,
        AutomatedReplyType.GENERIC_AUTOREPLY.value,
    }:
        await audit(
            session,
            "inbound.automated_reply_handled",
            case_id=case.id,
            actor="system",
            data={"email_id": email_row.id, **facts},
        )
        await session.commit()
        return True

    if reply_type == AutomatedReplyType.DEPARTED.value:
        case.contact.suppressed = True
        summary = "Contact appears to have left the company; verify any replacement contact"
        reason = HandoffReason.PERSONNEL_CHANGE
    elif reply_type == AutomatedReplyType.CONTACT_CHANGE.value:
        summary = "Inbound message reports a personnel or contact change"
        reason = HandoffReason.PERSONNEL_CHANGE
    else:
        summary = "Automated reply could not be handled safely"
        reason = HandoffReason.AUTOMATED_REPLY_REVIEW
    await audit(
        session,
        "inbound.automated_reply_escalated",
        case_id=case.id,
        actor="system",
        data={"email_id": email_row.id, **facts},
    )
    await create_handoff(
        session,
        case=case,
        reason=reason,
        summary=summary,
        facts=facts,
        source_email_id=email_row.id,
    )
    return True


async def process_inbound(session: AsyncSession, email_id: int) -> None:
    email_row = await session.get(EmailMessage, email_id)
    if email_row is None:
        return
    if email_row.is_bounce:
        await _handle_bounce(session, email_row)
        return
    # A reply to a reactivation is business-significant even when a mail client
    # omitted thread headers and the normal case matcher could not link it.
    if not email_row.is_automated_reply:
        await record_reactivation_reply(session, email_row)
    if email_row.case_id is None:
        return
    case = await session.get(SalesCase, email_row.case_id)
    if case is None:
        return
    reply_key = f"inbound-reply:{email_row.id}"
    existing_reply = await session.scalar(
        select(Outbox.id).where(
            or_(
                Outbox.business_key == reply_key,
                Outbox.business_key.like(f"{reply_key}:quote:%"),
            ),
            Outbox.status != DeliveryStatus.CANCELLED,
        )
    )
    if existing_reply is not None:
        return
    existing_handoff = await session.scalar(select(Handoff).where(Handoff.source_email_id == email_row.id))
    if existing_handoff is not None:
        await enqueue_job(
            session,
            "notify_handoff",
            {"handoff_id": existing_handoff.id},
            f"handoff-notify:{existing_handoff.id}",
        )
        return
    await session.refresh(case, ["customer", "contact", "product"])
    if await _handle_automated_reply(session, case=case, email_row=email_row):
        return
    settings = get_settings()
    commercial_context: QuoteContext | None = None
    ai = AIClient()
    try:
        analysis, metadata = await ai.analyze(email_row.subject, email_row.body_text, email_row.attachment_metadata)
    except Exception as exc:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.AI_FAILURE,
            summary=f"AI analysis failed: {type(exc).__name__}",
            source_email_id=email_row.id,
        )
        return
    analysis = analysis.model_copy(
        update={
            "risky_attachment": attachments_require_review(
                email_row.attachment_metadata,
                email_row.body_html,
            )
        }
    )
    analysis_facts = analysis.model_dump(mode="json")
    session.add(
        AIInvocation(
            case_id=case.id,
            provider=metadata["provider"],
            model=metadata["model"],
            purpose="inbound_analysis",
            request_hash=metadata["request_hash"],
            parsed_output=analysis_facts,
            success=True,
            input_tokens=metadata.get("input_tokens"),
            output_tokens=metadata.get("output_tokens"),
        )
    )
    if analysis.unsubscribe:
        case.contact.suppressed = True
        case.customer.do_not_contact = True
        case.status = CaseStatus.PAUSED
        await audit(session, "contact.unsubscribed", case_id=case.id, actor="customer")
        await session.commit()
        return
    # Weekly commercial-data readiness blocks only an autonomous quotation.
    # Unsubscribe, counteroffers, samples, orders, complaints, and all other
    # human-review paths must still be classified and surfaced immediately.
    if analysis.intent != Intent.QUOTE_REQUEST:
        send_decision = evaluate_send_policy(
            SendContext(
                intent=analysis.intent,
                stage=case.stage,
                status=case.status,
                intent_confidence=analysis.intent_confidence,
                product_confidence=analysis.product_confidence,
                numeric_confidence=analysis.numeric_confidence,
                auto_send_allowed=case.customer.auto_send_allowed,
                contact_suppressed=case.contact.suppressed,
                do_not_contact=case.customer.do_not_contact,
                has_risky_attachment=analysis.risky_attachment,
                product_known=analysis.product_code is None or product_codes_match(analysis.product_code, case.product.code),
                prebook_requested=analysis.prebook_requested,
                packaging_requested=analysis.packaging_requested,
                delivery_requested=analysis.shipping_requested,
            ),
            intent_threshold=get_settings().intent_confidence_threshold,
            product_threshold=get_settings().product_confidence_threshold,
            numeric_threshold=get_settings().numeric_confidence_threshold,
        )
        await create_handoff(
            session,
            case=case,
            reason=send_decision.reason or HandoffReason.LOW_CONFIDENCE,
            summary=f"Inbound {analysis.intent.value} requires human review",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return
    latest_quote = await session.scalar(
        select(Quote).where(Quote.case_id == case.id).order_by(Quote.round_number.desc())
    )
    if latest_quote is not None and latest_quote.currency != case.currency:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.NONSTANDARD,
            summary="The current case currency does not match its latest quotation",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return
    quantity = analysis.quantity or (latest_quote.quantity if latest_quote is not None else None)
    if quantity is None:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.LOW_CONFIDENCE,
            summary="Initial inquiry does not contain a reliable quotation quantity",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return
    commercial_context = await _commercial_quote_context(
        session,
        product_id=case.product_id,
        currency=case.currency,
        settings=settings,
        requested_quantity=quantity,
    )
    if commercial_context is not None and commercial_context.status is QuoteContextStatus.UNAVAILABLE:
        unavailable_reason = (
            HandoffReason.INVENTORY_UNAVAILABLE
            if commercial_context.reason.startswith("INVENTORY")
            else HandoffReason.NONSTANDARD
        )
        await create_handoff(
            session,
            case=case,
            reason=unavailable_reason,
            summary=(
                f"Current commercial data cannot quote {case.product.code}: "
                f"{commercial_context.reason}"
            ),
            facts={
                **analysis_facts,
                "commercial_cycle_id": commercial_context.cycle.id,
                "requested_quantity": quantity,
                "available_quantity": (
                    str(commercial_context.inventory.quantity)
                    if commercial_context.inventory is not None
                    and commercial_context.inventory.quantity is not None
                    else None
                ),
            },
            source_email_id=email_row.id,
        )
        return
    policy_row = (
        commercial_context.policy
        if commercial_context is not None
        else await active_policy(session, case.product_id, case.currency)
    )
    if policy_row is None:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.NONSTANDARD,
            summary="No standard price policy matched the inbound request",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return
    currency_standard = analysis.currency is None or analysis.currency.upper() == case.currency
    incoterm_standard = analysis.incoterm is None or analysis.incoterm.upper() == policy_row.standard_incoterm.upper()
    payment_standard = analysis.payment_term is None or analysis.payment_term.casefold() == policy_row.standard_payment_term.casefold()
    quantity_standard = quantity >= policy_row.min_quantity and (
        policy_row.max_quantity is None or quantity <= policy_row.max_quantity
    )
    send_decision = evaluate_send_policy(
        SendContext(
            intent=analysis.intent,
            stage=case.stage,
            status=case.status,
            intent_confidence=analysis.intent_confidence,
            product_confidence=analysis.product_confidence,
            numeric_confidence=analysis.numeric_confidence,
            auto_send_allowed=case.customer.auto_send_allowed,
            contact_suppressed=case.contact.suppressed,
            do_not_contact=case.customer.do_not_contact,
            has_risky_attachment=analysis.risky_attachment,
            currency_standard=currency_standard,
            quantity_standard=quantity_standard,
            incoterm_standard=incoterm_standard,
            payment_standard=payment_standard,
            product_known=analysis.product_code is None or product_codes_match(analysis.product_code, case.product.code),
            prebook_requested=analysis.prebook_requested,
            packaging_requested=analysis.packaging_requested,
            delivery_requested=analysis.shipping_requested,
            ready_stock_available=(
                commercial_context.ready_stock_available
                if commercial_context is not None
                else True
            ),
        ),
        intent_threshold=get_settings().intent_confidence_threshold,
        product_threshold=get_settings().product_confidence_threshold,
        numeric_threshold=get_settings().numeric_confidence_threshold,
    )
    if not send_decision.allow_send:
        await create_handoff(
            session,
            case=case,
            reason=send_decision.reason or HandoffReason.NONSTANDARD,
            summary=f"Inbound {analysis.intent.value} requires human review",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return
    price_decision = initial_quote(_pricing_policy(policy_row), quantity)
    if not price_decision.approved or price_decision.unit_price is None:
        reason = HandoffReason.BELOW_FLOOR if price_decision.reason and "floor" in price_decision.reason else HandoffReason.NONSTANDARD
        await create_handoff(
            session,
            case=case,
            reason=reason,
            summary=f"Pricing engine rejected autonomous reply: {price_decision.reason}",
            facts={
                **analysis_facts,
                "hard_minimum": str(price_decision.hard_minimum),
                "pricing_reason": price_decision.reason,
            },
            source_email_id=email_row.id,
        )
        return
    valid_until = quote_valid_until(
        quote_valid_days=policy_row.quote_valid_days,
        quote_valid_weekday=policy_row.quote_valid_weekday,
        today=datetime.now(UTC).astimezone(ZoneInfo(settings.business_timezone)).date(),
    )
    bundle = load_content(get_settings().content_dir)
    if not str(bundle.product_snippets.get(case.product.approved_text_key) or "").strip():
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.NONSTANDARD,
            summary=f"Approved product text is missing for key {case.product.approved_text_key}",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return
    try:
        plan = await ai.draft_plan(
            {
                "subject": email_row.subject,
                "contact_name": case.contact.name,
                "approved_product_key": case.product.approved_text_key,
            }
        )
        text, html_body = render_quote(
            plan=plan,
            bundle=bundle,
            product_key=case.product.approved_text_key,
            product_name=case.product.name,
            price=price_decision.unit_price,
            currency=policy_row.currency,
            quantity=quantity,
            unit=case.product.unit,
            incoterm=policy_row.standard_incoterm,
            payment_term=policy_row.standard_payment_term,
            valid_until=valid_until,
            taxes_included=policy_row.taxes_included,
            freight_included=policy_row.freight_included,
        )
        source = _reply_source(email_row)
        text, html_body = append_quoted_reply(
            text,
            html_body,
            from_address=email_row.from_address,
            source_body=source.body_text,
            source_html=source.body_html,
            occurred_at=email_row.received_at,
        )
    except Exception as exc:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.AI_FAILURE,
            summary=f"Reply drafting failed: {type(exc).__name__}",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return
    round_number = latest_quote.round_number + 1 if latest_quote is not None else 0
    case.negotiation_round = round_number
    if latest_quote is not None:
        case.stage = transition(case.stage, CaseStage.NEGOTIATING)
    quote = Quote(
        case_id=case.id,
        price_policy_id=policy_row.id,
        commercial_cycle_id=(commercial_context.cycle.id if commercial_context is not None else None),
        round_number=round_number,
        unit_price=price_decision.unit_price,
        currency=policy_row.currency,
        quantity=quantity,
        incoterm=policy_row.standard_incoterm,
        payment_term=policy_row.standard_payment_term,
        valid_until=valid_until,
        pricing_snapshot={
            "hard_minimum": str(price_decision.hard_minimum),
            "pricing_reason": price_decision.reason,
            "applied_markup_pct": str(price_decision.applied_markup_pct),
            "requested_price": str(analysis.requested_unit_price),
        },
    )
    session.add(quote)
    await session.flush()
    await freeze_outbox(
        session,
        case=case,
        quote=quote,
        subject=f"Re: {email_row.subject}",
        text_body=text,
        html_body=html_body,
        business_key=(
            f"inbound-reply:{email_row.id}:quote:{commercial_context.cycle.id}"
            if commercial_context is not None
            else f"inbound-reply:{email_row.id}"
        ),
        in_reply_to=email_row.message_id,
        references=_reply_references(email_row),
        inline_images=source.inline_images,
    )


async def notify_handoff(session: AsyncSession, handoff_id: int) -> None:
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None or handoff.dingtalk_status == "SENT":
        return
    case = await session.get(SalesCase, handoff.case_id) if handoff.case_id else None
    try:
        handoff.dingtalk_status = await DingTalkNotifier().notify(handoff, case)
    except Exception as exc:
        handoff.dingtalk_status = "FAILED"
        raise RuntimeError(str(exc)) from exc
    finally:
        await session.commit()


async def notify_commercial_refresh(session: AsyncSession, cycle_id: int) -> None:
    cycle = await session.get(CommercialDataCycle, cycle_id)
    if cycle is None or cycle.reminder_status in {"SENT", "LOGGED", "NOT_REQUIRED"}:
        return
    if cycle.price_status == "CONFIRMED" and cycle.inventory_status == "CONFIRMED":
        cycle.reminder_status = "NOT_REQUIRED"
        await session.commit()
        return
    try:
        cycle.reminder_status = await DingTalkNotifier().notify_commercial_refresh(cycle)
        cycle.reminder_sent_at = datetime.now(UTC)
    except Exception as exc:
        cycle.reminder_status = "FAILED"
        raise RuntimeError(str(exc)) from exc
    finally:
        await session.commit()


def _message_activity_key(source: str, row_id: int, message_id: str | None) -> str:
    normalized = (message_id or "").strip().lower()
    return f"message-id:{normalized}" if normalized else f"{source}:{row_id}"


async def _mailbox_sent_events_since(
    session: AsyncSession,
    mailbox: str,
    since: datetime,
    until: datetime,
) -> dict[str, datetime]:
    events: dict[str, datetime] = {}
    email_rows = await session.execute(
        select(EmailMessage.id, EmailMessage.message_id, EmailMessage.received_at).where(
            EmailMessage.mailbox == mailbox,
            EmailMessage.direction == "OUTBOUND",
            EmailMessage.received_at >= since,
            EmailMessage.received_at <= until,
        )
    )
    for row_id, message_id, occurred_at in email_rows:
        key = _message_activity_key("email", row_id, message_id)
        events[key] = max(events.get(key, occurred_at), occurred_at)

    outbox_rows = await session.execute(
        select(Outbox.id, Outbox.message_id, Outbox.sent_at).where(
            Outbox.sent_via == "smtp",
            Outbox.sent_at >= since,
            Outbox.sent_at <= until,
        )
    )
    for row_id, message_id, sent_at in outbox_rows:
        if sent_at is None:
            continue
        key = _message_activity_key("outbox", row_id, message_id)
        events[key] = max(events.get(key, sent_at), sent_at)
    return events


def _send_interval_seconds(settings: Settings, message_id: str) -> int:
    if settings.send_interval_jitter_seconds == 0:
        return settings.min_send_interval_seconds
    digest = hashlib.sha256(message_id.encode("utf-8")).digest()
    jitter = int.from_bytes(digest[:4], "big") % (settings.send_interval_jitter_seconds + 1)
    return settings.min_send_interval_seconds + jitter


def _smtp_rate_limit_cooldown_seconds(exc: smtplib.SMTPResponseException, settings: Settings) -> int | None:
    detail = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
    normalized = detail.lower()
    daily_markers = ("5.4.5", "daily user sending limit", "daily smtp", "daily limit")
    rate_markers = ("4.7.28", "rate limit", "too many", "quota", "temporarily deferred")
    if any(marker in normalized for marker in daily_markers):
        return settings.gmail_daily_cooldown_seconds
    if exc.smtp_code in {550, 554} and ("limit" in normalized or "quota" in normalized):
        return settings.gmail_daily_cooldown_seconds
    if 400 <= exc.smtp_code < 500 or any(marker in normalized for marker in rate_markers):
        return settings.gmail_transient_cooldown_seconds
    return None


async def _set_mailbox_cooldown(
    session: AsyncSession,
    mailbox: str,
    cooldown_until: datetime,
    reason: str,
) -> None:
    throttle = await session.get(MailboxThrottle, mailbox, with_for_update=True)
    if throttle is None:
        session.add(
            MailboxThrottle(
                mailbox=mailbox,
                cooldown_until=cooldown_until,
                reason=reason,
            )
        )
        return
    if throttle.cooldown_until is None or throttle.cooldown_until < cooldown_until:
        throttle.cooldown_until = cooldown_until
        throttle.reason = reason
    throttle.updated_at = datetime.now(UTC)


async def _cancel_and_requeue_stale_quote(
    session: AsyncSession,
    *,
    row: Outbox,
    quote: Quote | None,
    cycle: CommercialDataCycle,
    reason: str,
) -> None:
    """Cancel immutable old quote mail and create one cycle-scoped reprice job."""

    row.status = DeliveryStatus.CANCELLED
    row.last_error = f"commercial data gate cancelled frozen quote: {reason}"[:2000]
    await session.execute(
        delete(EmailMessage).where(
            EmailMessage.direction == "OUTBOUND",
            EmailMessage.message_id == row.message_id,
            EmailMessage.is_history.is_(False),
        )
    )
    await audit(
        session,
        "outbox.cancelled_stale_commercial_data",
        case_id=row.case_id,
        actor="commercial_gate",
        data={
            "outbox_id": row.id,
            "quote_id": quote.id if quote else None,
            "old_cycle_id": quote.commercial_cycle_id if quote else None,
            "new_cycle_id": cycle.id,
            "reason": reason,
        },
    )
    inbound_match = re.fullmatch(r"inbound-reply:(\d+)(?::quote:\d+)?", row.business_key)
    initial_match = re.fullmatch(r"initial-quote:case:(\d+)(?::cycle:\d+)?", row.business_key)
    if inbound_match:
        email_id = int(inbound_match.group(1))
        await enqueue_job(
            session,
            "process_inbound",
            {"email_id": email_id, "reprice": True},
            f"commercial-reprice:inbound:{email_id}:cycle:{cycle.id}",
        )
        return
    if initial_match and row.case_id is not None:
        await enqueue_job(
            session,
            "case_outreach",
            {
                "case_id": row.case_id,
                "quantity": quote.quantity if quote is not None else 1,
                "reprice": True,
            },
            f"commercial-reprice:case:{row.case_id}:cycle:{cycle.id}",
        )
        return
    await session.commit()


async def send_one_outbox(
    session: AsyncSession,
    settings: Settings | None = None,
    *,
    at: datetime | None = None,
) -> bool:
    settings = settings or get_settings()
    now = at or datetime.now(UTC)
    stale_before = now - timedelta(seconds=settings.outbox_lease_seconds)
    row = await session.scalar(
        select(Outbox)
        .where(
            or_(
                Outbox.status.in_([DeliveryStatus.PENDING, DeliveryStatus.FAILED]),
                and_(Outbox.status == DeliveryStatus.CLAIMED, Outbox.locked_at < stale_before),
            ),
            Outbox.available_at <= now,
        )
        # Live replies, quotations, and human-approved mail always stay ahead
        # of bulk reactivation messages, regardless of creation order.
        .order_by(
            sa_case((Outbox.message_kind == "REACTIVATION", 1), else_=0),
            Outbox.id,
        )
        .with_for_update(skip_locked=True)
    )
    if row is None:
        return False
    if row.attempts >= 5:
        row.status = DeliveryStatus.CANCELLED
        row.last_error = "outbox retry limit exhausted"
        await session.commit()
        case = await session.get(SalesCase, row.case_id) if row.case_id else None
        if case:
            await create_handoff(
                session,
                case=case,
                reason=HandoffReason.MAIL_FAILURE,
                summary=f"Outbound delivery exhausted retries for {row.message_id}",
            )
        return True
    reclaimed_claim = row.status == DeliveryStatus.CLAIMED
    if reclaimed_claim and settings.mail_transport == "smtp":
        row.status = DeliveryStatus.UNKNOWN
        row.last_error = "stale SMTP claim requires Sent-folder reconciliation"
        await session.commit()
        return True
    mailbox = (settings.gmail_address or parseaddr(settings.mail_from)[1]).lower()
    if settings.mail_transport == "smtp":
        throttle = await session.get(MailboxThrottle, mailbox)
        if throttle and throttle.cooldown_until and throttle.cooldown_until > now:
            row.status = DeliveryStatus.PENDING
            row.available_at = throttle.cooldown_until
            row.last_error = f"mailbox cooldown active: {throttle.reason or 'Gmail rate limit'}"[:2000]
            await session.commit()
            return True
    case: SalesCase | None = None
    human_approved = bool(
        row.approval_handoff_id is not None
        and row.human_approved_by
        and row.human_approved_at is not None
    )
    if row.message_kind == "REACTIVATION":
        guard = await reactivation_send_guard(session, row, settings=settings, at=now)
        if guard.action == "DEFER":
            row.status = DeliveryStatus.PENDING
            row.available_at = guard.available_at or (now + timedelta(minutes=15))
            row.last_error = guard.reason
            await session.commit()
            return True
        if guard.action == "BLOCK":
            row.status = DeliveryStatus.CANCELLED
            row.last_error = guard.reason
            await session.commit()
            return True
    if (
        settings.commercial_gate_enabled
        and not settings.demo_mode
        and not human_approved
        and not is_business_day(settings, now)
    ):
        row.status = DeliveryStatus.PENDING
        row.available_at = next_business_open(settings, now)
        row.last_error = "commercial gate deferred automated mail until Monday"
        await session.commit()
        return True
    if row.case_id:
        case = await session.scalar(
            select(SalesCase)
            .options(
                selectinload(SalesCase.customer),
                selectinload(SalesCase.contact),
            )
            .where(SalesCase.id == row.case_id)
        )
        if (
            case is None
            or case.contact.suppressed
            or case.customer.do_not_contact
            or case.contact.email.lower() != row.recipient.lower()
            or (
                human_approved
                and case.status in {CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST}
            )
            or (
                not human_approved
                and (
                    case.status != CaseStatus.ACTIVE
                    or not case.customer.auto_send_allowed
                )
            )
        ):
            row.status = DeliveryStatus.CANCELLED
            row.last_error = "case/contact eligibility changed after message was queued"
            await session.commit()
            return True
    is_auto_quote = not human_approved and (
        row.message_kind == "AUTO_QUOTE" or row.quote_id is not None
    )
    if settings.commercial_gate_enabled and not settings.demo_mode and is_auto_quote:
        quote = await session.get(Quote, row.quote_id) if row.quote_id is not None else None
        if case is None:
            row.status = DeliveryStatus.CANCELLED
            row.last_error = "commercial gate could not resolve the quote case"
            await session.commit()
            return True
        await lock_commercial_scope(session, settings.commercial_scope)
        context = await get_commercial_data_provider(settings).get_quote_context(
            session,
            product_id=case.product_id,
            currency=case.currency,
            requested_quantity=quote.quantity if quote is not None else None,
            at=now,
        )
        same_frozen_version = bool(
            quote is not None
            and quote.commercial_cycle_id == context.cycle.id
            and context.policy is not None
            and quote.price_policy_id == context.policy.id
        )
        if context.status is QuoteContextStatus.WAITING and same_frozen_version:
            await ensure_weekly_commercial_refresh(session, settings, at=now)
            row.status = DeliveryStatus.PENDING
            row.available_at = context.next_check_at or (
                now + timedelta(minutes=settings.commercial_retry_minutes)
            )
            row.last_error = f"commercial data gate waiting: {context.reason}"[:2000]
            await session.commit()
            return True
        if context.status is not QuoteContextStatus.AVAILABLE or not same_frozen_version:
            await _cancel_and_requeue_stale_quote(
                session,
                row=row,
                quote=quote,
                cycle=context.cycle,
                reason=(
                    context.reason
                    if context.status is not QuoteContextStatus.AVAILABLE
                    else "frozen quote belongs to an older commercial data version"
                ),
            )
            return True
    if settings.mail_transport == "smtp":
        recipient = row.recipient.lower()
        if settings.safe_mode and recipient not in settings.recipient_allowlist:
            row.status = DeliveryStatus.CANCELLED
            row.last_error = "SAFE_MODE blocked recipient not on allowlist"
            await audit(
                session,
                "outbox.blocked_safe_mode",
                case_id=row.case_id,
                actor="policy",
                data={"recipient": recipient},
            )
            await session.commit()
            return True
        if not settings.auto_send_enabled and not human_approved:
            row.status = DeliveryStatus.CANCELLED
            row.last_error = "AUTO_SEND_ENABLED is false"
            await session.commit()
            return True
        preflight_outcome, preflight_detail, preflight_facts = await _recipient_preflight(
            session,
            recipient,
            settings,
        )
        if preflight_outcome == "DEFER":
            row.status = DeliveryStatus.PENDING
            row.available_at = now + timedelta(minutes=settings.mx_temporary_retry_minutes)
            row.last_error = f"recipient preflight deferred: {preflight_detail}"[:2000]
            await audit(
                session,
                "outbox.preflight_deferred",
                case_id=row.case_id,
                actor="dns",
                data={"outbox_id": row.id, **preflight_facts},
            )
            await session.commit()
            return True
        if preflight_outcome == "BLOCK":
            row.status = DeliveryStatus.CANCELLED
            row.last_error = f"recipient preflight blocked: {preflight_detail}"[:2000]
            await audit(
                session,
                "outbox.preflight_blocked",
                case_id=row.case_id,
                actor="policy",
                data={"outbox_id": row.id, **preflight_facts},
            )
            await session.commit()
            if case is not None:
                await create_handoff(
                    session,
                    case=case,
                    reason=HandoffReason.EMAIL_DELIVERABILITY,
                    summary=f"Recipient preflight blocked {recipient}: {preflight_detail}",
                    facts={"outbox_id": row.id, **preflight_facts},
                )
            return True
        await audit(
            session,
            "outbox.preflight_passed",
            case_id=row.case_id,
            actor="dns",
            data={"outbox_id": row.id, **preflight_facts},
        )
        since_hour = now - timedelta(hours=1)
        since_day = now - timedelta(days=1)
        sent_events = await _mailbox_sent_events_since(session, mailbox, since_day, now)
        hourly_events = {key: value for key, value in sent_events.items() if value >= since_hour}
        if len(hourly_events) >= settings.max_sends_per_hour:
            row.status = DeliveryStatus.PENDING
            row.available_at = min(hourly_events.values()) + timedelta(hours=1)
            row.last_error = "mailbox-wide hourly send limit deferred message"
            await session.commit()
            return True
        if len(sent_events) >= settings.max_sends_per_day:
            row.status = DeliveryStatus.PENDING
            row.available_at = min(sent_events.values()) + timedelta(days=1)
            row.last_error = "mailbox-wide rolling 24-hour send limit deferred message"
            await session.commit()
            return True
        if sent_events:
            last_sent_at = max(sent_events.values())
            next_send_at = last_sent_at + timedelta(seconds=_send_interval_seconds(settings, row.message_id))
            if next_send_at > now:
                row.status = DeliveryStatus.PENDING
                row.available_at = next_send_at
                row.last_error = "mailbox-wide send spacing deferred message"
                await session.commit()
                return True
    row.status = DeliveryStatus.CLAIMED
    row.locked_at = datetime.now(UTC)
    row.attempts += 1
    await session.commit()
    try:
        transport_for(settings).send(row.raw_message, row.message_id, row.recipient)
        row.status = DeliveryStatus.SENT
        row.sent_at = datetime.now(UTC)
        row.sent_via = settings.mail_transport
        row.last_error = None
        await audit(
            session,
            "outbox.sent",
            case_id=row.case_id,
            actor=settings.mail_transport,
            data={
                "outbox_id": row.id,
                "message_id": row.message_id,
                "approval_handoff_id": row.approval_handoff_id,
                "human_approved_by": row.human_approved_by,
            },
        )
    except (smtplib.SMTPServerDisconnected, ConnectionResetError, TimeoutError) as exc:
        row.status = DeliveryStatus.UNKNOWN
        row.last_error = f"ambiguous transport outcome: {exc}"
    except smtplib.SMTPResponseException as exc:
        cooldown_seconds = _smtp_rate_limit_cooldown_seconds(exc, settings)
        detail = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        if cooldown_seconds is None:
            failure_type = classify_smtp_failure(exc.smtp_code, detail)
            if failure_type == BounceType.HARD:
                row.status = DeliveryStatus.CANCELLED
                row.last_error = f"permanent SMTP recipient failure {exc.smtp_code}: {detail}"[:2000]
                await _suppress_email_address(
                    session,
                    row.recipient,
                    reason="SMTP_HARD_BOUNCE",
                    bounce_type=failure_type.value,
                    diagnostic=detail,
                )
                if case and case.status == CaseStatus.ACTIVE:
                    case.status = CaseStatus.PAUSED
                await audit(
                    session,
                    "outbox.smtp_hard_bounce_suppressed",
                    case_id=row.case_id,
                    actor="smtp",
                    data={"outbox_id": row.id, "smtp_code": exc.smtp_code, "diagnostic": detail[:2000]},
                )
            else:
                row.status = DeliveryStatus.FAILED
                row.last_error = f"SMTP {exc.smtp_code}: {detail}"[:2000]
                row.available_at = datetime.now(UTC) + timedelta(minutes=min(60, 2**row.attempts))
        else:
            cooldown_until = datetime.now(UTC) + timedelta(seconds=cooldown_seconds)
            reason = f"Gmail SMTP {exc.smtp_code}: {detail}"[:2000]
            await _set_mailbox_cooldown(session, mailbox, cooldown_until, reason)
            row.status = DeliveryStatus.PENDING
            row.attempts = max(0, row.attempts - 1)
            row.available_at = cooldown_until
            row.last_error = reason
            await audit(
                session,
                "outbox.gmail_cooldown",
                case_id=row.case_id,
                actor="smtp",
                data={"outbox_id": row.id, "smtp_code": exc.smtp_code, "cooldown_seconds": cooldown_seconds},
            )
    except Exception as exc:
        row.status = DeliveryStatus.FAILED
        row.last_error = str(exc)[:2000]
        row.available_at = datetime.now(UTC) + timedelta(minutes=min(60, 2**row.attempts))
    await session.commit()
    return True


async def reconcile_unknown_outbox(session: AsyncSession, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    if settings.mail_transport != "smtp" or not (settings.gmail_address and settings.gmail_app_password):
        return False
    row = await session.scalar(
        select(Outbox)
        .where(
            Outbox.status == DeliveryStatus.UNKNOWN,
            Outbox.locked_at < datetime.now(UTC) - timedelta(minutes=10),
        )
        .order_by(Outbox.id)
        .with_for_update(skip_locked=True)
    )
    if row is None:
        return False
    try:
        found = await asyncio.to_thread(GmailIMAPClient(settings).sent_contains_message_id, row.message_id)
    except Exception as exc:
        # Keep an ambiguous delivery in UNKNOWN until Gmail Sent can be checked.
        # Retrying SMTP before reconciliation could deliver a duplicate message.
        row.locked_at = datetime.now(UTC)
        row.last_error = f"Gmail Sent reconciliation deferred: {type(exc).__name__}: {exc}"[:2000]
        await session.commit()
        logger.exception("outbox %s reconciliation failed", row.id)
        return True
    if found:
        row.status = DeliveryStatus.SENT
        row.sent_at = datetime.now(UTC)
        row.sent_via = "smtp"
        row.last_error = None
        await audit(
            session,
            "outbox.reconciled_sent",
            case_id=row.case_id,
            actor="gmail_sent",
            data={"outbox_id": row.id, "message_id": row.message_id},
        )
    else:
        row.status = DeliveryStatus.FAILED
        row.available_at = datetime.now(UTC)
        row.last_error = "Gmail Sent confirmed Message-ID absent; retry permitted"
    await session.commit()
    return True


JOB_HANDLERS = {
    "demo_outreach": lambda session, payload: create_demo_outreach(session, payload),
    "case_outreach": lambda session, payload: create_case_outreach(session, payload),
    "process_inbound": lambda session, payload: process_inbound(session, int(payload["email_id"])),
    "notify_handoff": lambda session, payload: notify_handoff(session, int(payload["handoff_id"])),
    "notify_commercial_refresh": lambda session, payload: notify_commercial_refresh(
        session, int(payload["cycle_id"])
    ),
}


async def claim_and_run_job(
    session: AsyncSession,
    worker_id: str,
    settings: Settings | None = None,
) -> bool:
    settings = settings or get_settings()
    stale_before = datetime.now(UTC) - timedelta(seconds=settings.job_lease_seconds)
    job = await session.scalar(
        select(Job)
        .where(
            or_(
                Job.status == JobStatus.PENDING,
                and_(Job.status == JobStatus.RUNNING, Job.locked_at < stale_before),
            ),
            Job.available_at <= datetime.now(UTC),
        )
        .order_by(Job.id)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return False
    job.status = JobStatus.RUNNING
    job.locked_at = datetime.now(UTC)
    job.locked_by = worker_id
    job.attempts += 1
    await session.commit()
    job_id = job.id
    try:
        handler = JOB_HANDLERS[job.kind]
        await handler(session, job.payload)
        job.status = JobStatus.DONE
        job.last_error = None
        job.locked_at = None
        job.locked_by = None
        job.updated_at = datetime.now(UTC)
        await session.commit()
    except JobDeferred as exc:
        await session.rollback()
        job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if job is None:
            raise RuntimeError(f"claimed job {job_id} disappeared") from exc
        job.status = JobStatus.PENDING
        job.attempts = max(0, job.attempts - 1)
        job.available_at = exc.available_at
        job.locked_at = None
        job.locked_by = None
        job.last_error = f"DEFERRED: {exc.reason}"[:2000]
        job.updated_at = datetime.now(UTC)
        await session.commit()
    except Exception as exc:
        logger.exception("job %s failed", job_id)
        error = f"{type(exc).__name__}: {exc}"[:2000]
        # Discard every uncommitted handler mutation before recording retry
        # bookkeeping. Otherwise a failed draft can leave an orphan quote or
        # consume a negotiation round without an outbound message.
        await session.rollback()
        job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if job is None:
            raise RuntimeError(f"claimed job {job_id} disappeared") from exc
        job.last_error = error
        if job.attempts >= job.max_attempts:
            job.status = JobStatus.FAILED
        else:
            job.status = JobStatus.PENDING
            job.available_at = datetime.now(UTC) + timedelta(seconds=min(300, 2**job.attempts))
        job.locked_at = None
        job.locked_by = None
        job.updated_at = datetime.now(UTC)
        await session.commit()
    return True
