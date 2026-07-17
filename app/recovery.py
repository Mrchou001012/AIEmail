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
from app.mail import attachments_require_review, parse_mime
from app.services import process_inbound
from app.settings import get_settings


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


@dataclass(frozen=True)
class FalseCounterofferRecoveryResult:
    archive_path: str
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


async def _prepare_false_counteroffer_recovery(
    request: FalseCounterofferRecoveryRequest,
    session_factory: async_sessionmaker[AsyncSession],
) -> Path:
    async with session_factory() as session:
        await _strict_idle_guard(session)

        email_row = await session.scalar(
            select(EmailMessage)
            .where(EmailMessage.id == request.email_id)
            .with_for_update()
        )
        case = await session.scalar(
            select(SalesCase)
            .where(SalesCase.id == request.case_id)
            .with_for_update()
        )
        handoff = await session.scalar(
            select(Handoff)
            .where(Handoff.id == request.handoff_id)
            .with_for_update()
        )

        _require(email_row is not None, f"email {request.email_id} was not found")
        _require(case is not None, f"case {request.case_id} was not found")
        _require(handoff is not None, f"handoff {request.handoff_id} was not found")

        _require(email_row.direction == "INBOUND", "source email is not INBOUND")
        _require(not email_row.is_history, "source email is marked as history")
        _require(email_row.case_id == request.case_id, "source email case changed")
        _require(case.status == CaseStatus.WAITING_HUMAN, f"case status is {case.status.value}")
        _require(case.negotiation_round == 0, f"case negotiation round is {case.negotiation_round}")

        _require(handoff.case_id == request.case_id, "handoff case changed")
        _require(handoff.source_email_id == request.email_id, "handoff source email changed")
        _require(handoff.reason_code == "PRICE_NEGOTIATION", "handoff reason is not PRICE_NEGOTIATION")
        _require(handoff.status == "OPEN", "handoff is no longer OPEN")
        _require(
            handoff.dingtalk_status == request.expected_dingtalk_status,
            f"DingTalk status is {handoff.dingtalk_status}",
        )

        other_open_handoffs = await session.scalar(
            select(func.count()).select_from(Handoff).where(
                Handoff.case_id == request.case_id,
                Handoff.status == "OPEN",
                Handoff.id != request.handoff_id,
            )
        )
        _require(other_open_handoffs == 0, f"case has {other_open_handoffs} other OPEN handoffs")

        approved_outbox = await session.scalar(
            select(func.count()).select_from(Outbox).where(
                Outbox.approval_handoff_id == request.handoff_id
            )
        )
        _require(approved_outbox == 0, "handoff already has an approved outbox")

        reply_key = f"inbound-reply:{request.email_id}"
        existing_replies = await session.scalar(
            select(func.count()).select_from(Outbox).where(
                or_(
                    Outbox.business_key == reply_key,
                    Outbox.business_key.like(f"{reply_key}:quote:%"),
                )
            )
        )
        _require(existing_replies == 0, "source email already has an outbox reply")

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

        process_job = await session.scalar(
            select(Job).where(Job.idempotency_key == f"process-inbound:{request.email_id}")
        )
        notify_job = await session.scalar(
            select(Job).where(Job.idempotency_key == f"handoff-notify:{request.handoff_id}")
        )
        _require(
            process_job is not None and process_job.status == JobStatus.DONE,
            "original process job is not DONE",
        )
        _require(
            notify_job is not None and notify_job.status == JobStatus.DONE,
            "original handoff notification job is not DONE",
        )

        archive_path = (
            get_settings().runtime_dir
            / "inbound_archive"
            / f"{email_row.raw_sha256}.eml"
        )
        _require(archive_path.is_file(), f"raw archive is missing: {archive_path}")
        parsed = parse_mime(archive_path.read_bytes())
        _require(parsed.raw_sha256 == email_row.raw_sha256, "raw SHA changed")
        _require(parsed.message_id == email_row.message_id, "Message-ID changed")
        _require(
            parsed.from_address.casefold() == email_row.from_address.casefold(),
            "sender changed",
        )
        _require(parsed.subject == email_row.subject, "subject changed")
        _require(
            parsed.body_text.strip() == request.expected_body.strip(),
            f"unexpected cleaned body: {parsed.body_text[:500]!r}",
        )
        _require(
            not attachments_require_review(parsed.attachments),
            "attachments still require human review",
        )

        email_row.body_text = parsed.body_text
        email_row.body_html = parsed.body_html
        email_row.attachment_metadata = parsed.attachments

        facts = dict(handoff.extracted_facts or {})
        facts.update(
            {
                "recovery_source_email_id": request.email_id,
                "recovery_commit": request.recovery_commit,
                "recovery_reason": "Localized quoted history caused false counteroffer",
            }
        )
        handoff.extracted_facts = facts
        handoff.status = "RESOLVED"
        handoff.resolution_note = (
            "False PRICE_NEGOTIATION caused by localized quoted message history; "
            f"DingTalk status remains {handoff.dingtalk_status}. Email {request.email_id} "
            "was reparsed and released for automatic reprocessing."
        )
        # process_inbound treats even a resolved source handoff as authoritative,
        # so preserve the audit record while releasing the unique source link.
        handoff.source_email_id = None
        case.status = CaseStatus.ACTIVE

        session.add_all(
            [
                AuditEvent(
                    case_id=request.case_id,
                    actor="deployment_recovery",
                    event_type="email.reparsed_for_recovery",
                    data={
                        "email_id": request.email_id,
                        "handoff_id": request.handoff_id,
                        "commit": request.recovery_commit,
                        "clean_body": parsed.body_text,
                    },
                    created_at=datetime.now(UTC),
                ),
                AuditEvent(
                    case_id=request.case_id,
                    actor="deployment_recovery",
                    event_type="handoff.false_positive_resolved",
                    data={
                        "handoff_id": request.handoff_id,
                        "email_id": request.email_id,
                        "dingtalk_status": handoff.dingtalk_status,
                    },
                    created_at=datetime.now(UTC),
                ),
            ]
        )
        await session.commit()
        return archive_path


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

        return FalseCounterofferRecoveryResult(
            archive_path=str(archive_path),
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
    """Reparse and reprocess one proven false PRICE_NEGOTIATION handoff.

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
