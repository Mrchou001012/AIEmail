import re
from dataclasses import dataclass
from enum import StrEnum


class AutomatedReplyType(StrEnum):
    OUT_OF_OFFICE = "OUT_OF_OFFICE"
    DEPARTED = "DEPARTED"
    CONTACT_CHANGE = "CONTACT_CHANGE"
    GENERIC_AUTOREPLY = "GENERIC_AUTOREPLY"
    SYSTEM_NOTIFICATION = "SYSTEM_NOTIFICATION"


@dataclass(frozen=True)
class AutomatedReplyClassification:
    reply_type: AutomatedReplyType | None
    confidence: float
    detected_by: tuple[str, ...] = ()
    return_hint: str | None = None
    replacement_emails: tuple[str, ...] = ()
    forwarded_to_replacement: bool = False

    @property
    def is_automated(self) -> bool:
        return self.reply_type is not None

    def metadata(self) -> dict[str, object]:
        return {
            "confidence": self.confidence,
            "detected_by": list(self.detected_by),
            "return_hint": self.return_hint,
            "replacement_emails": list(self.replacement_emails),
            "forwarded_to_replacement": self.forwarded_to_replacement,
        }


AUTO_HEADER_NAMES = (
    "auto-submitted",
    "precedence",
    "x-autoreply",
    "x-autorespond",
    "x-auto-response-suppress",
)

AUTO_SUBJECT_PATTERNS = (
    r"\bauto(?:matic)?[ -]?reply\b",
    r"\bout of (?:the )?office\b",
    r"\bvacation reply\b",
    r"自动回复",
    r"不在办公室",
)

SYSTEM_NOTIFICATION_SUBJECT_PATTERNS = (
    r"^\s*failure notification\s*$",
    r"^\s*(?:delivery status notification|non-delivery report|undeliverable)\b",
    r"^\s*(?:mail delivery failed|returned mail)\b",
)

SYSTEM_NOTIFICATION_BODY_PATTERNS = (
    r"\bdo not reply to this e-?mail\b",
    r"\bthis is an automated (?:server|system)\b",
    r"\bautomated server not responding to e-?mail communications\b",
)

OUT_OF_OFFICE_PATTERNS = (
    r"\bi am (?:currently )?(?:out of|away from) (?:the )?office\b",
    r"\bi am (?:currently )?on (?:annual |maternity |parental |medical )?leave\b",
    r"\bi am (?:currently )?on (?:a )?leave of absence\b",
    r"\bi (?:will be|am) (?:on vacation|away)\b",
    r"\blimited access to (?:my )?email\b",
    r"\bwill (?:return|be back)\b",
    r"\boffices? (?:is|are|will be) closed\b",
    r"休假中",
    r"正在休假",
    r"不在办公室",
    r"无法及时回复",
)

STRONG_OUT_OF_OFFICE_PREFIX_PATTERNS = (
    r"^\s*(?:thank you for (?:your )?email[.!]?\s*)?i am (?:currently )?(?:out of|away from) (?:the )?office\b",
    r"^\s*(?:thank you for (?:your )?email[.!]?\s*)?i will be away from (?:the )?office\b",
    r"^\s*(?:thank you for (?:your )?email[.!]?\s*)?i am (?:currently )?on (?:annual |maternity |parental |medical )?leave\b",
    r"^\s*(?:thank you for (?:your )?email[.!]?\s*)?i am (?:currently )?on (?:a )?leave of absence\b",
    r"^\s*(?:thank you for (?:your )?email[.!]?\s*)?(?:our )?offices? (?:is|are|will be) closed\b",
    r"^\s*(?:您好[，,。\s]*)?(?:我)?(?:正在休假|休假中|不在办公室)\b",
)

DEPARTED_PATTERNS = (
    r"\bno longer (?:works?|working|employed|with)\b",
    r"\bhas left (?:the|our) (?:company|organisation|organization|business)\b",
    r"\bleft (?:the|our) (?:company|organisation|organization|business)\b",
    r"\bis no longer (?:at|with)\b",
    r"\bis no longer associated with\b",
    r"\bformer employee\b",
    r"\bmailbox (?:is )?no longer (?:monitored|in use)\b",
    r"已经离职",
    r"已离职",
    r"已离开公司",
    r"不再任职",
    r"邮箱不再使用",
)

CONTACT_CHANGE_PATTERNS = (
    r"\bnew (?:point of )?contact\b",
    r"\byour (?:new )?(?:point of )?contact (?:is|will be)\b",
    r"\bgoing forward.{0,80}\b(?:contact|reach)\b",
    r"\bplease (?:contact|reach out to|direct .{0,30} to)\b",
    r"联系人变更",
    r"新联系人",
    r"今后请联系",
    r"后续请联系",
)

RETURN_HINT_PATTERNS = (
    r"\b(?:return(?:ing)?|back)(?: to the office)?(?: on)?\s+([^\n.;]{3,60})",
    r"\b(?:out of (?:the )?office|away from (?:the )?office|on leave|on vacation)\s+"
    r"(?:through|until|till)\s+([^\n.;]{3,60})",
    r"\boffices? (?:is|are|will be) closed\s+from\s+[^\n.;]{3,60}?\s+(?:to|through|until)\s+([^\n.;]{3,60})",
    r"(?:返岗|回来|休假至|休假到)[：:\s]*([^\n。；;]{2,40})",
)

EMAIL_PATTERN = re.compile(r"(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63})(?![\w-])", re.I)

QUOTED_HISTORY_PATTERNS = (
    re.compile(r"^\s*on\s+.+\bwrote:\s*$", re.I),
    re.compile(r"^\s*-{2,}\s*original message\s*-{2,}\s*$", re.I),
    re.compile(r"^\s*_{5,}\s*$"),
)

OUTLOOK_HEADER_PATTERN = re.compile(
    r"^\s*(from|sent|to|subject):\s+",
    re.I,
)

REPLACEMENT_CONTEXT_PATTERNS = (
    re.compile(r"\bplease\s+(?:contact|email|reach(?:\s+out)?\s+to)\b", re.I),
    re.compile(r"\b(?:contact|email|reach(?:\s+out)?\s+to)\s+[\w .,'’()/-]{0,100}$", re.I),
    re.compile(r"\bdirect\s+(?:any\s+|all\s+|future\s+|your\s+|the\s+)*correspondence\s+to\b", re.I),
    re.compile(r"\b(?:new|alternative|alternate|replacement|backup)\s+(?:point\s+of\s+)?contact\b", re.I),
    re.compile(r"\bfor\s+(?:urgent|immediate|procurement|purchasing|sourcing)[^\n.;]{0,120}\b(?:contact|email)\b", re.I),
)

FORWARDED_PATTERNS = (
    r"\b(?:has|have|had|is|was|will be) (?:already )?(?:automatically )?forwarded\b",
    r"\b(?:automatically )?forwarded (?:your|this|the) (?:email|message|inquiry|enquiry)\b",
    r"\bthis (?:email|message) has been (?:automatically )?forwarded\b",
)

# Exact infrastructure senders that can never represent a human sales contact.
# Keep this deliberately narrow: blocking an entire provider domain such as
# google.com could hide a legitimate inquiry from an employee of that company.
SYSTEM_NOTIFICATION_SENDERS = frozenset(
    {
        "no-reply@accounts.google.com",
    }
)


def _matches(patterns: tuple[str, ...], value: str) -> bool:
    return any(re.search(pattern, value, re.I | re.S) for pattern in patterns)


def _auto_header_signal(headers: dict[str, str]) -> tuple[bool, list[str]]:
    detected_by: list[str] = []
    auto_submitted = headers.get("auto-submitted", "").strip().casefold()
    if auto_submitted and auto_submitted != "no":
        detected_by.append("header:auto-submitted")
    precedence = headers.get("precedence", "").strip().casefold()
    if precedence in {"auto_reply", "autoreply", "bulk", "junk"}:
        detected_by.append("header:precedence")
    for name in ("x-autoreply", "x-autorespond"):
        if headers.get(name, "").strip():
            detected_by.append(f"header:{name}")
    suppress = headers.get("x-auto-response-suppress", "").strip().casefold()
    if suppress and suppress not in {"none", "no"}:
        detected_by.append("header:x-auto-response-suppress")
    return bool(detected_by), detected_by


def _return_hint(text: str) -> str | None:
    for pattern in RETURN_HINT_PATTERNS:
        match = re.search(pattern, text, re.I)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()[:80]
    return None


def latest_authored_text(body: str) -> str:
    """Return the newly authored portion, excluding common quoted history.

    Disposition extraction must not treat an address in an older quoted message
    as a newly proposed contact.  Keep this deliberately conservative: when a
    boundary is uncertain the original text is retained and no mutation is
    authorized merely by this helper.
    """

    lines = (body or "").replace("\r\n", "\n").replace("\r", "\n").splitlines()
    for index, line in enumerate(lines):
        outlook_header = _starts_outlook_header_block(lines, index)
        if index > 0 and (
            outlook_header
            or any(pattern.match(line) for pattern in QUOTED_HISTORY_PATTERNS)
        ):
            authored = "\n".join(lines[:index]).strip()
            if authored:
                return authored
    return (body or "").strip()


def _starts_outlook_header_block(lines: list[str], index: int) -> bool:
    """Require a real header block instead of truncating on an in-body Subject line."""

    fields: set[str] = set()
    for candidate in lines[index : index + 8]:
        stripped = candidate.strip()
        if not stripped:
            if fields:
                continue
            return False
        match = OUTLOOK_HEADER_PATTERN.match(stripped)
        if not match:
            break
        fields.add(match.group(1).casefold())
    return ("from" in fields and len(fields) >= 2) or len(fields) >= 3


def _contextual_replacement_emails(body: str, sender: str | None) -> tuple[str, ...]:
    """Extract only addresses explicitly presented as alternate contacts."""

    authored = latest_authored_text(body)[:50_000]
    matches = list(EMAIL_PATTERN.finditer(authored))
    candidates: list[str] = []
    normalized_sender = (sender or "").strip().casefold()
    for match in matches:
        address = match.group(1).casefold()
        if address == normalized_sender:
            continue
        before = authored[max(0, match.start() - 320) : match.start()]
        # A directive may introduce a short bulleted list of several contacts;
        # inspect the preceding context rather than accepting every signature.
        if any(pattern.search(before) for pattern in REPLACEMENT_CONTEXT_PATTERNS):
            candidates.append(address)
    return tuple(dict.fromkeys(candidates))


def classify_automated_reply(
    *,
    subject: str,
    body: str,
    headers: dict[str, str] | None = None,
    sender: str | None = None,
) -> AutomatedReplyClassification:
    normalized_headers = {str(key).casefold(): str(value) for key, value in (headers or {}).items()}
    normalized_sender = (sender or "").strip().casefold()
    if normalized_sender in SYSTEM_NOTIFICATION_SENDERS:
        return AutomatedReplyClassification(
            AutomatedReplyType.SYSTEM_NOTIFICATION,
            1.0,
            (f"sender:system-notification:{normalized_sender}",),
        )
    authored_body = latest_authored_text(body)
    if _matches(SYSTEM_NOTIFICATION_SUBJECT_PATTERNS, subject) or _matches(
        SYSTEM_NOTIFICATION_BODY_PATTERNS, authored_body
    ):
        return AutomatedReplyClassification(
            AutomatedReplyType.SYSTEM_NOTIFICATION,
            1.0,
            ("content:system-notification",),
        )
    auto_header, detected_by = _auto_header_signal(normalized_headers)
    subject_signal = _matches(AUTO_SUBJECT_PATTERNS, subject)
    if subject_signal:
        detected_by.append("subject:auto-reply")
    text = f"{subject}\n{authored_body}"[:100_000]
    replacement_emails = _contextual_replacement_emails(authored_body, sender)
    return_hint = _return_hint(text)
    forwarded_to_replacement = _matches(FORWARDED_PATTERNS, authored_body)

    if _matches(DEPARTED_PATTERNS, text):
        return AutomatedReplyClassification(
            AutomatedReplyType.DEPARTED,
            0.99,
            tuple([*detected_by, "body:departed"]),
            return_hint,
            replacement_emails,
            forwarded_to_replacement,
        )
    strong_ooo_prefix = _matches(STRONG_OUT_OF_OFFICE_PREFIX_PATTERNS, body[:1500])
    if _matches(OUT_OF_OFFICE_PATTERNS, text) and (auto_header or subject_signal or strong_ooo_prefix):
        return AutomatedReplyClassification(
            AutomatedReplyType.OUT_OF_OFFICE,
            0.98 if auto_header or subject_signal else 0.90,
            tuple([*detected_by, "body:out-of-office"]),
            return_hint,
            replacement_emails,
            forwarded_to_replacement,
        )
    if _matches(CONTACT_CHANGE_PATTERNS, text):
        return AutomatedReplyClassification(
            AutomatedReplyType.CONTACT_CHANGE,
            0.96,
            tuple([*detected_by, "body:contact-change"]),
            return_hint,
            replacement_emails,
            forwarded_to_replacement,
        )
    if auto_header or subject_signal:
        return AutomatedReplyClassification(
            AutomatedReplyType.GENERIC_AUTOREPLY,
            0.95,
            tuple(detected_by),
            return_hint,
            replacement_emails,
            forwarded_to_replacement,
        )
    return AutomatedReplyClassification(
        None,
        0.0,
        (),
        return_hint,
        replacement_emails,
        forwarded_to_replacement,
    )
