import base64
import email
import hashlib
import html
import imaplib
import logging
import re
import smtplib
import ssl
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage as MIMEEmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import Contact, EmailMessage, Outbox, SalesCase
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)
SIGNATURE_LOGO_CID = "lanyachem-logo"


def _attach_signature_logo(message: MIMEEmailMessage, html_body: str, token: str) -> None:
    if f"cid:{SIGNATURE_LOGO_CID}" not in html_body:
        return
    logo_path = get_settings().content_dir / "email_signature_logo.b64"
    try:
        logo_bytes = base64.b64decode(logo_path.read_text(encoding="ascii").strip(), validate=True)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"signature logo asset is unavailable or invalid: {logo_path}") from exc
    if not logo_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("signature logo asset is not a PNG image")
    html_part = message.get_payload()[-1]
    html_part.add_related(
        logo_bytes,
        maintype="image",
        subtype="png",
        cid=f"<{SIGNATURE_LOGO_CID}>",
        filename="lanyachem-logo.png",
        disposition="inline",
    )
    html_part.set_boundary(f"=_sales_agent_related_{token}")


def _imap_mailbox_arg(folder: str) -> str:
    """Return a safely quoted IMAP mailbox argument, including names with spaces."""
    escaped = folder.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


@dataclass(frozen=True)
class ParsedEmail:
    message_id: str | None
    in_reply_to: str | None
    references: list[str]
    from_address: str
    to_addresses: list[str]
    subject: str
    body_text: str
    body_html: str | None
    attachments: list[dict[str, Any]]
    header_metadata: dict[str, str]
    raw_sha256: str
    occurred_at: datetime | None


SAFE_INLINE_IMAGE_SUFFIXES = frozenset({".gif", ".jpeg", ".jpg", ".png", ".webp"})
SAFE_INLINE_IMAGE_CONTENT_TYPES = frozenset({"application/octet-stream"})
MAX_SAFE_INLINE_IMAGE_BYTES = 256 * 1024
MAX_QUOTED_REPLY_CHARS = 20_000


def is_safe_inline_image_attachment(item: dict[str, Any]) -> bool:
    """Recognize small CID images embedded by mail clients and signatures."""
    filename = str(item.get("filename") or "").strip()
    content_id = str(item.get("content_id") or "").strip()
    disposition = str(item.get("disposition") or "").strip().casefold()
    content_type = str(item.get("content_type") or "").strip().casefold()
    try:
        size = int(item.get("size") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        content_id
        and disposition != "attachment"
        and Path(filename).suffix.casefold() in SAFE_INLINE_IMAGE_SUFFIXES
        and (content_type.startswith("image/") or content_type in SAFE_INLINE_IMAGE_CONTENT_TYPES)
        and 0 < size <= MAX_SAFE_INLINE_IMAGE_BYTES
    )


def attachments_require_review(attachments: list[dict[str, Any]]) -> bool:
    """Require review for every attachment except a tightly bounded inline image."""
    return any(not is_safe_inline_image_attachment(item) for item in attachments)


def append_quoted_reply(
    text_body: str,
    html_body: str,
    *,
    from_address: str,
    source_body: str,
    occurred_at: datetime | None,
) -> tuple[str, str]:
    """Append one sanitized previous-message block without nesting older quotes."""
    clean_source = source_body.strip()
    if not clean_source:
        return text_body, html_body
    if len(clean_source) > MAX_QUOTED_REPLY_CHARS:
        clean_source = (
            clean_source[:MAX_QUOTED_REPLY_CHARS].rstrip()
            + "\n[Previous message truncated]"
        )
    timestamp = occurred_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    intro = f"On {timestamp.strftime('%a, %d %b %Y %H:%M %z')}, {from_address} wrote:"
    quoted_plain = "\n".join(f"> {line}" if line else ">" for line in clean_source.splitlines())
    quoted_html = "<br>".join(
        html.escape(line) if line else "&nbsp;" for line in clean_source.splitlines()
    )
    return (
        f"{text_body.rstrip()}\n\n{intro}\n{quoted_plain}",
        (
            f"{html_body.rstrip()}"
            '<div class="aiemail-quoted-reply" style="margin-top:1em">'
            f"<div>{html.escape(intro)}</div>"
            '<blockquote style="margin:0.5em 0 0 0.8ex;border-left:1px solid #ccc;'
            f'padding-left:1ex">{quoted_html}</blockquote></div>'
        ),
    )


def _decode_part(part: email.message.Message) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def _clean_plain(text: str) -> str:
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        normalized_line = html.unescape(line).replace("\xa0", " ").strip()
        if line.lstrip().startswith(">"):
            continue
        if re.match(r"^On .+ wrote:$", normalized_line, re.I):
            break
        if re.search(
            r"在\s*\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日.*?写道\s*[:：]",
            normalized_line,
            re.I,
        ):
            break
        if re.match(
            r"^-+\s*(?:Original Message|原始邮件|原始郵件)\s*-+$",
            normalized_line,
            re.I,
        ):
            break
        if normalized_line in {"--", "-- "}:
            break
        lines.append(line)
    return "\n".join(lines).strip()[:100_000]


def _html_to_text(value: str) -> str:
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup(["script", "style", "iframe", "object"]):
        tag.decompose()
    return _clean_plain(soup.get_text("\n"))


def parse_mime(raw: bytes) -> ParsedEmail:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict[str, Any]] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        content_type = part.get_content_type()
        if disposition == "attachment" or filename:
            payload = part.get_payload(decode=True) or b""
            attachments.append(
                {
                    "filename": filename or "unnamed",
                    "content_type": content_type,
                    "disposition": disposition,
                    "content_id": str(part.get("Content-ID") or "").strip() or None,
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
            continue
        if content_type == "text/plain":
            plain_parts.append(_decode_part(part))
        elif content_type == "text/html":
            html_parts.append(_decode_part(part))
    body_html = "\n".join(html_parts) or None
    body_text = _clean_plain("\n".join(plain_parts))
    if not body_text and body_html:
        body_text = _html_to_text(body_html)
    references = re.findall(r"<[^>]+>", str(message.get("References", "")))
    recipient_headers = [
        *message.get_all("To", []),
        *message.get_all("Cc", []),
        *message.get_all("Bcc", []),
    ]
    to_addresses = [addr.lower() for _, addr in getaddresses(recipient_headers) if addr]
    occurred_at = None
    if message.get("Date"):
        try:
            occurred_at = parsedate_to_datetime(str(message.get("Date")))
            if occurred_at.tzinfo is None:
                occurred_at = occurred_at.replace(tzinfo=UTC)
            else:
                occurred_at = occurred_at.astimezone(UTC)
        except (TypeError, ValueError, OverflowError):
            occurred_at = None
    relevant_headers = (
        "Auto-Submitted",
        "Precedence",
        "X-Autoreply",
        "X-Autorespond",
        "X-Auto-Response-Suppress",
    )
    header_metadata = {
        name.casefold(): str(message.get(name))[:1000]
        for name in relevant_headers
        if message.get(name) is not None
    }
    return ParsedEmail(
        message_id=str(message.get("Message-ID")) if message.get("Message-ID") else None,
        in_reply_to=str(message.get("In-Reply-To")) if message.get("In-Reply-To") else None,
        references=references,
        from_address=parseaddr(str(message.get("From", "")))[1].lower(),
        to_addresses=to_addresses,
        subject=str(message.get("Subject", ""))[:998],
        body_text=body_text,
        body_html=body_html,
        attachments=attachments,
        header_metadata=header_metadata,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        occurred_at=occurred_at,
    )


THREAD_SUBJECT_PREFIX = re.compile(
    r"^(?:(?:re|fw|fwd|aw|sv|回复|答复|回覆|转发|轉寄)\s*[:：]\s*)+",
    flags=re.I,
)


def has_thread_subject_prefix(subject: str) -> bool:
    return THREAD_SUBJECT_PREFIX.match(subject.strip()) is not None


def normalized_subject(subject: str) -> str:
    result = THREAD_SUBJECT_PREFIX.sub("", subject.strip())
    return re.sub(r"\s+", " ", result).lower()


async def match_case(
    session: AsyncSession,
    parsed: ParsedEmail,
    *,
    direction: str = "INBOUND",
    allow_subject_fallback: bool = True,
) -> tuple[SalesCase | None, bool]:
    direction = direction.upper()
    if direction not in {"INBOUND", "OUTBOUND"}:
        raise ValueError(f"unsupported email direction: {direction}")
    participants = [parsed.from_address] if direction == "INBOUND" else parsed.to_addresses
    candidates: list[int] = []
    message_ids = [item for item in [parsed.in_reply_to, *parsed.references] if item]
    if message_ids:
        rows = (
            (
                await session.execute(
                    select(EmailMessage.case_id).where(EmailMessage.message_id.in_(message_ids), EmailMessage.case_id.is_not(None))
                )
            )
            .scalars()
            .all()
        )
        candidates.extend([row for row in rows if row is not None])
    outbox_message_ids = [*message_ids]
    if direction == "OUTBOUND" and parsed.message_id:
        outbox_message_ids.append(parsed.message_id)
    if outbox_message_ids:
        outbox_rows = (
            (
                await session.execute(
                    select(Outbox.case_id).where(
                        Outbox.message_id.in_(outbox_message_ids),
                        Outbox.case_id.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        candidates.extend([row for row in outbox_rows if row is not None])
    unique = set(candidates)
    if len(unique) == 1:
        case = await session.scalar(select(SalesCase).options(selectinload(SalesCase.contact)).where(SalesCase.id == unique.pop()))
        if case and case.contact.email.lower() in participants:
            return case, False
        return None, True
    if len(unique) > 1:
        return None, True

    if not allow_subject_fallback:
        return None, False

    subject = normalized_subject(parsed.subject)
    rows = (
        (
            await session.execute(
                select(SalesCase)
                .join(Contact, SalesCase.contact_id == Contact.id)
                .where(
                    SalesCase.subject_key == subject,
                    Contact.email.in_(participants),
                )
            )
        )
        .scalars()
        .all()
    )
    if len(rows) == 1:
        return rows[0], False
    return None, len(rows) > 1


def build_message(
    *,
    from_address: str,
    recipient: str,
    subject: str,
    text_body: str,
    html_body: str,
    stable_key: str,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
) -> tuple[str, str]:
    domain = from_address.split("@")[-1] if "@" in from_address else "localhost"
    token = uuid.uuid5(uuid.NAMESPACE_URL, stable_key).hex
    message_id = f"<{token}@{domain}>"
    msg = MIMEEmailMessage(policy=policy.SMTP)
    display, address = parseaddr(from_address)
    if display and address:
        msg["From"] = Address(display_name=display, addr_spec=address)
    else:
        msg["From"] = from_address
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = " ".join(references[-20:])
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    _attach_signature_logo(msg, html_body, token)
    msg.set_boundary(f"=_sales_agent_{token}")
    return message_id, msg.as_string(policy=policy.SMTP)


class FileMailTransport:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def send(self, raw_message: str, message_id: str, recipient: str) -> None:
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", message_id.strip("<>"))
        target = self.output_dir / f"{safe_name}.eml"
        raw_bytes = raw_message.encode("utf-8")
        if target.exists() and target.read_bytes() == raw_bytes:
            return
        target.write_bytes(raw_bytes)


class SMTPTransport:
    def __init__(self, settings: Settings):
        self.settings = settings

    def send(self, raw_message: str, message_id: str, recipient: str) -> None:
        if not self.settings.gmail_address or not self.settings.gmail_app_password:
            raise RuntimeError("Gmail SMTP credentials are not configured")
        context = ssl.create_default_context()
        if self.settings.smtp_starttls:
            with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=30) as smtp:
                smtp.starttls(context=context)
                smtp.login(self.settings.gmail_address, self.settings.gmail_app_password)
                smtp.sendmail(parseaddr(self.settings.mail_from)[1], [recipient], raw_message)
        else:
            with smtplib.SMTP_SSL(self.settings.smtp_host, self.settings.smtp_port, context=context, timeout=30) as smtp:
                smtp.login(self.settings.gmail_address, self.settings.gmail_app_password)
                smtp.sendmail(parseaddr(self.settings.mail_from)[1], [recipient], raw_message)


def transport_for(settings: Settings | None = None) -> FileMailTransport | SMTPTransport:
    settings = settings or get_settings()
    if settings.mail_transport == "smtp":
        return SMTPTransport(settings)
    if settings.mail_transport == "file":
        return FileMailTransport(settings.runtime_dir / "demo_outbox")
    raise RuntimeError(f"unsupported mail transport: {settings.mail_transport}")


class GmailIMAPClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    @staticmethod
    def _close_quietly(client: imaplib.IMAP4_SSL | None) -> None:
        if client is None:
            return
        try:
            client.logout()
        except (imaplib.IMAP4.error, OSError, ssl.SSLError):
            # A remote EOF commonly makes LOGOUT fail too; do not hide the
            # original fetch result or exception with a cleanup error.
            pass

    def _open_folder(self, folder: str) -> tuple[imaplib.IMAP4_SSL, int]:
        client = imaplib.IMAP4_SSL(
            self.settings.imap_host,
            self.settings.imap_port,
            timeout=30,
        )
        try:
            client.login(self.settings.gmail_address, self.settings.gmail_app_password)
            status, _ = client.select(_imap_mailbox_arg(folder), readonly=True)
            if status != "OK":
                raise RuntimeError(f"unable to select IMAP folder: {folder}")
            uid_validity_response = client.response("UIDVALIDITY")[1]
            uid_validity = int(uid_validity_response[0]) if uid_validity_response else 0
            return client, uid_validity
        except Exception:
            self._close_quietly(client)
            raise

    def fetch_after(
        self,
        last_uid: int,
        expected_uid_validity: int | None = None,
        *,
        folder: str | None = None,
        limit: int | None = None,
        max_bytes: int | None = None,
    ) -> tuple[int, int, list[tuple[int, bytes]]]:
        if not self.settings.gmail_address or not self.settings.gmail_app_password:
            return 0, 0, []
        selected_folder = folder or self.settings.imap_folder
        batch_limit = limit or self.settings.imap_batch_size
        client: imaplib.IMAP4_SSL | None = None
        try:
            client, uid_validity = self._open_folder(selected_folder)
            search_after = 0 if expected_uid_validity not in {None, uid_validity} else last_uid
            status, data = client.uid("search", None, f"UID {search_after + 1}:*")
            if status != "OK":
                raise RuntimeError(f"IMAP UID search failed for folder: {selected_folder}")
            # IMAP sequence ranges are order-independent. If search_after is
            # already the highest UID, a server may interpret "N+1:*" as a
            # reversed range and return UID N again. Enforce the exclusive
            # lower bound locally so restarting the poller cannot re-ingest
            # the cursor message.
            available_uids = [
                item
                for item in (data[0].split() if data and data[0] else [])
                if int(item) > search_after
            ]
            highest_uid = max((int(item) for item in available_uids), default=search_after)
            result: list[tuple[int, bytes]] = []
            downloaded_bytes = 0
            for uid_bytes in available_uids[:batch_limit]:
                last_error: Exception | None = None
                for attempt in range(3):
                    try:
                        if client is None:
                            client, reopened_uid_validity = self._open_folder(selected_folder)
                            if reopened_uid_validity != uid_validity:
                                raise RuntimeError(
                                    f"IMAP UIDVALIDITY changed while fetching folder: {selected_folder}"
                                )
                        status, fetched = client.uid("fetch", uid_bytes, "(RFC822)")
                        if status != "OK" or not fetched or not isinstance(fetched[0], tuple):
                            raise imaplib.IMAP4.abort(f"invalid FETCH response for UID {uid_bytes!r}")
                        raw_message = fetched[0][1]
                        if not isinstance(raw_message, bytes):
                            raise imaplib.IMAP4.abort(f"missing RFC822 body for UID {uid_bytes!r}")
                        result.append((int(uid_bytes), raw_message))
                        downloaded_bytes += len(raw_message)
                        last_error = None
                        break
                    except (imaplib.IMAP4.abort, OSError, ssl.SSLError) as exc:
                        last_error = exc
                        self._close_quietly(client)
                        client = None
                        if attempt < 2:
                            time.sleep(attempt + 1)
                if last_error is not None:
                    if not result:
                        raise last_error
                    logger.warning(
                        "IMAP connection failed repeatedly at folder=%s uid=%s; returning %s earlier messages",
                        selected_folder,
                        uid_bytes.decode(errors="replace"),
                        len(result),
                    )
                    break
                if max_bytes is not None and downloaded_bytes >= max_bytes:
                    break
            return uid_validity, highest_uid, result
        finally:
            self._close_quietly(client)

    def sent_contains_message_id(self, message_id: str) -> bool:
        if not self.settings.gmail_address or not self.settings.gmail_app_password:
            return False
        client: imaplib.IMAP4_SSL | None = None
        try:
            client, _ = self._open_folder(self.settings.imap_sent_folder)
            status, data = client.search(None, "HEADER", "Message-ID", message_id)
            if status != "OK":
                raise RuntimeError("Gmail Sent Message-ID search failed")
            return bool(data and data[0].split())
        finally:
            self._close_quietly(client)
