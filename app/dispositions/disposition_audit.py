"""Conflict-aware rollback for reviewed inbound-disposition actions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import (
    AgentRun,
    AgentRunStatus,
    AgentStep,
    AgentStepStatus,
    AssistanceRequest,
    AssistanceStatus,
    AuditEvent,
    Contact,
    ContactReferral,
    Customer,
    DeliveryStatus,
    EmailMessage,
    Handoff,
    InboundDispositionAction,
    Job,
    JobStatus,
    Outbox,
    Quote,
    SalesCase,
)

SnapshotBuilder = Callable[
    [AsyncSession, EmailMessage],
    Awaitable[dict[str, Any]],
]


def _parse_snapshot_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


async def rollback_disposition_action(
    session: AsyncSession,
    *,
    action_id: int,
    actor: str,
    reason: str,
    snapshot_builder: SnapshotBuilder,
) -> dict[str, Any]:
    """Restore one disposition action if no irreversible/later change exists."""

    action = await session.scalar(
        select(InboundDispositionAction)
        .where(InboundDispositionAction.id == action_id)
        .with_for_update()
    )
    if action is None:
        raise ValueError("disposition action was not found")
    if action.status != "APPLIED":
        raise ValueError("only an applied disposition action can be rolled back")
    row = await session.scalar(
        select(EmailMessage)
        .where(EmailMessage.id == action.source_email_id)
        .with_for_update()
    )
    if row is None:
        raise ValueError("source email no longer exists")

    before = action.before_json or {}
    after = action.after_json or {}

    def snapshot_matches(current_value: Any, expected_value: Any) -> bool:
        """Compare against the action schema while tolerating newer snapshot keys."""

        if isinstance(expected_value, dict):
            return isinstance(current_value, dict) and all(
                key in current_value
                and snapshot_matches(current_value[key], expected)
                for key, expected in expected_value.items()
            )
        if isinstance(expected_value, list):
            return isinstance(current_value, list) and len(current_value) == len(
                expected_value
            ) and all(
                snapshot_matches(current_item, expected_item)
                for current_item, expected_item in zip(
                    current_value,
                    expected_value,
                    strict=True,
                )
            )
        return current_value == expected_value

    def snapshot_ids(key: str) -> set[int]:
        ids: set[int] = set()
        for snapshot in (before.get(key), after.get(key)):
            if isinstance(snapshot, dict) and isinstance(snapshot.get("id"), int):
                ids.add(snapshot["id"])
        return ids

    def snapshot_list_ids(key: str) -> set[int]:
        return {
            item["id"]
            for snapshot in (before.get(key) or [], after.get(key) or [])
            for item in [snapshot]
            if isinstance(item, dict) and isinstance(item.get("id"), int)
        }

    customer_ids = snapshot_ids("customer")
    contact_ids = snapshot_ids("contact") | snapshot_list_ids("target_contacts")
    case_ids = snapshot_ids("case")
    referral_ids = snapshot_list_ids("referrals")
    outbox_ids = snapshot_list_ids("outboxes")
    handoff_ids = snapshot_ids("handoff")
    run_ids = snapshot_ids("agent_run")
    assistance_ids = snapshot_list_ids("assistance_requests")
    step_ids = snapshot_list_ids("agent_steps")
    job_ids = snapshot_ids("notify_job")
    for model, ids in (
        (Customer, customer_ids),
        (Contact, contact_ids),
        (SalesCase, case_ids),
        (ContactReferral, referral_ids),
        (Outbox, outbox_ids),
        (Handoff, handoff_ids),
        (AgentRun, run_ids),
        (AssistanceRequest, assistance_ids),
        (AgentStep, step_ids),
        (Job, job_ids),
    ):
        if ids:
            await session.execute(
                select(model).where(model.id.in_(sorted(ids))).with_for_update()
            )
    current = await snapshot_builder(session, row)
    conflicts: list[str] = []

    for key in (
        "email",
        "contact",
        "customer",
        "case",
        "referrals",
        "target_contacts",
        "assistance_requests",
        "agent_steps",
    ):
        if before.get(key) != after.get(key) and not snapshot_matches(
            current.get(key), after.get(key)
        ):
            conflicts.append(f"{key.upper()}_CHANGED_AFTER_APPLY")
    for key in ("handoff", "agent_run", "notify_job"):
        # A new handoff may legitimately be created after a non-terminal apply;
        # restore only resources that this action itself changed.
        if before.get(key) != after.get(key) and not snapshot_matches(
            current.get(key), after.get(key)
        ):
            conflicts.append(f"{key.upper()}_CHANGED_AFTER_APPLY")

    before_outbox_ids = {item["id"] for item in before.get("outboxes") or []}
    after_outboxes = {
        item["id"]: item for item in after.get("outboxes") or []
    }
    created_outbox_ids = sorted(set(after_outboxes) - before_outbox_ids)
    created_outbox_message_ids = {
        after_outboxes[outbox_id]["message_id"]
        for outbox_id in created_outbox_ids
        if after_outboxes[outbox_id].get("message_id")
    }
    for outbox_id in created_outbox_ids:
        outbox = await session.get(Outbox, outbox_id)
        if outbox is None:
            conflicts.append(f"OUTBOX_{outbox_id}_MISSING_AFTER_APPLY")
        elif outbox.status in {
            DeliveryStatus.CLAIMED,
            DeliveryStatus.SENT,
            DeliveryStatus.UNKNOWN,
        }:
            conflicts.append(f"OUTBOX_{outbox_id}_{outbox.status.value}_IRREVERSIBLE")

    before_targets = {item["id"]: item for item in before.get("target_contacts") or []}
    after_targets = {item["id"]: item for item in after.get("target_contacts") or []}
    created_target_ids = sorted(set(after_targets) - set(before_targets))
    action_metadata = ((after.get("email") or {}).get("disposition_metadata") or {})
    before_case = before.get("case") or {}
    before_case_id = before_case.get("id") if isinstance(before_case, dict) else None
    before_case_ids = {before_case_id} if isinstance(before_case_id, int) else set()
    created_case_ids = (
        sorted(case_ids - before_case_ids)
        if "CREATE_REVIEW_CASE" in (action_metadata.get("applied_actions") or [])
        else []
    )
    removed_contact_ids: list[int] = []
    for contact_id in created_target_ids:
        target = await session.get(Contact, contact_id)
        if target is None:
            continue
        metadata = target.metadata_json or {}
        created_by_action = bool(
            metadata.get("source") == "inbound_contact_referral"
            and metadata.get("source_email_id") == row.id
        )
        if not created_by_action:
            continue
        later_email_filters = [
            EmailMessage.contact_id == contact_id,
            EmailMessage.id != row.id,
        ]
        if created_outbox_message_ids:
            later_email_filters.append(
                ~(
                    (EmailMessage.direction == "OUTBOUND")
                    & EmailMessage.message_id.in_(created_outbox_message_ids)
                )
            )
        later_email_count = await session.scalar(
            select(func.count())
            .select_from(EmailMessage)
            .where(*later_email_filters)
        )
        case_conditions = [SalesCase.contact_id == contact_id]
        if created_case_ids:
            case_conditions.append(SalesCase.id.not_in(created_case_ids))
        case_count = await session.scalar(
            select(func.count()).select_from(SalesCase).where(*case_conditions)
        )
        if (later_email_count or 0) > 0 or (case_count or 0) > 0:
            conflicts.append(f"NEW_CONTACT_{contact_id}_HAS_LATER_ACTIVITY")

    for case_id in created_case_ids:
        related_email_count = await session.scalar(
            select(func.count())
            .select_from(EmailMessage)
            .where(EmailMessage.case_id == case_id, EmailMessage.id != row.id)
        )
        related_outbox_count = await session.scalar(
            select(func.count()).select_from(Outbox).where(Outbox.case_id == case_id)
        )
        related_quote_count = await session.scalar(
            select(func.count()).select_from(Quote).where(Quote.case_id == case_id)
        )
        if any(
            count or 0
            for count in (
                related_email_count,
                related_outbox_count,
                related_quote_count,
            )
        ):
            conflicts.append(f"NEW_CASE_{case_id}_HAS_LATER_ACTIVITY")

    if conflicts:
        raise ValueError("rollback blocked: " + ", ".join(conflicts))

    removed_outbound_email_ids: list[int] = []
    for outbox_id in created_outbox_ids:
        outbox = await session.get(Outbox, outbox_id)
        if outbox is not None:
            outbox.status = DeliveryStatus.CANCELLED
            outbox.last_error = f"Rolled back by {actor[:128]}: {reason}"[:2000]
    for message_id in sorted(created_outbox_message_ids):
        staged_email = await session.scalar(
            select(EmailMessage).where(
                EmailMessage.direction == "OUTBOUND",
                EmailMessage.message_id == message_id,
            )
        )
        if staged_email is not None:
            removed_outbound_email_ids.append(staged_email.id)
            await session.delete(staged_email)

    current_referrals = {
        referral.id: referral
        for referral in (
            (
                await session.execute(
                    select(ContactReferral).where(
                        ContactReferral.source_email_id == row.id
                    )
                )
            )
            .scalars()
            .all()
        )
    }
    before_referrals = {item["id"]: item for item in before.get("referrals") or []}
    for referral_id, referral in list(current_referrals.items()):
        snapshot = before_referrals.get(referral_id)
        if snapshot is None:
            await session.delete(referral)
            continue
        referral.customer_id = snapshot["customer_id"]
        referral.original_contact_id = snapshot["original_contact_id"]
        referral.new_contact_id = snapshot["new_contact_id"]
        referral.referred_email = snapshot["referred_email"]
        referral.referred_name = snapshot["referred_name"]
        referral.relationship_type = snapshot["relationship_type"]
        referral.status = snapshot["status"]
        referral.forwarded_already = snapshot["forwarded_already"]
        referral.confidence = Decimal(snapshot["confidence"])
        referral.metadata_json = snapshot["metadata_json"]
    await session.flush()

    email_snapshot = before["email"]
    if "case_id" in email_snapshot:
        row.case_id = email_snapshot["case_id"]
    if "customer_id" in email_snapshot:
        row.customer_id = email_snapshot["customer_id"]
    if "contact_id" in email_snapshot:
        row.contact_id = email_snapshot["contact_id"]
    handoff_snapshot = before.get("handoff")
    if handoff_snapshot is not None and before.get("handoff") != after.get("handoff"):
        handoff = await session.get(Handoff, handoff_snapshot["id"])
        if handoff is not None:
            if "case_id" in handoff_snapshot:
                handoff.case_id = handoff_snapshot["case_id"]
    run_snapshot = before.get("agent_run")
    if run_snapshot is not None and before.get("agent_run") != after.get("agent_run"):
        run = await session.get(AgentRun, run_snapshot["id"])
        if run is not None:
            if "case_id" in run_snapshot:
                run.case_id = run_snapshot["case_id"]
    await session.flush()

    for case_id in created_case_ids:
        sales_case = await session.get(SalesCase, case_id)
        if sales_case is not None:
            await session.delete(sales_case)
    await session.flush()

    for contact_id in created_target_ids:
        target = await session.get(Contact, contact_id)
        if target is None:
            continue
        metadata = target.metadata_json or {}
        if (
            metadata.get("source") == "inbound_contact_referral"
            and metadata.get("source_email_id") == row.id
        ):
            await session.delete(target)
            removed_contact_ids.append(contact_id)

    contact_snapshot = before.get("contact")
    if contact_snapshot is not None and before.get("contact") != after.get("contact"):
        contact = await session.get(Contact, contact_snapshot["id"])
        if contact is not None:
            contact.suppressed = contact_snapshot["suppressed"]
            contact.lifecycle_status = contact_snapshot["lifecycle_status"]
            contact.unavailable_until = _parse_snapshot_datetime(
                contact_snapshot["unavailable_until"]
            )
    customer_snapshot = before.get("customer")
    if customer_snapshot is not None and before.get("customer") != after.get("customer"):
        customer = await session.get(Customer, customer_snapshot["id"])
        if customer is not None:
            customer.qualification_status = customer_snapshot["qualification_status"]
            customer.qualification_reason = customer_snapshot["qualification_reason"]
            customer.qualified_at = _parse_snapshot_datetime(
                customer_snapshot["qualified_at"]
            )

    if handoff_snapshot is not None and before.get("handoff") != after.get("handoff"):
        handoff = await session.get(Handoff, handoff_snapshot["id"])
        if handoff is not None:
            if "case_id" in handoff_snapshot:
                handoff.case_id = handoff_snapshot["case_id"]
            handoff.reason_code = handoff_snapshot.get(
                "reason_code", handoff.reason_code
            )
            handoff.summary = handoff_snapshot.get("summary", handoff.summary)
            handoff.status = handoff_snapshot["status"]
            handoff.dingtalk_status = handoff_snapshot["dingtalk_status"]
            handoff.resolution_note = handoff_snapshot["resolution_note"]
            handoff.extracted_facts = handoff_snapshot["extracted_facts"]
    if run_snapshot is not None and before.get("agent_run") != after.get("agent_run"):
        run = await session.get(AgentRun, run_snapshot["id"])
        if run is not None:
            if "case_id" in run_snapshot:
                run.case_id = run_snapshot["case_id"]
            run.status = AgentRunStatus(run_snapshot["status"])
            run.current_step = run_snapshot["current_step"]
            run.last_error = run_snapshot["last_error"]
            run.completed_at = _parse_snapshot_datetime(run_snapshot["completed_at"])
    if before.get("assistance_requests") != after.get("assistance_requests"):
        for snapshot in before.get("assistance_requests") or []:
            request = await session.get(AssistanceRequest, snapshot["id"])
            if request is not None:
                request.status = AssistanceStatus(snapshot["status"])
    if before.get("agent_steps") != after.get("agent_steps"):
        for snapshot in before.get("agent_steps") or []:
            step = await session.get(AgentStep, snapshot["id"])
            if step is not None:
                step.status = AgentStepStatus(snapshot["status"])
                step.completed_at = _parse_snapshot_datetime(snapshot["completed_at"])
    job_snapshot = before.get("notify_job")
    if job_snapshot is not None and before.get("notify_job") != after.get("notify_job"):
        job = await session.get(Job, job_snapshot["id"])
        if job is not None:
            job.status = JobStatus(job_snapshot["status"])
            job.last_error = job_snapshot["last_error"]
            job.locked_at = _parse_snapshot_datetime(job_snapshot["locked_at"])
            job.locked_by = job_snapshot["locked_by"]

    row.disposition_type = email_snapshot["disposition_type"]
    row.disposition_confidence = (
        Decimal(email_snapshot["disposition_confidence"])
        if email_snapshot["disposition_confidence"] is not None
        else None
    )
    row.disposition_metadata = email_snapshot["disposition_metadata"]
    row.disposition_handled_at = _parse_snapshot_datetime(
        email_snapshot["disposition_handled_at"]
    )
    row.automated_reply_handled_at = _parse_snapshot_datetime(
        email_snapshot["automated_reply_handled_at"]
    )
    action.status = "ROLLED_BACK"
    action.rolled_back_by = actor[:128]
    action.rolled_back_at = datetime.now(UTC)
    action.rollback_reason = reason[:2000]
    session.add(
        AuditEvent(
            case_id=row.case_id,
            actor=actor[:128],
            event_type="inbound.disposition_rolled_back",
            data={
                "email_id": row.id,
                "action_id": action.id,
                "disposition_type": action.disposition_type,
                "reason": reason[:2000],
                "cancelled_outbox_ids": created_outbox_ids,
                "removed_outbound_email_ids": removed_outbound_email_ids,
                "removed_contact_ids": removed_contact_ids,
            },
        )
    )
    await session.commit()
    return {
        "action_id": action.id,
        "email_id": row.id,
        "status": action.status,
        "cancelled_outbox_ids": created_outbox_ids,
        "removed_outbound_email_ids": removed_outbound_email_ids,
        "removed_contact_ids": removed_contact_ids,
    }

