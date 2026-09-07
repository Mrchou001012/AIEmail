"""Recipient deliverability and contact-endpoint safety primitives.

This module owns the reusable address-state, MX-preflight, suppression, and
contact-endpoint helpers. Business workflow orchestration remains in the
calling domain services.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime import finalize_handoff_agent_run
from app.db import (
    AuditEvent,
    CaseStatus,
    Contact,
    Customer,
    DeliveryStatus,
    EmailAddressStatus,
    EmailDomainStatus,
    Handoff,
    Outbox,
    ReactivationRecipient,
    SalesCase,
)
from app.deliverability import MXResult, MXStatus, lookup_mx, validate_address_format
from app.domain import HandoffReason
from app.settings import Settings

AUTO_SUPPRESS_PREFLIGHT_STATUSES = frozenset(
    {
        MXStatus.NO_DOMAIN.value,
        MXStatus.NULL_MX.value,
    }
)
DELIVERABILITY_BLOCK_STATUSES = frozenset(
    {
        *AUTO_SUPPRESS_PREFLIGHT_STATUSES,
        "INVALID_FORMAT",
        MXStatus.NO_MX.value,
        "SUPPRESSED",
    }
)


async def email_address_status(
    session: AsyncSession,
    email_address: str,
) -> EmailAddressStatus:
    normalized = email_address.strip().casefold()[:320]
    row = await session.get(EmailAddressStatus, normalized)
    if row is None:
        row = EmailAddressStatus(email=normalized, suppressed=False)
        session.add(row)
        await session.flush()
    return row


def recipient_delivery_gate_key(email_address: str) -> int:
    """Return a stable signed bigint key for PostgreSQL advisory locking."""
    normalized = email_address.strip().casefold()[:320]
    return int.from_bytes(
        hashlib.sha256(normalized.encode("utf-8")).digest()[:8],
        byteorder="big",
        signed=True,
    )


async def lock_recipient_delivery_gate(
    session: AsyncSession,
    email_address: str,
) -> None:
    """Serialize final delivery and endpoint suppression for one address.

    The transaction holding this lock is the linearization boundary: a
    suppression committed before the final delivery transaction acquires the
    lock blocks the message; a suppression that waits behind an in-progress
    SMTP transaction is applied only after that already-started delivery.
    """
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    await session.execute(
        select(func.pg_advisory_xact_lock(recipient_delivery_gate_key(email_address)))
    )


async def suppress_email_address(
    session: AsyncSession,
    email_address: str,
    *,
    reason: str,
    source_email_id: int | None = None,
    bounce_type: str | None = None,
    diagnostic: str | None = None,
) -> EmailAddressStatus:
    await lock_recipient_delivery_gate(session, email_address)
    now = datetime.now(UTC)
    status = await email_address_status(session, email_address)
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


async def recipient_preflight(
    session: AsyncSession,
    recipient: str,
    settings: Settings,
    *,
    mx_lookup: Callable[..., MXResult] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Return ALLOW, BLOCK, or DEFER plus a stable detail and audit facts."""
    if not settings.email_preflight_enabled:
        return "ALLOW", "recipient preflight disabled", {
            "preflight_status": "DISABLED"
        }

    now = datetime.now(UTC)
    format_result = validate_address_format(recipient)
    status = await email_address_status(session, format_result.normalized)
    status.format_valid = format_result.valid
    status.domain = format_result.domain
    status.last_preflight_at = now
    if status.suppressed:
        status.preflight_status = "SUPPRESSED"
        detail = (
            "recipient permanently suppressed: "
            f"{status.suppression_reason or 'unspecified'}"
        )
        status.last_preflight_detail = detail
        return "BLOCK", detail, {
            "recipient": status.email,
            "preflight_status": "SUPPRESSED",
            "suppression_reason": status.suppression_reason,
            "auto_suppressed": True,
        }
    if not format_result.valid:
        detail = f"invalid recipient format: {format_result.error or 'invalid address'}"
        status.preflight_status = "INVALID_FORMAT"
        status.last_preflight_detail = detail
        await suppress_email_address(session, status.email, reason="INVALID_FORMAT")
        return "BLOCK", detail, {
            "recipient": status.email,
            "preflight_status": "INVALID_FORMAT",
            "format_error": format_result.error,
            "suppression_reason": "INVALID_FORMAT",
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
            mx_lookup or lookup_mx,
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
    if mx_result.status.value in AUTO_SUPPRESS_PREFLIGHT_STATUSES:
        suppression_reason = f"PREFLIGHT_{mx_result.status.value}"
        await suppress_email_address(session, status.email, reason=suppression_reason)
        facts["suppression_reason"] = suppression_reason
        facts["auto_suppressed"] = True
    return "BLOCK", mx_result.error or "recipient domain cannot receive email", facts


def contact_metadata_with_manual_source(
    contact: Contact,
    *,
    actor: str,
    source: str,
    replaces_contact_id: int | None = None,
    replaces_email: str | None = None,
) -> None:
    metadata = dict(contact.metadata_json or {})
    metadata.setdefault("identity_kind", "EMAIL_ENDPOINT")
    metadata.setdefault("identity_verified", False)
    manual_entries = list(metadata.get("manual_entries") or [])
    entry: dict[str, Any] = {
        "actor": actor,
        "source": source,
        "created_at": datetime.now(UTC).isoformat(),
    }
    if replaces_contact_id is not None:
        entry["replaces_contact_id"] = replaces_contact_id
    if replaces_email:
        entry["replaces_email"] = replaces_email
    manual_entries.append(entry)
    metadata["manual_entries"] = manual_entries[-20:]
    contact.metadata_json = metadata


async def ensure_customer_contact(
    session: AsyncSession,
    *,
    customer: Customer,
    email: str,
    name: str,
    actor: str,
    source: str,
    replaces_contact: Contact | None = None,
) -> tuple[Contact, bool]:
    format_result = validate_address_format(email)
    if not format_result.valid:
        raise ValueError(
            f"invalid email address: {format_result.error or 'invalid format'}"
        )
    normalized = format_result.normalized
    existing = (
        (
            await session.execute(
                select(Contact)
                .where(func.lower(Contact.email) == normalized)
                .order_by(Contact.id)
            )
        )
        .scalars()
        .all()
    )
    foreign = [row for row in existing if row.customer_id != customer.id]
    if foreign:
        raise ValueError(
            "email address already belongs to another customer; "
            "review that customer before creating a duplicate identity"
        )
    same_customer = next(
        (row for row in existing if row.customer_id == customer.id),
        None,
    )
    address_status = await session.get(EmailAddressStatus, normalized)
    if same_customer is not None:
        if same_customer.suppressed or (
            address_status is not None and address_status.suppressed
        ):
            raise ValueError(
                "email address already exists but is permanently suppressed"
            )
        return same_customer, False

    contact = Contact(
        customer_id=customer.id,
        name=name.strip() or "Customer",
        email=normalized,
        language=(replaces_contact.language if replaces_contact else customer.language)
        or "en",
        suppressed=False,
        metadata_json={},
        first_contact_at=(
            replaces_contact.first_contact_at if replaces_contact else None
        ),
        last_contact_at=None,
    )
    contact_metadata_with_manual_source(
        contact,
        actor=actor,
        source=source,
        replaces_contact_id=replaces_contact.id if replaces_contact else None,
        replaces_email=replaces_contact.email if replaces_contact else None,
    )
    session.add(contact)
    await session.flush()
    return contact, True


async def cancel_pending_recipient_delivery(
    session: AsyncSession,
    *,
    recipient: str,
    reason: str,
) -> list[int]:
    rows = (
        (
            await session.execute(
                select(Outbox)
                .where(
                    func.lower(Outbox.recipient) == recipient,
                    Outbox.status.in_(
                        [
                            DeliveryStatus.PENDING,
                            DeliveryStatus.FAILED,
                            DeliveryStatus.CLAIMED,
                            DeliveryStatus.UNKNOWN,
                        ]
                    ),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    outbox_ids: list[int] = []
    for row in rows:
        row.status = DeliveryStatus.CANCELLED
        row.last_error = reason
        outbox_ids.append(row.id)
    if outbox_ids:
        campaign_rows = (
            (
                await session.execute(
                    select(ReactivationRecipient).where(
                        ReactivationRecipient.outbox_id.in_(outbox_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        for campaign_row in campaign_rows:
            if campaign_row.status not in {"SENT", "REPLIED"}:
                campaign_row.status = "SKIPPED"
                campaign_row.exclusion_reason = "EMAIL_UNDELIVERABLE"
    return outbox_ids


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
    if preflight_status not in DELIVERABILITY_BLOCK_STATUSES and not (
        address_status and address_status.suppressed
    ):
        raise ValueError("deliverability result is not a permanent recipient block")

    suppression_reason = (
        address_status.suppression_reason
        if address_status
        and address_status.suppressed
        and address_status.suppression_reason
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
        await session.scalar(
            select(ReactivationRecipient).where(
                ReactivationRecipient.outbox_id == outbox_id
            )
        )
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
    handoff.resolution_note = (
        note.strip() or f"Recipient {recipient} marked permanently undeliverable"
    )
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
    contact_ids = (
        (
            await session.execute(
                select(Contact.id).where(func.lower(Contact.email) == recipient)
            )
        )
        .scalars()
        .all()
    )
    cases = (
        (
            await session.execute(
                select(SalesCase).where(
                    SalesCase.contact_id.in_(contact_ids),
                    SalesCase.status.not_in(
                        [CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST]
                    ),
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
            event_type=(
                "contact.endpoint_created" if created else "contact.endpoint_reused"
            ),
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
        await session.scalar(
            select(ReactivationRecipient).where(
                ReactivationRecipient.outbox_id == outbox_id
            )
        )
        if isinstance(outbox_id, int)
        else None
    )
    if old_contact is None and campaign_recipient is not None:
        old_contact = await session.get(Contact, campaign_recipient.contact_id)
    if old_contact is None:
        matches = (
            (
                await session.execute(
                    select(Contact)
                    .where(func.lower(Contact.email) == old_email)
                    .order_by(Contact.id)
                )
            )
            .scalars()
            .all()
        )
        if len(matches) != 1:
            raise ValueError(
                "failed recipient does not map to exactly one customer contact"
            )
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
    handoff.resolution_note = note.strip() or (
        f"Replaced undeliverable recipient {old_email} with {new_contact.email}"
    )
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


# Compatibility aliases for existing callers while app.services is decomposed.
_email_address_status = email_address_status
_recipient_delivery_gate_key = recipient_delivery_gate_key
_lock_recipient_delivery_gate = lock_recipient_delivery_gate
_suppress_email_address = suppress_email_address
_recipient_preflight = recipient_preflight
_contact_metadata_with_manual_source = contact_metadata_with_manual_source
_ensure_customer_contact = ensure_customer_contact
_cancel_pending_recipient_delivery = cancel_pending_recipient_delivery
