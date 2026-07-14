import asyncio
import logging
from datetime import UTC, datetime

from app.db import MailboxCursor, MailboxDailyUsage, SessionLocal
from app.history import reconcile_email_history
from app.mail import GmailIMAPClient
from app.services import ingest_raw_email
from app.settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


async def poll_folder_once(client: GmailIMAPClient, folder: str, direction: str) -> int:
    settings = get_settings()
    mailbox = settings.gmail_address
    if not mailbox or not settings.gmail_app_password:
        return 0
    usage_date = datetime.now(UTC).date()
    daily_limit_bytes = settings.imap_daily_download_limit_mb * 1024 * 1024
    async with SessionLocal() as session:
        cursor = await session.get(MailboxCursor, (mailbox, folder))
        last_uid = cursor.last_uid if cursor else 0
        expected_uid_validity = cursor.uid_validity if cursor else None
        usage = await session.get(MailboxDailyUsage, (mailbox, usage_date))
        used_bytes = usage.imap_download_bytes if usage else 0
    remaining_bytes = max(0, daily_limit_bytes - used_bytes)
    if remaining_bytes == 0:
        logger.warning(
            "IMAP daily download budget exhausted mailbox=%s used_mb=%.1f limit_mb=%s",
            mailbox,
            used_bytes / 1024 / 1024,
            settings.imap_daily_download_limit_mb,
        )
        return 0

    uid_validity, highest_uid, messages = await asyncio.to_thread(
        client.fetch_after,
        last_uid,
        expected_uid_validity,
        folder=folder,
        limit=settings.imap_batch_size,
        max_bytes=remaining_bytes,
    )
    downloaded_bytes = sum(len(raw) for _, raw in messages)
    if downloaded_bytes:
        async with SessionLocal() as session:
            usage = await session.get(MailboxDailyUsage, (mailbox, usage_date), with_for_update=True)
            if usage is None:
                usage = MailboxDailyUsage(
                    mailbox=mailbox,
                    usage_date=usage_date,
                    imap_download_bytes=0,
                )
                session.add(usage)
            usage.imap_download_bytes += downloaded_bytes
            usage.updated_at = datetime.now(UTC)
            await session.commit()
    async with SessionLocal() as session:
        cursor = await session.get(MailboxCursor, (mailbox, folder))
        if cursor is None:
            cursor = MailboxCursor(
                mailbox=mailbox,
                folder=folder,
                uid_validity=uid_validity,
                last_uid=0,
                history_cutoff_uid=highest_uid,
                history_complete=highest_uid == 0,
            )
            session.add(cursor)
        elif cursor.uid_validity != uid_validity:
            cursor.uid_validity = uid_validity
            cursor.last_uid = 0
            cursor.history_cutoff_uid = highest_uid
            cursor.history_complete = highest_uid == 0
        await session.commit()

    count = 0
    for uid, raw in messages:
        async with SessionLocal() as session:
            cursor = await session.get(MailboxCursor, (mailbox, folder))
            if cursor is None:
                raise RuntimeError(f"mailbox cursor disappeared for {folder}")
            cutoff = cursor.history_cutoff_uid or 0
            is_history = not cursor.history_complete and uid <= cutoff
            await ingest_raw_email(
                session,
                raw,
                mailbox=mailbox,
                mailbox_folder=folder,
                uid_validity=uid_validity,
                imap_uid=uid,
                direction=direction,
                is_history=is_history,
            )
            cursor.last_uid = max(cursor.last_uid, uid)
            if not cursor.history_complete and cursor.last_uid >= cutoff:
                cursor.history_complete = True
            await session.commit()
            count += 1

    async with SessionLocal() as session:
        cursor = await session.get(MailboxCursor, (mailbox, folder))
        if cursor and not cursor.history_complete and cursor.last_uid >= (cursor.history_cutoff_uid or 0):
            cursor.history_complete = True
            await session.commit()
    return count


async def poll_once() -> int:
    settings = get_settings()
    if not settings.imap_sync_enabled or not settings.gmail_address or not settings.gmail_app_password:
        return 0
    client = GmailIMAPClient(settings)
    total = 0
    succeeded = 0
    errors: list[tuple[str, Exception]] = []
    # Sent is intentionally synchronized first so Inbox replies can resolve
    # their In-Reply-To/References chain during the same polling cycle.
    for folder, direction in (
        (settings.imap_sent_folder, "OUTBOUND"),
        (settings.imap_folder, "INBOUND"),
    ):
        try:
            total += await poll_folder_once(client, folder, direction)
            succeeded += 1
        except Exception as exc:
            errors.append((folder, exc))
            logger.exception("IMAP folder poll failed: %s", folder)
    if succeeded == 0:
        raise RuntimeError("all configured IMAP folders failed")
    async with SessionLocal() as session:
        result = await reconcile_email_history(session)
        logger.info(
            "Gmail history reconciliation matched=%s unmatched=%s replies_waiting=%s no_reply_paused=%s",
            result.matched_messages,
            result.unmatched_messages,
            result.replies_waiting_review,
            result.no_reply_cases_paused,
        )
    if errors:
        failed_folders = ", ".join(folder for folder, _ in errors)
        raise RuntimeError(f"IMAP folder polling partially failed: {failed_folders}") from errors[0][1]
    return total


def _imap_backoff_seconds(settings, failure_count: int, exc: Exception) -> int:
    delay = settings.imap_poll_seconds * (2 ** min(failure_count, 8))
    messages: list[str] = []
    current: BaseException | None = exc
    while current is not None:
        messages.append(str(current).lower())
        current = current.__cause__
    detail = " ".join(messages)
    throttle_markers = ("rate limit", "too many", "throttl", "bandwidth", "4.7.28", "temporarily unavailable")
    if any(marker in detail for marker in throttle_markers):
        delay = max(delay, 600)
    return min(settings.imap_max_backoff_seconds, delay)


async def main() -> None:
    settings = get_settings()
    logger.info(
        "IMAP poller started; credentials configured=%s inbox=%s sent=%s batch=%s",
        bool(settings.gmail_address),
        settings.imap_folder,
        settings.imap_sent_folder,
        settings.imap_batch_size,
    )
    failure_count = 0
    while True:
        try:
            await poll_once()
        except Exception as exc:
            failure_count += 1
            delay = _imap_backoff_seconds(settings, failure_count, exc)
            logger.exception("IMAP poll failed; retrying in %s seconds", delay)
        else:
            failure_count = 0
            delay = settings.imap_poll_seconds
        await asyncio.sleep(delay)


if __name__ == "__main__":
    asyncio.run(main())
