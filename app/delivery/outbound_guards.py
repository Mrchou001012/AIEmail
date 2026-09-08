"""Outbound guards workflow."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.common.service_core import _human_approval_from_outbox, audit
from app.db import CaseStatus, Contact, Customer, DeliveryStatus, EmailMessage, Outbox, ReactivationRecipient, SalesCase
from app.delivery.delivery_safety import _email_address_status, _lock_recipient_delivery_gate, _normalize_forward_recipient
from app.reactivation import reactivation_send_guard
from app.settings import Settings


async def _case_outbound_gate(
    session: AsyncSession,
    row: Outbox,
    *,
    at: datetime,
    human_approved: bool,
) -> tuple[SalesCase | None, str, str | None, datetime | None]:
    if row.message_kind == "FORWARD":
        return None, "PASS", None, None
    if row.case_id is None:
        return None, "PASS", None, None
    case = await session.scalar(
        select(SalesCase)
        .options(
            selectinload(SalesCase.customer),
            selectinload(SalesCase.contact),
        )
        .where(SalesCase.id == row.case_id)
    )
    if case is None:
        return None, "BLOCK", "case no longer exists", None
    if case.contact.suppressed or case.customer.do_not_contact or case.contact.email.strip().casefold() != row.recipient.strip().casefold():
        return case, "BLOCK", "case/contact eligibility changed", None
    if human_approved:
        if case.status in {CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST}:
            return case, "BLOCK", "case is closed", None
        return case, "PASS", None, None
    if case.customer.qualification_status == "NON_TARGET" or case.contact.lifecycle_status == "DEPARTED":
        return case, "BLOCK", "customer or contact is no longer a sales target", None
    if case.contact.lifecycle_status == "TEMPORARILY_UNAVAILABLE":
        unavailable_until = case.contact.unavailable_until
        if unavailable_until is None or unavailable_until > at:
            return (
                case,
                "DEFER",
                "contact is temporarily unavailable",
                unavailable_until or (at + timedelta(days=7)),
            )
        case.contact.lifecycle_status = "ACTIVE"
        case.contact.unavailable_until = None
    if case.status != CaseStatus.ACTIVE or not case.customer.auto_send_allowed:
        return case, "BLOCK", "case/customer is not eligible for autonomous mail", None
    return case, "PASS", None, None


async def _final_recipient_delivery_guard(
    session: AsyncSession,
    row: Outbox,
    *,
    settings: Settings,
    at: datetime,
) -> bool:
    """Re-check mutable recipient state after claiming and immediately before send.

    The advisory lock remains held by the transaction while the transport is
    called, so the API cannot commit a conflicting endpoint suppression between
    this check and SMTP delivery.
    """
    await _lock_recipient_delivery_gate(session, row.recipient)
    current = await session.scalar(select(Outbox).where(Outbox.id == row.id).execution_options(populate_existing=True))
    if current is None or current.status != DeliveryStatus.CLAIMED:
        await session.commit()
        return False

    if current.message_kind == "FORWARD":
        complete_approval = _human_approval_from_outbox(current) is not None
        try:
            normalized_forward_recipient = _normalize_forward_recipient(current.recipient)
        except ValueError as exc:
            current.status = DeliveryStatus.CANCELLED
            current.last_error = f"final forward authorization failed: {exc}"[:2000]
            await session.commit()
            return False
        if not complete_approval or normalized_forward_recipient != current.recipient:
            current.status = DeliveryStatus.CANCELLED
            current.last_error = "final forward gate requires complete human approval metadata"
            await session.commit()
            return False

    if current.message_kind == "REFERRAL_OUTREACH":
        referral_error = await _referral_outreach_eligibility_error(
            session,
            current,
            settings=settings,
        )
        if referral_error:
            current.status = DeliveryStatus.CANCELLED
            current.last_error = f"final referral gate blocked: {referral_error}"[:2000]
            await session.commit()
            return False

    current_human_approved = _human_approval_from_outbox(current) is not None
    _, case_action, case_reason, case_available_at = await _case_outbound_gate(
        session,
        current,
        at=at,
        human_approved=current_human_approved,
    )
    if case_action == "DEFER":
        current.status = DeliveryStatus.PENDING
        current.attempts = max(0, current.attempts - 1)
        current.available_at = case_available_at or (at + timedelta(days=7))
        current.last_error = f"final case gate deferred: {case_reason}"[:2000]
        await session.commit()
        return False
    if case_action == "BLOCK":
        current.status = DeliveryStatus.CANCELLED
        current.last_error = f"final case gate blocked: {case_reason}"[:2000]
        await session.commit()
        return False

    address_status = await _email_address_status(session, current.recipient)
    if address_status.suppressed:
        current.status = DeliveryStatus.CANCELLED
        current.last_error = (f"final delivery gate blocked suppressed recipient: {address_status.suppression_reason or 'unspecified'}")[
            :2000
        ]
        campaign_recipient = await session.scalar(select(ReactivationRecipient).where(ReactivationRecipient.outbox_id == current.id))
        if campaign_recipient is not None and campaign_recipient.status not in {"SENT", "REPLIED"}:
            campaign_recipient.status = "SKIPPED"
            campaign_recipient.exclusion_reason = "CONTACT_SUPPRESSED"
        await audit(
            session,
            "outbox.blocked_final_recipient_gate",
            case_id=current.case_id,
            actor="policy",
            data={
                "outbox_id": current.id,
                "recipient": current.recipient.strip().casefold(),
                "suppression_reason": address_status.suppression_reason,
            },
        )
        await session.commit()
        return False

    if current.message_kind == "REACTIVATION":
        guard = await reactivation_send_guard(
            session,
            current,
            settings=settings,
            at=at,
        )
        if guard.action == "DEFER":
            current.status = DeliveryStatus.PENDING
            current.attempts = max(0, current.attempts - 1)
            current.available_at = guard.available_at or (at + timedelta(minutes=15))
            current.last_error = guard.reason
            await session.commit()
            return False
        if guard.action == "BLOCK":
            current.status = DeliveryStatus.CANCELLED
            current.last_error = guard.reason
            await session.commit()
            return False
    return True


async def _referral_outreach_eligibility_error(
    session: AsyncSession,
    row: Outbox,
    *,
    settings: Settings,
) -> str | None:
    if not settings.referral_auto_contact_enabled:
        return "REFERRAL_AUTO_CONTACT_ENABLED is false"
    referral_email = await session.scalar(
        select(EmailMessage)
        .where(
            EmailMessage.message_id == row.message_id,
            EmailMessage.direction == "OUTBOUND",
        )
        .limit(1)
    )
    referral_contact = (
        await session.get(Contact, referral_email.contact_id)
        if referral_email is not None and referral_email.contact_id is not None
        else None
    )
    referral_customer = (
        await session.get(Customer, referral_email.customer_id)
        if referral_email is not None and referral_email.customer_id is not None
        else None
    )
    if referral_email is None or referral_contact is None or referral_customer is None:
        return "referral email, contact, or customer is missing"
    if referral_contact.customer_id != referral_customer.id:
        return "referral contact no longer belongs to the resolved customer"
    if referral_contact.email.strip().casefold() != row.recipient.strip().casefold():
        return "referral recipient no longer matches the contact"
    if referral_contact.suppressed or referral_contact.lifecycle_status != "ACTIVE":
        return "referral contact is no longer active"
    if (
        referral_customer.do_not_contact
        or referral_customer.qualification_status == "NON_TARGET"
        or not referral_customer.auto_send_allowed
    ):
        return "referral customer is no longer eligible"
    return None
