"""Bounce service workflow."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.service_core import audit, create_handoff
from app.db import (
    CaseStatus,
    DeliveryStatus,
    EmailMessage,
    Handoff,
    Job,
    JobStatus,
    Outbox,
    ReactivationRecipient,
    SalesCase,
)
from app.delivery.delivery_safety import _email_address_status, _suppress_email_address
from app.domain import HandoffReason
from app.handoffs.agent_runtime import finalize_handoff_agent_run
from app.inbound.bounces import BounceType, has_permanent_failure_evidence


async def _match_bounce_outbox(
    session: AsyncSession,
    email_row: EmailMessage,
) -> tuple[Outbox | None, str | None]:
    metadata = email_row.bounce_metadata or {}
    recipient = str(metadata.get("recipient") or "").strip().casefold() or None
    outbox = None
    if metadata.get("matched_outbox_id"):
        outbox = await session.get(Outbox, int(metadata["matched_outbox_id"]))
    if outbox is None and metadata.get("original_message_id"):
        outbox = await session.scalar(
            select(Outbox).where(
                Outbox.message_id == str(metadata["original_message_id"]),
                Outbox.status == DeliveryStatus.SENT,
            )
        )
    if outbox is None and recipient:
        outbox = await session.scalar(
            select(Outbox)
            .where(
                func.lower(Outbox.recipient) == recipient,
                Outbox.status == DeliveryStatus.SENT,
            )
            .order_by(Outbox.sent_at.desc(), Outbox.id.desc())
        )
    if outbox is not None:
        recipient = recipient or outbox.recipient.casefold()
        if recipient != outbox.recipient.casefold():
            return None, recipient
    return outbox, recipient


async def _apply_correlated_hard_bounce(
    session: AsyncSession,
    *,
    email_row: EmailMessage,
    outbox: Outbox,
    recipient: str,
    case: SalesCase | None,
    audit_event: str,
) -> None:
    metadata = dict(email_row.bounce_metadata or {})
    diagnostic = str(metadata.get("diagnostic") or "")[:2000] or None
    await _suppress_email_address(
        session,
        recipient,
        reason="HARD_BOUNCE",
        source_email_id=email_row.id,
        bounce_type=BounceType.HARD.value,
        diagnostic=diagnostic,
    )
    if case and case.status not in {CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST}:
        case.status = CaseStatus.PAUSED
    campaign_recipient = await session.scalar(select(ReactivationRecipient).where(ReactivationRecipient.outbox_id == outbox.id))
    if campaign_recipient is not None and campaign_recipient.status != "REPLIED":
        campaign_recipient.status = "FAILED"
        campaign_recipient.exclusion_reason = "HARD_BOUNCE"
    await audit(
        session,
        audit_event,
        case_id=case.id if case else None,
        actor="system",
        data={"email_id": email_row.id, "outbox_id": outbox.id, **metadata},
    )


async def _handle_bounce(session: AsyncSession, email_row: EmailMessage) -> None:
    if email_row.bounce_handled_at is not None:
        return
    outbox, recipient = await _match_bounce_outbox(session, email_row)
    case = await session.get(SalesCase, outbox.case_id) if outbox and outbox.case_id else None
    if case is None and email_row.case_id:
        case = await session.get(SalesCase, email_row.case_id)
    if case and email_row.case_id is None:
        email_row.case_id = case.id
    if case:
        email_row.customer_id = case.customer_id
        email_row.contact_id = case.contact_id

    metadata = dict(email_row.bounce_metadata or {})
    if outbox is not None:
        metadata["matched_outbox_id"] = outbox.id
    metadata["recipient"] = recipient
    email_row.bounce_metadata = metadata
    email_row.bounce_handled_at = datetime.now(UTC)
    diagnostic = str(metadata.get("diagnostic") or "")[:2000] or None

    if email_row.bounce_type == BounceType.HARD.value and outbox is not None and recipient:
        await _apply_correlated_hard_bounce(
            session,
            email_row=email_row,
            outbox=outbox,
            recipient=recipient,
            case=case,
            audit_event="inbound.hard_bounce_suppressed",
        )
        await session.commit()
        return

    if recipient:
        status = await _email_address_status(session, recipient)
        # A late or repeated delivery report is still useful endpoint history.
        # Preserve the newest bounce facts even when the address was already
        # suppressed, while avoiding another handoff/notification below.
        status.last_bounce_at = datetime.now(UTC)
        status.last_bounce_type = email_row.bounce_type
        status.last_bounce_diagnostic = diagnostic
        if status.suppressed:
            # The endpoint is already suppressed, so a late or repeated
            # delivery report must not create another human-review handoff
            # or DingTalk notification. Record it and move on.
            await audit(
                session,
                "inbound.bounce_ignored_suppressed",
                case_id=case.id if case else None,
                actor="system",
                data={
                    "email_id": email_row.id,
                    "outbox_id": outbox.id if outbox else None,
                    **metadata,
                },
            )
            await session.commit()
            return
    await audit(
        session,
        "inbound.bounce_review_required",
        case_id=case.id if case else None,
        actor="system",
        data={"email_id": email_row.id, "outbox_id": outbox.id if outbox else None, **metadata},
    )
    await create_handoff(
        session,
        case=case,
        reason=HandoffReason.BOUNCE_REVIEW,
        summary=(f"Review {email_row.bounce_type or 'unknown'} delivery failure for {recipient or 'an unidentified recipient'}"),
        facts={"email_id": email_row.id, "outbox_id": outbox.id if outbox else None, **metadata},
        source_email_id=email_row.id,
    )


async def reconcile_permanent_bounce_handoffs(session: AsyncSession) -> int:
    """Resolve legacy SOFT reviews whose content proves a permanent failure.

    This is deliberately limited to a bounce that can be correlated to an
    exact sent outbox recipient.  Uncorrelated delivery reports remain in
    review so an arbitrary inbound message cannot suppress a customer.
    """
    handoffs = (
        (
            await session.execute(
                select(Handoff)
                .where(
                    Handoff.status == "OPEN",
                    Handoff.reason_code == HandoffReason.BOUNCE_REVIEW.value,
                    Handoff.source_email_id.is_not(None),
                )
                .order_by(Handoff.id)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    resolved = 0
    for handoff in handoffs:
        email_row = await session.get(EmailMessage, handoff.source_email_id)
        if email_row is None or not email_row.is_bounce:
            continue
        facts = {**(email_row.bounce_metadata or {}), **(handoff.extracted_facts or {})}
        evidence = "\n".join(
            str(value)
            for value in (
                email_row.subject,
                email_row.body_text,
                handoff.summary,
                facts.get("diagnostic"),
                facts.get("detail"),
                facts.get("status_code"),
            )
            if value
        )
        if not has_permanent_failure_evidence(
            evidence,
            status_code=str(facts.get("status_code") or "") or None,
        ):
            continue

        outbox, recipient = await _match_bounce_outbox(session, email_row)
        if outbox is None or recipient is None:
            continue
        case_id = handoff.case_id or outbox.case_id or email_row.case_id
        case = await session.get(SalesCase, case_id) if case_id else None
        if case is not None:
            handoff.case_id = case.id
            email_row.case_id = case.id
            email_row.customer_id = case.customer_id
            email_row.contact_id = case.contact_id

        metadata = dict(email_row.bounce_metadata or {})
        detected_by = list(metadata.get("detected_by") or [])
        if "reconcile:permanent-failure-evidence" not in detected_by:
            detected_by.append("reconcile:permanent-failure-evidence")
        metadata.update(
            {
                "bounce_type": BounceType.HARD.value,
                "permanent": True,
                "recipient": recipient,
                "matched_outbox_id": outbox.id,
                "detected_by": detected_by,
            }
        )
        email_row.bounce_type = BounceType.HARD.value
        email_row.bounce_metadata = metadata
        email_row.bounce_handled_at = datetime.now(UTC)
        await _apply_correlated_hard_bounce(
            session,
            email_row=email_row,
            outbox=outbox,
            recipient=recipient,
            case=case,
            audit_event="inbound.bounce_review_auto_resolved",
        )
        handoff.status = "RESOLVED"
        handoff.resolution_note = f"Automatically resolved: {recipient} has a permanent recipient/domain failure"
        await finalize_handoff_agent_run(
            session,
            handoff_id=handoff.id,
            actor="bounce-reconciler",
            outcome="permanent-bounce",
            cancelled=True,
        )
        if handoff.dingtalk_status != "SENT":
            handoff.dingtalk_status = "CANCELLED"
        notify_job = await session.scalar(select(Job).where(Job.idempotency_key == f"handoff-notify:{handoff.id}"))
        if notify_job is not None and notify_job.status in {JobStatus.PENDING, JobStatus.FAILED}:
            notify_job.status = JobStatus.DONE
            notify_job.last_error = "Cancelled: permanent bounce was handled automatically"
            notify_job.locked_at = None
            notify_job.locked_by = None
            notify_job.updated_at = datetime.now(UTC)
        await session.commit()
        resolved += 1
    return resolved
