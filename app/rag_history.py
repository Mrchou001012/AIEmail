from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal

from app.ai import stub_analyze
from app.auto_replies import classify_automated_reply
from app.bounces import classify_bounce
from app.mail import extract_full_reply_source, normalized_subject, parse_mime

Direction = Literal["INBOUND", "OUTBOUND", "UNKNOWN"]

MESSAGE_ID_PATTERN = re.compile(r"<[^<>\s]+>")
WHITESPACE_PATTERN = re.compile(r"[ \t]+")
BLANK_LINES_PATTERN = re.compile(r"\n{3,}")
SIGNATURE_START_PATTERN = re.compile(
    r"^(?:best|kind|warm)?\s*regards[,.!]*$|"
    r"^(?:many\s+)?thanks(?:\s+(?:and|&)\s+regards)?[,.!]*$|"
    r"^sincerely(?:\s+yours)?[,.!]*$|"
    r"^yours\s+(?:faithfully|sincerely)[,.!]*$",
    re.I,
)
DISCLAIMER_START_PATTERN = re.compile(
    r"^(?:confidentiality notice|disclaimer|this e-?mail (?:and any attachments )?is confidential|"
    r"the information contained in this (?:e-?mail|message))",
    re.I,
)
SECURITY_BANNER_START_PATTERN = re.compile(
    r"^(?:"
    r"caution\s*:|"
    r"warning\s*:|"
    r"external\s+e-?mail\b|"
    r"you\s+don['’]t\s+often\s+get\s+email\s+from\b"
    r")",
    re.I,
)
SECURITY_BANNER_CONTINUATION_PATTERN = re.compile(
    r"^(?:"
    r"exercise\s+caution\b|"
    r"do\s+not\s+click\b|"
    r"if\s+you\s+believe\b|"
    r"learn\s+why\s+this\s+is\s+important\b|"
    r"report\s+(?:it|this|using)\b|"
    r"which\s+is\s+displayed\b|"
    r"or\s+send\s+to\b|"
    r"before\s+validating\b"
    r")",
    re.I,
)
BUSINESS_GREETING_PATTERN = re.compile(
    r"\b(?:dear|hello|hi|good\s+(?:morning|afternoon|evening|day))\b",
    re.I,
)
NEWSLETTER_SUBJECT_PATTERN = re.compile(
    r"\b(?:newsletter|daily digest|weekly digest|promotion|webinar|unsubscribe|bank alert|"
    r"transaction alert|chemical news)\b",
    re.I,
)
NEWSLETTER_LOCAL_PARTS = {
    "alerts",
    "mailer",
    "marketing",
    "newsletter",
    "no-reply",
    "noreply",
    "notifications",
}
HIGH_RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PRICE_OR_QUOTE", re.compile(r"\b(?:price|pricing|quote|quotation|proforma|invoice)\b", re.I)),
    ("COMPLIANCE", re.compile(r"\b(?:reach|rohs|compliance|regulatory|declaration)\b", re.I)),
    ("TECHNICAL", re.compile(r"\b(?:coa|tds|sds|msds|specification|technical data)\b", re.I)),
    ("CONTRACT_OR_TERMS", re.compile(r"\b(?:contract|agreement|payment terms?|incoterms?|liability)\b", re.I)),
    ("LOGISTICS", re.compile(r"\b(?:shipping|shipment|customs|freight|bill of lading|delivery)\b", re.I)),
    ("BANKING", re.compile(r"\b(?:bank account|beneficiary|swift|iban|wire transfer)\b", re.I)),
)
QUOTED_FROM_PATTERN = re.compile(
    r"^(?:from|发件人|寄件者|von|de|鍙戜欢浜).{0,4}[:：]\s*(.*)$",
    re.I,
)
QUOTED_TO_PATTERN = re.compile(
    r"^(?:to|收件人|recipient|鏀朵欢浜).{0,4}[:：]\s*(.*)$",
    re.I,
)
QUOTED_CC_PATTERN = re.compile(r"^(?:cc|抄送).{0,4}[:：]\s*(.*)$", re.I)
QUOTED_SUBJECT_PATTERN = re.compile(
    r"^(?:subject|主题|涓婚).{0,4}[:：]\s*(.*)$",
    re.I,
)
QUOTED_DATE_PATTERN = re.compile(
    r"^(?:date|sent|发送时间|鍙戦€佹椂闂).{0,6}[:：]\s*(.*)$",
    re.I,
)
EMAIL_ADDRESS_PATTERN = re.compile(
    r"(?i)(?<![\w.+-])([a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+)(?![\w-])"
)


@dataclass(frozen=True)
class HistoricalEmail:
    source_file: str
    source_kind: Literal["EML", "QUOTED_TURN"]
    source_thread_hint: str
    quoted_depth: int
    raw_sha256: str
    message_id: str | None
    in_reply_to: str | None
    references: tuple[str, ...]
    sender: str
    recipients: tuple[str, ...]
    subject: str
    normalized_subject: str
    occurred_at: datetime | None
    direction: Direction
    body_text: str
    body_fingerprint: str
    customer_key: str | None
    attachment_count: int
    attachment_names: tuple[str, ...]
    is_automated: bool
    automated_reply_type: str | None
    is_bounce: bool
    is_newsletter: bool
    is_internal: bool
    exclusion_reasons: tuple[str, ...]


@dataclass(frozen=True)
class ConversationPair:
    pair_id: str
    thread_id: str
    customer_key: str
    intent: str
    risk_flags: tuple[str, ...]
    subject: str
    request_at: datetime
    response_at: datetime
    response_delay_hours: float
    request_text: str
    response_text: str
    response_sender: str
    boss_anchor: bool
    request_attachment_names: tuple[str, ...]
    response_attachment_names: tuple[str, ...]
    request_source_file: str
    response_source_file: str
    direct_reply: bool
    quality_score: int
    quality_reasons: tuple[str, ...]

    def rag_document(self) -> dict[str, Any]:
        retrieval_text = (
            f"Intent: {self.intent}\n"
            f"Subject: {self.subject}\n"
            f"Customer request:\n{self.request_text}"
        )
        return {
            "schema_version": "email-rag-example.v1",
            "id": self.pair_id,
            "thread_id": self.thread_id,
            "customer_key": self.customer_key,
            "intent": self.intent,
            "risk_flags": list(self.risk_flags),
            "subject": self.subject,
            "request_at": _iso(self.request_at),
            "response_at": _iso(self.response_at),
            "response_delay_hours": self.response_delay_hours,
            "retrieval_text": retrieval_text,
            "request_text": self.request_text,
            "reference_response": self.response_text,
            "response_sender": self.response_sender,
            "boss_anchor": self.boss_anchor,
            "request_attachments": list(self.request_attachment_names),
            "response_attachments": list(self.response_attachment_names),
            "quality_score": self.quality_score,
            "direct_reply": self.direct_reply,
        }


@dataclass(frozen=True)
class PairingResult:
    pairs: tuple[ConversationPair, ...]
    rejected: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class DatasetSplit:
    knowledge_base: tuple[ConversationPair, ...]
    development: tuple[ConversationPair, ...]
    test_holdout: tuple[ConversationPair, ...]
    unused: tuple[ConversationPair, ...]


@dataclass(frozen=True)
class DatasetBuildResult:
    report: dict[str, Any]
    output_dir: Path


class _DisjointSet:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, first: int, second: int) -> None:
        first_root = self.find(first)
        second_root = self.find(second)
        if first_root == second_root:
            return
        if self.rank[first_root] < self.rank[second_root]:
            first_root, second_root = second_root, first_root
        self.parent[second_root] = first_root
        if self.rank[first_root] == self.rank[second_root]:
            self.rank[first_root] += 1


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _normalize_message_id(value: str | None) -> str | None:
    if not value:
        return None
    match = MESSAGE_ID_PATTERN.search(value)
    if match:
        return match.group(0).casefold()
    normalized = value.strip().casefold()
    return normalized or None


def _email_domain(address: str) -> str:
    return address.rpartition("@")[2].casefold()


def _stable_hash(value: str, length: int = 32) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:length]


def _body_fingerprint(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value).strip().casefold()
    return _stable_hash(normalized, 40)


def _strip_leading_security_banner(lines: list[str]) -> list[str]:
    """Remove mail-gateway warnings while preserving the first business line."""
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    banner_seen = False
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            if banner_seen:
                index += 1
                continue
            break
        is_banner = bool(SECURITY_BANNER_START_PATTERN.match(stripped))
        is_continuation = bool(
            banner_seen and SECURITY_BANNER_CONTINUATION_PATTERN.match(stripped)
        )
        if not is_banner and not is_continuation:
            break
        banner_seen = True
        greeting = BUSINESS_GREETING_PATTERN.search(stripped)
        if greeting is not None and greeting.start() > 0:
            return [stripped[greeting.start() :], *lines[index + 1 :]]
        index += 1
    return lines[index:] if banner_seen else lines


def clean_learning_text(value: str) -> str:
    """Remove quoted-history leftovers, standard signatures, and legal boilerplate."""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\u200b", "").replace("\ufeff", "").replace("\x00", "")
    lines = [WHITESPACE_PATTERN.sub(" ", line).rstrip() for line in normalized.splitlines()]
    lines = _strip_leading_security_banner(lines)
    cut_at = len(lines)
    non_empty_before = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        quoted_from = QUOTED_FROM_PATTERN.match(stripped)
        if (
            quoted_from
            and EMAIL_ADDRESS_PATTERN.search(quoted_from.group(1))
            and any(
                QUOTED_TO_PATTERN.match(item.strip())
                or QUOTED_DATE_PATTERN.match(item.strip())
                or QUOTED_SUBJECT_PATTERN.match(item.strip())
                for item in lines[index + 1 : index + 13]
            )
        ):
            cut_at = index
            break
        if DISCLAIMER_START_PATTERN.match(stripped):
            cut_at = index
            break
        if SIGNATURE_START_PATTERN.match(stripped) and non_empty_before >= 1:
            cut_at = index
            break
        non_empty_before += 1
    cleaned = "\n".join(lines[:cut_at]).strip()
    return BLANK_LINES_PATTERN.sub("\n\n", cleaned)[:100_000]


def _is_newsletter(message: Any, *, sender: str, subject: str) -> bool:
    local_part = sender.partition("@")[0].casefold()
    precedence = str(message.get("Precedence") or "").strip().casefold()
    return bool(
        message.get("List-Unsubscribe")
        or message.get("List-Id")
        or message.get("X-Campaign")
        or precedence in {"bulk", "list", "junk"}
        or local_part in NEWSLETTER_LOCAL_PARTS
        or NEWSLETTER_SUBJECT_PATTERN.search(subject)
    )


def _customer_key(
    *,
    sender: str,
    recipients: Iterable[str],
    mailbox_addresses: set[str],
    company_domains: set[str],
) -> str | None:
    normalized_sender = sender.casefold()
    if (
        normalized_sender
        and normalized_sender not in mailbox_addresses
        and _email_domain(normalized_sender) not in company_domains
    ):
        primary_external = normalized_sender
    else:
        primary_external = next(
            (
                address.casefold()
                for address in recipients
                if address
                and address.casefold() not in mailbox_addresses
                and _email_domain(address) not in company_domains
            ),
            None,
        )
    if primary_external is None:
        return None
    return _stable_hash(primary_external, 24)


def _direction(
    *,
    sender: str,
    recipients: Iterable[str],
    mailbox_addresses: set[str],
    company_domains: set[str],
    workspace_route_archive: bool,
) -> Direction:
    normalized_recipients = tuple(address.casefold() for address in recipients)
    if sender in mailbox_addresses:
        return "OUTBOUND"
    if any(address in mailbox_addresses for address in normalized_recipients):
        return "INBOUND"
    if not workspace_route_archive:
        return "UNKNOWN"
    sender_is_company = _email_domain(sender) in company_domains
    has_company_recipient = any(
        _email_domain(address) in company_domains for address in normalized_recipients
    )
    has_external_recipient = any(
        _email_domain(address) not in company_domains for address in normalized_recipients
    )
    if not sender_is_company and has_company_recipient:
        return "INBOUND"
    if sender_is_company and has_external_recipient:
        return "OUTBOUND"
    return "UNKNOWN"


def parse_historical_email(
    path: Path,
    *,
    source_root: Path,
    mailbox_addresses: set[str],
    company_domains: set[str],
    workspace_route_archive: bool = False,
) -> HistoricalEmail:
    raw = path.read_bytes()
    parsed = parse_mime(raw)
    message = BytesParser(policy=policy.default).parsebytes(raw)
    sender = parsed.from_address.casefold()
    recipients = tuple(dict.fromkeys(address.casefold() for address in parsed.to_addresses if address))
    direction = _direction(
        sender=sender,
        recipients=recipients,
        mailbox_addresses=mailbox_addresses,
        company_domains=company_domains,
        workspace_route_archive=workspace_route_archive,
    )

    body_text = clean_learning_text(parsed.body_text)
    automated = classify_automated_reply(
        subject=parsed.subject,
        body=body_text,
        headers=parsed.header_metadata,
        sender=sender,
    )
    bounce = classify_bounce(raw, subject=parsed.subject, body=body_text, sender=sender)
    newsletter = _is_newsletter(message, sender=sender, subject=parsed.subject)
    participant_domains = {
        _email_domain(address)
        for address in (sender, *recipients)
        if _email_domain(address)
    }
    internal = bool(participant_domains) and participant_domains.issubset(company_domains)
    customer_key = _customer_key(
        sender=sender,
        recipients=recipients,
        mailbox_addresses=mailbox_addresses,
        company_domains=company_domains,
    )
    exclusion_reasons: list[str] = []
    if direction == "UNKNOWN":
        exclusion_reasons.append("mailbox_not_participant")
    if not body_text:
        exclusion_reasons.append("empty_body")
    if internal:
        exclusion_reasons.append("internal_only")
    if automated.is_automated:
        exclusion_reasons.append("automated_reply")
    if bounce.is_bounce:
        exclusion_reasons.append("bounce")
    if newsletter:
        exclusion_reasons.append("newsletter_or_bulk")
    if customer_key is None:
        exclusion_reasons.append("no_external_customer")
    if parsed.occurred_at is None:
        exclusion_reasons.append("missing_date")

    return HistoricalEmail(
        source_file=path.relative_to(source_root).as_posix(),
        source_kind="EML",
        source_thread_hint=parsed.raw_sha256,
        quoted_depth=0,
        raw_sha256=parsed.raw_sha256,
        message_id=_normalize_message_id(parsed.message_id),
        in_reply_to=_normalize_message_id(parsed.in_reply_to),
        references=tuple(
            value
            for reference in parsed.references
            if (value := _normalize_message_id(reference)) is not None
        ),
        sender=sender,
        recipients=recipients,
        subject=parsed.subject.strip(),
        normalized_subject=normalized_subject(parsed.subject),
        occurred_at=parsed.occurred_at,
        direction=direction,
        body_text=body_text,
        body_fingerprint=_body_fingerprint(body_text),
        customer_key=customer_key,
        attachment_count=len(parsed.attachments),
        attachment_names=tuple(
            str(item.get("filename") or "unnamed")[:255]
            for item in parsed.attachments
            if not bool(item.get("inline_content"))
        ),
        is_automated=automated.is_automated,
        automated_reply_type=automated.reply_type.value if automated.reply_type else None,
        is_bounce=bounce.is_bounce,
        is_newsletter=newsletter,
        is_internal=internal,
        exclusion_reasons=tuple(dict.fromkeys(exclusion_reasons)),
    )


def _quoted_datetime(value: str | None, fallback: datetime | None) -> datetime | None:
    if value:
        normalized = value.replace("\xa0", " ").replace("聽", " ").strip()
        try:
            parsed = parsedate_to_datetime(normalized)
        except (TypeError, ValueError, OverflowError):
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        numeric = re.search(
            r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})"
            r"(?:\D+(\d{1,2})[:：](\d{2})(?:[:：](\d{2}))?)?",
            normalized,
        )
        if numeric:
            try:
                return datetime(
                    int(numeric.group(1)),
                    int(numeric.group(2)),
                    int(numeric.group(3)),
                    int(numeric.group(4) or 0),
                    int(numeric.group(5) or 0),
                    int(numeric.group(6) or 0),
                    tzinfo=UTC,
                )
            except ValueError:
                pass
    return fallback


def extract_quoted_history_turns(
    path: Path,
    *,
    source_root: Path,
    parent: HistoricalEmail,
    mailbox_addresses: set[str],
    company_domains: set[str],
    workspace_route_archive: bool,
) -> list[HistoricalEmail]:
    raw = path.read_bytes()
    try:
        full_body = extract_full_reply_source(raw).body_text
    except (LookupError, RecursionError, ValueError):
        return []
    lines = full_body.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    boundaries: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = QUOTED_FROM_PATTERN.match(line.strip())
        if match is None:
            continue
        addresses = EMAIL_ADDRESS_PATTERN.findall(match.group(1))
        if not addresses:
            continue
        following = lines[index + 1 : index + 13]
        if not any(
            QUOTED_TO_PATTERN.match(item.strip())
            or QUOTED_DATE_PATTERN.match(item.strip())
            or QUOTED_SUBJECT_PATTERN.match(item.strip())
            for item in following
        ):
            continue
        boundaries.append((index, addresses[0].casefold()))

    turns: list[HistoricalEmail] = []
    for depth, (start, sender) in enumerate(boundaries, start=1):
        end = boundaries[depth][0] if depth < len(boundaries) else len(lines)
        recipients: list[str] = []
        subject = parent.subject
        date_value: str | None = None
        body_start = start + 1
        for header_index in range(start + 1, min(end, start + 16)):
            stripped = lines[header_index].strip()
            to_match = QUOTED_TO_PATTERN.match(stripped)
            cc_match = QUOTED_CC_PATTERN.match(stripped)
            subject_match = QUOTED_SUBJECT_PATTERN.match(stripped)
            date_match = QUOTED_DATE_PATTERN.match(stripped)
            if to_match or cc_match:
                recipients.extend(
                    address.casefold()
                    for _, address in getaddresses(
                        [(to_match or cc_match).group(1)]
                    )
                    if address
                )
                body_start = header_index + 1
            elif subject_match:
                subject = subject_match.group(1).strip() or subject
                body_start = header_index + 1
            elif date_match:
                date_value = date_match.group(1).strip()
                body_start = header_index + 1
            elif stripped and header_index > body_start:
                break
        body_text = clean_learning_text("\n".join(lines[body_start:end]))
        if not body_text:
            continue
        unique_recipients = tuple(
            dict.fromkeys(
                address
                for address in recipients
                if "@" in address and _email_domain(address)
            )
        )
        direction = _direction(
            sender=sender,
            recipients=unique_recipients,
            mailbox_addresses=mailbox_addresses,
            company_domains=company_domains,
            workspace_route_archive=workspace_route_archive,
        )
        if workspace_route_archive and not unique_recipients and direction == "UNKNOWN":
            direction = (
                "OUTBOUND"
                if _email_domain(sender) in company_domains
                else "INBOUND"
            )
        customer_key = _customer_key(
            sender=sender,
            recipients=unique_recipients,
            mailbox_addresses=mailbox_addresses,
            company_domains=company_domains,
        )
        if workspace_route_archive and customer_key is None:
            customer_key = parent.customer_key
        participant_domains = {
            _email_domain(address)
            for address in (sender, *unique_recipients)
            if _email_domain(address)
        }
        internal = (
            bool(unique_recipients)
            and bool(participant_domains)
            and participant_domains.issubset(company_domains)
        )
        automated = classify_automated_reply(
            subject=subject,
            body=body_text,
            sender=sender,
        )
        occurred_at = _quoted_datetime(
            date_value,
            parent.occurred_at - timedelta(minutes=depth)
            if parent.occurred_at
            else None,
        )
        exclusion_reasons: list[str] = []
        if direction == "UNKNOWN":
            exclusion_reasons.append("mailbox_not_participant")
        if internal:
            exclusion_reasons.append("internal_only")
        if automated.is_automated:
            exclusion_reasons.append("automated_reply")
        if customer_key is None:
            exclusion_reasons.append("no_external_customer")
        if occurred_at is None:
            exclusion_reasons.append("missing_date")
        synthetic_sha = hashlib.sha256(
            f"{parent.raw_sha256}|{depth}|{sender}|{subject}|{body_text}".encode(
                "utf-8",
                errors="replace",
            )
        ).hexdigest()
        turns.append(
            HistoricalEmail(
                source_file=f"{parent.source_file}#quoted-{depth:04d}",
                source_kind="QUOTED_TURN",
                source_thread_hint=parent.raw_sha256,
                quoted_depth=depth,
                raw_sha256=synthetic_sha,
                message_id=None,
                in_reply_to=None,
                references=(),
                sender=sender,
                recipients=unique_recipients,
                subject=subject,
                normalized_subject=normalized_subject(subject),
                occurred_at=occurred_at,
                direction=direction,
                body_text=body_text,
                body_fingerprint=_body_fingerprint(body_text),
                customer_key=customer_key,
                attachment_count=0,
                attachment_names=(),
                is_automated=automated.is_automated,
                automated_reply_type=(
                    automated.reply_type.value if automated.reply_type else None
                ),
                is_bounce=False,
                is_newsletter=False,
                is_internal=internal,
                exclusion_reasons=tuple(dict.fromkeys(exclusion_reasons)),
            )
        )
    return turns


def load_historical_emails(
    raw_dir: Path,
    *,
    mailbox_addresses: set[str],
    company_domains: set[str],
    raw_limit: int | None = None,
    workspace_route_archive: bool = False,
    include_quoted_history: bool = False,
) -> tuple[list[HistoricalEmail], list[dict[str, str]]]:
    paths = sorted(
        path
        for path in raw_dir.rglob("*")
        if path.is_file() and path.suffix.casefold() == ".eml"
    )
    records: list[HistoricalEmail] = []
    errors: list[dict[str, str]] = []
    seen_sha256: set[str] = set()
    for path in paths:
        try:
            record = parse_historical_email(
                path,
                source_root=raw_dir,
                mailbox_addresses=mailbox_addresses,
                company_domains=company_domains,
                workspace_route_archive=workspace_route_archive,
            )
        except Exception as exc:  # One malformed export must not stop the batch.
            errors.append(
                {
                    "source_file": path.relative_to(raw_dir).as_posix(),
                    "error": f"{type(exc).__name__}: {exc}"[:1000],
                }
            )
            continue
        if record.raw_sha256 in seen_sha256:
            continue
        seen_sha256.add(record.raw_sha256)
        records.append(record)

    records.sort(
        key=lambda item: (
            item.occurred_at or datetime.min.replace(tzinfo=UTC),
            item.source_file,
        )
    )
    if raw_limit is not None and raw_limit > 0 and len(records) > raw_limit:
        records = records[-raw_limit:]
    if include_quoted_history:
        quoted_turns: list[HistoricalEmail] = []
        seen_quoted: set[tuple[str, str, str, str]] = set()
        for record in records:
            source_path = raw_dir / Path(record.source_file)
            for turn in extract_quoted_history_turns(
                source_path,
                source_root=raw_dir,
                parent=record,
                mailbox_addresses=mailbox_addresses,
                company_domains=company_domains,
                workspace_route_archive=workspace_route_archive,
            ):
                key = (
                    turn.sender,
                    turn.customer_key or "",
                    turn.normalized_subject,
                    turn.body_fingerprint,
                )
                if key in seen_quoted:
                    continue
                seen_quoted.add(key)
                quoted_turns.append(turn)
        records.extend(quoted_turns)
        records.sort(
            key=lambda item: (
                item.occurred_at or datetime.min.replace(tzinfo=UTC),
                item.source_file,
            )
        )
    return records, errors


def assign_thread_ids(records: list[HistoricalEmail]) -> dict[int, str]:
    disjoint = _DisjointSet(len(records))
    message_indexes: dict[str, int] = {}
    source_hint_indexes: dict[str, int] = {}
    for index, record in enumerate(records):
        if record.message_id and record.message_id not in message_indexes:
            message_indexes[record.message_id] = index
        if record.source_thread_hint in source_hint_indexes:
            disjoint.union(index, source_hint_indexes[record.source_thread_hint])
        else:
            source_hint_indexes[record.source_thread_hint] = index
    for index, record in enumerate(records):
        for reference in (record.in_reply_to, *record.references):
            if reference and reference in message_indexes:
                disjoint.union(index, message_indexes[reference])

    fallback_groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, record in enumerate(records):
        if record.customer_key and record.normalized_subject:
            fallback_groups[(record.customer_key, record.normalized_subject)].append(index)
    for indexes in fallback_groups.values():
        indexes.sort(
            key=lambda index: records[index].occurred_at
            or datetime.min.replace(tzinfo=UTC)
        )
        for previous, current in zip(indexes, indexes[1:], strict=False):
            previous_at = records[previous].occurred_at
            current_at = records[current].occurred_at
            if (
                previous_at is not None
                and current_at is not None
                and current_at - previous_at <= timedelta(days=120)
            ):
                disjoint.union(previous, current)

    grouped: dict[int, list[int]] = defaultdict(list)
    for index in range(len(records)):
        grouped[disjoint.find(index)].append(index)
    result: dict[int, str] = {}
    for indexes in grouped.values():
        stable_parts = sorted(
            {
                records[index].message_id
                or (
                    f"{records[index].customer_key}|"
                    f"{records[index].normalized_subject}|"
                    f"{_iso(records[index].occurred_at)}"
                )
                for index in indexes
            }
        )
        thread_id = f"thread_{_stable_hash('|'.join(stable_parts), 24)}"
        for index in indexes:
            result[index] = thread_id
    return result


def _risk_flags(request_text: str, response_text: str) -> tuple[str, ...]:
    combined = f"{request_text}\n{response_text}"
    return tuple(name for name, pattern in HIGH_RISK_PATTERNS if pattern.search(combined))


def _quality(
    request: HistoricalEmail,
    response: HistoricalEmail,
    *,
    direct_reply: bool,
) -> tuple[int, tuple[str, ...]]:
    score = 65
    reasons: list[str] = []
    if direct_reply:
        score += 15
        reasons.append("direct_message_id_reply")
    elif request.message_id and request.message_id in response.references:
        score += 10
        reasons.append("references_request")
    else:
        reasons.append("subject_customer_time_fallback")
    if request.normalized_subject == response.normalized_subject:
        score += 5
        reasons.append("matching_subject")
    if len(request.body_text) >= 40 and len(response.body_text) >= 40:
        score += 5
        reasons.append("substantive_bodies")
    if len(request.body_text) < 15:
        score -= 20
        reasons.append("short_request")
    if len(response.body_text) < 15:
        score -= 20
        reasons.append("short_response")
    if request.occurred_at and response.occurred_at:
        delay = response.occurred_at - request.occurred_at
        if delay <= timedelta(days=7):
            score += 5
            reasons.append("timely_response")
        elif delay > timedelta(days=30):
            score -= 10
            reasons.append("long_response_delay")
    return max(0, min(score, 100)), tuple(reasons)


def build_conversation_pairs(
    records: list[HistoricalEmail],
    *,
    minimum_quality: int = 60,
    maximum_response_delay_days: int = 60,
    boss_addresses: set[str] | None = None,
) -> PairingResult:
    normalized_boss_addresses = {
        address.strip().casefold()
        for address in (boss_addresses or set())
        if address.strip()
    }
    thread_ids = assign_thread_ids(records)
    by_thread: dict[str, list[tuple[int, HistoricalEmail]]] = defaultdict(list)
    for index, record in enumerate(records):
        by_thread[thread_ids[index]].append((index, record))

    pairs: list[ConversationPair] = []
    rejected: list[dict[str, Any]] = []
    seen_pair_bodies: set[tuple[str, str]] = set()
    for thread_id, items in by_thread.items():
        items.sort(
            key=lambda item: (
                item[1].occurred_at or datetime.min.replace(tzinfo=UTC),
                item[1].source_file,
            )
        )
        unused_inbound: list[HistoricalEmail] = []
        for _, record in items:
            if record.exclusion_reasons:
                continue
            if record.direction == "INBOUND":
                unused_inbound.append(record)
                continue
            if record.direction != "OUTBOUND" or record.occurred_at is None:
                continue

            eligible = [
                request
                for request in unused_inbound
                if request.occurred_at is not None
                and request.customer_key == record.customer_key
                and request.occurred_at <= record.occurred_at
                and record.occurred_at - request.occurred_at
                <= timedelta(days=maximum_response_delay_days)
            ]
            if not eligible:
                continue
            direct = [
                request
                for request in eligible
                if request.message_id
                and (
                    record.in_reply_to == request.message_id
                    or request.message_id in record.references
                )
            ]
            request = max(
                direct or eligible,
                key=lambda candidate: candidate.occurred_at
                or datetime.min.replace(tzinfo=UTC),
            )
            direct_reply = bool(direct)
            score, quality_reasons = _quality(
                request,
                record,
                direct_reply=direct_reply,
            )
            body_key = (request.body_fingerprint, record.body_fingerprint)
            if body_key in seen_pair_bodies:
                rejected.append(
                    {
                        "thread_id": thread_id,
                        "request_source_file": request.source_file,
                        "response_source_file": record.source_file,
                        "reason": "duplicate_pair_body",
                    }
                )
                unused_inbound.remove(request)
                continue
            if score < minimum_quality:
                rejected.append(
                    {
                        "thread_id": thread_id,
                        "request_source_file": request.source_file,
                        "response_source_file": record.source_file,
                        "reason": "quality_below_threshold",
                        "quality_score": score,
                    }
                )
                unused_inbound.remove(request)
                continue
            seen_pair_bodies.add(body_key)
            analysis = stub_analyze(
                request.subject,
                request.body_text,
                [
                    {"filename": name, "content_type": "application/octet-stream"}
                    for name in request.attachment_names
                ],
            )
            delay_hours = round(
                (record.occurred_at - request.occurred_at).total_seconds() / 3600,
                2,
            )
            stable_value = (
                f"{thread_id}|{request.raw_sha256}|{record.raw_sha256}|"
                f"{request.body_fingerprint}|{record.body_fingerprint}"
            )
            pairs.append(
                ConversationPair(
                    pair_id=f"pair_{_stable_hash(stable_value, 24)}",
                    thread_id=thread_id,
                    customer_key=record.customer_key or request.customer_key or "",
                    intent=analysis.intent.value,
                    risk_flags=_risk_flags(request.body_text, record.body_text),
                    subject=request.subject or record.subject,
                    request_at=request.occurred_at,
                    response_at=record.occurred_at,
                    response_delay_hours=delay_hours,
                    request_text=request.body_text,
                    response_text=record.body_text,
                    response_sender=record.sender,
                    boss_anchor=record.sender in normalized_boss_addresses,
                    request_attachment_names=request.attachment_names,
                    response_attachment_names=record.attachment_names,
                    request_source_file=request.source_file,
                    response_source_file=record.source_file,
                    direct_reply=direct_reply,
                    quality_score=score,
                    quality_reasons=quality_reasons,
                )
            )
            unused_inbound.remove(request)

    pairs.sort(key=lambda pair: (pair.response_at, pair.pair_id))
    return PairingResult(tuple(pairs), tuple(rejected))


def split_conversation_pairs(
    pairs: Iterable[ConversationPair],
    *,
    knowledge_base_target: int = 200,
    development_target: int = 50,
    test_minimum: int = 50,
    test_maximum: int = 100,
) -> DatasetSplit:
    all_pairs = sorted(pairs, key=lambda pair: (pair.response_at, pair.pair_id))
    if not all_pairs:
        return DatasetSplit((), (), (), ())

    total_target = knowledge_base_target + development_target + test_maximum
    candidate_pairs = all_pairs[-total_target:]
    groups: dict[str, list[ConversationPair]] = defaultdict(list)
    for pair in candidate_pairs:
        groups[pair.thread_id].append(pair)
    ordered_groups = sorted(
        groups.values(),
        key=lambda group: max(pair.response_at for pair in group),
    )

    desired_test = min(
        test_maximum,
        max(test_minimum, len(candidate_pairs) - knowledge_base_target - development_target),
    )

    def take_recency_biased_groups(
        groups_to_use: list[list[ConversationPair]],
        target: int,
    ) -> tuple[list[ConversationPair], list[list[ConversationPair]]]:
        if not groups_to_use or target <= 0:
            return [], list(groups_to_use)
        newest_first = list(reversed(groups_to_use))
        largest_group = max(len(group) for group in newest_first)
        upper_bound = target + largest_group
        states: dict[int, tuple[int, ...]] = {0: ()}
        for index, group in enumerate(newest_first):
            group_size = len(group)
            additions: dict[int, tuple[int, ...]] = {}
            for total, selected_indices in list(states.items()):
                new_total = total + group_size
                if new_total > upper_bound or new_total in states or new_total in additions:
                    continue
                additions[new_total] = (*selected_indices, index)
            states.update(additions)
        at_or_above = [total for total in states if total >= target]
        chosen_total = min(at_or_above) if at_or_above else max(states)
        chosen_indices = set(states[chosen_total])
        selected_groups = [
            group
            for index, group in enumerate(newest_first)
            if index in chosen_indices
        ]
        remaining_newest = [
            group
            for index, group in enumerate(newest_first)
            if index not in chosen_indices
        ]
        selected = [
            pair
            for group in selected_groups
            for pair in group
        ]
        selected.sort(key=lambda pair: (pair.response_at, pair.pair_id))
        return selected, list(reversed(remaining_newest))

    test, remaining_groups = take_recency_biased_groups(
        ordered_groups,
        desired_test,
    )
    development, remaining_groups = take_recency_biased_groups(
        remaining_groups,
        development_target,
    )
    knowledge_base, remaining_groups = take_recency_biased_groups(
        remaining_groups,
        knowledge_base_target,
    )
    selected_ids = {
        pair.pair_id for pair in (*knowledge_base, *development, *test)
    }
    unused = [pair for pair in all_pairs if pair.pair_id not in selected_ids]
    return DatasetSplit(
        tuple(knowledge_base),
        tuple(development),
        tuple(test),
        tuple(unused),
    )


def _email_document(record: HistoricalEmail) -> dict[str, Any]:
    value = asdict(record)
    value["occurred_at"] = _iso(record.occurred_at)
    value["references"] = list(record.references)
    value["recipients"] = list(record.recipients)
    value["attachment_names"] = list(record.attachment_names)
    value["exclusion_reasons"] = list(record.exclusion_reasons)
    return value


def _pair_document(pair: ConversationPair) -> dict[str, Any]:
    value = asdict(pair)
    value["request_at"] = _iso(pair.request_at)
    value["response_at"] = _iso(pair.response_at)
    value["risk_flags"] = list(pair.risk_flags)
    value["request_attachment_names"] = list(pair.request_attachment_names)
    value["response_attachment_names"] = list(pair.response_attachment_names)
    value["quality_reasons"] = list(pair.quality_reasons)
    return value


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            stream.write("\n")


def build_history_dataset(
    *,
    raw_dir: Path,
    output_dir: Path,
    mailbox_addresses: set[str],
    company_domains: set[str],
    raw_limit: int | None = 1200,
    knowledge_base_target: int = 200,
    development_target: int = 50,
    test_minimum: int = 50,
    test_maximum: int = 100,
    minimum_quality: int = 60,
    workspace_route_archive: bool = False,
    include_quoted_history: bool = False,
    boss_addresses: set[str] | None = None,
) -> DatasetBuildResult:
    normalized_mailboxes = {
        address.strip().casefold() for address in mailbox_addresses if address.strip()
    }
    normalized_domains = {
        domain.strip().casefold().lstrip("@")
        for domain in company_domains
        if domain.strip()
    }
    if not normalized_mailboxes:
        raise ValueError("at least one mailbox address is required")
    if not raw_dir.exists():
        raise FileNotFoundError(f"raw EML directory does not exist: {raw_dir}")

    records, parse_errors = load_historical_emails(
        raw_dir,
        mailbox_addresses=normalized_mailboxes,
        company_domains=normalized_domains,
        raw_limit=raw_limit,
        workspace_route_archive=workspace_route_archive,
        include_quoted_history=include_quoted_history,
    )
    pairing = build_conversation_pairs(
        records,
        minimum_quality=minimum_quality,
        boss_addresses=boss_addresses,
    )
    split = split_conversation_pairs(
        pairing.pairs,
        knowledge_base_target=knowledge_base_target,
        development_target=development_target,
        test_minimum=test_minimum,
        test_maximum=test_maximum,
    )

    _write_jsonl(
        output_dir / "private" / "normalized_emails.jsonl",
        (_email_document(record) for record in records),
    )
    _write_jsonl(
        output_dir / "private" / "all_pairs.jsonl",
        (_pair_document(pair) for pair in pairing.pairs),
    )
    _write_jsonl(
        output_dir / "private" / "rejected_pairs.jsonl",
        pairing.rejected,
    )
    _write_jsonl(
        output_dir / "knowledge_base" / "examples.jsonl",
        (pair.rag_document() for pair in split.knowledge_base),
    )
    _write_jsonl(
        output_dir / "development" / "examples.jsonl",
        (pair.rag_document() for pair in split.development),
    )
    _write_jsonl(
        output_dir / "test_holdout" / "examples.jsonl",
        (pair.rag_document() for pair in split.test_holdout),
    )

    excluded_counts: dict[str, int] = defaultdict(int)
    for record in records:
        for reason in record.exclusion_reasons:
            excluded_counts[reason] += 1
    intent_counts: dict[str, int] = defaultdict(int)
    risk_counts: dict[str, int] = defaultdict(int)
    for pair in pairing.pairs:
        intent_counts[pair.intent] += 1
        for risk_flag in pair.risk_flags:
            risk_counts[risk_flag] += 1
    split_thread_sets = {
        "knowledge_base": {pair.thread_id for pair in split.knowledge_base},
        "development": {pair.thread_id for pair in split.development},
        "test_holdout": {pair.thread_id for pair in split.test_holdout},
    }
    thread_overlap = {
        "kb_dev": sorted(
            split_thread_sets["knowledge_base"] & split_thread_sets["development"]
        ),
        "kb_test": sorted(
            split_thread_sets["knowledge_base"] & split_thread_sets["test_holdout"]
        ),
        "dev_test": sorted(
            split_thread_sets["development"] & split_thread_sets["test_holdout"]
        ),
    }
    report: dict[str, Any] = {
        "schema_version": "email-rag-build-report.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "raw_dir": str(raw_dir.resolve()),
        "output_dir": str(output_dir.resolve()),
        "mailbox_count": len(normalized_mailboxes),
        "boss_addresses": sorted(
            address.strip().casefold()
            for address in (boss_addresses or set())
            if address.strip()
        ),
        "boss_anchor_pairs": sum(pair.boss_anchor for pair in pairing.pairs),
        "company_domains": sorted(normalized_domains),
        "raw_eml_files": sum(
            1
            for path in raw_dir.rglob("*")
            if path.is_file() and path.suffix.casefold() == ".eml"
        ),
        "unique_parsed_emails": sum(
            1 for record in records if record.source_kind == "EML"
        ),
        "quoted_history_turns": sum(
            1 for record in records if record.source_kind == "QUOTED_TURN"
        ),
        "workspace_route_archive": workspace_route_archive,
        "quoted_history_enabled": include_quoted_history,
        "parse_errors": len(parse_errors),
        "parse_error_details": parse_errors[:100],
        "excluded_email_flags": dict(sorted(excluded_counts.items())),
        "valid_pairs": len(pairing.pairs),
        "rejected_pairs": len(pairing.rejected),
        "splits": {
            "knowledge_base": len(split.knowledge_base),
            "development": len(split.development),
            "test_holdout": len(split.test_holdout),
            "unused": len(split.unused),
        },
        "split_strategy": "thread_exclusive_recency_biased_packing",
        "intent_counts": dict(sorted(intent_counts.items())),
        "risk_counts": dict(sorted(risk_counts.items())),
        "thread_overlap": thread_overlap,
        "thread_isolation_ok": not any(thread_overlap.values()),
        "targets_met": {
            "knowledge_base": len(split.knowledge_base) >= knowledge_base_target,
            "development": len(split.development) >= development_target,
            "test_holdout": len(split.test_holdout) >= test_minimum,
        },
        "token_usage": {
            "llm_calls": 0,
            "embedding_calls": 0,
            "api_tokens": 0,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary = (
        "# Historical email RAG dataset\n\n"
        f"- Raw EML files: {report['raw_eml_files']}\n"
        f"- Unique parsed emails: {report['unique_parsed_emails']}\n"
        f"- Valid conversation pairs: {report['valid_pairs']}\n"
        f"- Knowledge base: {report['splits']['knowledge_base']}\n"
        f"- Development: {report['splits']['development']}\n"
        f"- Test holdout: {report['splits']['test_holdout']}\n"
        f"- Thread isolation: {'OK' if report['thread_isolation_ok'] else 'FAILED'}\n"
        "- LLM/embedding token usage during preparation: 0\n\n"
        "The `test_holdout` directory must not be loaded by retrieval, prompt tuning, "
        "or manual example selection. It is only for final evaluation.\n"
    )
    (output_dir / "README.md").write_text(summary, encoding="utf-8")
    return DatasetBuildResult(report=report, output_dir=output_dir)
