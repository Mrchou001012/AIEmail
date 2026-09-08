"""Outbound pacing workflow."""

import hashlib
import smtplib
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import EmailMessage, MailboxThrottle, Outbox
from app.settings import Settings


def _message_activity_key(source: str, row_id: int, message_id: str | None) -> str:
    normalized = (message_id or "").strip().lower()
    return f"message-id:{normalized}" if normalized else f"{source}:{row_id}"


async def _mailbox_sent_events_since(
    session: AsyncSession,
    mailbox: str,
    since: datetime,
    until: datetime,
) -> dict[str, datetime]:
    events: dict[str, datetime] = {}
    email_rows = await session.execute(
        select(EmailMessage.id, EmailMessage.message_id, EmailMessage.received_at).where(
            EmailMessage.mailbox == mailbox,
            EmailMessage.direction == "OUTBOUND",
            EmailMessage.received_at >= since,
            EmailMessage.received_at <= until,
        )
    )
    for row_id, message_id, occurred_at in email_rows:
        key = _message_activity_key("email", row_id, message_id)
        events[key] = max(events.get(key, occurred_at), occurred_at)

    outbox_rows = await session.execute(
        select(Outbox.id, Outbox.message_id, Outbox.sent_at).where(
            Outbox.sent_via == "smtp",
            Outbox.sent_at >= since,
            Outbox.sent_at <= until,
        )
    )
    for row_id, message_id, sent_at in outbox_rows:
        if sent_at is None:
            continue
        key = _message_activity_key("outbox", row_id, message_id)
        events[key] = max(events.get(key, sent_at), sent_at)
    return events


def _send_interval_seconds(settings: Settings, message_id: str) -> int:
    if settings.send_interval_jitter_seconds == 0:
        return settings.min_send_interval_seconds
    digest = hashlib.sha256(message_id.encode("utf-8")).digest()
    jitter = int.from_bytes(digest[:4], "big") % (settings.send_interval_jitter_seconds + 1)
    return settings.min_send_interval_seconds + jitter


def _smtp_rate_limit_cooldown_seconds(exc: smtplib.SMTPResponseException, settings: Settings) -> int | None:
    detail = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
    normalized = detail.lower()
    daily_markers = ("5.4.5", "daily user sending limit", "daily smtp", "daily limit")
    rate_markers = ("4.7.28", "rate limit", "too many", "quota", "temporarily deferred")
    if any(marker in normalized for marker in daily_markers):
        return settings.gmail_daily_cooldown_seconds
    if exc.smtp_code in {550, 554} and ("limit" in normalized or "quota" in normalized):
        return settings.gmail_daily_cooldown_seconds
    if 400 <= exc.smtp_code < 500 or any(marker in normalized for marker in rate_markers):
        return settings.gmail_transient_cooldown_seconds
    return None


async def _set_mailbox_cooldown(
    session: AsyncSession,
    mailbox: str,
    cooldown_until: datetime,
    reason: str,
) -> None:
    throttle = await session.get(MailboxThrottle, mailbox, with_for_update=True)
    if throttle is None:
        session.add(
            MailboxThrottle(
                mailbox=mailbox,
                cooldown_until=cooldown_until,
                reason=reason,
            )
        )
        return
    if throttle.cooldown_until is None or throttle.cooldown_until < cooldown_until:
        throttle.cooldown_until = cooldown_until
        throttle.reason = reason
    throttle.updated_at = datetime.now(UTC)
