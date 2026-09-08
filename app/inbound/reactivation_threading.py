"""Reactivation threading workflow."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.service_core import CaseLessReactivationParent
from app.db import Contact, Customer, DeliveryStatus, Outbox, ReactivationRecipient
from app.delivery.delivery_safety import _ensure_customer_contact
from app.mail import ParsedEmail
from app.research.company_research_context import _nonfree_email_domain


async def _case_less_reactivation_parent(
    session: AsyncSession,
    parsed: ParsedEmail,
) -> CaseLessReactivationParent | None:
    """Find a verified case-less reactivation message referenced by this reply.

    Exact sender matching remains the primary path. A changed sender address is
    accepted only when one exact ``In-Reply-To`` parent exists and both old and
    new addresses share the same non-free corporate domain. The new endpoint is
    stored as a separate contact; the historical recipient is never overwritten.
    """

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
    exact_matches = [
        CaseLessReactivationParent(
            outbox=outbox,
            recipient=recipient,
            original_contact=contact,
            reply_contact=contact,
        )
        for outbox, recipient, contact in rows
        if outbox.recipient.strip().casefold() == sender and contact.email.strip().casefold() == sender
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]
    if exact_matches or parsed.in_reply_to is None or len(rows) != 1:
        return None

    outbox, recipient, original_contact = rows[0]
    original_recipient = outbox.recipient.strip().casefold()
    original_contact_email = original_contact.email.strip().casefold()
    sender_domain = _nonfree_email_domain(sender)
    if (
        sender_domain is None
        or sender_domain != _nonfree_email_domain(original_recipient)
        or sender_domain != _nonfree_email_domain(original_contact_email)
        or original_recipient != original_contact_email
    ):
        return None
    customer = await session.get(Customer, recipient.customer_id)
    if customer is None or customer.id != original_contact.customer_id:
        return None
    try:
        reply_contact, created = await _ensure_customer_contact(
            session,
            customer=customer,
            email=sender,
            name="Customer",
            actor="thread_resolver",
            source="exact_reactivation_message_id_same_company_domain",
        )
    except ValueError:
        # A suppressed or cross-customer identity is never silently reassigned.
        return None

    metadata = dict(reply_contact.metadata_json or {})
    thread_links = list(metadata.get("reactivation_thread_links") or [])
    link = {
        "source": "exact_reactivation_message_id_same_company_domain",
        "original_contact_id": original_contact.id,
        "original_email": original_contact_email,
        "parent_outbox_id": outbox.id,
        "parent_message_id": outbox.message_id,
        "matched_domain": sender_domain,
        "linked_at": datetime.now(UTC).isoformat(),
    }
    if not any(item.get("parent_outbox_id") == outbox.id for item in thread_links if isinstance(item, dict)):
        thread_links.append(link)
    metadata["reactivation_thread_links"] = thread_links[-20:]
    reply_contact.metadata_json = metadata
    return CaseLessReactivationParent(
        outbox=outbox,
        recipient=recipient,
        original_contact=original_contact,
        reply_contact=reply_contact,
        sender_changed=True,
        reply_contact_created=created,
        matched_domain=sender_domain,
    )
