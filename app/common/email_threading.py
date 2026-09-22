"""MIME-safe reply threading and forwarding transformations."""

import html
import logging
from datetime import UTC, datetime

from app.db import (
    EmailMessage,
)
from app.mail import (
    FullReplySource,
    OutboundAttachment,
    _sanitize_quoted_html,
    extract_full_reply_source,
    html_requires_mime_resources,
)
from app.settings import get_settings

logger = logging.getLogger(__name__)


def _extract_forward_attachments(raw: bytes) -> tuple[OutboundAttachment, ...]:
    """Collect non-inline MIME parts as forward attachments."""
    from email import policy as _email_policy
    from email.parser import BytesParser as _BytesParser

    message = _BytesParser(policy=_email_policy.default).parsebytes(raw)
    result: list[OutboundAttachment] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        content_id = str(part.get("Content-ID") or "").strip()
        content_location = str(part.get("Content-Location") or "").strip()
        inline_resource = bool(
            (content_id or content_location) and (disposition == "inline" or part.get_content_type().startswith("image/"))
        )
        if disposition == "attachment" or (filename and not inline_resource):
            payload = part.get_payload(decode=True) or b""
            if payload:
                result.append(
                    OutboundAttachment(
                        filename=filename or "unnamed",
                        content_type=part.get_content_type(),
                        payload=payload,
                    )
                )
    return tuple(result)


def _forwarded_message_bodies(
    *,
    note: str,
    source: FullReplySource,
    original_from: str,
    original_to: str,
    original_subject: str,
    occurred_at: datetime | None,
) -> tuple[str, str]:
    """Build a Gmail-style forwarded message with text and HTML preserved."""
    timestamp = occurred_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    date_line = timestamp.astimezone(UTC).strftime("%a, %d %b %Y %H:%M %z")
    body_text = source.body_text or ""
    header_lines = [
        "---------- Forwarded message ---------",
        f"From: {original_from}",
        f"Date: {date_line}",
        f"Subject: {original_subject}",
        f"To: {original_to}",
    ]
    text = "\n".join(header_lines) + "\n\n" + body_text
    if note.strip():
        text = f"{note.strip()}\n\n{text}"

    header_html = "".join(
        f"<b>{html.escape(label)}:</b> {html.escape(value)}<br>"
        for label, value in (
            ("From", original_from),
            ("Date", date_line),
            ("Subject", original_subject),
            ("To", original_to),
        )
    )
    if source.body_html:
        quoted_html = _sanitize_quoted_html(source.body_html)
    else:
        quoted_html = '<div style="white-space:pre-wrap">' + html.escape(body_text) + "</div>"
    note_html = f"<p>{html.escape(note.strip())}</p>" if note.strip() else ""
    html_body = (
        "<div>"
        f"{note_html}"
        "<p><b>---------- Forwarded message ---------</b></p>"
        f"<p>{header_html}</p>"
        f'<div class="gmail_quote">{quoted_html}</div>'
        "</div>"
    )
    return text, html_body


def _reply_references(source_email: EmailMessage) -> list[str]:
    """Build a complete, ordered RFC reply chain for a response."""
    return list(
        dict.fromkeys(
            item
            for item in [
                *source_email.references_json,
                source_email.in_reply_to,
                source_email.message_id,
            ]
            if item
        )
    )


MAX_REPLY_SOURCE_ARCHIVE_BYTES = 30 * 1024 * 1024


def _reply_source(source_email: EmailMessage) -> FullReplySource:
    """Load the complete direct-parent display body and its inline resources."""
    archive_folder = "mail_archive" if source_email.is_history else "inbound_archive"
    archive_path = get_settings().runtime_dir / archive_folder / f"{source_email.raw_sha256}.eml"
    try:
        archive_size = archive_path.stat().st_size
        raw = archive_path.read_bytes()
    except OSError as exc:
        if html_requires_mime_resources(source_email.body_html):
            raise RuntimeError(f"complete reply source with inline images is unavailable for email_id={source_email.id}") from exc
        logger.warning(
            "Complete reply archive unavailable for email_id=%s; using stored body without MIME resources",
            source_email.id,
        )
        return FullReplySource(
            body_text=source_email.body_text,
            body_html=source_email.body_html,
        )
    if archive_size > MAX_REPLY_SOURCE_ARCHIVE_BYTES:
        raise RuntimeError(f"complete reply source exceeds {MAX_REPLY_SOURCE_ARCHIVE_BYTES} bytes")
    try:
        return extract_full_reply_source(raw)
    except (ValueError, LookupError, RecursionError) as exc:
        raise RuntimeError(f"complete reply source could not preserve inline content for email_id={source_email.id}") from exc
