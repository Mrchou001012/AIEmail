import html
import logging
import string
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from email.utils import parseaddr
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import (
    AuditEvent,
    CaseStage,
    CaseStatus,
    Contact,
    Customer,
    DeliveryStatus,
    EmailAddressStatus,
    EmailMessage,
    Handoff,
    Job,
    JobStatus,
    Outbox,
    Product,
    Quote,
    ReactivationCampaign,
    ReactivationRecipient,
    SalesCase,
)
from app.deliverability import MXStatus, validate_address_format
from app.imports import load_content
from app.mail import (
    append_quoted_reply,
    build_message,
    extract_full_reply_source,
    has_thread_subject_prefix,
    normalized_subject,
    parse_mime,
)
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)

CAMPAIGN_STATUSES = frozenset({"DRAFT", "RUNNING", "PAUSED", "COMPLETED", "CANCELLED"})
REPLY_FILTERS = frozenset({"ANY", "NEVER_REPLIED", "PREVIOUSLY_REPLIED"})
SELECTABLE_STATUSES = frozenset({"CANDIDATE", "SELECTED"})
TERMINAL_RECIPIENT_STATUSES = frozenset({"SENT", "REPLIED", "SKIPPED", "FAILED", "EXCLUDED"})
HUMAN_CONTROLLED_CASE_STATUSES = frozenset(
    {CaseStatus.WAITING_HUMAN, CaseStatus.HUMAN_TAKEOVER}
)
OPEN_DELIVERY_STATUSES = frozenset(
    {
        DeliveryStatus.PENDING,
        DeliveryStatus.CLAIMED,
        DeliveryStatus.FAILED,
        DeliveryStatus.UNKNOWN,
    }
)
CLOSED_CASE_STATUSES = frozenset({CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST})
INVALID_MX_STATUSES = frozenset(
    {MXStatus.NO_DOMAIN.value, MXStatus.NO_MX.value, MXStatus.NULL_MX.value}
)
DEFAULT_SUBJECT = "Checking in from Lanya Chem"
DEFAULT_BODY = """Dear {contact_name},

I hope you are doing well.

It has been some time since we last spoke. I am writing to reconnect and ask whether you currently have any requirements for {product_code}.

If useful, please send the product and quantity you need. We can then confirm the current availability and price.

We look forward to hearing from you."""
ALLOWED_TEMPLATE_FIELDS = frozenset(
    {
        "contact_name",
        "company_name",
        "product_code",
        "product_name",
        "last_contact_date",
    }
)


@dataclass(frozen=True)
class SendGuard:
    action: str
    reason: str | None = None
    available_at: datetime | None = None


def default_templates(settings: Settings | None = None) -> tuple[str, str]:
    settings = settings or get_settings()
    subject_path = settings.content_dir / "reactivation_subject.txt"
    body_path = settings.content_dir / "reactivation_body.txt"
    subject = subject_path.read_text(encoding="utf-8").strip() if subject_path.exists() else DEFAULT_SUBJECT
    body = body_path.read_text(encoding="utf-8").strip() if body_path.exists() else DEFAULT_BODY
    validate_template(subject)
    validate_template(body)
    return subject, body


def validate_template(value: str) -> None:
    if not value.strip():
        raise ValueError("template cannot be empty")
    for _, field_name, format_spec, conversion in string.Formatter().parse(value):
        if field_name is None:
            continue
        if field_name not in ALLOWED_TEMPLATE_FIELDS:
            raise ValueError(f"unsupported template field: {field_name}")
        if format_spec or conversion:
            raise ValueError("template format specifications and conversions are not allowed")


def _render_template(value: str, fields: dict[str, str]) -> str:
    validate_template(value)
    return value.format_map(fields)


def _body_html(value: str) -> str:
    paragraphs = [part.strip() for part in value.replace("\r\n", "\n").split("\n\n") if part.strip()]
    return "".join(f"<p>{html.escape(part).replace(chr(10), '<br>')}</p>" for part in paragraphs)


def _latest(first: datetime | None, second: datetime | None) -> datetime | None:
    values = [item for item in (first, second) if item is not None]
    return max(values) if values else None


async def _contact_workflow_block_reason(
    session: AsyncSession,
    contact_id: int,
    *,
    at: datetime,
    exclude_outbox_id: int | None = None,
) -> str | None:
    case_rows = (
        (
            await session.execute(
                select(SalesCase).where(
                    SalesCase.contact_id == contact_id,
                    SalesCase.status.not_in(CLOSED_CASE_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    if any(row.status in HUMAN_CONTROLLED_CASE_STATUSES for row in case_rows):
        return "HUMAN_CONTROLLED_CASE"
    case_ids = [row.id for row in case_rows]
    open_handoff = await session.scalar(
        select(Handoff.id)
        .outerjoin(SalesCase, Handoff.case_id == SalesCase.id)
        .outerjoin(EmailMessage, Handoff.source_email_id == EmailMessage.id)
        .where(
            Handoff.status == "OPEN",
            or_(SalesCase.contact_id == contact_id, EmailMessage.contact_id == contact_id),
        )
        .limit(1)
    )
    if open_handoff is not None:
        return "OPEN_HUMAN_HANDOFF"
    if not case_ids:
        return None
    pending_outbox_query = select(Outbox.id).where(
        Outbox.case_id.in_(case_ids),
        Outbox.status.in_(OPEN_DELIVERY_STATUSES),
    )
    if exclude_outbox_id is not None:
        pending_outbox_query = pending_outbox_query.where(Outbox.id != exclude_outbox_id)
    if await session.scalar(pending_outbox_query.limit(1)) is not None:
        return "IN_FLIGHT_OUTBOX"
    valid_quote = await session.scalar(
        select(Quote.id)
        .where(Quote.case_id.in_(case_ids), Quote.valid_until >= at.date())
        .limit(1)
    )
    if valid_quote is not None:
        return "ACTIVE_QUOTE"
    active_jobs = (
        (
            await session.execute(
                select(Job).where(
                    Job.kind == "case_outreach",
                    Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
                )
            )
        )
        .scalars()
        .all()
    )
    for job in active_jobs:
        try:
            job_case_id = int((job.payload or {}).get("case_id") or 0)
        except (TypeError, ValueError):
            continue
        if job_case_id in case_ids:
            return "PENDING_CASE_JOB"
    return None


def _next_weekday(value: date) -> date:
    result = value
    while result.weekday() >= 5:
        result += timedelta(days=1)
    return result


def next_campaign_window(campaign: ReactivationCampaign, at: datetime) -> datetime | None:
    timezone = ZoneInfo(campaign.timezone)
    local = at.astimezone(timezone)
    current_date = _next_weekday(local.date())
    start = datetime.combine(current_date, time(campaign.send_window_start_hour), timezone)
    end = datetime.combine(current_date, time(campaign.send_window_end_hour), timezone)
    if local.date().weekday() < 5 and start <= local < end:
        return None
    if local.date().weekday() < 5 and local < start:
        return start.astimezone(UTC)
    next_date = _next_weekday(local.date() + timedelta(days=1))
    return datetime.combine(next_date, time(campaign.send_window_start_hour), timezone).astimezone(UTC)


def _schedule_slots(
    campaign: ReactivationCampaign,
    count: int,
    *,
    at: datetime,
) -> list[datetime]:
    timezone = ZoneInfo(campaign.timezone)
    local_now = at.astimezone(timezone)
    current_date = _next_weekday(max(campaign.start_date, local_now.date()))
    window_seconds = (campaign.send_window_end_hour - campaign.send_window_start_hour) * 3600
    spacing = max(1, window_seconds // campaign.daily_limit)
    result: list[datetime] = []
    while len(result) < count:
        if current_date.weekday() >= 5:
            current_date = _next_weekday(current_date)
        start = datetime.combine(current_date, time(campaign.send_window_start_hour), timezone)
        for index in range(campaign.daily_limit):
            slot = start + timedelta(seconds=index * spacing)
            if slot <= local_now:
                continue
            result.append(slot.astimezone(UTC))
            if len(result) == count:
                break
        current_date = _next_weekday(current_date + timedelta(days=1))
    return result


async def scan_campaign_candidates(
    session: AsyncSession,
    campaign: ReactivationCampaign,
    *,
    at: datetime | None = None,
) -> dict[str, int]:
    """Snapshot every known contact and explain every exclusion deterministically."""

    if campaign.status != "DRAFT":
        raise ValueError("only a draft campaign can refresh candidates")
    observed_at = at or datetime.now(UTC)
    require_consent = bool((campaign.metadata_json or {}).get("require_consent_basis", True))

    contacts = (
        (
            await session.execute(
                select(Contact).options(selectinload(Contact.customer)).order_by(Contact.id)
            )
        )
        .scalars()
        .all()
    )
    emails = (
        (
            await session.execute(
                select(EmailMessage)
                .where(EmailMessage.contact_id.is_not(None))
                .order_by(EmailMessage.received_at, EmailMessage.id)
            )
        )
        .scalars()
        .all()
    )
    case_rows = (
        await session.execute(
            select(SalesCase, Product)
            .join(Product, SalesCase.product_id == Product.id)
            .order_by(SalesCase.last_activity_at.desc(), SalesCase.id.desc())
        )
    ).all()
    address_rows = ((await session.execute(select(EmailAddressStatus))).scalars().all())
    previous_rows = (
        (
            await session.execute(
                select(ReactivationRecipient).where(
                    ReactivationRecipient.campaign_id != campaign.id,
                    ReactivationRecipient.status.in_(["SENT", "REPLIED"]),
                )
            )
        )
        .scalars()
        .all()
    )

    activities: dict[int, dict[str, Any]] = defaultdict(
        lambda: {"last_inbound": None, "last_outbound": None, "has_replied": False}
    )
    for row in emails:
        if row.contact_id is None:
            continue
        state = activities[row.contact_id]
        if row.direction == "INBOUND":
            if row.is_bounce or row.is_automated_reply:
                continue
            state["last_inbound"] = _latest(state["last_inbound"], row.received_at)
            state["has_replied"] = True
        elif row.direction == "OUTBOUND":
            state["last_outbound"] = _latest(state["last_outbound"], row.received_at)

    cases_by_contact: dict[int, list[tuple[SalesCase, Product]]] = defaultdict(list)
    for sales_case, product in case_rows:
        cases_by_contact[sales_case.contact_id].append((sales_case, product))

    previous_by_contact: dict[int, list[ReactivationRecipient]] = defaultdict(list)
    for row in previous_rows:
        previous_by_contact[row.contact_id].append(row)

    open_handoff_contacts = set(
        (
            await session.execute(
                select(SalesCase.contact_id)
                .join(Handoff, Handoff.case_id == SalesCase.id)
                .where(Handoff.status == "OPEN")
            )
        ).scalars()
    )
    open_handoff_contacts.update(
        (
            await session.execute(
                select(EmailMessage.contact_id)
                .join(Handoff, Handoff.source_email_id == EmailMessage.id)
                .where(Handoff.status == "OPEN", EmailMessage.contact_id.is_not(None))
            )
        ).scalars()
    )
    address_by_email = {row.email.strip().casefold(): row for row in address_rows}
    contact_ids_by_email: dict[str, list[int]] = defaultdict(list)
    for contact in contacts:
        contact_ids_by_email[contact.email.strip().casefold()].append(contact.id)
    duplicate_contact_emails = {
        email for email, contact_ids in contact_ids_by_email.items() if len(contact_ids) > 1
    }
    case_contact_by_id = {
        sales_case.id: sales_case.contact_id
        for sales_case, _ in case_rows
        if sales_case.status not in CLOSED_CASE_STATUSES
    }
    human_controlled_contacts = {
        sales_case.contact_id
        for sales_case, _ in case_rows
        if sales_case.status in HUMAN_CONTROLLED_CASE_STATUSES
    }
    recent_delivery_cutoff = observed_at - timedelta(days=campaign.min_inactive_days)
    operationally_active_contacts: set[int] = set()
    if case_contact_by_id:
        delivery_rows = (
            await session.execute(
                select(
                    Outbox.case_id,
                    Outbox.status,
                    Outbox.sent_at,
                    Outbox.message_kind,
                ).where(Outbox.case_id.in_(case_contact_by_id))
            )
        ).all()
        for case_id, status, sent_at, message_kind in delivery_rows:
            if status in OPEN_DELIVERY_STATUSES or (
                status == DeliveryStatus.SENT
                and message_kind != "REACTIVATION"
                and sent_at is not None
                and sent_at >= recent_delivery_cutoff
            ):
                operationally_active_contacts.add(case_contact_by_id[case_id])
        valid_quote_case_ids = (
            (
                await session.execute(
                    select(Quote.case_id).where(
                        Quote.case_id.in_(case_contact_by_id),
                        Quote.valid_until >= observed_at.date(),
                    )
                )
            )
            .scalars()
            .all()
        )
        operationally_active_contacts.update(
            case_contact_by_id[case_id] for case_id in valid_quote_case_ids
        )
        active_jobs = (
            (
                await session.execute(
                    select(Job).where(
                        Job.kind == "case_outreach",
                        Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
                    )
                )
            )
            .scalars()
            .all()
        )
        for job in active_jobs:
            try:
                case_id = int((job.payload or {}).get("case_id") or 0)
            except (TypeError, ValueError):
                continue
            if case_id in case_contact_by_id:
                operationally_active_contacts.add(case_contact_by_id[case_id])

    await session.execute(
        delete(ReactivationRecipient).where(ReactivationRecipient.campaign_id == campaign.id)
    )
    eligible_count = 0
    excluded_count = 0
    for contact in contacts:
        customer = contact.customer
        activity = activities[contact.id]
        last_inbound = activity["last_inbound"]
        last_outbound = activity["last_outbound"]
        mailbox_last_contact = _latest(last_inbound, last_outbound)
        last_contact = _latest(mailbox_last_contact, contact.last_contact_at)
        latest_direction = None
        if contact.last_contact_at is not None and (
            mailbox_last_contact is None or contact.last_contact_at > mailbox_last_contact
        ):
            latest_direction = "IMPORTED"
        elif last_contact is not None:
            latest_direction = "OUTBOUND" if last_outbound == last_contact else "INBOUND"
        contact_cases = cases_by_contact.get(contact.id, [])
        unique_products = {product.id: product for _, product in contact_cases}
        source_case = contact_cases[0][0] if len(unique_products) == 1 and contact_cases else None
        source_product = next(iter(unique_products.values())) if len(unique_products) == 1 else None
        prior = previous_by_contact.get(contact.id, [])
        prior_sent_at = max((row.sent_at for row in prior if row.sent_at), default=None)
        address_status = address_by_email.get(contact.email.strip().casefold())
        formatted = validate_address_format(contact.email)
        reason: str | None = None
        if customer.do_not_contact:
            reason = "DO_NOT_CONTACT"
        elif contact.suppressed:
            reason = "CONTACT_SUPPRESSED"
        elif not formatted.valid:
            reason = "INVALID_EMAIL_FORMAT"
        elif contact.email.strip().casefold() in duplicate_contact_emails:
            reason = "DUPLICATE_EMAIL_CONTACT"
        elif address_status and address_status.suppressed:
            reason = "ADDRESS_SUPPRESSED"
        elif address_status and address_status.last_bounce_type == "HARD":
            reason = "HARD_BOUNCE"
        elif address_status and address_status.preflight_status in INVALID_MX_STATUSES:
            reason = "KNOWN_UNDELIVERABLE_DOMAIN"
        elif not customer.auto_send_allowed:
            reason = "AUTO_SEND_NOT_ALLOWED"
        elif require_consent and not (customer.consent_basis or "").strip():
            reason = "CONSENT_BASIS_MISSING"
        elif last_contact is None:
            reason = "NEVER_CONTACTED"
        elif contact.id in open_handoff_contacts:
            reason = "OPEN_HUMAN_HANDOFF"
        elif contact.id in human_controlled_contacts:
            reason = "HUMAN_CONTROLLED_CASE"
        elif contact.id in operationally_active_contacts:
            reason = "ACTIVE_CONVERSATION"
        elif len(prior) >= campaign.max_reactivations:
            reason = "MAX_REACTIVATIONS_REACHED"
        elif prior_sent_at and last_inbound and last_inbound > prior_sent_at:
            reason = "REPLIED_AFTER_REACTIVATION"
        elif prior_sent_at and last_outbound and last_outbound > prior_sent_at + timedelta(minutes=1):
            reason = "CONTACTED_AFTER_REACTIVATION"
        elif prior_sent_at and contact.last_contact_at and contact.last_contact_at > prior_sent_at:
            reason = "CONTACTED_AFTER_REACTIVATION"
        elif prior_sent_at and observed_at < prior_sent_at + timedelta(days=campaign.second_reactivation_days):
            reason = "SECOND_REACTIVATION_WAIT"
        elif not prior and observed_at < last_contact + timedelta(days=campaign.min_inactive_days):
            reason = "RECENT_CONTACT"
        elif campaign.reply_filter == "NEVER_REPLIED" and activity["has_replied"]:
            reason = "PREVIOUSLY_REPLIED"
        elif campaign.reply_filter == "PREVIOUSLY_REPLIED" and not activity["has_replied"]:
            reason = "NEVER_REPLIED"

        eligible = reason is None
        eligible_count += int(eligible)
        excluded_count += int(not eligible)
        inactive_days = (observed_at - last_contact).days if last_contact else None
        session.add(
            ReactivationRecipient(
                campaign_id=campaign.id,
                customer_id=customer.id,
                contact_id=contact.id,
                case_id=source_case.id if source_case else None,
                status="CANDIDATE" if eligible else "EXCLUDED",
                eligible=eligible,
                selected=False,
                exclusion_reason=reason,
                has_ever_replied=bool(activity["has_replied"]),
                latest_direction=latest_direction,
                last_contact_at=last_contact,
                last_inbound_at=last_inbound,
                last_outbound_at=last_outbound,
                previous_reactivation_count=len(prior),
                snapshot_json={
                    "company_name": customer.company_name,
                    "contact_name": contact.name,
                    "email": contact.email,
                    "consent_basis": customer.consent_basis,
                    "inactive_days": inactive_days,
                    "source_case_id": source_case.id if source_case else None,
                    "product_code": source_product.code if source_product else None,
                    "product_name": source_product.name if source_product else None,
                    "product_context_count": len(unique_products),
                    "contact_first_contact_at": (
                        contact.first_contact_at.isoformat() if contact.first_contact_at else None
                    ),
                    "contact_last_contact_at": (
                        contact.last_contact_at.isoformat() if contact.last_contact_at else None
                    ),
                    "address_preflight_status": (
                        address_status.preflight_status if address_status else None
                    ),
                    "last_bounce_type": address_status.last_bounce_type if address_status else None,
                },
            )
        )
    campaign.metadata_json = {
        **(campaign.metadata_json or {}),
        "last_scan_at": observed_at.isoformat(),
        "candidate_count": eligible_count,
        "excluded_count": excluded_count,
    }
    await session.commit()
    return {"eligible": eligible_count, "excluded": excluded_count, "total": len(contacts)}


async def start_campaign(
    session: AsyncSession,
    campaign: ReactivationCampaign,
    *,
    at: datetime | None = None,
) -> int:
    if campaign.status != "DRAFT":
        raise ValueError("only a draft campaign can be started")
    if campaign.reply_filter not in REPLY_FILTERS:
        raise ValueError("invalid reply filter")
    selected = (
        (
            await session.execute(
                select(ReactivationRecipient)
                .where(
                    ReactivationRecipient.campaign_id == campaign.id,
                    ReactivationRecipient.eligible.is_(True),
                    ReactivationRecipient.selected.is_(True),
                    ReactivationRecipient.status.in_(SELECTABLE_STATUSES),
                )
                .order_by(ReactivationRecipient.last_contact_at, ReactivationRecipient.id)
            )
        )
        .scalars()
        .all()
    )
    if not selected:
        raise ValueError("select at least one eligible recipient before starting")
    observed_at = at or datetime.now(UTC)
    for recipient, slot in zip(selected, _schedule_slots(campaign, len(selected), at=observed_at), strict=True):
        recipient.status = "SCHEDULED"
        recipient.scheduled_for = slot
    campaign.status = "RUNNING"
    campaign.started_at = campaign.started_at or observed_at
    campaign.paused_at = None
    session.add(
        AuditEvent(
            case_id=None,
            actor=campaign.created_by,
            event_type="reactivation.campaign_started",
            data={"campaign_id": campaign.id, "selected": len(selected)},
        )
    )
    await session.commit()
    return len(selected)


async def pause_campaign(session: AsyncSession, campaign: ReactivationCampaign) -> int:
    if campaign.status != "RUNNING":
        raise ValueError("only a running campaign can be paused")
    campaign.status = "PAUSED"
    campaign.paused_at = datetime.now(UTC)
    rows = (
        (
            await session.execute(
                select(ReactivationRecipient, Outbox)
                .join(Outbox, ReactivationRecipient.outbox_id == Outbox.id)
                .where(
                    ReactivationRecipient.campaign_id == campaign.id,
                    Outbox.status.in_([DeliveryStatus.PENDING, DeliveryStatus.FAILED]),
                )
            )
        )
        .all()
    )
    for recipient, outbox in rows:
        outbox.status = DeliveryStatus.CANCELLED
        outbox.last_error = "reactivation campaign paused"
        recipient.status = "QUEUED"
    session.add(
        AuditEvent(
            case_id=None,
            actor=campaign.created_by,
            event_type="reactivation.campaign_paused",
            data={"campaign_id": campaign.id, "cancelled_pending": len(rows)},
        )
    )
    await session.commit()
    return len(rows)


async def resume_campaign(session: AsyncSession, campaign: ReactivationCampaign) -> int:
    if campaign.status != "PAUSED":
        raise ValueError("only a paused campaign can be resumed")
    campaign.status = "RUNNING"
    campaign.paused_at = None
    now = datetime.now(UTC)
    rows = (
        (
            await session.execute(
                select(ReactivationRecipient, Outbox)
                .join(Outbox, ReactivationRecipient.outbox_id == Outbox.id)
                .where(
                    ReactivationRecipient.campaign_id == campaign.id,
                    Outbox.status == DeliveryStatus.CANCELLED,
                    Outbox.last_error == "reactivation campaign paused",
                )
            )
        )
        .all()
    )
    for recipient, outbox in rows:
        outbox.status = DeliveryStatus.PENDING
        outbox.available_at = max(now, recipient.scheduled_for or now)
        outbox.last_error = None
    session.add(
        AuditEvent(
            case_id=None,
            actor=campaign.created_by,
            event_type="reactivation.campaign_resumed",
            data={"campaign_id": campaign.id, "restored_pending": len(rows)},
        )
    )
    await session.commit()
    return len(rows)


async def cancel_campaign(session: AsyncSession, campaign: ReactivationCampaign) -> int:
    if campaign.status in {"COMPLETED", "CANCELLED"}:
        return 0
    campaign.status = "CANCELLED"
    rows = (
        (
            await session.execute(
                select(ReactivationRecipient).where(
                    ReactivationRecipient.campaign_id == campaign.id,
                    ReactivationRecipient.status.in_(["SCHEDULED", "QUEUED"]),
                )
            )
        )
        .scalars()
        .all()
    )
    for recipient in rows:
        if recipient.outbox_id:
            outbox = await session.get(Outbox, recipient.outbox_id)
            if outbox and outbox.status in {DeliveryStatus.PENDING, DeliveryStatus.FAILED}:
                outbox.status = DeliveryStatus.CANCELLED
                outbox.last_error = "reactivation campaign cancelled"
        recipient.status = "SKIPPED"
        recipient.exclusion_reason = "CAMPAIGN_CANCELLED"
    await session.commit()
    return len(rows)


async def _queue_recipient(
    session: AsyncSession,
    recipient: ReactivationRecipient,
    campaign: ReactivationCampaign,
    *,
    at: datetime,
) -> bool:
    if campaign.status != "RUNNING" or recipient.status != "SCHEDULED" or not recipient.selected:
        return False
    contact = await session.scalar(
        select(Contact).options(selectinload(Contact.customer)).where(Contact.id == recipient.contact_id)
    )
    if contact is None:
        recipient.status = "SKIPPED"
        recipient.exclusion_reason = "CONTACT_MISSING"
        await session.commit()
        return True
    customer = contact.customer
    address_status = await session.get(EmailAddressStatus, contact.email.strip().casefold())
    workflow_reason = await _contact_workflow_block_reason(
        session,
        contact.id,
        at=at,
    )
    activity_after_selection = await session.scalar(
        select(EmailMessage.id)
        .where(
            EmailMessage.contact_id == contact.id,
            EmailMessage.received_at > campaign.created_at,
        )
        .limit(1)
    )
    imported_activity_after_selection = bool(
        contact.last_contact_at is not None
        and (
            recipient.last_contact_at is None
            or contact.last_contact_at > recipient.last_contact_at
        )
    )
    reason = None
    if customer.do_not_contact:
        reason = "DO_NOT_CONTACT"
    elif contact.suppressed or (address_status and address_status.suppressed):
        reason = "CONTACT_SUPPRESSED"
    elif not customer.auto_send_allowed:
        reason = "AUTO_SEND_NOT_ALLOWED"
    elif workflow_reason is not None:
        reason = workflow_reason
    elif activity_after_selection is not None or imported_activity_after_selection:
        reason = "ACTIVITY_AFTER_SELECTION"
    if reason:
        recipient.status = "SKIPPED"
        recipient.exclusion_reason = reason
        await session.commit()
        return True

    source_case = await session.get(SalesCase, recipient.case_id) if recipient.case_id else None
    product = await session.get(Product, source_case.product_id) if source_case else None
    if source_case is not None and (product is None or not product.active):
        recipient.status = "SKIPPED"
        recipient.exclusion_reason = "PRODUCT_UNAVAILABLE"
        await session.commit()
        return True

    snapshot = dict(recipient.snapshot_json or {})
    fields = {
        "contact_name": contact.name or "Sir/Madam",
        "company_name": customer.company_name,
        "product_code": product.code if product else "our products",
        "product_name": product.name if product else "our product range",
        "last_contact_date": recipient.last_contact_at.date().isoformat() if recipient.last_contact_at else "",
    }
    subject = _render_template(campaign.subject_template, fields).strip()[:998]
    business_text = _render_template(campaign.body_template, fields).strip()
    bundle = load_content(get_settings().content_dir)
    text_body = "\n".join([business_text, "", bundle.signature_text.strip()])
    html_body = _body_html(business_text) + bundle.signature_html
    in_reply_to = None
    references: list[str] = []
    inline_images = ()
    prior_result = (
        await session.execute(
            select(ReactivationRecipient, Outbox)
            .join(Outbox, ReactivationRecipient.outbox_id == Outbox.id)
            .where(
                ReactivationRecipient.contact_id == contact.id,
                ReactivationRecipient.campaign_id != campaign.id,
                ReactivationRecipient.status == "SENT",
                Outbox.status == DeliveryStatus.SENT,
            )
            .order_by(ReactivationRecipient.sent_at.desc(), ReactivationRecipient.id.desc())
            .limit(1)
        )
    ).first()
    source_case_id = source_case.id if source_case else None
    if prior_result is not None and prior_result[0].case_id == source_case_id:
        # A second wake-up stays in the first wake-up thread. Preserve the
        # complete visible prior message (including inline signature images)
        # instead of creating a lookalike new conversation.
        _, prior_outbox = prior_result
        prior_parsed = parse_mime(prior_outbox.raw_message.encode("utf-8"))
        subject = (
            prior_parsed.subject
            if has_thread_subject_prefix(prior_parsed.subject)
            else f"Re: {prior_parsed.subject}"
        )[:998]
        prior_source = extract_full_reply_source(prior_outbox.raw_message.encode("utf-8"))
        text_body, html_body = append_quoted_reply(
            text_body,
            html_body,
            from_address=parseaddr(get_settings().mail_from)[1],
            source_body=prior_source.body_text,
            source_html=prior_source.body_html,
            occurred_at=prior_outbox.sent_at or prior_outbox.created_at,
        )
        inline_images = prior_source.inline_images
        in_reply_to = prior_outbox.message_id
        references = [prior_outbox.message_id]
        target_case = source_case
    elif source_case is None or product is None:
        # Product context is intentionally optional for a general reconnect.
        # The inbound reply classifier creates a case once the customer names a product.
        target_case = None
    else:
        target_case = SalesCase(
            customer_id=customer.id,
            contact_id=contact.id,
            product_id=product.id,
            currency=source_case.currency,
            stage=CaseStage.FOLLOW_UP,
            status=CaseStatus.ACTIVE,
            subject_key=normalized_subject(subject),
        )
        session.add(target_case)
        await session.flush()
    stable_key = f"reactivation:{campaign.id}:{recipient.id}"
    message_id, raw = build_message(
        from_address=get_settings().mail_from,
        recipient=contact.email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        stable_key=stable_key,
        in_reply_to=in_reply_to,
        references=references,
        inline_images=inline_images,
    )
    parsed = parse_mime(raw.encode("utf-8"))
    outbox = Outbox(
        case_id=target_case.id if target_case else None,
        quote_id=None,
        message_kind="REACTIVATION",
        business_key=stable_key,
        message_id=message_id,
        recipient=contact.email,
        raw_message=raw,
        status=DeliveryStatus.PENDING,
        available_at=at,
    )
    session.add(outbox)
    await session.flush()
    session.add(
        EmailMessage(
            case_id=target_case.id if target_case else None,
            customer_id=customer.id,
            contact_id=contact.id,
            direction="OUTBOUND",
            message_id=message_id,
            in_reply_to=in_reply_to,
            references_json=references,
            from_address=parseaddr(get_settings().mail_from)[1],
            to_addresses=[contact.email],
            subject=subject,
            body_text=text_body,
            body_html=html_body,
            attachment_metadata=[],
            raw_sha256=parsed.raw_sha256,
            is_history=False,
            received_at=at,
        )
    )
    recipient.case_id = target_case.id if target_case else None
    recipient.outbox_id = outbox.id
    recipient.status = "QUEUED"
    recipient.snapshot_json = {
        **snapshot,
        "source_case_id": snapshot.get("source_case_id") or source_case_id,
    }
    session.add(
        AuditEvent(
            case_id=target_case.id if target_case else None,
            actor="reactivation_scheduler",
            event_type="reactivation.outbox_frozen",
            data={
                "campaign_id": campaign.id,
                "recipient_id": recipient.id,
                "outbox_id": outbox.id,
                "message_id": message_id,
            },
        )
    )
    await session.commit()
    return True


async def _sync_delivery_states(session: AsyncSession) -> bool:
    changed = False
    rows = (
        await session.execute(
            select(ReactivationRecipient, Outbox)
            .join(Outbox, ReactivationRecipient.outbox_id == Outbox.id)
            .where(ReactivationRecipient.status == "QUEUED")
        )
    ).all()
    for recipient, outbox in rows:
        if outbox.status == DeliveryStatus.SENT:
            recipient.status = "SENT"
            recipient.sent_at = outbox.sent_at
            changed = True
        elif outbox.status == DeliveryStatus.CANCELLED and outbox.last_error not in {
            "reactivation campaign paused"
        }:
            recipient.status = "SKIPPED"
            recipient.exclusion_reason = (
                "EMAIL_UNDELIVERABLE"
                if (outbox.last_error or "").startswith("recipient preflight blocked:")
                else "OUTBOX_CANCELLED"
            )
            changed = True
    running = (
        (
            await session.execute(
                select(ReactivationCampaign).where(ReactivationCampaign.status == "RUNNING")
            )
        )
        .scalars()
        .all()
    )
    for campaign in running:
        remaining = await session.scalar(
            select(func.count())
            .select_from(ReactivationRecipient)
            .where(
                ReactivationRecipient.campaign_id == campaign.id,
                ReactivationRecipient.selected.is_(True),
                ReactivationRecipient.status.in_(["SCHEDULED", "QUEUED"]),
            )
        )
        if not remaining:
            campaign.status = "COMPLETED"
            campaign.completed_at = datetime.now(UTC)
            changed = True
    if changed:
        await session.commit()
    return changed


async def ensure_reactivation_dispatch(
    session: AsyncSession,
    settings: Settings | None = None,
    *,
    at: datetime | None = None,
) -> bool:
    settings = settings or get_settings()
    if not settings.reactivation_enabled:
        return False
    changed = await _sync_delivery_states(session)
    # Never materialize bulk mail while a real inbound message is waiting for
    # AI analysis or human routing. The normal job worker clears that backlog
    # first and this scheduler resumes automatically afterwards.
    inbound_backlog = await session.scalar(
        select(Job.id)
        .where(
            Job.kind == "process_inbound",
            Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
        )
        .limit(1)
    )
    if inbound_backlog is not None:
        return changed
    observed_at = at or datetime.now(UTC)
    row = await session.execute(
        select(ReactivationRecipient, ReactivationCampaign)
        .join(ReactivationCampaign, ReactivationRecipient.campaign_id == ReactivationCampaign.id)
        .where(
            ReactivationCampaign.status == "RUNNING",
            ReactivationRecipient.status == "SCHEDULED",
            ReactivationRecipient.selected.is_(True),
            ReactivationRecipient.scheduled_for <= observed_at,
        )
        .order_by(ReactivationRecipient.scheduled_for, ReactivationRecipient.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    result = row.first()
    if result is None:
        return changed
    recipient, campaign = result
    return await _queue_recipient(session, recipient, campaign, at=observed_at) or changed


async def reactivation_send_guard(
    session: AsyncSession,
    outbox: Outbox,
    *,
    settings: Settings | None = None,
    at: datetime | None = None,
) -> SendGuard:
    if outbox.message_kind != "REACTIVATION":
        return SendGuard("ALLOW")
    settings = settings or get_settings()
    observed_at = at or datetime.now(UTC)
    result = (
        await session.execute(
            select(ReactivationRecipient, ReactivationCampaign, Contact, Customer)
            .join(ReactivationCampaign, ReactivationRecipient.campaign_id == ReactivationCampaign.id)
            .join(Contact, ReactivationRecipient.contact_id == Contact.id)
            .join(Customer, ReactivationRecipient.customer_id == Customer.id)
            .where(ReactivationRecipient.outbox_id == outbox.id)
        )
    ).first()
    if result is None:
        return SendGuard("BLOCK", "reactivation recipient record is missing")
    recipient, campaign, contact, customer = result
    if campaign.status != "RUNNING":
        return SendGuard("BLOCK", f"reactivation campaign is {campaign.status.lower()}")
    if recipient.status != "QUEUED" or not recipient.selected:
        return SendGuard("BLOCK", "reactivation recipient is no longer selected")
    address_status = await session.get(EmailAddressStatus, contact.email.strip().casefold())
    if customer.do_not_contact or contact.suppressed or (address_status and address_status.suppressed):
        recipient.status = "SKIPPED"
        recipient.exclusion_reason = "CONTACT_SUPPRESSED"
        return SendGuard("BLOCK", "reactivation contact is suppressed")
    if contact.last_contact_at is not None and (
        recipient.last_contact_at is None or contact.last_contact_at > recipient.last_contact_at
    ):
        recipient.status = "SKIPPED"
        recipient.exclusion_reason = "ACTIVITY_AFTER_SELECTION"
        return SendGuard("BLOCK", "contact activity changed after reactivation selection")
    workflow_reason = await _contact_workflow_block_reason(
        session,
        contact.id,
        at=observed_at,
        exclude_outbox_id=outbox.id,
    )
    if workflow_reason is not None:
        recipient.status = "SKIPPED"
        recipient.exclusion_reason = workflow_reason
        return SendGuard("BLOCK", f"contact workflow changed: {workflow_reason}")
    newer_inbound = await session.scalar(
        select(EmailMessage.id)
        .where(
            EmailMessage.contact_id == contact.id,
            EmailMessage.direction == "INBOUND",
            EmailMessage.is_bounce.is_(False),
            EmailMessage.is_automated_reply.is_(False),
            EmailMessage.received_at > outbox.created_at,
        )
        .limit(1)
    )
    if newer_inbound is not None:
        recipient.status = "SKIPPED"
        recipient.exclusion_reason = "REPLIED_BEFORE_SEND"
        return SendGuard("BLOCK", "customer replied before reactivation delivery")
    next_window = next_campaign_window(campaign, observed_at)
    if next_window is not None:
        return SendGuard("DEFER", "outside reactivation business hours", next_window)

    timezone = ZoneInfo(campaign.timezone)
    local = observed_at.astimezone(timezone)
    day_start = datetime.combine(local.date(), time.min, timezone).astimezone(UTC)
    day_end = day_start + timedelta(days=1)
    campaign_sent_today = await session.scalar(
        select(func.count())
        .select_from(ReactivationRecipient)
        .where(
            ReactivationRecipient.campaign_id == campaign.id,
            ReactivationRecipient.sent_at >= day_start,
            ReactivationRecipient.sent_at < day_end,
        )
    )
    all_sent_today = await session.scalar(
        select(func.count())
        .select_from(ReactivationRecipient)
        .where(
            ReactivationRecipient.sent_at >= day_start,
            ReactivationRecipient.sent_at < day_end,
        )
    )
    if (campaign_sent_today or 0) >= campaign.daily_limit or (
        all_sent_today or 0
    ) >= settings.reactivation_max_sends_per_day:
        tomorrow = datetime.combine(
            _next_weekday(local.date() + timedelta(days=1)),
            time(campaign.send_window_start_hour),
            timezone,
        ).astimezone(UTC)
        return SendGuard("DEFER", "reactivation daily limit reached", tomorrow)
    return SendGuard("ALLOW")


async def record_reactivation_reply(
    session: AsyncSession,
    email_row: EmailMessage,
    *,
    recipient_id: int | None = None,
    commit: bool = True,
) -> bool:
    if email_row.direction != "INBOUND" or email_row.is_bounce or email_row.is_automated_reply:
        return False
    contact_id = email_row.contact_id
    if contact_id is None and email_row.from_address:
        contact_id = await session.scalar(
            select(Contact.id).where(func.lower(Contact.email) == email_row.from_address.strip().casefold())
        )
    if contact_id is None:
        return False
    if recipient_id is not None:
        sent = await session.scalar(
            select(ReactivationRecipient)
            .where(
                ReactivationRecipient.id == recipient_id,
                ReactivationRecipient.contact_id == contact_id,
                ReactivationRecipient.status.in_(["QUEUED", "SENT"]),
            )
            .with_for_update()
        )
        if sent is not None and sent.sent_at is None and sent.outbox_id is not None:
            sent_outbox = await session.get(Outbox, sent.outbox_id)
            sent.sent_at = sent_outbox.sent_at if sent_outbox else None
    else:
        sent = await session.scalar(
            select(ReactivationRecipient)
            .where(
                ReactivationRecipient.contact_id == contact_id,
                ReactivationRecipient.status == "SENT",
                ReactivationRecipient.sent_at.is_not(None),
                ReactivationRecipient.sent_at <= email_row.received_at,
            )
            .order_by(ReactivationRecipient.sent_at.desc(), ReactivationRecipient.id.desc())
            .limit(1)
        )
    if sent is None:
        return False
    sent.status = "REPLIED"
    sent.replied_at = email_row.received_at
    future = (
        (
            await session.execute(
                select(ReactivationRecipient).where(
                    ReactivationRecipient.contact_id == contact_id,
                    ReactivationRecipient.id != sent.id,
                    ReactivationRecipient.status.in_(["SCHEDULED", "QUEUED"]),
                )
            )
        )
        .scalars()
        .all()
    )
    for recipient in future:
        recipient.status = "SKIPPED"
        recipient.exclusion_reason = "CUSTOMER_REPLIED"
        if recipient.outbox_id:
            queued = await session.get(Outbox, recipient.outbox_id)
            if queued and queued.status in {DeliveryStatus.PENDING, DeliveryStatus.FAILED}:
                queued.status = DeliveryStatus.CANCELLED
                queued.last_error = "customer replied before reactivation"
    session.add(
        AuditEvent(
            case_id=email_row.case_id or sent.case_id,
            actor="imap",
            event_type="reactivation.customer_replied",
            data={
                "campaign_id": sent.campaign_id,
                "recipient_id": sent.id,
                "email_id": email_row.id,
                "cancelled_future": len(future),
            },
        )
    )
    if commit:
        await session.commit()
    return True
