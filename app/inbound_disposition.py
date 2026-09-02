import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TypedDict

from app.ai import explicit_product_list_requested
from app.auto_replies import (
    AutomatedReplyType,
    classify_automated_reply,
    latest_authored_text,
)


class InboundDispositionType(StrEnum):
    BUSINESS = "BUSINESS"
    TEMPORARY_ABSENCE = "TEMPORARY_ABSENCE"
    DEPARTED = "DEPARTED"
    CONTACT_REFERRAL = "CONTACT_REFERRAL"
    FORWARDED_TO_COLLEAGUE = "FORWARDED_TO_COLLEAGUE"
    CONTACT_IDENTITY_MISMATCH = "CONTACT_IDENTITY_MISMATCH"
    NON_TARGET = "NON_TARGET"
    UNCERTAIN = "UNCERTAIN"
    AUTOMATED_ACKNOWLEDGEMENT = "AUTOMATED_ACKNOWLEDGEMENT"
    SYSTEM_NOTIFICATION = "SYSTEM_NOTIFICATION"


NON_TARGET_ROLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "LOGISTICS_SERVICE_PROVIDER",
        re.compile(
            r"\b(?:i\s+am|we\s+are|our\s+company\s+is)\s+(?:an?\s+)?"
            r"(?:logistics(?:\s+service)?\s+provider|freight\s+forwarder|"
            r"freight\s+forwarding\s+company|customs\s+broker)\b",
            re.I,
        ),
    ),
    (
        "LOGISTICS_SERVICE_PROVIDER",
        re.compile(
            r"\bwe\s+(?:provide|offer|speciali[sz]e\s+in)\s+"
            r"(?:freight\s+forwarding|customs\s+clearance|logistics\s+services)\b",
            re.I,
        ),
    ),
    (
        "SUPPLIER_VENDOR",
        re.compile(
            r"\b(?:please|kindly)\s+find\s+our\s+(?:best\s+|updated\s+|current\s+)?"
            r"(?:offer|quotation|quote)\b|"
            r"\b(?:our|the)\s+(?:updated\s+|best\s+|current\s+)?price\s+"
            r"(?:of\s+this\s+week\s+)?"
            r"(?:is|would\s+be|could\s+be)\b",
            re.I,
        ),
    ),
)

CONTACT_IDENTITY_MISMATCH_PATTERNS = (
    re.compile(
        r"(?i:\bthere\s+(?:is|are)\s+no\s+)"
        r"(?:[A-Z]{2,}|[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})"
        r"(?i:\s+(?:in|at|with)\s+(?:our|this)\s+"
        r"(?:company|organisation|organization)\b)",
    ),
    re.compile(
        r"\bwe\s+(?:do\s+not|don't)\s+(?:have|employ|know)\s+"
        r"(?:anyone|anybody|an?\s+(?:employee|person|contact))\s+"
        r"(?:named\s+|called\s+)?[a-z][a-z .'-]{1,80}\b",
        re.I,
    ),
    re.compile(
        r"\b(?:you\s+have|this\s+is|you(?:'|’)ve\s+reached)\s+"
        r"(?:the\s+)?wrong\s+(?:person|contact|company|address|email)\b",
        re.I,
    ),
)

FORWARDED_TO_COLLEAGUE_PATTERNS = (
    re.compile(
        r"\b(?:i|we)\s+(?:have\s+)?(?:already\s+)?forwarded\s+"
        r"(?:your|this|the)\s+(?:email|message|inquiry|enquiry)\s+to\b",
        re.I,
    ),
    re.compile(
        r"\b(?:your|this|the)\s+(?:email|message|inquiry|enquiry)\s+"
        r"(?:has|have)\s+been\s+forwarded\s+to\b",
        re.I,
    ),
)


@dataclass(frozen=True)
class InboundDisposition:
    disposition_type: InboundDispositionType
    confidence: float
    reason: str
    authored_text: str
    replacement_emails: tuple[str, ...] = ()
    return_hint: str | None = None
    forwarded_to_replacement: bool = False
    non_target_reason: str | None = None
    product_list_requested: bool = False
    automated_reply_type: AutomatedReplyType | None = None
    automated_transport_signal: bool = False
    classifier_source: str = "deterministic_rule"
    classifier_model: str | None = None
    classifier_request_hash: str | None = None
    classifier_request_id: str | None = None
    evidence: tuple[str, ...] = ()
    classification_error: str | None = None
    normalization_notes: tuple[str, ...] = ()

    @property
    def continue_business_processing(self) -> bool:
        if self.disposition_type is InboundDispositionType.BUSINESS:
            return True
        return bool(
            self.product_list_requested
            and not self.automated_transport_signal
            and self.disposition_type
            in {
                InboundDispositionType.DEPARTED,
                InboundDispositionType.CONTACT_REFERRAL,
            }
        )

    def metadata(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "replacement_emails": list(self.replacement_emails),
            "return_hint": self.return_hint,
            "forwarded_to_replacement": self.forwarded_to_replacement,
            "non_target_reason": self.non_target_reason,
            "product_list_requested": self.product_list_requested,
            "automated_reply_type": (
                self.automated_reply_type.value if self.automated_reply_type else None
            ),
            "automated_transport_signal": self.automated_transport_signal,
            "classifier_source": self.classifier_source,
            "classifier_model": self.classifier_model,
            "classifier_request_hash": self.classifier_request_hash,
            "classifier_request_id": self.classifier_request_id,
            "evidence": list(self.evidence),
            "classification_error": self.classification_error,
            "normalization_notes": list(self.normalization_notes),
            "continue_business_processing": self.continue_business_processing,
        }


class _CommonDispositionArgs(TypedDict):
    authored_text: str
    replacement_emails: tuple[str, ...]
    return_hint: str | None
    forwarded_to_replacement: bool
    product_list_requested: bool
    automated_reply_type: AutomatedReplyType | None
    automated_transport_signal: bool


def _non_target_reason(text: str) -> str | None:
    for reason, pattern in NON_TARGET_ROLE_PATTERNS:
        if pattern.search(text):
            return reason
    return None


def classify_inbound_disposition(
    *,
    subject: str,
    body: str,
    headers: dict[str, str] | None = None,
    sender: str | None = None,
) -> InboundDisposition:
    """Classify an inbound message into a bounded operational disposition.

    The result describes the proposed action; it does not authorize database
    mutation or sending.  Callers must independently resolve the sender and the
    referenced outbound thread before applying any recommendation.
    """

    authored = latest_authored_text(body)
    automated = classify_automated_reply(
        subject=subject,
        body=authored,
        headers=headers,
        sender=sender,
    )
    transport_signal = any(
        marker.startswith("header:") or marker == "subject:auto-reply"
        for marker in automated.detected_by
    )
    product_list_requested = explicit_product_list_requested(
        f"{subject}\n{authored}"
    )

    common: _CommonDispositionArgs = {
        "authored_text": authored,
        "replacement_emails": automated.replacement_emails,
        "return_hint": automated.return_hint,
        "forwarded_to_replacement": automated.forwarded_to_replacement,
        "product_list_requested": product_list_requested,
        "automated_reply_type": automated.reply_type,
        "automated_transport_signal": transport_signal,
    }

    if automated.reply_type is AutomatedReplyType.SYSTEM_NOTIFICATION:
        return InboundDisposition(
            InboundDispositionType.SYSTEM_NOTIFICATION,
            1.0,
            "trusted infrastructure notification",
            **common,
        )
    if automated.reply_type is AutomatedReplyType.DEPARTED:
        return InboundDisposition(
            InboundDispositionType.DEPARTED,
            automated.confidence,
            "sender or referenced employee is no longer with the company",
            **common,
        )
    if automated.reply_type is AutomatedReplyType.OUT_OF_OFFICE:
        return InboundDisposition(
            InboundDispositionType.TEMPORARY_ABSENCE,
            automated.confidence,
            "temporary absence or office closure",
            **common,
        )

    if any(pattern.search(authored) for pattern in CONTACT_IDENTITY_MISMATCH_PATTERNS):
        return InboundDisposition(
            InboundDispositionType.CONTACT_IDENTITY_MISMATCH,
            0.99,
            "sender explicitly rejects the named contact or recipient identity",
            **common,
        )

    non_target_reason = _non_target_reason(authored)
    if non_target_reason:
        return InboundDisposition(
            InboundDispositionType.NON_TARGET,
            0.99,
            "sender explicitly identifies as a non-customer service provider",
            non_target_reason=non_target_reason,
            **common,
        )

    forwarded = automated.forwarded_to_replacement or any(
        pattern.search(authored) for pattern in FORWARDED_TO_COLLEAGUE_PATTERNS
    )
    if forwarded:
        return InboundDisposition(
            InboundDispositionType.FORWARDED_TO_COLLEAGUE,
            0.97,
            "message says the inquiry has already been forwarded",
            **common,
        )
    if automated.reply_type is AutomatedReplyType.CONTACT_CHANGE:
        return InboundDisposition(
            InboundDispositionType.CONTACT_REFERRAL,
            automated.confidence,
            "message explicitly directs correspondence to another contact",
            **common,
        )
    if automated.reply_type is AutomatedReplyType.GENERIC_AUTOREPLY:
        return InboundDisposition(
            InboundDispositionType.AUTOMATED_ACKNOWLEDGEMENT,
            automated.confidence,
            "generic automated acknowledgement",
            **common,
        )
    return InboundDisposition(
        InboundDispositionType.BUSINESS,
        0.80 if product_list_requested else 0.50,
        "business message or no safe lifecycle disposition",
        **common,
    )
