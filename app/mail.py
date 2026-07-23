import base64
import binascii
import email
import hashlib
import html
import imaplib
import logging
import mimetypes
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
from urllib.parse import unquote

from bs4 import BeautifulSoup, Comment
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import Contact, EmailMessage, Outbox, SalesCase
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)
SIGNATURE_LOGO_CID = "lanyachem-logo"
MAX_INLINE_IMAGE_BYTES = 5 * 1024 * 1024
MAX_INLINE_IMAGES_TOTAL_BYTES = 15 * 1024 * 1024
MAX_OUTBOUND_ATTACHMENT_COUNT = 10
MAX_OUTBOUND_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_OUTBOUND_ATTACHMENTS_TOTAL_BYTES = 15 * 1024 * 1024
MAX_OUTBOUND_MESSAGE_BYTES = 24 * 1024 * 1024
DISALLOWED_OUTBOUND_ATTACHMENT_SUFFIXES = frozenset(
    {
        ".bat",
        ".cmd",
        ".com",
        ".exe",
        ".hta",
        ".jar",
        ".js",
        ".jse",
        ".msi",
        ".ps1",
        ".scr",
        ".sh",
        ".vbs",
        ".vbe",
        ".wsf",
    }
)


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


@dataclass(frozen=True)
class InlineImageAsset:
    content_id: str
    content_type: str
    filename: str
    payload: bytes


@dataclass(frozen=True)
class OutboundAttachment:
    filename: str
    content_type: str
    payload: bytes


@dataclass(frozen=True)
class FullReplySource:
    body_text: str
    body_html: str | None
    inline_images: tuple[InlineImageAsset, ...] = ()


@dataclass(frozen=True)
class RemoteImageReference:
    token: str
    url: str
    alt: str


@dataclass(frozen=True)
class EmailDisplayContent:
    body_text: str
    body_html: str | None
    remote_images: tuple[RemoteImageReference, ...] = ()


@dataclass(frozen=True)
class EmailResource:
    filename: str
    content_type: str
    payload: bytes


SAFE_INLINE_IMAGE_SUFFIXES = frozenset(
    {".avif", ".bmp", ".gif", ".ico", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
)
SAFE_INLINE_IMAGE_CONTENT_TYPES = frozenset({"application/octet-stream"})

QUOTED_HTML_ALLOWED_TAGS = frozenset(
    {
        "a",
        "b",
        "blockquote",
        "br",
        "code",
        "del",
        "div",
        "em",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "i",
        "img",
        "li",
        "ol",
        "p",
        "pre",
        "s",
        "small",
        "span",
        "strong",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "u",
        "ul",
    }
)
QUOTED_HTML_SAFE_STYLE_PROPERTIES = frozenset(
    {
        "background-color",
        "border",
        "border-bottom",
        "border-collapse",
        "border-color",
        "border-left",
        "border-right",
        "border-spacing",
        "border-style",
        "border-top",
        "border-width",
        "color",
        "font-family",
        "font-size",
        "font-style",
        "font-weight",
        "height",
        "line-height",
        "margin",
        "margin-bottom",
        "margin-left",
        "margin-right",
        "margin-top",
        "max-width",
        "min-width",
        "padding",
        "padding-bottom",
        "padding-left",
        "padding-right",
        "padding-top",
        "text-align",
        "text-decoration",
        "vertical-align",
        "white-space",
        "width",
    }
)
QUOTED_HTML_DANGEROUS_TAGS = frozenset(
    {
        "base",
        "button",
        "embed",
        "form",
        "iframe",
        "input",
        "link",
        "math",
        "meta",
        "object",
        "script",
        "select",
        "style",
        "svg",
        "textarea",
    }
)
SAFE_DATA_IMAGE_PATTERN = re.compile(
    r"^data:image/(?:png|jpe?g|gif|webp|bmp|tiff?|x-icon|vnd\.microsoft\.icon);base64,([a-z0-9+/=\s]+)$",
    flags=re.I,
)


def is_safe_inline_image_attachment(
    item: dict[str, Any],
    referenced_content_ids: set[str] | None = None,
    referenced_content_locations: set[str] | None = None,
) -> bool:
    """Recognize a raster image that is actually referenced by the HTML body."""
    filename = str(item.get("filename") or "").strip()
    content_id = str(item.get("content_id") or "").strip()
    content_location = str(item.get("content_location") or "").strip()
    content_type = str(item.get("content_type") or "").strip().casefold()
    detected_content_type = str(item.get("detected_content_type") or "").strip().casefold()
    try:
        size = int(item.get("size") or 0)
    except (TypeError, ValueError):
        return False
    normalized_cid = _normalize_content_id(content_id) if content_id else ""
    normalized_location = (
        _normalized_content_location(content_location) if content_location else ""
    )
    is_referenced = bool(
        item.get("inline_content") is True
        or (
            normalized_cid
            and referenced_content_ids is not None
            and normalized_cid in referenced_content_ids
        )
        or (
            normalized_location
            and referenced_content_locations is not None
            and normalized_location in referenced_content_locations
        )
    )
    if "detected_content_type" in item:
        raster_type_is_safe = detected_content_type.startswith("image/")
    else:
        # Compatibility for rows parsed before detected MIME types were stored.
        raster_type_is_safe = bool(
            content_type.startswith("image/")
            or (
                content_type in SAFE_INLINE_IMAGE_CONTENT_TYPES
                and Path(filename).suffix.casefold() in SAFE_INLINE_IMAGE_SUFFIXES
            )
        )
    return bool(
        (normalized_cid or normalized_location)
        and is_referenced
        and raster_type_is_safe
        and 0 < size <= MAX_INLINE_IMAGE_BYTES
    )


def attachments_require_review(
    attachments: list[dict[str, Any]],
    body_html: str | None = None,
) -> bool:
    """Require review only for real attachments, not HTML-referenced body images."""
    referenced_content_ids = _referenced_inline_content_ids(body_html)
    referenced_content_locations = _referenced_inline_content_locations(body_html)
    return any(
        not is_safe_inline_image_attachment(
            item,
            referenced_content_ids,
            referenced_content_locations,
        )
        for item in attachments
    )


def append_quoted_reply(
    text_body: str,
    html_body: str,
    *,
    from_address: str,
    source_body: str,
    source_html: str | None = None,
    occurred_at: datetime | None,
) -> tuple[str, str]:
    """Append the complete previous message, including its existing quote chain."""
    clean_source = source_body.strip()
    quoted_html = _sanitize_quoted_html(source_html) if source_html else ""
    html_source_text = _html_to_full_text(quoted_html) if quoted_html else ""
    if html_source_text and (
        not clean_source or len(clean_source) * 4 < len(html_source_text) * 3
    ):
        clean_source = html_source_text
    if not clean_source:
        return text_body, html_body
    timestamp = occurred_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    intro = f"On {timestamp.strftime('%a, %d %b %Y %H:%M %z')}, {from_address} wrote:"
    quoted_plain = "\n".join(f"> {line}" if line else ">" for line in clean_source.splitlines())
    if not quoted_html:
        quoted_html = (
            '<div style="white-space:pre-wrap">'
            + html.escape(clean_source)
            + "</div>"
        )
    return (
        f"{text_body.rstrip()}\n\n{intro}\n{quoted_plain}",
        (
            f"{html_body.rstrip()}"
            '<div class="aiemail-quoted-reply gmail_quote" style="margin-top:1em">'
            f"<div>{html.escape(intro)}</div>"
            '<blockquote type="cite" style="margin:0.5em 0 0 0.8ex;border-left:1px solid #ccc;'
            f'padding-left:1ex">{quoted_html}</blockquote></div>'
        ),
    )


def _decode_part(part: email.message.Message) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def _normalize_content_id(value: str) -> str:
    return unquote(html.unescape(value)).strip().strip("<>").casefold()


def _referenced_inline_content_ids(value: str | None) -> set[str]:
    if not value:
        return set()
    soup = BeautifulSoup(value, "html.parser")
    result: set[str] = set()
    for image in soup.find_all("img"):
        source = str(image.get("src") or "").strip()
        if source.casefold().startswith("cid:"):
            normalized = _normalize_content_id(source[4:])
            if normalized:
                result.add(normalized)
    return result


def _referenced_inline_content_locations(value: str | None) -> set[str]:
    if not value:
        return set()
    soup = BeautifulSoup(value, "html.parser")
    return {
        _normalized_content_location(source)
        for image in soup.find_all("img")
        if (source := str(image.get("src") or "").strip())
        and not re.match(r"^(?:cid:|data:image/)", source, flags=re.I)
    }


def html_requires_mime_resources(value: str | None) -> bool:
    """Return whether HTML references a non-self-contained embedded resource."""
    if not value:
        return False
    soup = BeautifulSoup(value, "html.parser")
    for image in soup.find_all("img"):
        source = str(image.get("src") or "").strip()
        if source and not re.match(r"^(?:https?://|data:image/)", source, flags=re.I):
            return True
    return False


def _sniff_inline_image_content_type(payload: bytes) -> str | None:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return "image/webp"
    if payload.startswith(b"BM"):
        return "image/bmp"
    if payload.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if payload.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    if len(payload) >= 12 and payload[4:8] == b"ftyp" and payload[8:12] in {
        b"avif",
        b"avis",
    }:
        return "image/avif"
    return None


def _iter_mime_tree(
    part: email.message.Message,
    path: tuple[int, ...] = (),
) -> list[tuple[tuple[int, ...], email.message.Message]]:
    """Walk a message without descending into attached message/rfc822 files."""
    result = [(path, part)]
    if not part.is_multipart() or part.get_content_type() == "message/rfc822":
        return result
    payload = part.get_payload()
    if not isinstance(payload, list):
        return result
    for index, child in enumerate(payload):
        result.extend(_iter_mime_tree(child, (*path, index)))
    return result


def _normalized_content_location(value: str) -> str:
    return unquote(html.unescape(value)).strip().casefold()


def _image_filename(content_type: str, digest: str, raw_filename: str | None) -> str:
    suffix = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "image/tiff": ".tiff",
        "image/x-icon": ".ico",
        "image/avif": ".avif",
    }[content_type]
    filename = re.sub(
        r"[^a-zA-Z0-9_.-]",
        "_",
        Path(str(raw_filename or "")).name,
    )[:200]
    return filename or f"quoted-inline-{digest[:12]}{suffix}"


def _inline_image_asset(payload: bytes, raw_filename: str | None = None) -> InlineImageAsset:
    content_type = _sniff_inline_image_content_type(payload)
    if content_type is None:
        raise ValueError("referenced inline content is not a supported raster image")
    if len(payload) > MAX_INLINE_IMAGE_BYTES:
        raise ValueError(
            f"referenced inline image exceeds {MAX_INLINE_IMAGE_BYTES} bytes"
        )
    digest = hashlib.sha256(payload).hexdigest()
    content_id = f"quoted-{digest[:32]}@aiemail"
    return InlineImageAsset(
        content_id=content_id,
        content_type=content_type,
        filename=_image_filename(content_type, digest, raw_filename),
        payload=payload,
    )


def _rewrite_referenced_image_sources(
    value: str,
    *,
    cid_replacements: dict[str, str],
    location_replacements: dict[str, str],
    data_replacements: dict[str, str],
) -> str:
    soup = BeautifulSoup(value, "html.parser")
    for image in soup.find_all("img"):
        source = str(image.get("src") or "").strip()
        replacement: str | None = None
        if source.casefold().startswith("cid:"):
            replacement = cid_replacements.get(_normalize_content_id(source[4:]))
        elif source.casefold().startswith("data:image/"):
            replacement = data_replacements.get(source)
        elif not re.match(r"^https?://", source, flags=re.I):
            replacement = location_replacements.get(_normalized_content_location(source))
        if replacement:
            image["src"] = f"cid:{replacement}"
    return str(soup)


def extract_full_reply_source(raw: bytes) -> FullReplySource:
    """Extract complete display content and referenced inline images from one email."""
    message = BytesParser(policy=policy.default).parsebytes(raw)
    plain_part = message.get_body(preferencelist=("plain",))
    html_part = message.get_body(preferencelist=("html",))
    body_text = (
        _decode_part(plain_part).replace("\r\n", "\n").strip()
        if plain_part is not None
        else ""
    )
    body_html = _decode_part(html_part).strip() if html_part is not None else None
    if not body_text and body_html:
        body_text = _html_to_full_text(_sanitize_quoted_html(body_html))
    if not body_html:
        return FullReplySource(body_text=body_text, body_html=body_html)

    soup = BeautifulSoup(body_html, "html.parser")
    image_sources = [
        str(image.get("src") or "").strip()
        for image in soup.find_all("img")
        if str(image.get("src") or "").strip()
    ]
    referenced_cids = {
        _normalize_content_id(source[4:])
        for source in image_sources
        if source.casefold().startswith("cid:")
    }
    referenced_locations = {
        _normalized_content_location(source)
        for source in image_sources
        if not source.casefold().startswith(("cid:", "data:image/"))
    }
    data_sources = {
        source for source in image_sources if source.casefold().startswith("data:image/")
    }
    if not referenced_cids and not referenced_locations and not data_sources:
        return FullReplySource(body_text=body_text, body_html=body_html)

    tree = _iter_mime_tree(message)
    html_path = next((path for path, part in tree if part is html_part), ())
    related_scope: tuple[int, ...] = ()
    for path, part in tree:
        if (
            len(path) <= len(html_path)
            and html_path[: len(path)] == path
            and part.get_content_type() == "multipart/related"
        ):
            related_scope = path
    scoped_parts = [
        part
        for path, part in tree
        if path[: len(related_scope)] == related_scope and not part.is_multipart()
    ]
    parts_by_cid: dict[str, list[email.message.Message]] = {}
    parts_by_location: dict[str, list[email.message.Message]] = {}
    for part in scoped_parts:
        if part.get("Content-ID"):
            parts_by_cid.setdefault(
                _normalize_content_id(str(part.get("Content-ID"))), []
            ).append(part)
        if part.get("Content-Location"):
            parts_by_location.setdefault(
                _normalized_content_location(str(part.get("Content-Location"))), []
            ).append(part)

    cid_replacements: dict[str, str] = {}
    location_replacements: dict[str, str] = {}
    data_replacements: dict[str, str] = {}
    assets_by_cid: dict[str, InlineImageAsset] = {}
    for original_cid in sorted(referenced_cids):
        candidates = parts_by_cid.get(original_cid, [])
        if len(candidates) != 1:
            raise ValueError(f"referenced inline image is missing from MIME: {original_cid}")
        part = candidates[0]
        payload = part.get_payload(decode=True) or b""
        asset = _inline_image_asset(payload, part.get_filename())
        cid_replacements[original_cid] = asset.content_id
        assets_by_cid.setdefault(asset.content_id, asset)
    for original_location in sorted(referenced_locations):
        candidates = parts_by_location.get(original_location, [])
        if not candidates and re.match(r"^https?://", original_location, flags=re.I):
            continue
        if len(candidates) != 1:
            raise ValueError(
                f"referenced inline image location is missing from MIME: {original_location}"
            )
        part = candidates[0]
        asset = _inline_image_asset(part.get_payload(decode=True) or b"", part.get_filename())
        location_replacements[original_location] = asset.content_id
        assets_by_cid.setdefault(asset.content_id, asset)
    for source in sorted(data_sources):
        data_match = SAFE_DATA_IMAGE_PATTERN.fullmatch(source)
        if data_match is None:
            raise ValueError("referenced data image is invalid or unsupported")
        try:
            payload = base64.b64decode(
                re.sub(r"\s+", "", data_match.group(1)),
                validate=True,
            )
        except (binascii.Error, ValueError) as exc:
            raise ValueError("referenced data image is invalid") from exc
        asset = _inline_image_asset(payload)
        data_replacements[source] = asset.content_id
        assets_by_cid.setdefault(asset.content_id, asset)
    total_inline_bytes = sum(len(asset.payload) for asset in assets_by_cid.values())
    if total_inline_bytes > MAX_INLINE_IMAGES_TOTAL_BYTES:
        raise ValueError(
            "referenced inline images exceed the complete-history message limit"
        )
    rewritten_html = _rewrite_referenced_image_sources(
        body_html,
        cid_replacements=cid_replacements,
        location_replacements=location_replacements,
        data_replacements=data_replacements,
    )
    return FullReplySource(
        body_text=body_text,
        body_html=rewritten_html,
        inline_images=tuple(assets_by_cid.values()),
    )


def extract_email_display(
    raw: bytes,
    *,
    resource_url_prefix: str,
) -> EmailDisplayContent:
    """Return allowlist-sanitized email HTML with local, authenticated inline images."""
    source = extract_full_reply_source(raw)
    if not source.body_html:
        return EmailDisplayContent(body_text=source.body_text, body_html=None)

    sanitized = _sanitize_quoted_html(source.body_html)
    soup = BeautifulSoup(sanitized, "html.parser")
    assets_by_cid = {
        _normalize_content_id(asset.content_id): asset
        for asset in source.inline_images
    }
    remote_images: list[RemoteImageReference] = []
    prefix = resource_url_prefix.rstrip("/")
    for index, image in enumerate(list(soup.find_all("img"))):
        image_source = str(image.get("src") or "").strip()
        asset = (
            assets_by_cid.get(_normalize_content_id(image_source[4:]))
            if image_source.casefold().startswith("cid:")
            else None
        )
        if re.match(r"^https?://", image_source, flags=re.I):
            alt = str(image.get("alt") or "").strip()[:500]
            token = hashlib.sha256(
                f"{index}\0{image_source}".encode()
            ).hexdigest()[:24]
            remote_images.append(
                RemoteImageReference(
                    token=token,
                    url=image_source,
                    alt=alt,
                )
            )
            placeholder = soup.new_tag("span")
            placeholder["class"] = "aiemail-remote-image"
            placeholder["data-remote-image"] = token
            placeholder.string = alt or "[Remote image blocked]"
            image.replace_with(placeholder)
            continue
        if asset is None:
            replacement = str(image.get("alt") or "").strip() or "[Image unavailable]"
            image.replace_with(replacement)
            continue
        digest = hashlib.sha256(asset.payload).hexdigest()
        image["src"] = f"{prefix}/{digest}?disposition=inline"
        image["loading"] = "lazy"
        image["referrerpolicy"] = "no-referrer"
    for link in soup.find_all("a"):
        link["target"] = "_blank"
        link["rel"] = "noopener noreferrer"
    return EmailDisplayContent(
        body_text=source.body_text,
        body_html=str(soup),
        remote_images=tuple(remote_images),
    )


def _is_mime_resource_part(part: email.message.Message) -> bool:
    disposition = part.get_content_disposition()
    filename = part.get_filename()
    content_type = part.get_content_type()
    content_id = str(part.get("Content-ID") or "").strip()
    content_location = str(part.get("Content-Location") or "").strip()
    return bool(
        disposition == "attachment"
        or filename
        or (
            (content_id or content_location)
            and content_type not in {"text/plain", "text/html"}
        )
    )


def extract_email_resource(raw: bytes, digest: str) -> EmailResource | None:
    """Resolve one attachment or inline image by its immutable SHA-256 digest."""
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    message = BytesParser(policy=policy.default).parsebytes(raw)
    for part in message.walk():
        if part.is_multipart() or not _is_mime_resource_part(part):
            continue
        payload = part.get_payload(decode=True) or b""
        if hashlib.sha256(payload).hexdigest() != digest:
            continue
        filename = part.get_filename() or f"attachment-{digest[:12]}"
        content_type = _sniff_inline_image_content_type(payload) or part.get_content_type()
        return EmailResource(
            filename=filename,
            content_type=content_type,
            payload=payload,
        )
    try:
        inline_images = extract_full_reply_source(raw).inline_images
    except (ValueError, LookupError, RecursionError):
        inline_images = ()
    for asset in inline_images:
        if hashlib.sha256(asset.payload).hexdigest() == digest:
            return EmailResource(
                filename=asset.filename,
                content_type=asset.content_type,
                payload=asset.payload,
            )
    return None


def extract_full_message_bodies(raw: bytes) -> tuple[str, str | None]:
    """Compatibility helper returning complete text and HTML display bodies."""
    source = extract_full_reply_source(raw)
    return source.body_text, source.body_html


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


def _html_to_full_text(value: str) -> str:
    """Convert display HTML to text while retaining nested quoted history."""
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup(QUOTED_HTML_DANGEROUS_TAGS):
        tag.decompose()
    text = html.unescape(soup.get_text("\n")).replace("\xa0", " ")
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    result: list[str] = []
    previous_blank = False
    for line in lines:
        blank = not line.strip()
        if blank and previous_blank:
            continue
        result.append(line)
        previous_blank = blank
    return "\n".join(result).strip()


def _sanitize_quoted_style(value: str) -> str:
    safe_declarations: list[str] = []
    for declaration in value.split(";"):
        if ":" not in declaration:
            continue
        property_name, property_value = declaration.split(":", 1)
        property_name = property_name.strip().casefold()
        property_value = property_value.strip()
        lowered_value = property_value.casefold()
        if (
            property_name not in QUOTED_HTML_SAFE_STYLE_PROPERTIES
            or not property_value
            or len(property_value) > 200
            or any(
                marker in lowered_value
                for marker in (
                    "url(",
                    "expression(",
                    "javascript:",
                    "data:",
                    "@import",
                    "behavior:",
                    "-moz-binding",
                )
            )
        ):
            continue
        safe_declarations.append(f"{property_name}:{property_value}")
    return ";".join(safe_declarations)


def _safe_quoted_image_source(value: str) -> str | None:
    source = html.unescape(value).strip()
    if re.match(r"^https?://", source, flags=re.I):
        return source
    if source.casefold().startswith("cid:") and _normalize_content_id(source[4:]):
        return source
    data_match = SAFE_DATA_IMAGE_PATTERN.fullmatch(source)
    if data_match is None:
        return None
    try:
        payload = base64.b64decode(re.sub(r"\s+", "", data_match.group(1)), validate=True)
    except (binascii.Error, ValueError):
        return None
    return source if _sniff_inline_image_content_type(payload) is not None else None


def _sanitize_quoted_html(value: str | None) -> str:
    """Keep readable email formatting while removing active and tracking content."""
    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    for comment in soup.find_all(string=lambda item: isinstance(item, Comment)):
        comment.extract()
    for tag in soup(QUOTED_HTML_DANGEROUS_TAGS):
        tag.decompose()
    root = soup.body or soup
    for tag in list(root.find_all(True)):
        name = tag.name.casefold()
        if name not in QUOTED_HTML_ALLOWED_TAGS:
            tag.unwrap()
            continue
        safe_attributes: dict[str, str] = {}
        if name == "img":
            source = _safe_quoted_image_source(str(tag.get("src") or ""))
            alt = str(tag.get("alt") or "").strip()[:500]
            if source is None:
                tag.replace_with(alt)
                continue
            safe_attributes["src"] = source
            if alt:
                safe_attributes["alt"] = alt
            title = str(tag.get("title") or "").strip()[:500]
            if title:
                safe_attributes["title"] = title
            for attribute in ("width", "height"):
                dimension = str(tag.get(attribute) or "").strip()
                if re.fullmatch(r"\d{1,5}(?:px|%)?", dimension, flags=re.I):
                    safe_attributes[attribute] = dimension
        elif name == "a":
            href = str(tag.get("href") or "").strip()
            if re.match(r"^(?:https?://|mailto:)", href, flags=re.I):
                safe_attributes["href"] = href
                safe_attributes["rel"] = "noopener noreferrer"
        if name in {"td", "th"}:
            for attribute in ("colspan", "rowspan"):
                value = str(tag.get(attribute) or "").strip()
                if value.isdigit() and 1 <= int(value) <= 100:
                    safe_attributes[attribute] = value
        safe_style = _sanitize_quoted_style(str(tag.get("style") or ""))
        if safe_style:
            safe_attributes["style"] = safe_style
        tag.attrs = safe_attributes
    return "".join(str(child) for child in root.contents).strip()


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
        content_id = str(part.get("Content-ID") or "").strip() or None
        content_location = str(part.get("Content-Location") or "").strip() or None
        if _is_mime_resource_part(part):
            payload = part.get_payload(decode=True) or b""
            attachments.append(
                {
                    "filename": filename or "unnamed",
                    "content_type": content_type,
                    "detected_content_type": _sniff_inline_image_content_type(payload),
                    "disposition": disposition,
                    "content_id": content_id,
                    "content_location": content_location,
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
    referenced_content_ids = _referenced_inline_content_ids(body_html)
    referenced_content_locations = _referenced_inline_content_locations(body_html)
    for item in attachments:
        content_id = str(item.get("content_id") or "")
        content_location = str(item.get("content_location") or "")
        item["inline_content"] = bool(
            (
                content_id
                and _normalize_content_id(content_id) in referenced_content_ids
            )
            or (
                content_location
                and _normalized_content_location(content_location)
                in referenced_content_locations
            )
        )
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


def _signature_logo_asset() -> InlineImageAsset:
    logo_path = get_settings().content_dir / "email_signature_logo.b64"
    try:
        logo_bytes = base64.b64decode(
            logo_path.read_text(encoding="ascii").strip(),
            validate=True,
        )
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"signature logo asset is unavailable or invalid: {logo_path}"
        ) from exc
    if _sniff_inline_image_content_type(logo_bytes) != "image/png":
        raise RuntimeError("signature logo asset is not a PNG image")
    return InlineImageAsset(
        content_id=SIGNATURE_LOGO_CID,
        content_type="image/png",
        filename="lanyachem-logo.png",
        payload=logo_bytes,
    )


def _attach_related_images(
    message: MIMEEmailMessage,
    html_body: str,
    token: str,
    inline_images: tuple[InlineImageAsset, ...],
) -> None:
    referenced_cids = _referenced_inline_content_ids(html_body)
    assets_by_cid: dict[str, InlineImageAsset] = {}
    if _normalize_content_id(SIGNATURE_LOGO_CID) in referenced_cids:
        logo = _signature_logo_asset()
        assets_by_cid[_normalize_content_id(logo.content_id)] = logo
    for asset in inline_images:
        normalized_cid = _normalize_content_id(asset.content_id)
        if normalized_cid not in referenced_cids:
            continue
        existing = assets_by_cid.get(normalized_cid)
        if existing is not None and existing.payload != asset.payload:
            raise ValueError(f"conflicting inline image Content-ID: {asset.content_id}")
        detected_type = _sniff_inline_image_content_type(asset.payload)
        if detected_type is None or detected_type != asset.content_type.casefold():
            raise ValueError(f"invalid inline image payload: {asset.content_id}")
        if len(asset.payload) > MAX_INLINE_IMAGE_BYTES:
            raise ValueError(f"inline image is too large: {asset.content_id}")
        assets_by_cid[normalized_cid] = asset
    missing_cids = referenced_cids.difference(assets_by_cid)
    if missing_cids:
        raise ValueError(
            "HTML contains unresolved inline images: " + ", ".join(sorted(missing_cids))
        )
    if not assets_by_cid:
        return
    if sum(len(asset.payload) for asset in assets_by_cid.values()) > MAX_INLINE_IMAGES_TOTAL_BYTES:
        raise ValueError("inline image content exceeds the complete-history message limit")
    html_part = message.get_payload()[-1]
    for asset in assets_by_cid.values():
        maintype, subtype = asset.content_type.split("/", 1)
        html_part.add_related(
            asset.payload,
            maintype=maintype,
            subtype=subtype,
            cid=f"<{asset.content_id}>",
            filename=asset.filename,
            disposition="inline",
        )
    html_part.set_boundary(f"=_sales_agent_related_{token}")


def _safe_outbound_attachment_filename(raw_filename: str) -> str:
    filename = re.split(r"[\\/]", raw_filename)[-1]
    filename = re.sub(r"[\x00-\x1f\x7f]", "", filename).strip()
    if not filename or filename in {".", ".."}:
        raise ValueError("attachment filename is missing or invalid")
    if len(filename) > 255:
        raise ValueError(f"attachment filename is too long: {filename[:80]}")
    if Path(filename).suffix.casefold() in DISALLOWED_OUTBOUND_ATTACHMENT_SUFFIXES:
        raise ValueError(f"executable attachment type is not allowed: {filename}")
    return filename


def _normalized_outbound_attachment_type(content_type: str, filename: str) -> str:
    normalized = content_type.partition(";")[0].strip().casefold()
    if normalized == "application/octet-stream" or not re.fullmatch(
        r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+",
        normalized,
    ):
        normalized = (mimetypes.guess_type(filename)[0] or "application/octet-stream").casefold()
    return normalized


def _attach_outbound_files(
    message: MIMEEmailMessage,
    attachments: tuple[OutboundAttachment, ...],
) -> None:
    if len(attachments) > MAX_OUTBOUND_ATTACHMENT_COUNT:
        raise ValueError(f"at most {MAX_OUTBOUND_ATTACHMENT_COUNT} attachments are allowed")
    total_bytes = 0
    for attachment in attachments:
        filename = _safe_outbound_attachment_filename(attachment.filename)
        size = len(attachment.payload)
        if not size:
            raise ValueError(f"attachment is empty: {filename}")
        if size > MAX_OUTBOUND_ATTACHMENT_BYTES:
            raise ValueError(f"attachment is too large: {filename}")
        total_bytes += size
        if total_bytes > MAX_OUTBOUND_ATTACHMENTS_TOTAL_BYTES:
            raise ValueError("attachments exceed the total upload size limit")
        content_type = _normalized_outbound_attachment_type(
            attachment.content_type,
            filename,
        )
        maintype, subtype = content_type.split("/", 1)
        message.add_attachment(
            attachment.payload,
            maintype=maintype,
            subtype=subtype,
            filename=filename,
            disposition="attachment",
        )


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
    inline_images: tuple[InlineImageAsset, ...] = (),
    attachments: tuple[OutboundAttachment, ...] = (),
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
        ordered_references = list(dict.fromkeys(item for item in references if item))
        if len(ordered_references) > 20:
            ordered_references = [ordered_references[0], *ordered_references[-19:]]
        msg["References"] = " ".join(ordered_references)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    _attach_related_images(msg, html_body, token, inline_images)
    if attachments:
        msg.set_boundary(f"=_sales_agent_alternative_{token}")
        _attach_outbound_files(msg, attachments)
    msg.set_boundary(f"=_sales_agent_{token}")
    raw = msg.as_string(policy=policy.SMTP)
    if len(raw.encode("utf-8")) > MAX_OUTBOUND_MESSAGE_BYTES:
        raise ValueError("complete reply exceeds the outbound email size limit")
    return message_id, raw


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
