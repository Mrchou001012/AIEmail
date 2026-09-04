"""Database actions used by reviewed inbound-disposition workflows."""

from __future__ import annotations

import html
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parseaddr

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime import finalize_handoff_agent_run
from app.ai import explicit_product_list_requested
from app.db import (
    AgentRun,
    AgentStep,
    AssistanceRequest,
    CaseStage,
    CaseStatus,
    Contact,
    ContactReferral,
    Customer,
    DeliveryStatus,
    EmailMessage,
    Handoff,
    Job,
    JobStatus,
    Outbox,
    SalesCase,
)
from app.deliverability import validate_address_format
from app.disposition_resolution import same_company_domain as _same_company_domain
from app.domain import HandoffReason
from app.email_identity import reply_contact_name
from app.imports import load_content
from app.inbound_disposition import InboundDisposition, InboundDispositionType
from app.mail import build_message, normalized_subject, parse_mime
from app.settings import Settings


async def _save_referrals(
    session: AsyncSession,
    *,
    row: EmailMessage,
    disposition: InboundDisposition,
    contact: Contact | None,
    customer: Customer | None,
) -> list[ContactReferral]:
    saved: list[ContactReferral] = []
    for address in disposition.replacement_emails:
        source_location = (
            "recipient_header"
            if f"REPLACEMENT_FROM_RECIPIENT_HEADER:{address}"
            in disposition.normalization_notes
            else "authored_body"
        )
        existing = await session.scalar(
            select(ContactReferral).where(
                ContactReferral.source_email_id == row.id,
                func.lower(ContactReferral.referred_email) == address.casefold(),
            )
        )
        if existing is not None:
            saved.append(existing)
            continue
        referral = ContactReferral(
                source_email_id=row.id,
                customer_id=customer.id if customer else None,
                original_contact_id=contact.id if contact else None,
                referred_email=address,
                relationship_type=(
                    "TEMPORARY_BACKUP"
                    if disposition.disposition_type
                    is InboundDispositionType.TEMPORARY_ABSENCE
                    else "REPLACEMENT"
                ),
                status=(
                    "WAITING_FOR_FORWARDED_REPLY"
                    if disposition.forwarded_to_replacement
                    else "CANDIDATE"
                ),
                forwarded_already=disposition.forwarded_to_replacement,
                confidence=Decimal(str(disposition.confidence)),
                metadata_json={
                    "same_company_domain": _same_company_domain(
                        row.from_address,
                        address,
                    ),
                    "source_disposition": disposition.disposition_type.value,
                    "source_location": source_location,
                },
            )
        session.add(referral)
        saved.append(referral)
    await session.flush()
    return saved


async def _save_verified_reply_contact_referral(
    session: AsyncSession,
    *,
    row: EmailMessage,
    disposition: InboundDisposition,
    original_contact: Contact,
    reply_contact: Contact,
    customer: Customer,
) -> ContactReferral:
    existing = await session.scalar(
        select(ContactReferral).where(
            ContactReferral.source_email_id == row.id,
            func.lower(ContactReferral.referred_email)
            == reply_contact.email.strip().casefold(),
        )
    )
    if existing is not None:
        existing.customer_id = customer.id
        existing.original_contact_id = original_contact.id
        existing.new_contact_id = reply_contact.id
        existing.referred_name = reply_contact.name
        existing.status = "ACTIVE_CONTACT"
        existing.metadata_json = {
            **(existing.metadata_json or {}),
            "verified_reactivation_parent": True,
            "reply_contact_already_engaged": True,
        }
        return existing
    referral = ContactReferral(
        source_email_id=row.id,
        customer_id=customer.id,
        original_contact_id=original_contact.id,
        new_contact_id=reply_contact.id,
        referred_email=reply_contact.email.strip().casefold(),
        referred_name=reply_contact.name,
        relationship_type="REPLACEMENT",
        status="ACTIVE_CONTACT",
        forwarded_already=False,
        confidence=Decimal(str(disposition.confidence)),
        metadata_json={
            "same_company_domain": _same_company_domain(
                original_contact.email,
                reply_contact.email,
            ),
            "source_disposition": disposition.disposition_type.value,
            "verified_reactivation_parent": True,
            "reply_contact_already_engaged": True,
        },
    )
    session.add(referral)
    await session.flush()
    return referral


async def _ensure_reviewed_reply_contact(
    session: AsyncSession,
    *,
    row: EmailMessage,
    disposition: InboundDisposition,
    customer: Customer,
    original_contact: Contact,
    existing_sender_contact: Contact | None,
) -> tuple[Contact | None, bool]:
    """Create a same-company reply endpoint only after human confirmation."""

    sender = row.from_address.strip().casefold()
    if not sender or not _same_company_domain(sender, original_contact.email):
        return None, False
    if existing_sender_contact is not None:
        return (
            (existing_sender_contact, False)
            if existing_sender_contact.customer_id == customer.id
            else (None, False)
        )
    matches = (
        (
            await session.execute(
                select(Contact).where(func.lower(Contact.email) == sender).limit(2)
            )
        )
        .scalars()
        .all()
    )
    if matches:
        return (
            (matches[0], False)
            if len(matches) == 1 and matches[0].customer_id == customer.id
            else (None, False)
        )
    contact = Contact(
        customer_id=customer.id,
        name=reply_contact_name(None, disposition.authored_text),
        email=sender,
        language=original_contact.language,
        metadata_json={
            "source": "inbound_contact_referral",
            "source_email_id": row.id,
            "relationship": "verified_same_domain_reply",
        },
    )
    session.add(contact)
    await session.flush()
    return contact, True


async def _continue_reviewed_business_reply(
    session: AsyncSession,
    *,
    row: EmailMessage,
    disposition: InboundDisposition,
    customer: Customer,
    reply_contact: Contact,
) -> tuple[SalesCase | None, bool]:
    """Attach a reviewed mixed-intent reply to an open, draft-only case."""

    if not disposition.continue_business_processing:
        return None, False
    subject_key = normalized_subject(row.subject)[:255]
    candidates = (
        (
            await session.execute(
                select(SalesCase)
                .where(
                    SalesCase.contact_id == reply_contact.id,
                    SalesCase.subject_key == subject_key,
                    SalesCase.status.not_in(
                        [CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST]
                    ),
                )
                .order_by(SalesCase.id.desc())
                .limit(2)
            )
        )
        .scalars()
        .all()
    )
    if len(candidates) > 1:
        return None, False
    created = not candidates
    sales_case = candidates[0] if candidates else SalesCase(
        customer_id=customer.id,
        contact_id=reply_contact.id,
        product_id=None,
        currency="USD",
        stage=CaseStage.FOLLOW_UP,
        status=CaseStatus.WAITING_HUMAN,
        subject_key=subject_key,
        last_activity_at=row.received_at,
    )
    if created:
        session.add(sales_case)
        await session.flush()
    row.case_id = sales_case.id
    row.customer_id = customer.id
    row.contact_id = reply_contact.id
    handoff = await session.scalar(
        select(Handoff).where(Handoff.source_email_id == row.id)
    )
    if handoff is not None:
        handoff.case_id = sales_case.id
        if disposition.product_list_requested and explicit_product_list_requested(
            f"{row.subject}\n{row.body_text}"
        ):
            handoff.reason_code = HandoffReason.PRODUCT_LIST_REVIEW.value
            handoff.summary = (
                "Product-list request recovered from a reviewed personnel reply"
            )
        handoff.extracted_facts = {
            **(handoff.extracted_facts or {}),
            "inbound_disposition_continuation": {
                "source_email_id": row.id,
                "customer_id": customer.id,
                "contact_id": reply_contact.id,
                "case_id": sales_case.id,
                "product_list_requested": disposition.product_list_requested,
            },
        }
        run = await session.scalar(
            select(AgentRun).where(AgentRun.handoff_id == handoff.id)
        )
        if run is not None:
            run.case_id = sales_case.id
    return sales_case, created


async def _stage_referral_outreach(
    session: AsyncSession,
    *,
    row: EmailMessage,
    disposition: InboundDisposition,
    source_contact: Contact,
    customer: Customer,
    referrals: list[ContactReferral],
    settings: Settings,
) -> Outbox | None:
    """Queue one bounded referral introduction behind three independent gates."""

    if (
        not settings.referral_auto_contact_enabled
        or disposition.forwarded_to_replacement
        or len(referrals) != 1
        or customer.do_not_contact
        or customer.qualification_status == "NON_TARGET"
        or not customer.auto_send_allowed
    ):
        return None
    referral = referrals[0]
    target_email = referral.referred_email.strip().casefold()
    if (
        not _same_company_domain(source_contact.email, target_email)
        or not validate_address_format(target_email).valid
    ):
        return None
    contacts = (
        (
            await session.execute(
                select(Contact).where(func.lower(Contact.email) == target_email).limit(2)
            )
        )
        .scalars()
        .all()
    )
    if any(item.customer_id != customer.id for item in contacts):
        return None
    target = contacts[0] if contacts else None
    if target is None:
        target = Contact(
            customer_id=customer.id,
            name="Customer",
            email=target_email,
            language=source_contact.language,
            metadata_json={
                "source": "inbound_contact_referral",
                "source_email_id": row.id,
                "original_contact_id": source_contact.id,
            },
        )
        session.add(target)
        await session.flush()
    if target.suppressed or target.lifecycle_status == "DEPARTED":
        return None

    business_key = f"referral-outreach:{row.id}:{target_email}"
    existing = await session.scalar(
        select(Outbox).where(Outbox.business_key == business_key)
    )
    if existing is not None:
        referral.new_contact_id = target.id
        referral.status = "OUTREACH_QUEUED"
        return existing

    bundle = load_content(settings.content_dir)
    subject = "Lanya Chem product contact"
    business_text = (
        "Dear Sir/Madam,\n\n"
        "The previous contact at your company directed future correspondence to "
        "this address. This is Shreya from Lanya Chem.\n\n"
        "Could you please confirm whether you are the appropriate contact for "
        "product sourcing? If so, we can share our current product list and follow "
        "up on any products you require."
    )
    text_body = "\n".join([business_text, "", bundle.signature_text.strip()])
    html_body = (
        "<p>"
        + "</p><p>".join(
            html.escape(line) if line else "&nbsp;"
            for line in business_text.splitlines()
        )
        + "</p>"
        + bundle.signature_html
    )
    message_id, raw = build_message(
        from_address=settings.mail_from,
        recipient=target_email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        stable_key=business_key,
    )
    parsed = parse_mime(raw.encode("utf-8"))
    now = datetime.now(UTC)
    outbox = Outbox(
        case_id=None,
        quote_id=None,
        message_kind="REFERRAL_OUTREACH",
        business_key=business_key,
        message_id=message_id,
        recipient=target_email,
        raw_message=raw,
        status=DeliveryStatus.PENDING,
        available_at=now,
    )
    session.add(outbox)
    session.add(
        EmailMessage(
            case_id=None,
            customer_id=customer.id,
            contact_id=target.id,
            direction="OUTBOUND",
            message_id=message_id,
            from_address=parseaddr(settings.mail_from)[1],
            to_addresses=[target_email],
            subject=subject,
            body_text=text_body,
            body_html=html_body,
            attachment_metadata=[],
            raw_sha256=parsed.raw_sha256,
            is_history=False,
            received_at=now,
        )
    )
    referral.new_contact_id = target.id
    referral.status = "OUTREACH_QUEUED"
    await session.flush()
    return outbox


async def _resolve_terminal_handoff(
    session: AsyncSession,
    *,
    row: EmailMessage,
    disposition: InboundDisposition,
    actor: str,
) -> None:
    handoff = await session.scalar(
        select(Handoff).where(Handoff.source_email_id == row.id).with_for_update()
    )
    if handoff is None or handoff.status != "OPEN":
        return
    handoff.status = "RESOLVED"
    handoff.resolution_note = (
        f"Automatically resolved by inbound disposition: "
        f"{disposition.disposition_type.value}"
    )
    if handoff.dingtalk_status != "SENT":
        handoff.dingtalk_status = "CANCELLED"
    await finalize_handoff_agent_run(
        session,
        handoff_id=handoff.id,
        actor=actor,
        outcome=f"disposition-{disposition.disposition_type.value.casefold()}",
    )
    notify_job = await session.scalar(
        select(Job).where(Job.idempotency_key == f"handoff-notify:{handoff.id}")
    )
    if notify_job is not None and notify_job.status in {
        JobStatus.PENDING,
        JobStatus.FAILED,
    }:
        notify_job.status = JobStatus.DONE
        notify_job.last_error = "Cancelled: inbound disposition resolved the handoff"
        notify_job.locked_at = None
        notify_job.locked_by = None


async def _sync_open_handoff_facts(
    session: AsyncSession,
    *,
    row: EmailMessage,
    disposition: InboundDisposition,
) -> None:
    handoff = await session.scalar(
        select(Handoff).where(Handoff.source_email_id == row.id).with_for_update()
    )
    if handoff is None or handoff.status != "OPEN":
        return
    handoff.extracted_facts = {
        **(handoff.extracted_facts or {}),
        "inbound_disposition": {
            "type": disposition.disposition_type.value,
            "confidence": disposition.confidence,
            **disposition.metadata(),
        },
    }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.isoformat()


def _decimal_text(value: Decimal | None) -> str | None:
    return format(value.normalize(), "f") if value is not None else None


async def _lock_disposition_related_resources(
    session: AsyncSession,
    row: EmailMessage,
) -> None:
    await session.execute(
        select(ContactReferral)
        .where(ContactReferral.source_email_id == row.id)
        .with_for_update()
    )
    await session.execute(
        select(Outbox)
        .where(Outbox.business_key.like(f"referral-outreach:{row.id}:%"))
        .with_for_update()
    )
    handoff = await session.scalar(
        select(Handoff)
        .where(Handoff.source_email_id == row.id)
        .with_for_update()
    )
    if handoff is None:
        return
    run = await session.scalar(
        select(AgentRun).where(AgentRun.handoff_id == handoff.id).with_for_update()
    )
    await session.execute(
        select(Job)
        .where(Job.idempotency_key == f"handoff-notify:{handoff.id}")
        .with_for_update()
    )
    if run is None:
        return
    await session.execute(
        select(AssistanceRequest)
        .where(AssistanceRequest.run_id == run.id)
        .with_for_update()
    )
    await session.execute(
        select(AgentStep).where(AgentStep.run_id == run.id).with_for_update()
    )


