"""Resolve contacts, customers, return dates, and reviewed parent messages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from email.utils import parsedate_to_datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import (
    Contact,
    Customer,
    DeliveryStatus,
    EmailMessage,
    Outbox,
    ReactivationRecipient,
)
from app.deliverability import validate_address_format
from app.inbound_disposition import InboundDisposition, InboundDispositionType
from app.quoted_reply_resolution import resolve_quoted_outbound_parent

FREE_MAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "yahoo.com",
        "yahoo.co.in",
        "qq.com",
        "163.com",
        "126.com",
    }
)

MONTH_FORMATS = (
    "%B %d %Y",
    "%b %d %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d",
    "%b %d",
    "%d %B",
    "%d %b",
)

RECIPIENT_HEADER_REFERRAL_CUES = (
    re.compile(
        r"\b(?:i|we)\s+(?:have\s+)?(?:marked|kept|put)\s+"
        r"(?:a\s+)?(?:copy|cc)\s+to\b",
        re.I,
    ),
    re.compile(
        r"\b(?:i|we)\s+(?:have\s+)?(?:copied|cc(?:'d|ed)|included)\s+"
        r"[\w .,'’()/-]{0,100}\s+(?:in|on)\s+(?:this|the)\s+"
        r"(?:email|message|communication|thread)\b",
        re.I,
    ),
)


def email_domain(address: str | None) -> str | None:
    value = (address or "").strip().casefold()
    if "@" not in value:
        return None
    domain = value.rsplit("@", 1)[1].strip(". ")
    return domain or None


def same_company_domain(first: str | None, second: str | None) -> bool:
    first_domain = email_domain(first)
    second_domain = email_domain(second)
    return bool(
        first_domain
        and first_domain == second_domain
        and first_domain not in FREE_MAIL_DOMAINS
    )


def recipient_header_referral_candidates(
    row: EmailMessage,
    *,
    authored_text: str,
) -> tuple[str, ...]:
    """Use a copied same-domain recipient only when the body says it did so."""

    if not any(
        pattern.search(authored_text) for pattern in RECIPIENT_HEADER_REFERRAL_CUES
    ):
        return ()
    sender = row.from_address.strip().casefold()
    candidates: list[str] = []
    for raw_address in row.to_addresses or []:
        validation = validate_address_format(raw_address)
        address = validation.normalized if validation.valid else None
        if (
            address
            and address != sender
            and same_company_domain(sender, address)
            and address not in candidates
        ):
            candidates.append(address)
    return tuple(candidates)


def parse_return_until(
    return_hint: str | None,
    *,
    received_at: datetime,
) -> datetime | None:
    if not return_hint:
        return None
    cleaned = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b", "", return_hint, flags=re.I)
    cleaned = re.sub(r"\bof\b", " ", cleaned, flags=re.I)
    range_parts = re.split(
        r"\s+(?:to|until|till|through)\s+|\s+[\-–—]\s+",
        cleaned,
        flags=re.I,
    )
    if len(range_parts) > 1:
        cleaned = range_parts[-1]
    cleaned = cleaned.replace(",", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-")
    parsed: datetime | None = None
    try:
        parsed = parsedate_to_datetime(cleaned)
    except (TypeError, ValueError, OverflowError):
        pass
    if parsed is None:
        for format_string in MONTH_FORMATS:
            try:
                if "%Y" in format_string:
                    parsed = datetime.strptime(cleaned, format_string)
                else:
                    parsed = datetime.strptime(
                        f"{cleaned} {received_at.year}",
                        f"{format_string} %Y",
                    )
            except ValueError:
                continue
            if "%Y" not in format_string:
                if parsed.date() < received_at.date() - timedelta(days=7):
                    parsed = parsed.replace(year=received_at.year + 1)
            break
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=received_at.tzinfo or UTC)
    resume_date = parsed.date() + timedelta(days=1)
    return datetime.combine(resume_date, time.min, parsed.tzinfo).astimezone(UTC)


async def unique_sender_contact(
    session: AsyncSession,
    row: EmailMessage,
) -> Contact | None:
    if row.contact_id is not None:
        return await session.get(Contact, row.contact_id)
    sender = row.from_address.strip().casefold()
    contacts = (
        (
            await session.execute(
                select(Contact).where(func.lower(Contact.email) == sender).limit(2)
            )
        )
        .scalars()
        .all()
    )
    return contacts[0] if len(contacts) == 1 else None


async def disposition_contact(
    session: AsyncSession,
    row: EmailMessage,
    disposition: InboundDisposition,
    sender_contact: Contact | None,
) -> Contact | None:
    """Resolve the endpoint affected by a verified personnel message."""

    metadata = row.disposition_metadata or {}
    if (
        disposition.disposition_type is not InboundDispositionType.DEPARTED
        or not (
            (
                metadata.get("verified_reactivation_parent")
                and metadata.get("sender_changed")
            )
            or metadata.get("reviewed_parent_source")
        )
    ):
        return sender_contact
    raw_original_contact_id = metadata.get("original_contact_id")
    if not isinstance(raw_original_contact_id, (int, str)) or isinstance(
        raw_original_contact_id, bool
    ):
        return sender_contact
    try:
        original_contact_id = int(raw_original_contact_id)
    except (TypeError, ValueError):
        return sender_contact
    original = await session.get(Contact, original_contact_id)
    expected_customer_id = row.customer_id or (
        sender_contact.customer_id if sender_contact is not None else None
    )
    if (
        original is None
        or expected_customer_id is None
        or original.customer_id != expected_customer_id
    ):
        return sender_contact
    return original


async def resolved_customer(
    session: AsyncSession,
    row: EmailMessage,
    contact: Contact | None,
) -> Customer | None:
    customer_id = row.customer_id or (contact.customer_id if contact else None)
    return await session.get(Customer, customer_id) if customer_id else None


@dataclass(frozen=True)
class ParentResources:
    contact: Contact
    customer: Customer
    source: str
    parent_email_id: int | None = None
    parent_message_id: str | None = None


@dataclass(frozen=True)
class ResolvedDispositionResources:
    sender_contact: Contact | None
    contact: Contact | None
    customer: Customer | None
    parent: ParentResources | None = None


async def _exact_reactivation_parent_resources(
    session: AsyncSession,
    row: EmailMessage,
) -> ParentResources | None:
    if not row.in_reply_to:
        return None
    matches = (
        await session.execute(
            select(Outbox, Contact, Customer)
            .select_from(Outbox)
            .join(
                ReactivationRecipient,
                ReactivationRecipient.outbox_id == Outbox.id,
            )
            .join(Contact, Contact.id == ReactivationRecipient.contact_id)
            .join(Customer, Customer.id == ReactivationRecipient.customer_id)
            .where(
                Outbox.message_id == row.in_reply_to,
                Outbox.message_kind == "REACTIVATION",
                Outbox.status == DeliveryStatus.SENT,
                Outbox.sent_at.is_not(None),
                Outbox.sent_at <= row.received_at,
                ReactivationRecipient.status.in_(["QUEUED", "SENT", "REPLIED"]),
                ReactivationRecipient.customer_id == Contact.customer_id,
                func.lower(Outbox.recipient) == func.lower(Contact.email),
            )
            .limit(2)
        )
    ).all()
    if len(matches) != 1:
        return None
    outbox, contact, customer = matches[0]
    return ParentResources(
        contact=contact,
        customer=customer,
        source="EXACT_REACTIVATION_PARENT",
        parent_message_id=outbox.message_id,
    )


async def _quoted_outbound_parent_resources(
    session: AsyncSession,
    row: EmailMessage,
) -> ParentResources | None:
    quoted = await resolve_quoted_outbound_parent(
        session,
        row,
        excluded_domains=FREE_MAIL_DOMAINS,
    )
    if quoted is None:
        return None
    contact = await session.get(Contact, quoted.contact_id)
    customer = await session.get(Customer, quoted.customer_id)
    if contact is None or customer is None or contact.customer_id != customer.id:
        return None
    return ParentResources(
        contact=contact,
        customer=customer,
        source="QUOTED_OUTBOUND_PARENT",
        parent_email_id=quoted.email_id,
        parent_message_id=quoted.message_id,
    )


async def resolve_disposition_resources(
    session: AsyncSession,
    row: EmailMessage,
    disposition: InboundDisposition,
) -> ResolvedDispositionResources:
    sender_contact = await unique_sender_contact(session, row)
    contact = await disposition_contact(session, row, disposition, sender_contact)
    customer = await resolved_customer(session, row, contact)
    parent: ParentResources | None = None
    if (
        customer is None
        and disposition.disposition_type
        in {
            InboundDispositionType.DEPARTED,
            InboundDispositionType.CONTACT_REFERRAL,
            InboundDispositionType.FORWARDED_TO_COLLEAGUE,
        }
    ):
        parent = await _exact_reactivation_parent_resources(session, row)
        if parent is None:
            parent = await _quoted_outbound_parent_resources(session, row)
        if parent is not None:
            contact, customer = parent.contact, parent.customer
    return ResolvedDispositionResources(
        sender_contact=sender_contact,
        contact=contact,
        customer=customer,
        parent=parent,
    )
