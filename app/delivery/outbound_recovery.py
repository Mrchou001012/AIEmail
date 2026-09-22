"""Outbound recovery workflow."""

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.service_core import audit
from app.db import (
    CommercialDataCycle,
    DeliveryStatus,
    EmailMessage,
    Outbox,
    Quote,
)
from app.jobs import enqueue_job
from app.mail import GmailIMAPClient
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


async def _cancel_and_requeue_stale_quote(
    session: AsyncSession,
    *,
    row: Outbox,
    quote: Quote | None,
    cycle: CommercialDataCycle,
    reason: str,
) -> None:
    """Cancel immutable old quote mail and create one cycle-scoped reprice job."""

    row.status = DeliveryStatus.CANCELLED
    row.last_error = f"commercial data gate cancelled frozen quote: {reason}"[:2000]
    await session.execute(
        delete(EmailMessage).where(
            EmailMessage.direction == "OUTBOUND",
            EmailMessage.message_id == row.message_id,
            EmailMessage.is_history.is_(False),
        )
    )
    await audit(
        session,
        "outbox.cancelled_stale_commercial_data",
        case_id=row.case_id,
        actor="commercial_gate",
        data={
            "outbox_id": row.id,
            "quote_id": quote.id if quote else None,
            "old_cycle_id": quote.commercial_cycle_id if quote else None,
            "new_cycle_id": cycle.id,
            "reason": reason,
        },
    )
    inbound_match = re.fullmatch(r"inbound-reply:(\d+)(?::quote:\d+)?", row.business_key)
    initial_match = re.fullmatch(r"initial-quote:case:(\d+)(?::cycle:\d+)?", row.business_key)
    if inbound_match:
        email_id = int(inbound_match.group(1))
        await enqueue_job(
            session,
            "process_inbound",
            {"email_id": email_id, "reprice": True},
            f"commercial-reprice:inbound:{email_id}:cycle:{cycle.id}",
        )
        return
    if initial_match and row.case_id is not None:
        await enqueue_job(
            session,
            "case_outreach",
            {
                "case_id": row.case_id,
                "quantity": quote.quantity if quote is not None else 1,
                "reprice": True,
            },
            f"commercial-reprice:case:{row.case_id}:cycle:{cycle.id}",
        )
        return
    await session.commit()


async def reconcile_unknown_outbox(session: AsyncSession, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    if settings.mail_transport != "smtp" or not (settings.gmail_address and settings.gmail_app_password):
        return False
    row = await session.scalar(
        select(Outbox)
        .where(
            Outbox.status == DeliveryStatus.UNKNOWN,
            Outbox.locked_at < datetime.now(UTC) - timedelta(minutes=10),
        )
        .order_by(Outbox.id)
        .with_for_update(skip_locked=True)
    )
    if row is None:
        return False
    try:
        found = await asyncio.to_thread(GmailIMAPClient(settings).sent_contains_message_id, row.message_id)
    except Exception as exc:
        # Keep an ambiguous delivery in UNKNOWN until Gmail Sent can be checked.
        # Retrying SMTP before reconciliation could deliver a duplicate message.
        row.locked_at = datetime.now(UTC)
        row.last_error = f"Gmail Sent reconciliation deferred: {type(exc).__name__}: {exc}"[:2000]
        await session.commit()
        logger.exception("outbox %s reconciliation failed", row.id)
        return True
    if found:
        row.status = DeliveryStatus.SENT
        row.sent_at = datetime.now(UTC)
        row.sent_via = "smtp"
        row.last_error = None
        await audit(
            session,
            "outbox.reconciled_sent",
            case_id=row.case_id,
            actor="gmail_sent",
            data={"outbox_id": row.id, "message_id": row.message_id},
        )
    else:
        row.status = DeliveryStatus.FAILED
        row.available_at = datetime.now(UTC)
        row.last_error = "Gmail Sent confirmed Message-ID absent; retry permitted"
    await session.commit()
    return True
