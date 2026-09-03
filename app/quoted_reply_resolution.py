"""Conservative recovery of missing email thread headers from quoted content.

Some customer mail systems omit ``In-Reply-To`` and ``References`` while
leaving the complete outbound message in the reply body.  This module recovers
only a unique, CRM-linked outbound parent.  It never mutates CRM data and is
intended to provide evidence for a human-reviewed disposition action.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Contact, Customer, EmailMessage
from app.mail import normalized_subject

_WHITESPACE = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^\w@.+-]+", re.UNICODE)


@dataclass(frozen=True)
class QuotedOutboundParent:
    email_id: int
    message_id: str | None
    contact_id: int
    customer_id: int
    recipient: str
    matched_domain: str
    evidence: str


def _domain(address: str | None) -> str | None:
    value = str(address or "").strip().casefold()
    if "@" not in value:
        return None
    domain = value.rsplit("@", 1)[1].strip(" >.;,)")
    return domain or None


def _normalized_text(value: str | None) -> str:
    simplified = _NON_WORD.sub(" ", str(value or "").casefold())
    return _WHITESPACE.sub(" ", simplified).strip()


def _quoted_body_evidence(parent_body: str, reply_body: str) -> str | None:
    """Return a bounded fingerprint when the reply quotes the parent body."""

    parent = _normalized_text(parent_body)
    reply = _normalized_text(reply_body)
    if len(parent) < 80 or not reply:
        return None
    # Signatures and legal notices vary between mail systems.  The beginning of
    # the authored outbound text is the most stable and discriminating segment.
    fingerprint = parent[: min(240, len(parent))].strip()
    if len(fingerprint) < 80 or fingerprint not in reply:
        return None
    return fingerprint[:160]


async def resolve_quoted_outbound_parent(
    session: AsyncSession,
    row: EmailMessage,
    *,
    excluded_domains: frozenset[str],
    max_age: timedelta = timedelta(days=14),
) -> QuotedOutboundParent | None:
    """Resolve one same-domain CRM-linked parent quoted by an inbound email.

    The match requires all of the following:

    * the reply has no usable transport parent;
    * sender and original recipient use the same non-free company domain;
    * subject and a substantial prefix of the outbound body are quoted;
    * the outbound email is linked to a consistent customer/contact pair; and
    * exactly one candidate satisfies every condition.
    """

    if row.direction != "INBOUND" or row.in_reply_to:
        return None
    sender_domain = _domain(row.from_address)
    if sender_domain is None or sender_domain in excluded_domains:
        return None

    candidates = (
        (
            await session.execute(
                select(EmailMessage, Contact, Customer)
                .join(Contact, Contact.id == EmailMessage.contact_id)
                .join(Customer, Customer.id == EmailMessage.customer_id)
                .where(
                    EmailMessage.direction == "OUTBOUND",
                    EmailMessage.received_at < row.received_at,
                    EmailMessage.received_at >= row.received_at - max_age,
                    EmailMessage.customer_id.is_not(None),
                    EmailMessage.contact_id.is_not(None),
                    Contact.customer_id == Customer.id,
                )
                .order_by(EmailMessage.received_at.desc())
                .limit(200)
            )
        ).all()
    )
    subject_key = normalized_subject(row.subject)
    matches: list[QuotedOutboundParent] = []
    for parent, contact, customer in candidates:
        if normalized_subject(parent.subject) != subject_key:
            continue
        recipients = {
            str(address or "").strip().casefold()
            for address in parent.to_addresses or []
            if str(address or "").strip()
        }
        contact_email = contact.email.strip().casefold()
        if (
            contact.customer_id != customer.id
            or contact_email not in recipients
            or _domain(contact_email) != sender_domain
        ):
            continue
        evidence = _quoted_body_evidence(parent.body_text, row.body_text)
        if evidence is None:
            continue
        matches.append(
            QuotedOutboundParent(
                email_id=parent.id,
                message_id=parent.message_id,
                contact_id=contact.id,
                customer_id=customer.id,
                recipient=contact_email,
                matched_domain=sender_domain,
                evidence=evidence,
            )
        )
        if len(matches) > 1:
            return None
    return matches[0] if len(matches) == 1 else None
