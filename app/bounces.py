import re
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr
from enum import StrEnum


class BounceType(StrEnum):
    HARD = "HARD"
    SOFT = "SOFT"
    POLICY = "POLICY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class BounceClassification:
    is_bounce: bool
    bounce_type: BounceType | None = None
    recipient: str | None = None
    action: str | None = None
    status_code: str | None = None
    diagnostic: str | None = None
    original_message_id: str | None = None
    detected_by: tuple[str, ...] = ()

    @property
    def permanent(self) -> bool:
        return self.bounce_type == BounceType.HARD

    def metadata(self) -> dict[str, object]:
        return {
            "bounce_type": self.bounce_type.value if self.bounce_type else None,
            "recipient": self.recipient,
            "action": self.action,
            "status_code": self.status_code,
            "diagnostic": self.diagnostic,
            "original_message_id": self.original_message_id,
            "detected_by": list(self.detected_by),
            "permanent": self.permanent,
        }


_STATUS_RE = re.compile(r"\b([245]\.\d{1,3}\.\d{1,3})\b")
_EMAIL_RE = re.compile(r"(?i)(?<![\w.+-])([a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+)(?![\w-])")
_SUBJECT_MARKERS = re.compile(
    r"(?i)(delivery status notification|delivery failure|mail delivery failed|undeliverable|returned mail|failure notice|邮件投递失败|退信)"
)
_BOUNCE_TEXT_MARKERS = re.compile(
    r"(?i)(address not found|couldn.?t be found|no such user|user unknown|unknown recipient|recipient address rejected|"
    r"mailbox unavailable|delivery (?:has )?failed|message (?:was )?not delivered|undeliverable|returned to sender|"
    r"域名不存在|用户不存在|收件人不存在|邮箱地址不存在|投递失败)"
)
_SOFT_MARKERS = re.compile(
    r"(?i)(mailbox (?:is )?full|over quota|quota exceeded|temporar(?:y|ily)|try again|greylist|rate limit|too many messages)"
)
_POLICY_MARKERS = re.compile(
    r"(?i)(spam|policy|reputation|blacklist|blocked|prohibited|authentication required|dmarc|spf|dkim)"
)
_HARD_MARKERS = re.compile(
    r"(?i)(no such user|user unknown|unknown recipient|address not found|recipient address rejected|"
    r"does not exist|unrouteable address|bad destination mailbox|bad destination system|domain (?:does not exist|not found)|"
    r"用户不存在|收件人不存在|邮箱地址不存在|域名不存在)"
)
_PERMANENT_FAILURE_MARKERS = re.compile(
    r"(?i)(nxdomain|badrcptdomain|no such (?:user|domain)|user unknown|unknown recipient|"
    r"address not found|email account (?:does not|doesn't) exist|unrouteable address|"
    r"bad destination (?:mailbox|system)|domain name not found|"
    r"domain\s+[^\s,;]+\s+(?:does not exist|not found|couldn.?t be found|could not be found))"
)


def _field(block: Message, name: str) -> str | None:
    value = block.get(name)
    return str(value).strip()[:2000] if value is not None else None


def _recipient(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.split(";", 1)[-1].strip()
    parsed = parseaddr(candidate)[1].casefold()
    if parsed:
        return parsed
    match = _EMAIL_RE.search(candidate)
    return match.group(1).casefold() if match else None


def _original_message_data(message: Message) -> tuple[str | None, str | None]:
    for part in message.walk():
        if part.get_content_type() != "message/rfc822":
            continue
        payload = part.get_payload()
        nested = payload[0] if isinstance(payload, list) and payload else None
        if isinstance(nested, Message):
            message_id = str(nested.get("Message-ID")) if nested.get("Message-ID") else None
            recipients = [*nested.get_all("To", []), *nested.get_all("Cc", [])]
            addresses = [address.casefold() for _, address in getaddresses(recipients) if address]
            return message_id, addresses[0] if addresses else None
    return None, None


def has_permanent_failure_evidence(text: str, *, status_code: str | None = None) -> bool:
    """Return true only for evidence that identifies a permanently invalid route.

    Some providers emit a temporary-looking enhanced status or include phrases
    such as ``try again`` in a permanent NXDOMAIN/unknown-user report.  Those
    generic soft markers must not override an explicit nonexistent domain or
    recipient.
    """
    status_match = _STATUS_RE.search(status_code or text)
    normalized_status = status_match.group(1) if status_match else status_code
    return bool(
        (
            normalized_status
            and (normalized_status.startswith("5.1.") or normalized_status == "5.4.4")
        )
        or _PERMANENT_FAILURE_MARKERS.search(text)
    )


def classify_bounce(raw: bytes, *, subject: str, body: str, sender: str) -> BounceClassification:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    detected_by: list[str] = []
    content_type = message.get_content_type().casefold()
    report_type = str(message.get_param("report-type") or "").casefold()
    if content_type == "multipart/report" and report_type == "delivery-status":
        detected_by.append("mime:delivery-status-report")

    sender_local = sender.partition("@")[0].casefold()
    if sender_local in {"mailer-daemon", "postmaster"}:
        detected_by.append("sender:mail-system")
    failed_header = str(message.get("X-Failed-Recipients") or "").strip()
    if failed_header:
        detected_by.append("header:x-failed-recipients")

    action = None
    status_code = None
    diagnostic = None
    recipient = _recipient(failed_header)
    original_message_id = str(message.get("Original-Message-ID") or "").strip() or None
    for part in message.walk():
        if part.get_content_type() != "message/delivery-status":
            continue
        detected_by.append("mime:message-delivery-status")
        payload = part.get_payload()
        blocks = payload if isinstance(payload, list) else []
        for block in blocks:
            if not isinstance(block, Message):
                continue
            recipient = recipient or _recipient(_field(block, "Final-Recipient")) or _recipient(_field(block, "Original-Recipient"))
            action = action or _field(block, "Action")
            status_code = status_code or _field(block, "Status")
            diagnostic = diagnostic or _field(block, "Diagnostic-Code")
            original_message_id = original_message_id or _field(block, "Original-Message-ID")

    nested_message_id, nested_recipient = _original_message_data(message)
    original_message_id = original_message_id or nested_message_id
    recipient = recipient or nested_recipient
    combined = "\n".join(value for value in (subject, body, diagnostic or "") if value)
    status_match = _STATUS_RE.search(status_code or diagnostic or combined)
    status_code = status_match.group(1) if status_match else status_code

    structured = any(item.startswith("mime:") for item in detected_by) or bool(failed_header)
    marker = bool(_SUBJECT_MARKERS.search(subject) or _BOUNCE_TEXT_MARKERS.search(combined))
    if not structured and not ("sender:mail-system" in detected_by and marker):
        return BounceClassification(False)
    if marker:
        detected_by.append("text:bounce-marker")

    if recipient is None:
        candidates = [match.group(1).casefold() for match in _EMAIL_RE.finditer(combined)]
        recipient = next((candidate for candidate in candidates if candidate != sender.casefold()), None)

    normalized_action = (action or "").casefold()
    if has_permanent_failure_evidence(combined, status_code=status_code):
        bounce_type = BounceType.HARD
    elif (status_code and status_code.startswith("5.7.")) or _POLICY_MARKERS.search(combined):
        bounce_type = BounceType.POLICY
    elif (
        (status_code and status_code.startswith("4."))
        or (status_code == "5.2.2")
        or normalized_action in {"delayed", "expanded"}
        or _SOFT_MARKERS.search(combined)
    ):
        bounce_type = BounceType.SOFT
    elif (
        (status_code and (status_code.startswith("5.1.") or status_code in {"5.4.4"}))
        or _HARD_MARKERS.search(combined)
    ):
        bounce_type = BounceType.HARD
    else:
        bounce_type = BounceType.UNKNOWN

    return BounceClassification(
        True,
        bounce_type=bounce_type,
        recipient=recipient,
        action=action,
        status_code=status_code,
        diagnostic=diagnostic,
        original_message_id=original_message_id,
        detected_by=tuple(dict.fromkeys(detected_by)),
    )


def classify_smtp_failure(smtp_code: int, diagnostic: str) -> BounceType:
    status_match = _STATUS_RE.search(diagnostic)
    status_code = status_match.group(1) if status_match else None
    if has_permanent_failure_evidence(diagnostic, status_code=status_code):
        return BounceType.HARD
    if (status_code and status_code.startswith("5.7.")) or _POLICY_MARKERS.search(diagnostic):
        return BounceType.POLICY
    if smtp_code < 500 or (status_code and status_code.startswith("4.")) or _SOFT_MARKERS.search(diagnostic):
        return BounceType.SOFT
    if (
        (status_code and (status_code.startswith("5.1.") or status_code == "5.4.4"))
        or _HARD_MARKERS.search(diagnostic)
    ):
        return BounceType.HARD
    return BounceType.UNKNOWN
