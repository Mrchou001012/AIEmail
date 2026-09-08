"""Outbound claiming, policy gates, transport, retry, and reconciliation."""

import logging
import smtplib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy import case as sa_case
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.service_core import _human_approval_from_outbox, audit, create_handoff
from app.db import CaseStatus, DeliveryStatus, MailboxThrottle, Outbox, Quote, SalesCase
from app.delivery.delivery_safety import _normalize_forward_recipient, _suppress_email_address, recipient_preflight
from app.delivery.outbound_guards import _case_outbound_gate, _final_recipient_delivery_guard, _referral_outreach_eligibility_error
from app.delivery.outbound_pacing import (
    _mailbox_sent_events_since,
    _send_interval_seconds,
    _set_mailbox_cooldown,
    _smtp_rate_limit_cooldown_seconds,
)
from app.delivery.outbound_recovery import _cancel_and_requeue_stale_quote
from app.domain import HandoffReason
from app.inbound.bounces import BounceType, classify_smtp_failure
from app.mail import transport_for
from app.quotations.commercial import (
    QuoteContextStatus,
    get_commercial_data_provider,
    is_business_day,
    lock_commercial_scope,
    next_business_open,
)
from app.quotations.commercial_service import ensure_weekly_commercial_refresh
from app.reactivation import reactivation_send_guard
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


async def send_one_outbox(
    session: AsyncSession,
    settings: Settings | None = None,
    *,
    at: datetime | None = None,
    recipient_preflight_fn: Callable[..., Awaitable[tuple[str, str, dict[str, Any]]]] | None = None,
    transport_factory: Callable[[Settings], Any] | None = None,
) -> bool:
    settings = settings or get_settings()
    now = at or datetime.now(UTC)
    stale_before = now - timedelta(seconds=settings.outbox_lease_seconds)
    row = await session.scalar(
        select(Outbox)
        .where(
            or_(
                Outbox.status.in_([DeliveryStatus.PENDING, DeliveryStatus.FAILED]),
                and_(Outbox.status == DeliveryStatus.CLAIMED, Outbox.locked_at < stale_before),
            ),
            Outbox.available_at <= now,
        )
        # Live replies, quotations, and human-approved mail always stay ahead
        # of bulk reactivation messages, regardless of creation order.
        .order_by(
            sa_case((Outbox.message_kind == "REACTIVATION", 1), else_=0),
            Outbox.id,
        )
        .with_for_update(skip_locked=True)
    )
    if row is None:
        return False
    if row.attempts >= 5:
        row.status = DeliveryStatus.CANCELLED
        row.last_error = "outbox retry limit exhausted"
        await session.commit()
        failed_case = await session.get(SalesCase, row.case_id) if row.case_id else None
        if failed_case:
            await create_handoff(
                session,
                case=failed_case,
                reason=HandoffReason.MAIL_FAILURE,
                summary=f"Outbound delivery exhausted retries for {row.message_id}",
            )
        return True
    reclaimed_claim = row.status == DeliveryStatus.CLAIMED
    if reclaimed_claim and settings.mail_transport == "smtp":
        row.status = DeliveryStatus.UNKNOWN
        row.last_error = "stale SMTP claim requires Sent-folder reconciliation"
        await session.commit()
        return True
    mailbox = (settings.gmail_address or parseaddr(settings.mail_from)[1]).lower()
    if settings.mail_transport == "smtp":
        throttle = await session.get(MailboxThrottle, mailbox)
        if throttle and throttle.cooldown_until and throttle.cooldown_until > now:
            row.status = DeliveryStatus.PENDING
            row.available_at = throttle.cooldown_until
            row.last_error = f"mailbox cooldown active: {throttle.reason or 'Gmail rate limit'}"[:2000]
            await session.commit()
            return True
    case: SalesCase | None = None
    human_approved = _human_approval_from_outbox(row) is not None
    is_forward = row.message_kind == "FORWARD"
    if is_forward:
        try:
            normalized_forward_recipient = _normalize_forward_recipient(row.recipient)
        except ValueError as exc:
            row.status = DeliveryStatus.CANCELLED
            row.last_error = f"forward authorization failed: {exc}"[:2000]
            await session.commit()
            return True
        if not human_approved or normalized_forward_recipient != row.recipient:
            row.status = DeliveryStatus.CANCELLED
            row.last_error = "forward requires complete human approval metadata"
            await session.commit()
            return True
    if row.message_kind == "REFERRAL_OUTREACH":
        referral_error = await _referral_outreach_eligibility_error(
            session,
            row,
            settings=settings,
        )
        if referral_error:
            row.status = DeliveryStatus.CANCELLED
            row.last_error = f"referral contact eligibility changed: {referral_error}"[:2000]
            await session.commit()
            return True
    internal_message = human_approved
    if row.message_kind == "REACTIVATION":
        guard = await reactivation_send_guard(session, row, settings=settings, at=now)
        if guard.action == "DEFER":
            row.status = DeliveryStatus.PENDING
            row.available_at = guard.available_at or (now + timedelta(minutes=15))
            row.last_error = guard.reason
            await session.commit()
            return True
        if guard.action == "BLOCK":
            row.status = DeliveryStatus.CANCELLED
            row.last_error = guard.reason
            await session.commit()
            return True
    if settings.commercial_gate_enabled and not settings.demo_mode and not internal_message and not is_business_day(settings, now):
        row.status = DeliveryStatus.PENDING
        row.available_at = next_business_open(settings, now)
        row.last_error = "commercial gate deferred automated mail until Monday"
        await session.commit()
        return True
    case, case_action, case_reason, case_available_at = await _case_outbound_gate(
        session,
        row,
        at=now,
        human_approved=human_approved,
    )
    if case_action == "DEFER":
        row.status = DeliveryStatus.PENDING
        row.available_at = case_available_at or (now + timedelta(days=7))
        row.last_error = f"case gate deferred: {case_reason}"[:2000]
        await session.commit()
        return True
    if case_action == "BLOCK":
        row.status = DeliveryStatus.CANCELLED
        row.last_error = f"case/contact eligibility changed: {case_reason}"[:2000]
        await session.commit()
        return True
    is_auto_quote = not human_approved and (row.message_kind == "AUTO_QUOTE" or row.quote_id is not None)
    if settings.commercial_gate_enabled and not settings.demo_mode and is_auto_quote:
        quote = await session.get(Quote, row.quote_id) if row.quote_id is not None else None
        if case is None:
            row.status = DeliveryStatus.CANCELLED
            row.last_error = "commercial gate could not resolve the quote case"
            await session.commit()
            return True
        if case.product_id is None:
            row.status = DeliveryStatus.CANCELLED
            row.last_error = "commercial gate blocked a quote whose case product is pending"
            await session.commit()
            return True
        await lock_commercial_scope(session, settings.commercial_scope)
        context = await get_commercial_data_provider(settings).get_quote_context(
            session,
            product_id=case.product_id,
            currency=case.currency,
            requested_quantity=quote.quantity if quote is not None else None,
            at=now,
        )
        same_frozen_version = bool(
            quote is not None
            and quote.commercial_cycle_id == context.cycle.id
            and context.policy is not None
            and quote.price_policy_id == context.policy.id
        )
        if context.status is QuoteContextStatus.WAITING and same_frozen_version:
            await ensure_weekly_commercial_refresh(session, settings, at=now)
            row.status = DeliveryStatus.PENDING
            row.available_at = context.next_check_at or (now + timedelta(minutes=settings.commercial_retry_minutes))
            row.last_error = f"commercial data gate waiting: {context.reason}"[:2000]
            await session.commit()
            return True
        if context.status is not QuoteContextStatus.AVAILABLE or not same_frozen_version:
            await _cancel_and_requeue_stale_quote(
                session,
                row=row,
                quote=quote,
                cycle=context.cycle,
                reason=(
                    context.reason
                    if context.status is not QuoteContextStatus.AVAILABLE
                    else "frozen quote belongs to an older commercial data version"
                ),
            )
            return True
    if settings.mail_transport == "smtp":
        recipient = row.recipient.lower()
        if settings.safe_mode and recipient not in settings.recipient_allowlist:
            row.status = DeliveryStatus.CANCELLED
            row.last_error = "SAFE_MODE blocked recipient not on allowlist"
            await audit(
                session,
                "outbox.blocked_safe_mode",
                case_id=row.case_id,
                actor="policy",
                data={"recipient": recipient},
            )
            await session.commit()
            return True
        if not settings.auto_send_enabled and not human_approved:
            row.status = DeliveryStatus.CANCELLED
            row.last_error = "AUTO_SEND_ENABLED is false"
            await session.commit()
            return True
        preflight = recipient_preflight_fn or recipient_preflight
        preflight_outcome, preflight_detail, preflight_facts = await preflight(
            session,
            recipient,
            settings,
        )
        if preflight_outcome == "DEFER":
            row.status = DeliveryStatus.PENDING
            row.available_at = now + timedelta(minutes=settings.mx_temporary_retry_minutes)
            row.last_error = f"recipient preflight deferred: {preflight_detail}"[:2000]
            await audit(
                session,
                "outbox.preflight_deferred",
                case_id=row.case_id,
                actor="dns",
                data={"outbox_id": row.id, **preflight_facts},
            )
            await session.commit()
            return True
        if preflight_outcome == "BLOCK":
            row.status = DeliveryStatus.CANCELLED
            row.last_error = f"recipient preflight blocked: {preflight_detail}"[:2000]
            auto_suppressed = bool(preflight_facts.get("auto_suppressed"))
            if (
                auto_suppressed
                and case is not None
                and case.status
                not in {
                    CaseStatus.CLOSED_WON,
                    CaseStatus.CLOSED_LOST,
                }
            ):
                case.status = CaseStatus.PAUSED
            await audit(
                session,
                "outbox.preflight_blocked",
                case_id=row.case_id,
                actor="policy",
                data={"outbox_id": row.id, **preflight_facts},
            )
            await session.commit()
            if case is not None and not auto_suppressed:
                await create_handoff(
                    session,
                    case=case,
                    reason=HandoffReason.EMAIL_DELIVERABILITY,
                    summary=f"Recipient preflight blocked {recipient}: {preflight_detail}",
                    facts={"outbox_id": row.id, **preflight_facts},
                )
            return True
        await audit(
            session,
            "outbox.preflight_passed",
            case_id=row.case_id,
            actor="dns",
            data={"outbox_id": row.id, **preflight_facts},
        )
        since_hour = now - timedelta(hours=1)
        since_day = now - timedelta(days=1)
        sent_events = await _mailbox_sent_events_since(session, mailbox, since_day, now)
        hourly_events = {key: value for key, value in sent_events.items() if value >= since_hour}
        if len(hourly_events) >= settings.max_sends_per_hour:
            row.status = DeliveryStatus.PENDING
            row.available_at = min(hourly_events.values()) + timedelta(hours=1)
            row.last_error = "mailbox-wide hourly send limit deferred message"
            await session.commit()
            return True
        if len(sent_events) >= settings.max_sends_per_day:
            row.status = DeliveryStatus.PENDING
            row.available_at = min(sent_events.values()) + timedelta(days=1)
            row.last_error = "mailbox-wide rolling 24-hour send limit deferred message"
            await session.commit()
            return True
        if sent_events:
            last_sent_at = max(sent_events.values())
            next_send_at = last_sent_at + timedelta(seconds=_send_interval_seconds(settings, row.message_id))
            if next_send_at > now:
                row.status = DeliveryStatus.PENDING
                row.available_at = next_send_at
                row.last_error = "mailbox-wide send spacing deferred message"
                await session.commit()
                return True
    row.status = DeliveryStatus.CLAIMED
    row.locked_at = datetime.now(UTC)
    row.attempts += 1
    await session.commit()
    if not await _final_recipient_delivery_guard(
        session,
        row,
        settings=settings,
        at=now,
    ):
        return True
    try:
        (transport_factory or transport_for)(settings).send(
            row.raw_message,
            row.message_id,
            row.recipient,
        )
        row.status = DeliveryStatus.SENT
        row.sent_at = datetime.now(UTC)
        row.sent_via = settings.mail_transport
        row.last_error = None
        await audit(
            session,
            "outbox.sent",
            case_id=row.case_id,
            actor=settings.mail_transport,
            data={
                "outbox_id": row.id,
                "message_id": row.message_id,
                "approval_handoff_id": row.approval_handoff_id,
                "human_approved_by": row.human_approved_by,
            },
        )
    except (smtplib.SMTPServerDisconnected, ConnectionResetError, TimeoutError) as exc:
        row.status = DeliveryStatus.UNKNOWN
        row.last_error = f"ambiguous transport outcome: {exc}"
    except smtplib.SMTPResponseException as exc:
        cooldown_seconds = _smtp_rate_limit_cooldown_seconds(exc, settings)
        detail = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        if cooldown_seconds is None:
            failure_type = classify_smtp_failure(exc.smtp_code, detail)
            if failure_type == BounceType.HARD:
                row.status = DeliveryStatus.CANCELLED
                row.last_error = f"permanent SMTP recipient failure {exc.smtp_code}: {detail}"[:2000]
                await _suppress_email_address(
                    session,
                    row.recipient,
                    reason="SMTP_HARD_BOUNCE",
                    bounce_type=failure_type.value,
                    diagnostic=detail,
                )
                if case and case.status == CaseStatus.ACTIVE:
                    case.status = CaseStatus.PAUSED
                await audit(
                    session,
                    "outbox.smtp_hard_bounce_suppressed",
                    case_id=row.case_id,
                    actor="smtp",
                    data={"outbox_id": row.id, "smtp_code": exc.smtp_code, "diagnostic": detail[:2000]},
                )
            else:
                row.status = DeliveryStatus.FAILED
                row.last_error = f"SMTP {exc.smtp_code}: {detail}"[:2000]
                row.available_at = datetime.now(UTC) + timedelta(minutes=min(60, 2**row.attempts))
        else:
            cooldown_until = datetime.now(UTC) + timedelta(seconds=cooldown_seconds)
            reason = f"Gmail SMTP {exc.smtp_code}: {detail}"[:2000]
            await _set_mailbox_cooldown(session, mailbox, cooldown_until, reason)
            row.status = DeliveryStatus.PENDING
            row.attempts = max(0, row.attempts - 1)
            row.available_at = cooldown_until
            row.last_error = reason
            await audit(
                session,
                "outbox.gmail_cooldown",
                case_id=row.case_id,
                actor="smtp",
                data={"outbox_id": row.id, "smtp_code": exc.smtp_code, "cooldown_seconds": cooldown_seconds},
            )
    except Exception as exc:
        row.status = DeliveryStatus.FAILED
        row.last_error = str(exc)[:2000]
        row.available_at = datetime.now(UTC) + timedelta(minutes=min(60, 2**row.attempts))
    await session.commit()
    return True
