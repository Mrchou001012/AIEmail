"""Human resolution of failed recipients and customer contact endpoints."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import (
    AuditEvent,
    CaseStatus,
    Contact,
    Customer,
    DeliveryStatus,
    EmailAddressStatus,
    Handoff,
    Outbox,
    ReactivationRecipient,
    SalesCase,
)
from app.delivery.deliverability import validate_address_format
from app.delivery.delivery_safety import (
    DELIVERABILITY_BLOCK_STATUSES,
    cancel_pending_recipient_delivery,
    ensure_customer_contact,
    suppress_email_address,
)
from app.domain import HandoffReason
from app.handoffs.agent_runtime import finalize_handoff_agent_run


async def resolve_deliverability_handoff(
    session: AsyncSession,
    *,
    handoff_id: int,
    actor: str,
    note: str = "",
) -> Handoff:
    """Resolve an old deliverability handoff by suppressing only its recipient."""
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None:
        raise ValueError("handoff not found")
    if handoff.status != "OPEN":
        raise ValueError("handoff is already resolved")
    if handoff.reason_code != HandoffReason.EMAIL_DELIVERABILITY.value:
        raise ValueError("handoff is not an email deliverability review")

    facts = dict(handoff.extracted_facts or {})
    recipient = str(facts.get("recipient") or "").strip().casefold()
    preflight_status = str(facts.get("preflight_status") or "").strip().upper()
    if not recipient:
        raise ValueError("deliverability handoff has no recipient")
    address_status = await session.get(EmailAddressStatus, recipient)
    if preflight_status not in DELIVERABILITY_BLOCK_STATUSES and not (address_status and address_status.suppressed):
        raise ValueError("deliverability result is not a permanent recipient block")

    suppression_reason = (
        address_status.suppression_reason
        if address_status and address_status.suppressed and address_status.suppression_reason
        else f"PREFLIGHT_{preflight_status or 'UNDELIVERABLE'}"
    )
    updated_status = await suppress_email_address(
        session,
        recipient,
        reason=suppression_reason,
    )
    if preflight_status:
        updated_status.preflight_status = preflight_status

    outbox_id = facts.get("outbox_id")
    outbox = await session.get(Outbox, outbox_id) if isinstance(outbox_id, int) else None
    if outbox is not None and outbox.status in {
        DeliveryStatus.PENDING,
        DeliveryStatus.FAILED,
        DeliveryStatus.CLAIMED,
        DeliveryStatus.UNKNOWN,
    }:
        outbox.status = DeliveryStatus.CANCELLED
        outbox.last_error = "recipient marked permanently undeliverable by operator"

    campaign_recipient = (
        await session.scalar(select(ReactivationRecipient).where(ReactivationRecipient.outbox_id == outbox_id))
        if isinstance(outbox_id, int)
        else None
    )
    if campaign_recipient is not None and campaign_recipient.status not in {
        "SENT",
        "REPLIED",
    }:
        campaign_recipient.status = "SKIPPED"
        campaign_recipient.exclusion_reason = "EMAIL_UNDELIVERABLE"

    case = await session.get(SalesCase, handoff.case_id) if handoff.case_id else None
    if case is not None and case.status not in {
        CaseStatus.CLOSED_WON,
        CaseStatus.CLOSED_LOST,
    }:
        case.status = CaseStatus.PAUSED
    handoff.status = "RESOLVED"
    handoff.resolution_note = note.strip() or f"Recipient {recipient} marked permanently undeliverable"
    await finalize_handoff_agent_run(
        session,
        handoff_id=handoff.id,
        actor=actor,
        outcome="recipient-suppressed",
        cancelled=True,
    )
    session.add(
        AuditEvent(
            case_id=handoff.case_id,
            actor=actor,
            event_type="handoff.deliverability_recipient_suppressed",
            data={
                "handoff_id": handoff.id,
                "outbox_id": outbox_id,
                "recipient": recipient,
                "preflight_status": preflight_status,
                "suppression_reason": suppression_reason,
            },
        )
    )
    await session.commit()
    return handoff


async def suppress_contact_endpoint(
    session: AsyncSession,
    *,
    contact_id: int,
    actor: str,
    note: str = "",
) -> Contact:
    """Suppress one exact email endpoint without affecting sibling contacts."""
    contact = await session.get(Contact, contact_id)
    if contact is None:
        raise ValueError("contact not found")
    recipient = contact.email.strip().casefold()
    await suppress_email_address(
        session,
        recipient,
        reason="MANUAL_CONTACT_ENDPOINT_SUPPRESSION",
    )
    outbox_ids = await cancel_pending_recipient_delivery(
        session,
        recipient=recipient,
        reason="recipient endpoint manually marked undeliverable",
    )
    contact_ids = (await session.execute(select(Contact.id).where(func.lower(Contact.email) == recipient))).scalars().all()
    cases = (
        (
            await session.execute(
                select(SalesCase).where(
                    SalesCase.contact_id.in_(contact_ids),
                    SalesCase.status.not_in([CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST]),
                )
            )
        )
        .scalars()
        .all()
    )
    for case in cases:
        case.status = CaseStatus.PAUSED
    session.add(
        AuditEvent(
            case_id=None,
            actor=actor,
            event_type="contact.endpoint_suppressed",
            data={
                "contact_id": contact.id,
                "customer_id": contact.customer_id,
                "recipient": recipient,
                "cancelled_outbox_ids": outbox_ids,
                "paused_case_ids": [case.id for case in cases],
                "note": note.strip(),
            },
        )
    )
    await session.commit()
    return contact


async def add_customer_contact_endpoint(
    session: AsyncSession,
    *,
    customer_id: int,
    email: str,
    name: str,
    actor: str,
    note: str = "",
) -> tuple[Contact, bool]:
    """Add a separately deliverable address to one customer."""
    customer = await session.get(Customer, customer_id)
    if customer is None:
        raise ValueError("customer not found")
    contact, created = await ensure_customer_contact(
        session,
        customer=customer,
        email=email,
        name=name,
        actor=actor,
        source="contact_directory",
    )
    session.add(
        AuditEvent(
            case_id=None,
            actor=actor,
            event_type=("contact.endpoint_created" if created else "contact.endpoint_reused"),
            data={
                "contact_id": contact.id,
                "customer_id": customer.id,
                "email": contact.email,
                "note": note.strip(),
            },
        )
    )
    await session.commit()
    return contact, created


async def replace_handoff_recipient(
    session: AsyncSession,
    *,
    handoff_id: int,
    new_email: str,
    new_name: str,
    actor: str,
    note: str = "",
    resume_case: bool = False,
) -> tuple[Handoff, Contact, bool]:
    """Retire a failed endpoint and move this handoff to a replacement."""
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None:
        raise ValueError("handoff not found")
    if handoff.status != "OPEN":
        raise ValueError("handoff is already resolved")
    if handoff.reason_code not in {
        HandoffReason.EMAIL_DELIVERABILITY.value,
        HandoffReason.BOUNCE_REVIEW.value,
    }:
        raise ValueError("handoff is not a recipient deliverability review")

    facts = dict(handoff.extracted_facts or {})
    old_email = str(facts.get("recipient") or "").strip().casefold()
    if not old_email:
        raise ValueError("deliverability handoff has no failed recipient")
    new_format = validate_address_format(new_email)
    if new_format.valid and new_format.normalized == old_email:
        raise ValueError("replacement email must differ from the failed address")

    case = await session.get(SalesCase, handoff.case_id) if handoff.case_id else None
    old_contact = await session.get(Contact, case.contact_id) if case is not None else None
    outbox_id = facts.get("outbox_id")
    campaign_recipient = (
        await session.scalar(select(ReactivationRecipient).where(ReactivationRecipient.outbox_id == outbox_id))
        if isinstance(outbox_id, int)
        else None
    )
    if old_contact is None and campaign_recipient is not None:
        old_contact = await session.get(Contact, campaign_recipient.contact_id)
    if old_contact is None:
        matches = (
            (await session.execute(select(Contact).where(func.lower(Contact.email) == old_email).order_by(Contact.id))).scalars().all()
        )
        if len(matches) != 1:
            raise ValueError("failed recipient does not map to exactly one customer contact")
        old_contact = matches[0]
    if old_contact.email.strip().casefold() != old_email:
        raise ValueError("handoff case contact does not match the failed recipient")
    customer = await session.get(Customer, old_contact.customer_id)
    if customer is None:
        raise ValueError("customer not found")

    new_contact, created = await ensure_customer_contact(
        session,
        customer=customer,
        email=new_email,
        name=new_name.strip() or old_contact.name,
        actor=actor,
        source="deliverability_handoff_replacement",
        replaces_contact=old_contact,
    )
    await suppress_email_address(
        session,
        old_email,
        reason="REPLACED_UNDELIVERABLE_ENDPOINT",
        source_email_id=handoff.source_email_id,
    )
    cancelled_outbox_ids = await cancel_pending_recipient_delivery(
        session,
        recipient=old_email,
        reason="recipient replaced after delivery failure",
    )
    old_metadata = dict(old_contact.metadata_json or {})
    old_metadata["replacement_contact_id"] = new_contact.id
    old_metadata["replacement_email"] = new_contact.email
    old_metadata["replaced_at"] = datetime.now(UTC).isoformat()
    old_metadata["replaced_by"] = actor
    old_contact.metadata_json = old_metadata

    if case is not None:
        case.contact_id = new_contact.id
        case.customer_id = customer.id
        if resume_case and case.status not in {
            CaseStatus.CLOSED_WON,
            CaseStatus.CLOSED_LOST,
        }:
            case.status = CaseStatus.ACTIVE
        elif case.status not in {CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST}:
            case.status = CaseStatus.PAUSED
    if campaign_recipient is not None and campaign_recipient.status not in {
        "SENT",
        "REPLIED",
    }:
        campaign_recipient.status = "SKIPPED"
        campaign_recipient.exclusion_reason = "EMAIL_REPLACED"

    handoff.status = "RESOLVED"
    handoff.resolution_note = note.strip() or (f"Replaced undeliverable recipient {old_email} with {new_contact.email}")
    await finalize_handoff_agent_run(
        session,
        handoff_id=handoff.id,
        actor=actor,
        outcome="recipient-replaced",
        cancelled=True,
    )
    session.add(
        AuditEvent(
            case_id=handoff.case_id,
            actor=actor,
            event_type="handoff.deliverability_recipient_replaced",
            data={
                "handoff_id": handoff.id,
                "customer_id": customer.id,
                "old_contact_id": old_contact.id,
                "old_email": old_email,
                "new_contact_id": new_contact.id,
                "new_email": new_contact.email,
                "created_contact": created,
                "cancelled_outbox_ids": cancelled_outbox_ids,
                "case_resumed": bool(case is not None and resume_case),
            },
        )
    )
    await session.commit()
    return handoff, new_contact, created
