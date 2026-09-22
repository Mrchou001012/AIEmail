"""Automated reply service workflow."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.service_core import audit, create_handoff
from app.db import EmailMessage, SalesCase
from app.domain import HandoffReason
from app.inbound.auto_replies import AutomatedReplyType
from app.settings import get_settings


async def _handle_automated_reply(
    session: AsyncSession,
    *,
    case: SalesCase,
    email_row: EmailMessage,
) -> bool:
    if not email_row.is_automated_reply:
        return False
    if email_row.automated_reply_handled_at is not None:
        return True

    reply_type = email_row.automated_reply_type
    settings = get_settings()
    facts = {
        "automated_reply_type": reply_type,
        **(email_row.automated_reply_metadata or {}),
        "inbound_disposition": {
            "type": email_row.disposition_type,
            "confidence": (str(email_row.disposition_confidence) if email_row.disposition_confidence is not None else None),
            **(email_row.disposition_metadata or {}),
        },
    }
    if (
        settings.inbound_disposition_enabled
        and settings.inbound_disposition_apply_enabled
        and reply_type == AutomatedReplyType.OUT_OF_OFFICE.value
        and email_row.disposition_type == "TEMPORARY_ABSENCE"
        and email_row.disposition_handled_at is None
    ):
        email_row.automated_reply_handled_at = datetime.now(UTC)
        await audit(
            session,
            "inbound.automated_reply_escalated",
            case_id=case.id,
            actor="inbound_disposition",
            data={"email_id": email_row.id, **facts},
        )
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.AUTOMATED_REPLY_REVIEW,
            summary="Temporary absence could not be applied within the automatic safety boundary",
            facts=facts,
            source_email_id=email_row.id,
        )
        return True
    if reply_type in {
        AutomatedReplyType.OUT_OF_OFFICE.value,
        AutomatedReplyType.GENERIC_AUTOREPLY.value,
        AutomatedReplyType.SYSTEM_NOTIFICATION.value,
    }:
        email_row.automated_reply_handled_at = datetime.now(UTC)
        await audit(
            session,
            "inbound.automated_reply_handled",
            case_id=case.id,
            actor="system",
            data={"email_id": email_row.id, **facts},
        )
        await session.commit()
        return True

    if reply_type in {
        AutomatedReplyType.DEPARTED.value,
        AutomatedReplyType.CONTACT_CHANGE.value,
    }:
        if email_row.automated_reply_metadata.get("personnel_change_handled"):
            # The old endpoint was already retired and a new contact was
            # linked during ingestion; record the personnel change and keep
            # processing the business request (quote / product list / ...).
            await audit(
                session,
                "inbound.personnel_change_recorded",
                case_id=case.id,
                actor="system",
                data={"email_id": email_row.id, **facts},
            )
            return False
        if settings.inbound_disposition_enabled:
            disposition_metadata = email_row.disposition_metadata or {}
            if not disposition_metadata.get("automated_transport_signal"):
                if not disposition_metadata.get("personnel_observation_recorded"):
                    email_row.disposition_metadata = {
                        **disposition_metadata,
                        "personnel_observation_recorded": True,
                    }
                    await audit(
                        session,
                        "inbound.personnel_change_observed",
                        case_id=case.id,
                        actor="inbound_disposition",
                        data={"email_id": email_row.id, **facts},
                    )
                # A person may be reporting that somebody else left while also
                # asking for a catalog or quotation. Never suppress the sender.
                return False
            email_row.automated_reply_handled_at = datetime.now(UTC)
            await audit(
                session,
                "inbound.automated_reply_escalated",
                case_id=case.id,
                actor="inbound_disposition",
                data={"email_id": email_row.id, **facts},
            )
            await create_handoff(
                session,
                case=case,
                reason=HandoffReason.PERSONNEL_CHANGE,
                summary=(
                    "Personnel change could not be applied within the automatic safety boundary; verify the old and replacement contacts"
                ),
                facts=facts,
                source_email_id=email_row.id,
            )
            return True
        email_row.automated_reply_handled_at = datetime.now(UTC)
        # No reactivation context: conservative fallback keeps the original
        # behavior (retire the current contact and ask a human to verify).
        case.contact.suppressed = True
        summary = "Contact appears to have left the company; verify any replacement contact"
        reason = HandoffReason.PERSONNEL_CHANGE
    else:
        email_row.automated_reply_handled_at = datetime.now(UTC)
        summary = "Automated reply could not be handled safely"
        reason = HandoffReason.AUTOMATED_REPLY_REVIEW
    await audit(
        session,
        "inbound.automated_reply_escalated",
        case_id=case.id,
        actor="system",
        data={"email_id": email_row.id, **facts},
    )
    await create_handoff(
        session,
        case=case,
        reason=reason,
        summary=summary,
        facts=facts,
        source_email_id=email_row.id,
    )
    return True
