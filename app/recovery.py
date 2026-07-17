from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db import (
    AuditEvent,
    CaseStatus,
    DeliveryStatus,
    EmailMessage,
    Handoff,
    Job,
    JobStatus,
    Outbox,
    Quote,
    SalesCase,
    engine,
)
from app.mail import ParsedEmail, attachments_require_review, parse_mime
from app.services import process_inbound
from app.settings import get_settings


@dataclass(frozen=True)
class FalseCounterofferDuplicateSource:
    email_id: int
    handoff_id: int


@dataclass(frozen=True)
class FalseCounterofferRecoveryRequest:
    email_id: int
    case_id: int
    handoff_id: int
    expected_body: str
    expected_existing_quantity: int
    expected_new_quantity: int
    expected_recipient: str
    recovery_commit: str
    expected_dingtalk_status: str = "SENT"
    duplicate_sources: tuple[FalseCounterofferDuplicateSource, ...] = ()
    max_duplicate_gap_seconds: int = 300


@dataclass(frozen=True)
class FalseCounterofferRecoveryResult:
    archive_path: str
    canonical_email_id: int
    duplicate_email_ids: tuple[int, ...]
    resolved_handoff_ids: tuple[int, ...]
    case_id: int
    case_stage: str
    case_status: str
    negotiation_round: int
    quote_id: int
    quote_quantity: int
    quote_unit_price: str
    outbox_id: int
    outbox_status: str
    recipient: str
    in_reply_to: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _RecoverySourceState:
    email: EmailMessage
    handoff: Handoff
    archive_path: Path
    parsed: ParsedEmail


def _attachment_fingerprints(parsed: ParsedEmail) -> list[tuple[str, int, str, str]]:
    return sorted(
        (
            str(item.get("sha256") or ""),
            int(item.get("size") or 0),
            str(item.get("content_type") or ""),
            str(item.get("disposition") or ""),
        )
        for item in parsed.attachments
    )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"recovery guard failed: {message}")


async def _strict_idle_guard(session: AsyncSession) -> None:
    running_jobs = await session.scalar(
        select(func.count()).select_from(Job).where(Job.status == JobStatus.RUNNING)
    )
    claimed_outbox = await session.scalar(
        select(func.count()).select_from(Outbox).where(Outbox.status == DeliveryStatus.CLAIMED)
    )
    _require(running_jobs == 0, f"{running_jobs} RUNNING jobs remain")
    _require(claimed_outbox == 0, f"{claimed_outbox} CLAIMED outbox rows remain")


async def _load_recovery_source(
    session: AsyncSession,
    request: FalseCounterofferRecoveryRequest,
    *,
    email_id: int,
    handoff_id: int,
) -> _RecoverySourceState:
    email_row = await session.scalar(
        select(EmailMessage).where(EmailMessage.id == email_id).with_for_update()
    )
    handoff = await session.scalar(
        select(Handoff).where(Handoff.id == handoff_id).with_for_update()
    )

    _require(email_row is not None, f"email {email_id} was not found")
    _require(handoff is not None, f"handoff {handoff_id} was not found")
    _require(email_row.direction == "INBOUND", f"email {email_id} is not INBOUND")
    _require(not email_row.is_history, f"email {email_id} is marked as history")
    _require(email_row.case_id == request.case_id, f"email {email_id} case changed")
    _require(handoff.case_id == request.case_id, f"handoff {handoff_id} case changed")
    _require(handoff.source_email_id == email_id, f"handoff {handoff_id} source email changed")
    _require(
        handoff.reason_code == "PRICE_NEGOTIATION",
        f"handoff {handoff_id} reason is not PRICE_NEGOTIATION",
    )
    _require(handoff.status == "OPEN", f"handoff {handoff_id} is no longer OPEN")
    _require(
        handoff.dingtalk_status == request.expected_dingtalk_status,
        f"handoff {handoff_id} DingTalk status is {handoff.dingtalk_status}",
    )

    approved_outbox = await session.scalar(
        select(func.count()).select_from(Outbox).where(Outbox.approval_handoff_id == handoff_id)
    )
    _require(approved_outbox == 0, f"handoff {handoff_id} already has an approved outbox")

    reply_key = f"inbound-reply:{email_id}"
    existing_replies = await session.scalar(
        select(func.count()).select_from(Outbox).where(
            or_(
                Outbox.business_key == reply_key,
                Outbox.business_key.like(f"{reply_key}:quote:%"),
            )
        )
    )
    _require(existing_replies == 0, f"email {email_id} already has an outbox reply")

    process_job = await session.scalar(
        select(Job).where(Job.idempotency_key == f"process-inbound:{email_id}")
    )
    notify_job = await session.scalar(
        select(Job).where(Job.idempotency_key == f"handoff-notify:{handoff_id}")
    )
    _require(
        process_job is not None
        and process_job.kind == "process_inbound"
        and process_job.payload.get("email_id") == email_id
        and process_job.status == JobStatus.DONE,
        f"email {email_id} original process job is not DONE",
    )
    _require(
        notify_job is not None
        and notify_job.kind == "notify_handoff"
        and notify_job.payload.get("handoff_id") == handoff_id
        and notify_job.status == JobStatus.DONE,
        f"handoff {handoff_id} notification job is not DONE",
    )

    archive_path = get_settings().runtime_dir / "inbound_archive" / f"{email_row.raw_sha256}.eml"
    _require(archive_path.is_file(), f"raw archive is missing: {archive_path}")
    parsed = parse_mime(archive_path.read_bytes())
    _require(parsed.raw_sha256 == email_row.raw_sha256, f"email {email_id} raw SHA changed")
    _require(parsed.message_id == email_row.message_id, f"email {email_id} Message-ID changed")
    _require(parsed.in_reply_to == email_row.in_reply_to, f"email {email_id} In-Reply-To changed")
    _require(parsed.references == email_row.references_json, f"email {email_id} References changed")
    _require(
        parsed.from_address.casefold() == email_row.from_address.casefold(),
        f"email {email_id} sender changed",
    )
    _require(
        {address.casefold() for address in parsed.to_addresses}
        == {address.casefold() for address in email_row.to_addresses},
        f"email {email_id} recipients changed",
    )
    _require(parsed.subject == email_row.subject, f"email {email_id} subject changed")
    _require(
        parsed.body_text.strip() == request.expected_body.strip(),
        f"email {email_id} has unexpected cleaned body: {parsed.body_text[:500]!r}",
    )
    _require(
        not attachments_require_review(parsed.attachments),
        f"email {email_id} attachments still require human review",
    )
    return _RecoverySourceState(
        email=email_row,
        handoff=handoff,
        archive_path=archive_path,
        parsed=parsed,
    )


async def _prepare_false_counteroffer_recovery(
    request: FalseCounterofferRecoveryRequest,
    session_factory: async_sessionmaker[AsyncSession],
) -> Path:
    async with session_factory() as session:
        await _strict_idle_guard(session)
        case = await session.scalar(
            select(SalesCase)
            .where(SalesCase.id == request.case_id)
            .with_for_update()
        )
        _require(case is not None, f"case {request.case_id} was not found")
        _require(case.status == CaseStatus.WAITING_HUMAN, f"case status is {case.status.value}")
        _require(case.negotiation_round == 0, f"case negotiation round is {case.negotiation_round}")

        source_pairs = [
            (request.email_id, request.handoff_id),
            *((source.email_id, source.handoff_id) for source in request.duplicate_sources),
        ]
        email_ids = [email_id for email_id, _ in source_pairs]
        handoff_ids = [handoff_id for _, handoff_id in source_pairs]
        _require(len(email_ids) == len(set(email_ids)), "recovery email IDs are not unique")
        _require(len(handoff_ids) == len(set(handoff_ids)), "recovery handoff IDs are not unique")
        _require(request.max_duplicate_gap_seconds > 0, "duplicate time window must be positive")

        states = [
            await _load_recovery_source(
                session,
                request,
                email_id=email_id,
                handoff_id=handoff_id,
            )
            for email_id, handoff_id in source_pairs
        ]

        open_handoff_ids = set(
            (
                await session.scalars(
                    select(Handoff.id)
                    .where(Handoff.case_id == request.case_id, Handoff.status == "OPEN")
                    .with_for_update()
                )
            ).all()
        )
        expected_open_handoff_ids = set(handoff_ids)
        _require(
            open_handoff_ids == expected_open_handoff_ids,
            "case OPEN handoffs changed: "
            f"expected {sorted(expected_open_handoff_ids)}, found {sorted(open_handoff_ids)}",
        )

        canonical = states[0]
        if request.duplicate_sources:
            _require(
                canonical.parsed.in_reply_to is not None,
                "canonical duplicate candidate has no In-Reply-To",
            )
        canonical_recipients = {address.casefold() for address in canonical.parsed.to_addresses}
        canonical_attachments = _attachment_fingerprints(canonical.parsed)
        for duplicate in states[1:]:
            gap_seconds = (canonical.email.received_at - duplicate.email.received_at).total_seconds()
            _require(
                0 <= gap_seconds <= request.max_duplicate_gap_seconds,
                f"email {duplicate.email.id} is not within {request.max_duplicate_gap_seconds}s "
                f"before canonical email {canonical.email.id}; observed gap is {gap_seconds:g}s, "
                "and the canonical email must be the latest request",
            )
            _require(
                duplicate.email.raw_sha256 != canonical.email.raw_sha256,
                f"email {duplicate.email.id} is not a distinct stored message",
            )
            _require(
                duplicate.email.customer_id == canonical.email.customer_id
                and duplicate.email.contact_id == canonical.email.contact_id,
                f"email {duplicate.email.id} customer or contact differs from canonical email",
            )
            _require(
                duplicate.email.mailbox == canonical.email.mailbox
                and duplicate.email.mailbox_folder == canonical.email.mailbox_folder,
                f"email {duplicate.email.id} mailbox differs from canonical email",
            )
            _require(
                duplicate.parsed.message_id is not None
                and duplicate.parsed.message_id != canonical.parsed.message_id,
                f"email {duplicate.email.id} does not have a distinct Message-ID",
            )
            _require(
                duplicate.parsed.from_address.casefold()
                == canonical.parsed.from_address.casefold(),
                f"email {duplicate.email.id} sender differs from canonical email",
            )
            _require(
                {address.casefold() for address in duplicate.parsed.to_addresses}
                == canonical_recipients,
                f"email {duplicate.email.id} recipients differ from canonical email",
            )
            _require(
                duplicate.parsed.subject == canonical.parsed.subject,
                f"email {duplicate.email.id} subject differs from canonical email",
            )
            _require(
                duplicate.parsed.in_reply_to == canonical.parsed.in_reply_to,
                f"email {duplicate.email.id} replies to a different message",
            )
            _require(
                duplicate.parsed.references == canonical.parsed.references,
                f"email {duplicate.email.id} References differ from canonical email",
            )
            _require(
                _attachment_fingerprints(duplicate.parsed) == canonical_attachments,
                f"email {duplicate.email.id} attachments differ from canonical email",
            )

        active_outbox = await session.scalar(
            select(func.count()).select_from(Outbox).where(
                Outbox.status.in_(
                    [
                        DeliveryStatus.PENDING,
                        DeliveryStatus.FAILED,
                        DeliveryStatus.CLAIMED,
                        DeliveryStatus.UNKNOWN,
                    ]
                )
            )
        )
        _require(active_outbox == 0, f"{active_outbox} unrelated active outbox rows exist")

        active_jobs = await session.scalar(
            select(func.count()).select_from(Job).where(
                Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING])
            )
        )
        _require(active_jobs == 0, f"{active_jobs} unrelated active jobs exist")

        quotes = list(
            (
                await session.scalars(
                    select(Quote)
                    .where(Quote.case_id == request.case_id)
                    .order_by(Quote.round_number)
                )
            ).all()
        )
        _require(
            len(quotes) == 1
            and quotes[0].round_number == 0
            and quotes[0].quantity == request.expected_existing_quantity,
            "quote history is not the expected single round-zero quote",
        )

        audit_events: list[AuditEvent] = []
        now = datetime.now(UTC)
        for index, state in enumerate(states):
            state.email.body_text = state.parsed.body_text
            state.email.body_html = state.parsed.body_html
            state.email.attachment_metadata = state.parsed.attachments

            is_canonical = index == 0
            facts = dict(state.handoff.extracted_facts or {})
            facts.update(
                {
                    "recovery_source_email_id": state.email.id,
                    "recovery_commit": request.recovery_commit,
                    "recovery_reason": "Localized quoted history caused false counteroffer",
                    "recovery_role": "canonical" if is_canonical else "duplicate",
                }
            )
            if not is_canonical:
                facts["duplicate_of_email_id"] = request.email_id
            state.handoff.extracted_facts = facts
            state.handoff.status = "RESOLVED"
            if is_canonical:
                state.handoff.resolution_note = (
                    "False PRICE_NEGOTIATION caused by localized quoted message history; "
                    f"DingTalk status remains {state.handoff.dingtalk_status}. "
                    f"Canonical email {state.email.id} was reparsed and released for "
                    "automatic reprocessing."
                )
                # process_inbound treats even a resolved source handoff as authoritative,
                # so release only the canonical source link. Duplicate links remain as
                # durable idempotency guards against a later accidental reprocessing.
                state.handoff.source_email_id = None
            else:
                state.handoff.resolution_note = (
                    "Duplicate customer request suppressed without reply; canonical email "
                    f"{request.email_id} is the only message released for reprocessing. "
                    f"DingTalk status remains {state.handoff.dingtalk_status}."
                )

            audit_events.extend(
                [
                    AuditEvent(
                        case_id=request.case_id,
                        actor="deployment_recovery",
                        event_type="email.reparsed_for_recovery",
                        data={
                            "email_id": state.email.id,
                            "handoff_id": state.handoff.id,
                            "commit": request.recovery_commit,
                            "clean_body": state.parsed.body_text,
                            "role": "canonical" if is_canonical else "duplicate",
                        },
                        created_at=now,
                    ),
                    AuditEvent(
                        case_id=request.case_id,
                        actor="deployment_recovery",
                        event_type="handoff.false_positive_resolved",
                        data={
                            "handoff_id": state.handoff.id,
                            "email_id": state.email.id,
                            "dingtalk_status": state.handoff.dingtalk_status,
                            "role": "canonical" if is_canonical else "duplicate",
                        },
                        created_at=now,
                    ),
                ]
            )
            if not is_canonical:
                audit_events.append(
                    AuditEvent(
                        case_id=request.case_id,
                        actor="deployment_recovery",
                        event_type="email.duplicate_suppressed",
                        data={
                            "email_id": state.email.id,
                            "handoff_id": state.handoff.id,
                            "canonical_email_id": request.email_id,
                            "commit": request.recovery_commit,
                        },
                        created_at=now,
                    )
                )

        case.status = CaseStatus.ACTIVE
        session.add_all(audit_events)
        await session.commit()
        return canonical.archive_path


async def _verify_false_counteroffer_recovery(
    request: FalseCounterofferRecoveryRequest,
    archive_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> FalseCounterofferRecoveryResult:
    async with session_factory() as session:
        email_row = await session.get(EmailMessage, request.email_id)
        case = await session.get(SalesCase, request.case_id)
        old_handoff = await session.get(Handoff, request.handoff_id)
        _require(email_row is not None, "source email disappeared during reprocessing")
        _require(case is not None, "case disappeared during reprocessing")
        _require(old_handoff is not None, "old handoff disappeared during reprocessing")

        new_handoffs = list(
            (
                await session.scalars(
                    select(Handoff).where(Handoff.source_email_id == request.email_id)
                )
            ).all()
        )
        _require(not new_handoffs, f"reprocessing created handoffs: {[row.id for row in new_handoffs]}")
        _require(old_handoff.status == "RESOLVED", "old handoff was not resolved")
        _require(old_handoff.source_email_id is None, "old handoff still blocks the source email")
        _require(
            old_handoff.dingtalk_status == request.expected_dingtalk_status,
            "old DingTalk audit status changed",
        )
        _require(
            old_handoff.extracted_facts.get("recovery_role") == "canonical",
            "old handoff is not marked as the canonical recovery source",
        )
        _require(
            email_row.body_text.strip() == request.expected_body.strip(),
            "canonical email does not contain the reparsed body",
        )

        duplicate_email_ids: list[int] = []
        resolved_handoff_ids = [old_handoff.id]
        for duplicate_source in request.duplicate_sources:
            duplicate_email = await session.get(EmailMessage, duplicate_source.email_id)
            duplicate_handoff = await session.get(Handoff, duplicate_source.handoff_id)
            _require(
                duplicate_email is not None,
                f"duplicate email {duplicate_source.email_id} disappeared during reprocessing",
            )
            _require(
                duplicate_handoff is not None,
                f"duplicate handoff {duplicate_source.handoff_id} disappeared during reprocessing",
            )
            _require(
                duplicate_email.body_text.strip() == request.expected_body.strip(),
                f"duplicate email {duplicate_source.email_id} does not contain the reparsed body",
            )
            _require(
                duplicate_handoff.status == "RESOLVED",
                f"duplicate handoff {duplicate_source.handoff_id} was not resolved",
            )
            _require(
                duplicate_handoff.source_email_id == duplicate_source.email_id,
                f"duplicate handoff {duplicate_source.handoff_id} lost its durable source link",
            )
            _require(
                duplicate_handoff.dingtalk_status == request.expected_dingtalk_status,
                f"duplicate handoff {duplicate_source.handoff_id} DingTalk status changed",
            )
            _require(
                duplicate_handoff.extracted_facts.get("recovery_role") == "duplicate"
                and duplicate_handoff.extracted_facts.get("duplicate_of_email_id")
                == request.email_id,
                f"duplicate handoff {duplicate_source.handoff_id} recovery facts are incomplete",
            )

            duplicate_reply_key = f"inbound-reply:{duplicate_source.email_id}"
            duplicate_replies = await session.scalar(
                select(func.count()).select_from(Outbox).where(
                    or_(
                        Outbox.business_key == duplicate_reply_key,
                        Outbox.business_key.like(f"{duplicate_reply_key}:quote:%"),
                    )
                )
            )
            _require(
                duplicate_replies == 0,
                f"duplicate email {duplicate_source.email_id} unexpectedly has a reply",
            )
            duplicate_email_ids.append(duplicate_email.id)
            resolved_handoff_ids.append(duplicate_handoff.id)

        open_handoffs = await session.scalar(
            select(func.count()).select_from(Handoff).where(
                Handoff.case_id == request.case_id,
                Handoff.status == "OPEN",
            )
        )
        _require(open_handoffs == 0, f"{open_handoffs} OPEN handoffs remain on the case")

        _require(case.status == CaseStatus.ACTIVE, f"case status is {case.status.value}")
        _require(case.negotiation_round == 1, f"case negotiation round is {case.negotiation_round}")
        quotes = list(
            (
                await session.scalars(
                    select(Quote)
                    .where(Quote.case_id == request.case_id)
                    .order_by(Quote.round_number)
                )
            ).all()
        )
        _require(
            len(quotes) == 2
            and quotes[1].round_number == 1
            and quotes[1].quantity == request.expected_new_quantity,
            "expected second-round quantity quote was not created",
        )

        reply_key = f"inbound-reply:{request.email_id}"
        replies = list(
            (
                await session.scalars(select(Outbox).where(Outbox.business_key == reply_key))
            ).all()
        )
        _require(len(replies) == 1, f"expected one reply outbox, found {len(replies)}")
        outbox = replies[0]
        _require(outbox.status == DeliveryStatus.PENDING, f"outbox status is {outbox.status.value}")
        _require(
            outbox.recipient.casefold() == request.expected_recipient.casefold(),
            f"unexpected recipient: {outbox.recipient}",
        )

        parsed_outbound = parse_mime(outbox.raw_message.encode("utf-8"))
        _require(
            parsed_outbound.in_reply_to == email_row.message_id,
            "outbound In-Reply-To does not match the inbound Message-ID",
        )
        _require(
            f"Quantity: {request.expected_new_quantity} " in parsed_outbound.body_text,
            "new quantity is missing from the rendered reply",
        )
        _require(
            f"Quantity: {request.expected_existing_quantity} " not in parsed_outbound.body_text,
            "old quantity leaked into the rendered reply",
        )

        active_outboxes = list(
            (
                await session.scalars(
                    select(Outbox).where(
                        Outbox.status.in_(
                            [
                                DeliveryStatus.PENDING,
                                DeliveryStatus.FAILED,
                                DeliveryStatus.CLAIMED,
                                DeliveryStatus.UNKNOWN,
                            ]
                        )
                    )
                )
            ).all()
        )
        _require(
            [row.id for row in active_outboxes] == [outbox.id],
            f"unexpected active outbox rows: {[row.id for row in active_outboxes]}",
        )
        active_jobs = await session.scalar(
            select(func.count()).select_from(Job).where(
                Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING])
            )
        )
        _require(active_jobs == 0, f"{active_jobs} active jobs appeared during reprocessing")

        source_count = 1 + len(request.duplicate_sources)
        reparsed_audits = await session.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.case_id == request.case_id,
                AuditEvent.event_type == "email.reparsed_for_recovery",
            )
        )
        resolved_audits = await session.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.case_id == request.case_id,
                AuditEvent.event_type == "handoff.false_positive_resolved",
            )
        )
        duplicate_audits = await session.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.case_id == request.case_id,
                AuditEvent.event_type == "email.duplicate_suppressed",
            )
        )
        _require(reparsed_audits == source_count, "reparse audit count is incorrect")
        _require(resolved_audits == source_count, "resolved handoff audit count is incorrect")
        _require(
            duplicate_audits == len(request.duplicate_sources),
            "duplicate suppression audit count is incorrect",
        )

        return FalseCounterofferRecoveryResult(
            archive_path=str(archive_path),
            canonical_email_id=email_row.id,
            duplicate_email_ids=tuple(duplicate_email_ids),
            resolved_handoff_ids=tuple(resolved_handoff_ids),
            case_id=case.id,
            case_stage=case.stage.value,
            case_status=case.status.value,
            negotiation_round=case.negotiation_round,
            quote_id=quotes[1].id,
            quote_quantity=quotes[1].quantity,
            quote_unit_price=str(quotes[1].unit_price),
            outbox_id=outbox.id,
            outbox_status=outbox.status.value,
            recipient=outbox.recipient,
            in_reply_to=parsed_outbound.in_reply_to,
        )


async def recover_false_counteroffer(
    request: FalseCounterofferRecoveryRequest,
    *,
    db_engine: AsyncEngine = engine,
) -> FalseCounterofferRecoveryResult:
    """Recover one false PRICE_NEGOTIATION and suppress explicit duplicates.

    The API, worker, and IMAP poller must be stopped before this function is called.
    Every mutable production assumption is guarded before the audit-preserving
    recovery transaction is committed. Application-level commits are isolated
    in savepoints so any failure in reparsing, AI processing, or final
    verification rolls the entire recovery back.
    """

    async with db_engine.connect() as connection:
        async with connection.begin():
            atomic_sessions = async_sessionmaker(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            archive_path = await _prepare_false_counteroffer_recovery(
                request,
                atomic_sessions,
            )
            async with atomic_sessions() as session:
                await process_inbound(session, request.email_id)
            return await _verify_false_counteroffer_recovery(
                request,
                archive_path,
                atomic_sessions,
            )
