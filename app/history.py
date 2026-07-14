from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AuditEvent, CaseStatus, Contact, EmailMessage, Handoff, Outbox, SalesCase
from app.domain import HandoffReason
from app.mail import normalized_subject

HISTORY_REVIEW_SUMMARY = "Historical Gmail reply requires review"


@dataclass(frozen=True)
class HistoryReconciliation:
    matched_messages: int
    unmatched_messages: int
    replies_waiting_review: int
    no_reply_cases_paused: int


def _participants(row: EmailMessage) -> list[str]:
    if row.direction == "INBOUND":
        return [row.from_address.lower()]
    return [address.lower() for address in row.to_addresses]


async def _thread_case_ids(session: AsyncSession, row: EmailMessage) -> set[int]:
    message_ids = [item for item in [row.in_reply_to, *row.references_json] if item]
    case_ids: set[int] = set()
    if message_ids:
        email_cases = (
            (
                await session.execute(
                    select(EmailMessage.case_id).where(
                        EmailMessage.message_id.in_(message_ids),
                        EmailMessage.case_id.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        case_ids.update(case_id for case_id in email_cases if case_id is not None)
    outbox_ids = [*message_ids]
    if row.direction == "OUTBOUND" and row.message_id:
        outbox_ids.append(row.message_id)
    if outbox_ids:
        outbox_cases = (
            (
                await session.execute(
                    select(Outbox.case_id).where(
                        Outbox.message_id.in_(outbox_ids),
                        Outbox.case_id.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        case_ids.update(case_id for case_id in outbox_cases if case_id is not None)
    return case_ids


async def _contact_case(session: AsyncSession, row: EmailMessage) -> SalesCase | None:
    participants = _participants(row)
    if not participants:
        return None
    candidates = (
        (
            await session.execute(
                select(SalesCase)
                .join(Contact, SalesCase.contact_id == Contact.id)
                .where(
                    Contact.email.in_(participants),
                    SalesCase.status.not_in([CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST]),
                )
            )
        )
        .scalars()
        .all()
    )
    if len(candidates) == 1:
        return candidates[0]
    subject = normalized_subject(row.subject)
    subject_matches = [case for case in candidates if normalized_subject(case.subject_key or "") == subject]
    return subject_matches[0] if len(subject_matches) == 1 else None


async def reconcile_email_history(session: AsyncSession) -> HistoryReconciliation:
    unmatched = (
        (
            await session.execute(
                select(EmailMessage)
                .where(EmailMessage.is_history.is_(True), EmailMessage.case_id.is_(None))
                .order_by(EmailMessage.received_at, EmailMessage.id)
            )
        )
        .scalars()
        .all()
    )
    matched = 0
    for _ in range(20):
        changed = 0
        for row in unmatched:
            if row.case_id is not None:
                continue
            participants = _participants(row)
            thread_case_ids = await _thread_case_ids(session, row)
            if len(thread_case_ids) == 1:
                case = await session.get(SalesCase, next(iter(thread_case_ids)))
                if case is not None:
                    contact_email = await session.scalar(select(Contact.email).where(Contact.id == case.contact_id))
                    if contact_email and contact_email.lower() in participants:
                        row.case_id = case.id
            if row.case_id is None:
                case = await _contact_case(session, row)
                if case is not None:
                    row.case_id = case.id
            if row.case_id is not None:
                changed += 1
                matched += 1
        await session.flush()
        if changed == 0:
            break

    history_case_ids = (
        (
            await session.execute(
                select(EmailMessage.case_id)
                .where(EmailMessage.is_history.is_(True), EmailMessage.case_id.is_not(None))
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    replies_waiting = 0
    no_reply_paused = 0
    for case_id in history_case_ids:
        case = await session.get(SalesCase, case_id)
        if case is None:
            continue
        latest = await session.scalar(
            select(EmailMessage)
            .where(EmailMessage.case_id == case.id, EmailMessage.is_history.is_(True))
            .order_by(EmailMessage.received_at.desc(), EmailMessage.id.desc())
            .limit(1)
        )
        if latest is None:
            continue
        case.last_activity_at = max(case.last_activity_at, latest.received_at)
        if latest.direction == "INBOUND":
            if case.status not in {CaseStatus.HUMAN_TAKEOVER, CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST}:
                case.status = CaseStatus.WAITING_HUMAN
            existing_review = await session.scalar(
                select(Handoff.id).where(
                    Handoff.case_id == case.id,
                    Handoff.reason_code == HandoffReason.HUMAN_CONTROL.value,
                    Handoff.summary == HISTORY_REVIEW_SUMMARY,
                    Handoff.status == "OPEN",
                )
            )
            if existing_review is None:
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
    remaining = await session.scalar(
        select(func.count())
        .select_from(EmailMessage)
        .where(EmailMessage.is_history.is_(True), EmailMessage.case_id.is_(None))
    )
    return HistoryReconciliation(
        matched_messages=matched,
        unmatched_messages=remaining or 0,
        replies_waiting_review=replies_waiting,
        no_reply_cases_paused=no_reply_paused,
    )
