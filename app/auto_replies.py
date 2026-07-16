import re
from dataclasses import dataclass
from enum import StrEnum


class AutomatedReplyType(StrEnum):
    OUT_OF_OFFICE = "OUT_OF_OFFICE"
    DEPARTED = "DEPARTED"
    CONTACT_CHANGE = "CONTACT_CHANGE"
    GENERIC_AUTOREPLY = "GENERIC_AUTOREPLY"


@dataclass(frozen=True)
class AutomatedReplyClassification:
    reply_type: AutomatedReplyType | None
    confidence: float
    detected_by: tuple[str, ...] = ()
    return_hint: str | None = None
    replacement_emails: tuple[str, ...] = ()

    @property
    def is_automated(self) -> bool:
        return self.reply_type is not None

    def metadata(self) -> dict[str, object]:
        return {
            "confidence": self.confidence,
            "detected_by": list(self.detected_by),
            "return_hint": self.return_hint,
            "replacement_emails": list(self.replacement_emails),
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

OUT_OF_OFFICE_PATTERNS = (
    r"\bi am (?:currently )?(?:out of|away from) (?:the )?office\b",
    r"\bi am (?:currently )?on (?:annual |maternity |parental |medical )?leave\b",
    r"\bi (?:will be|am) (?:on vacation|away)\b",
    r"\blimited access to (?:my )?email\b",
    r"\bwill (?:return|be back)\b",
    r"休假中",
    r"正在休假",
    r"不在办公室",
    r"无法及时回复",
)

STRONG_OUT_OF_OFFICE_PREFIX_PATTERNS = (
    r"^\s*(?:thank you for (?:your )?email[.!]?\s*)?i am (?:currently )?(?:out of|away from) (?:the )?office\b",
    r"^\s*(?:thank you for (?:your )?email[.!]?\s*)?i am (?:currently )?on (?:annual |maternity |parental |medical )?leave\b",
    r"^\s*(?:您好[，,。\s]*)?(?:我)?(?:正在休假|休假中|不在办公室)\b",
)

DEPARTED_PATTERNS = (
    r"\bno longer (?:works?|working|employed|with)\b",
    r"\bhas left (?:the|our) (?:company|organisation|organization|business)\b",
    r"\bleft (?:the|our) (?:company|organisation|organization|business)\b",
    r"\bis no longer (?:at|with)\b",
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
    r"\b(?:out of (?:the )?office|on leave|on vacation)\s+(?:through|until)\s+([^\n.;]{3,60})",
    r"(?:返岗|回来|休假至|休假到)[：:\s]*([^\n。；;]{2,40})",
)

EMAIL_PATTERN = re.compile(r"(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63})(?![\w-])", re.I)


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


def classify_automated_reply(
    *,
    subject: str,
    body: str,
    headers: dict[str, str] | None = None,
    sender: str | None = None,
) -> AutomatedReplyClassification:
    normalized_headers = {str(key).casefold(): str(value) for key, value in (headers or {}).items()}
    auto_header, detected_by = _auto_header_signal(normalized_headers)
    subject_signal = _matches(AUTO_SUBJECT_PATTERNS, subject)
    if subject_signal:
        detected_by.append("subject:auto-reply")
    text = f"{subject}\n{body}"[:100_000]
    replacement_emails = tuple(
        dict.fromkeys(
            address.casefold()
            for address in EMAIL_PATTERN.findall(body)
            if not sender or address.casefold() != sender.casefold()
        )
    )
    return_hint = _return_hint(text)

    if _matches(DEPARTED_PATTERNS, text):
        return AutomatedReplyClassification(
            AutomatedReplyType.DEPARTED,
            0.99,
            tuple([*detected_by, "body:departed"]),
            return_hint,
            replacement_emails,
        )
    strong_ooo_prefix = _matches(STRONG_OUT_OF_OFFICE_PREFIX_PATTERNS, body[:1500])
    if _matches(OUT_OF_OFFICE_PATTERNS, text) and (auto_header or subject_signal or strong_ooo_prefix):
        return AutomatedReplyClassification(
            AutomatedReplyType.OUT_OF_OFFICE,
            0.98 if auto_header or subject_signal else 0.90,
            tuple([*detected_by, "body:out-of-office"]),
            return_hint,
            replacement_emails,
        )
    if _matches(CONTACT_CHANGE_PATTERNS, text):
        return AutomatedReplyClassification(
            AutomatedReplyType.CONTACT_CHANGE,
            0.96,
            tuple([*detected_by, "body:contact-change"]),
            return_hint,
            replacement_emails,
        )
    if auto_header or subject_signal:
        return AutomatedReplyClassification(
            AutomatedReplyType.GENERIC_AUTOREPLY,
            0.95,
            tuple(detected_by),
            return_hint,
            replacement_emails,
        )
    return AutomatedReplyClassification(None, 0.0)
