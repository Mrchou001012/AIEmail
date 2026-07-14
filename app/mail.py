import email
import hashlib
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
    raw_sha256: str
    occurred_at: datetime | None


def _decode_part(part: email.message.Message) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def _clean_plain(text: str) -> str:
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.lstrip().startswith(">"):
            continue
        if re.match(r"^On .+ wrote:$", line.strip(), re.I):
            break
        if line.strip() in {"--", "-- "}:
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
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        occurred_at=occurred_at,
    )


def normalized_subject(subject: str) -> str:
    result = re.sub(r"^(?:(?:re|fw|fwd)\s*:\s*)+", "", subject.strip(), flags=re.I)
    return re.sub(r"\s+", " ", result).lower()


async def match_case(
    session: AsyncSession,
    parsed: ParsedEmail,
    *,
    direction: str = "INBOUND",
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
            available_uids = data[0].split() if data and data[0] else []
            highest_uid = max((int(item) for item in available_uids), default=search_after)
            result: list[tuple[int, bytes]] = []
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
