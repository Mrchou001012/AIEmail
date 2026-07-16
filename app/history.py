from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import (
    AuditEvent,
    CaseStatus,
    Contact,
    EmailMessage,
    Handoff,
    Outbox,
    Product,
    SalesCase,
)
from app.domain import HandoffReason
from app.mail import normalized_subject
from app.products import find_product_codes

HISTORY_REVIEW_SUMMARY = "Historical Gmail reply requires review"
CLOSED_CASE_STATUSES = {CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST}


@dataclass(frozen=True)
class HistoryReconciliation:
    # The original two fields remain as case-level compatibility fields for
    # callers that consumed the earlier endpoint response.
    matched_messages: int
    unmatched_messages: int
    customer_matched_messages: int
    customer_unmatched_messages: int
    customer_matched_case_unmatched_messages: int
    replies_waiting_review: int
    no_reply_cases_paused: int


def _participants(row: EmailMessage) -> list[str]:
    values = [row.from_address] if row.direction == "INBOUND" else row.to_addresses
    return list(
        dict.fromkeys(
            address.strip().casefold()
            for address in values
            if address and address.strip()
        )
    )


async def resolve_unique_contact(
    session: AsyncSession,
    addresses: list[str],
) -> Contact | None:
    """Resolve addresses only when they identify one Contact globally."""
    normalized = list(
        dict.fromkeys(
            address.strip().casefold()
            for address in addresses
            if address and address.strip()
        )
    )
    if not normalized:
        return None
    contacts = (
        (
            await session.execute(
                select(Contact).where(func.lower(Contact.email).in_(normalized))
            )
        )
        .scalars()
        .all()
    )
    unique = {contact.id: contact for contact in contacts}
    return next(iter(unique.values())) if len(unique) == 1 else None


def apply_case_identity(row: EmailMessage, case: SalesCase) -> None:
    row.case_id = case.id
    row.customer_id = case.customer_id
    row.contact_id = case.contact_id


def _contact_from_participants(
    participants: list[str],
    contacts_by_email: dict[str, list[Contact]],
) -> Contact | None:
    candidates = {
        contact.id: contact
        for address in participants
        for contact in contacts_by_email.get(address, [])
    }
    return next(iter(candidates.values())) if len(candidates) == 1 else None


def _contact_case(
    row: EmailMessage,
    candidates: list[SalesCase],
    product_codes: dict[int, str],
) -> SalesCase | None:
    if not candidates:
        return None

    explicit_codes = find_product_codes(f"{row.subject}\n{row.body_text}")
    if explicit_codes:
        product_matches = [
            case for case in candidates if product_codes.get(case.product_id) in explicit_codes
        ]
        return product_matches[0] if len(product_matches) == 1 else None

    subject = normalized_subject(row.subject)
    subject_matches = [
        case
        for case in candidates
        if case.subject_key and normalized_subject(case.subject_key) == subject
    ]
    if len(subject_matches) == 1:
        return subject_matches[0]
    return candidates[0] if len(candidates) == 1 else None


def _thread_case_ids(
    row: EmailMessage,
    email_message_cases: dict[str, set[int]],
    outbox_message_cases: dict[str, set[int]],
) -> set[int]:
    message_ids = [item for item in [row.in_reply_to, *row.references_json] if item]
    if row.direction == "OUTBOUND" and row.message_id:
        message_ids.append(row.message_id)
    return {
        case_id
        for message_id in message_ids
        for case_id in (
            email_message_cases.get(message_id, set())
            | outbox_message_cases.get(message_id, set())
        )
    }


async def reconcile_email_history(session: AsyncSession) -> HistoryReconciliation:
    history_rows = (
        (
            await session.execute(
                select(EmailMessage)
                .where(EmailMessage.is_history.is_(True))
                .order_by(EmailMessage.received_at, EmailMessage.id)
            )
        )
        .scalars()
        .all()
    )
    contacts = ((await session.execute(select(Contact))).scalars().all())
    contacts_by_id = {contact.id: contact for contact in contacts}
    contacts_by_email: dict[str, list[Contact]] = defaultdict(list)
    for contact in contacts:
        contacts_by_email[contact.email.strip().casefold()].append(contact)

    case_rows = (
        (
            await session.execute(
                select(SalesCase, Product.code).join(Product, SalesCase.product_id == Product.id)
            )
        )
        .all()
    )
    cases_by_id = {case.id: case for case, _ in case_rows}
    cases_by_contact: dict[int, list[SalesCase]] = defaultdict(list)
    product_codes: dict[int, str] = {}
    for case, product_code in case_rows:
        product_codes[case.product_id] = product_code
        if case.status not in CLOSED_CASE_STATUSES:
            cases_by_contact[case.contact_id].append(case)

    email_message_cases: dict[str, set[int]] = defaultdict(set)
    existing_email_links = await session.execute(
        select(EmailMessage.message_id, EmailMessage.case_id).where(
            EmailMessage.message_id.is_not(None),
            EmailMessage.case_id.is_not(None),
        )
    )
    for message_id, case_id in existing_email_links.all():
        email_message_cases[message_id].add(case_id)
    outbox_message_cases: dict[str, set[int]] = defaultdict(set)
    existing_outbox_links = await session.execute(
        select(Outbox.message_id, Outbox.case_id).where(
            Outbox.message_id.is_not(None),
            Outbox.case_id.is_not(None),
        )
    )
    for message_id, case_id in existing_outbox_links.all():
        outbox_message_cases[message_id].add(case_id)

    customer_matched = 0
    case_matched = 0
    for _ in range(20):
        changed_cases = 0
        for row in history_rows:
            participants = _participants(row)
            case = cases_by_id.get(row.case_id) if row.case_id is not None else None
            if case is not None:
                apply_case_identity(row, case)
            else:
                contact = contacts_by_id.get(row.contact_id) if row.contact_id is not None else None
                if contact is None:
                    contact = _contact_from_participants(participants, contacts_by_email)
                if contact is not None:
                    was_unmatched = row.contact_id is None
                    row.contact_id = contact.id
                    row.customer_id = contact.customer_id
                    if was_unmatched:
                        customer_matched += 1

                thread_case_ids = _thread_case_ids(
                    row,
                    email_message_cases,
                    outbox_message_cases,
                )
                if len(thread_case_ids) == 1:
                    candidate = cases_by_id.get(next(iter(thread_case_ids)))
                    candidate_contact = (
                        contacts_by_id.get(candidate.contact_id) if candidate is not None else None
                    )
                    if (
                        candidate is not None
                        and candidate_contact is not None
                        and candidate_contact.email.strip().casefold() in participants
                    ):
                        case = candidate
                if case is None and row.contact_id is not None:
                    case = _contact_case(
                        row,
                        cases_by_contact.get(row.contact_id, []),
                        product_codes,
                    )
                if case is not None:
                    apply_case_identity(row, case)
                    case_matched += 1
                    changed_cases += 1

            if row.case_id is not None and row.message_id:
                email_message_cases[row.message_id].add(row.case_id)
        if changed_cases == 0:
            break

    await session.flush()

    latest_by_case: dict[int, EmailMessage] = {}
    for row in history_rows:
        if row.case_id is not None:
            latest_by_case[row.case_id] = row
    latest_email_ids = [row.id for row in latest_by_case.values()]
    existing_review_sources: set[int] = set()
    if latest_email_ids:
        existing_review_sources = set(
            (
                await session.execute(
                    select(Handoff.source_email_id).where(
                        Handoff.source_email_id.in_(latest_email_ids)
                    )
                )
            )
            .scalars()
            .all()
        )

    replies_waiting = 0
    no_reply_paused = 0
    for case_id, latest in latest_by_case.items():
        case = cases_by_id.get(case_id)
        if case is None:
            continue
        case.last_activity_at = max(case.last_activity_at, latest.received_at)
        if latest.direction == "INBOUND":
            if case.status not in {
                CaseStatus.HUMAN_TAKEOVER,
                CaseStatus.CLOSED_WON,
                CaseStatus.CLOSED_LOST,
            }:
                case.status = CaseStatus.WAITING_HUMAN
            if latest.id not in existing_review_sources:
                session.add(
                    Handoff(
                        case_id=case.id,
                        source_email_id=latest.id,
                        reason_code=HandoffReason.HUMAN_CONTROL.value,
                        summary=HISTORY_REVIEW_SUMMARY,
                        extracted_facts={
                            "history_import": True,
                            "latest_email_id": latest.id,
                            "latest_email_at": latest.received_at.isoformat(),
                        },
                    )
                )
                session.add(
                    AuditEvent(
                        case_id=case.id,
                        actor="gmail_history",
                        event_type="history.reply_waiting_review",
                        data={"email_id": latest.id},
                    )
                )
                replies_waiting += 1
        elif case.status == CaseStatus.ACTIVE:
            case.status = CaseStatus.PAUSED
            session.add(
                AuditEvent(
                    case_id=case.id,
                    actor="gmail_history",
                    event_type="history.no_reply_paused",
                    data={"latest_email_id": latest.id},
                )
            )
            no_reply_paused += 1

    await session.commit()
    customer_unmatched = await session.scalar(
        select(func.count())
        .select_from(EmailMessage)
        .where(EmailMessage.is_history.is_(True), EmailMessage.contact_id.is_(None))
    )
    case_unmatched = await session.scalar(
        select(func.count())
        .select_from(EmailMessage)
        .where(EmailMessage.is_history.is_(True), EmailMessage.case_id.is_(None))
    )
    customer_matched_case_unmatched = await session.scalar(
        select(func.count())
        .select_from(EmailMessage)
        .where(
            EmailMessage.is_history.is_(True),
            EmailMessage.contact_id.is_not(None),
            EmailMessage.case_id.is_(None),
        )
    )
    return HistoryReconciliation(
        matched_messages=case_matched,
        unmatched_messages=case_unmatched or 0,
        customer_matched_messages=customer_matched,
        customer_unmatched_messages=customer_unmatched or 0,
        customer_matched_case_unmatched_messages=customer_matched_case_unmatched or 0,
        replies_waiting_review=replies_waiting,
        no_reply_cases_paused=no_reply_paused,
    )
