"""Inbound followup workflow."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.service_core import audit, create_handoff
from app.db import EmailMessage
from app.dispositions.disposition_service import apply_email_disposition
from app.domain import HandoffReason
from app.inbound.auto_replies import AutomatedReplyType
from app.jobs import enqueue_job


async def _ensure_inbound_follow_up(
    session: AsyncSession,
    row: EmailMessage,
    *,
    ambiguous: bool = False,
    review_reason: HandoffReason | None = None,
    review_summary: str | None = None,
    review_facts: dict[str, Any] | None = None,
) -> None:
    if row.is_bounce:
        await enqueue_job(
            session,
            "process_inbound",
            {"email_id": row.id},
            f"process-inbound:{row.id}",
        )
        return
    if await apply_email_disposition(session, row):
        await session.commit()
        return
    if row.automated_reply_type == AutomatedReplyType.SYSTEM_NOTIFICATION.value:
        if row.automated_reply_handled_at is None:
            row.automated_reply_handled_at = datetime.now(UTC)
            await audit(
                session,
                "inbound.system_notification_ignored",
                case_id=row.case_id,
                actor="system",
                data={
                    "email_id": row.id,
                    "sender": row.from_address,
                    "subject": row.subject,
                    **(row.automated_reply_metadata or {}),
                },
            )
            await session.commit()
        return
    if row.case_id is not None:
        await enqueue_job(
            session,
            "process_inbound",
            {"email_id": row.id},
            f"process-inbound:{row.id}",
        )
        return
    summary_prefix = "Ambiguous thread" if ambiguous else "No case matched inbound email"
    disposition_facts = (
        {
            "inbound_disposition": {
                "type": row.disposition_type,
                "confidence": (str(row.disposition_confidence) if row.disposition_confidence is not None else None),
                **(row.disposition_metadata or {}),
            }
        }
        if row.disposition_type and row.disposition_type != "BUSINESS"
        else {}
    )
    await create_handoff(
        session,
        case=None,
        reason=review_reason or HandoffReason.THREAD_AMBIGUOUS,
        summary=review_summary or f"{summary_prefix} from {row.from_address}: {row.subject}",
        facts={**(review_facts or {}), **disposition_facts},
        source_email_id=row.id,
    )
