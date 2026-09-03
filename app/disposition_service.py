import hashlib
import html
import json
import re
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime import finalize_handoff_agent_run
from app.ai import (
    AIClient,
    InboundDispositionDecision,
    explicit_product_list_requested,
    inbound_disposition_message_params,
)
from app.auto_replies import AutomatedReplyType
from app.db import (
    AgentRun,
    AgentRunStatus,
    AgentStep,
    AgentStepStatus,
    AssistanceRequest,
    AssistanceStatus,
    AuditEvent,
    CaseStage,
    CaseStatus,
    Contact,
    ContactReferral,
    Customer,
    DeliveryStatus,
    EmailMessage,
    Handoff,
    InboundDispositionAction,
    Job,
    JobStatus,
    Outbox,
    Quote,
    ReactivationRecipient,
    SalesCase,
)
from app.deliverability import validate_address_format
from app.domain import HandoffReason
from app.email_identity import reply_contact_name
from app.imports import load_content
from app.inbound_disposition import (
    InboundDisposition,
    InboundDispositionType,
    classify_inbound_disposition,
)
from app.mail import build_message, normalized_subject, parse_mime
from app.quoted_reply_resolution import resolve_quoted_outbound_parent
from app.settings import Settings, get_settings

FREE_MAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "yahoo.com",
        "yahoo.co.in",
        "qq.com",
        "163.com",
        "126.com",
    }
)

MONTH_FORMATS = (
    "%B %d %Y",
    "%b %d %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d",
    "%b %d",
    "%d %B",
    "%d %b",
)
DISPOSITION_PROVENANCE_KEYS = frozenset(
    {
        "verified_reactivation_parent",
        "original_contact_id",
        "reply_contact_id",
        "sender_changed",
        "personnel_observation_recorded",
        "reviewed_parent_source",
        "reviewed_parent_email_id",
        "reviewed_parent_message_id",
        "customer_id",
    }
)
RECIPIENT_HEADER_REFERRAL_CUES = (
    re.compile(
        r"\b(?:i|we)\s+(?:have\s+)?(?:marked|kept|put)\s+"
        r"(?:a\s+)?(?:copy|cc)\s+to\b",
        re.I,
    ),
    re.compile(
        r"\b(?:i|we)\s+(?:have\s+)?(?:copied|cc(?:'d|ed)|included)\s+"
        r"[\w .,'’()/-]{0,100}\s+(?:in|on)\s+(?:this|the)\s+"
        r"(?:email|message|communication|thread)\b",
        re.I,
    ),
)


def _headers(row: EmailMessage) -> dict[str, str]:
    raw = (row.automated_reply_metadata or {}).get("headers") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def rule_classify_email_disposition(
    row: EmailMessage,
    *,
    settings: Settings | None = None,
) -> InboundDisposition:
    settings = settings or get_settings()
    return classify_inbound_disposition(
        subject=row.subject,
        body=row.body_text,
        headers=_headers(row),
        sender=row.from_address,
        internal_domains=settings.inbound_disposition_internal_domains,
    )


def _ai_reply_type(disposition_type: InboundDispositionType) -> AutomatedReplyType | None:
    return {
        InboundDispositionType.TEMPORARY_ABSENCE: AutomatedReplyType.OUT_OF_OFFICE,
        InboundDispositionType.DEPARTED: AutomatedReplyType.DEPARTED,
        InboundDispositionType.CONTACT_REFERRAL: AutomatedReplyType.CONTACT_CHANGE,
        InboundDispositionType.FORWARDED_TO_COLLEAGUE: AutomatedReplyType.CONTACT_CHANGE,
        InboundDispositionType.AUTOMATED_ACKNOWLEDGEMENT: AutomatedReplyType.GENERIC_AUTOREPLY,
        InboundDispositionType.SYSTEM_NOTIFICATION: AutomatedReplyType.SYSTEM_NOTIFICATION,
    }.get(disposition_type)


def decision_to_disposition(
    row: EmailMessage,
    *,
    rule: InboundDisposition,
    decision: InboundDispositionDecision,
    metadata: dict[str, Any],
) -> InboundDisposition:
    disposition_type = InboundDispositionType(decision.disposition_type)
    authored_casefold = rule.authored_text.casefold()
    sender = row.from_address.strip().casefold()
    header_replacements = _recipient_header_referral_candidates(
        row,
        authored_text=rule.authored_text,
    )
    replacement_emails: list[str] = []
    for raw_address in [
        *decision.replacement_emails,
        *rule.replacement_emails,
        *header_replacements,
    ]:
        validation = validate_address_format(raw_address)
        address = validation.normalized if validation.valid else None
        if (
            address
            and address != sender
            and (
                address.casefold() in authored_casefold
                or address in header_replacements
            )
            and address not in replacement_emails
        ):
            replacement_emails.append(address)
    return_hint = (decision.return_hint or "").strip() or None
    if return_hint and return_hint.casefold() not in authored_casefold:
        return_hint = None
    return_hint = return_hint or rule.return_hint
    evidence_source = f"{row.subject}\n{rule.authored_text}".casefold()
    evidence = tuple(
        snippet.strip()[:500]
        for snippet in decision.evidence
        if snippet.strip() and snippet.strip().casefold() in evidence_source
    )
    reason = decision.reason.strip()[:500]
    confidence = decision.confidence
    non_target_reason: str | None = decision.non_target_reason
    normalization_notes: list[str] = list(rule.normalization_notes)
    product_list_requested = bool(
        decision.product_list_requested or rule.product_list_requested
    )
    normalization_notes.extend(
        f"REPLACEMENT_FROM_RECIPIENT_HEADER:{address}"
        for address in header_replacements
        if address in replacement_emails
    )

    # These deterministic signals are deliberately narrow and operationally
    # safer than sampling a second semantic label. They also define the primary
    # category when one email contains both an absence and a referral.
    guarded_rule_types = {
        InboundDispositionType.DEPARTED,
        InboundDispositionType.TEMPORARY_ABSENCE,
        InboundDispositionType.FORWARDED_TO_COLLEAGUE,
        InboundDispositionType.CONTACT_IDENTITY_MISMATCH,
        InboundDispositionType.NON_TARGET,
    }
    authoritative_rule = (
        rule.disposition_type in guarded_rule_types
        or "INTERNAL_SENDER_REQUIRES_REVIEW" in rule.normalization_notes
        or "EXPLICIT_BUSINESS_REQUEST" in rule.normalization_notes
    )
    if authoritative_rule:
        if disposition_type is not rule.disposition_type:
            normalization_notes.append(
                f"PRIMARY_CATEGORY_NORMALIZED:{disposition_type.value}"
                f"->{rule.disposition_type.value}"
            )
        disposition_type = rule.disposition_type
        reason = rule.reason
        confidence = rule.confidence
        non_target_reason = rule.non_target_reason

    # A referral without an address cannot create or contact a replacement.
    # Keep it visible as uncertain instead of hiding it as ordinary business.
    if (
        disposition_type is InboundDispositionType.CONTACT_REFERRAL
        and not replacement_emails
    ):
        disposition_type = InboundDispositionType.UNCERTAIN
        reason = "AI proposed a contact referral without a valid replacement address"
        normalization_notes.append("CONTACT_REFERRAL_WITHOUT_VALID_EMAIL")

    # Customer-wide suppression is too destructive when only the model inferred
    # a role. A strong authored-text rule must corroborate NON_TARGET.
    if (
        disposition_type is InboundDispositionType.NON_TARGET
        and rule.disposition_type is not InboundDispositionType.NON_TARGET
    ):
        if product_list_requested:
            disposition_type = InboundDispositionType.BUSINESS
            reason = (
                "Explicit product-list request continues the business workflow; "
                "the AI-only non-target label was not corroborated"
            )
            confidence = rule.confidence
            normalization_notes.append(
                "UNVERIFIED_NON_TARGET_WITH_PRODUCT_REQUEST->BUSINESS"
            )
        else:
            disposition_type = InboundDispositionType.UNCERTAIN
            reason = (
                "AI non-target classification lacks a corroborating explicit role signal"
            )
            normalization_notes.append("NON_TARGET_NOT_RULE_CORROBORATED")
        non_target_reason = None

    if (
        disposition_type is InboundDispositionType.CONTACT_IDENTITY_MISMATCH
        and rule.disposition_type
        is not InboundDispositionType.CONTACT_IDENTITY_MISMATCH
    ):
        disposition_type = InboundDispositionType.UNCERTAIN
        reason = "AI identity-mismatch classification lacks an explicit authored-text signal"
        normalization_notes.append("IDENTITY_MISMATCH_NOT_RULE_CORROBORATED")

    if disposition_type not in {
        InboundDispositionType.TEMPORARY_ABSENCE,
        InboundDispositionType.DEPARTED,
        InboundDispositionType.CONTACT_REFERRAL,
        InboundDispositionType.FORWARDED_TO_COLLEAGUE,
    } and replacement_emails:
        replacement_emails = []
        normalization_notes.append("NON_ACTIONABLE_REPLACEMENT_EMAILS_DROPPED")

    replacement_emails.sort()

    return InboundDisposition(
        disposition_type=disposition_type,
        confidence=confidence,
        reason=reason,
        authored_text=rule.authored_text,
        replacement_emails=tuple(replacement_emails),
        return_hint=return_hint,
        forwarded_to_replacement=(
            decision.forwarded_to_replacement or rule.forwarded_to_replacement
        ),
        non_target_reason=non_target_reason,
        product_list_requested=product_list_requested,
        automated_reply_type=_ai_reply_type(disposition_type),
        # Transport authenticity is never delegated to the model.
        automated_transport_signal=rule.automated_transport_signal,
        classifier_source="anthropic",
        classifier_model=str(metadata.get("model") or "") or None,
        classifier_request_hash=(
            str(metadata.get("request_hash") or "") or None
        ),
        classifier_request_id=(
            str(metadata.get("request_id") or "") or None
        ),
        evidence=evidence,
        normalization_notes=tuple(normalization_notes),
    )


async def classify_email_disposition(
    row: EmailMessage,
    *,
    settings: Settings | None = None,
    ai_client: AIClient | None = None,
) -> InboundDisposition:
    """Use AI for semantics while retaining deterministic safety signals."""

    settings = settings or get_settings()
    rule = rule_classify_email_disposition(row, settings=settings)
    # Trusted machine-notification rules are deterministic and do not justify
    # the cost or latency of a model call. Bounces are excluded by the caller.
    if rule.disposition_type is InboundDispositionType.SYSTEM_NOTIFICATION:
        return rule
    if not settings.inbound_disposition_ai_enabled:
        return rule
    stored = _reusable_stored_ai_disposition(row, settings)
    if stored is not None:
        return stored
    try:
        client = ai_client or AIClient(settings)
        decision, metadata = await client.classify_inbound_disposition(
            subject=row.subject,
            body=row.body_text,
            sender=row.from_address,
            headers=_headers(row),
        )
    except Exception as exc:  # fail closed; the plan exposes a blocker
        return replace(
            rule,
            classifier_source="deterministic_fallback",
            classification_error=type(exc).__name__,
        )
    if decision is None:
        return replace(rule, classifier_source="deterministic_stub")
    return decision_to_disposition(
        row,
        rule=rule,
        decision=decision,
        metadata=metadata,
    )


def disposition_to_payload(disposition: InboundDisposition) -> dict[str, Any]:
    return {
        "disposition_type": disposition.disposition_type.value,
        "confidence": disposition.confidence,
        **disposition.metadata(),
    }


def disposition_from_payload(
    row: EmailMessage,
    payload: dict[str, Any],
) -> InboundDisposition:
    rule = rule_classify_email_disposition(row)
    raw_type = payload.get("disposition_type")
    if not raw_type:
        return rule
    try:
        disposition_type = InboundDispositionType(str(raw_type))
    except ValueError:
        return rule
    automated_type_value = payload.get("automated_reply_type")
    try:
        automated_type = (
            AutomatedReplyType(str(automated_type_value))
            if automated_type_value
            else _ai_reply_type(disposition_type)
        )
    except ValueError:
        automated_type = _ai_reply_type(disposition_type)
    raw_confidence = payload.get("confidence")
    confidence = (
        float(raw_confidence) if raw_confidence is not None else rule.confidence
    )
    return replace(
        rule,
        disposition_type=disposition_type,
        confidence=confidence,
        reason=str(payload.get("reason") or rule.reason),
        replacement_emails=tuple(payload.get("replacement_emails") or ()),
        return_hint=payload.get("return_hint"),
        forwarded_to_replacement=bool(payload.get("forwarded_to_replacement")),
        non_target_reason=payload.get("non_target_reason"),
        product_list_requested=bool(payload.get("product_list_requested")),
        automated_reply_type=automated_type,
        automated_transport_signal=bool(
            payload.get("automated_transport_signal")
        ),
        classifier_source=str(
            payload.get("classifier_source") or rule.classifier_source
        ),
        classifier_model=payload.get("classifier_model"),
        classifier_request_hash=payload.get("classifier_request_hash"),
        classifier_request_id=payload.get("classifier_request_id"),
        evidence=tuple(payload.get("evidence") or ()),
        classification_error=payload.get("classification_error"),
        normalization_notes=tuple(payload.get("normalization_notes") or ()),
    )


def _stored_or_rule_disposition(row: EmailMessage) -> InboundDisposition:
    return disposition_from_payload(
        row,
        {
            "disposition_type": row.disposition_type,
            "confidence": (
                float(row.disposition_confidence)
                if row.disposition_confidence is not None
                else None
            ),
            **(row.disposition_metadata or {}),
        },
    )


def _reusable_stored_ai_disposition(
    row: EmailMessage,
    settings: Settings,
) -> InboundDisposition | None:
    metadata = row.disposition_metadata or {}
    if (
        metadata.get("classifier_source") != "anthropic"
        or metadata.get("classification_error")
        or metadata.get("classifier_model") != settings.anthropic_model
    ):
        return None
    _, expected_hash = inbound_disposition_message_params(
        settings=settings,
        subject=row.subject,
        body=row.body_text,
        sender=row.from_address,
        headers=_headers(row),
    )
    if metadata.get("classifier_request_hash") != expected_hash:
        return None
    return _stored_or_rule_disposition(row)


async def _ensure_classification_review_handoff(
    session: AsyncSession,
    *,
    row: EmailMessage,
    disposition: InboundDisposition,
) -> Handoff:
    """Fail closed for a semantic result that cannot enter business automation."""

    handoff = await session.scalar(
        select(Handoff).where(Handoff.source_email_id == row.id)
    )
    created = handoff is None
    if handoff is None:
        handoff = Handoff(
            case_id=row.case_id,
            source_email_id=row.id,
            reason_code=HandoffReason.HUMAN_CONTROL.value,
            summary=(
                "Review contact identity mismatch before further correspondence"
                if disposition.disposition_type
                is InboundDispositionType.CONTACT_IDENTITY_MISMATCH
                else "Review uncertain inbound disposition before further correspondence"
            ),
            extracted_facts={},
        )
        session.add(handoff)
        await session.flush()
    handoff.extracted_facts = {
        **(handoff.extracted_facts or {}),
        "inbound_disposition": {
            "type": disposition.disposition_type.value,
            "confidence": disposition.confidence,
            **disposition.metadata(),
        },
    }
    if handoff.status == "OPEN":
        job_key = f"handoff-notify:{handoff.id}"
        existing_job = await session.scalar(
            select(Job.id).where(Job.idempotency_key == job_key)
        )
        if existing_job is None:
            session.add(
                Job(
                    kind="notify_handoff",
                    payload={"handoff_id": handoff.id},
                    idempotency_key=job_key,
                    status=JobStatus.PENDING,
                )
            )
    if created:
        session.add(
            AuditEvent(
                case_id=row.case_id,
                actor="inbound_disposition",
                event_type="inbound.classification_review_required",
                data={
                    "email_id": row.id,
                    "disposition_type": disposition.disposition_type.value,
                    **disposition.metadata(),
                },
            )
        )
    return handoff


def _domain(address: str | None) -> str | None:
    value = (address or "").strip().casefold()
    if "@" not in value:
        return None
    domain = value.rsplit("@", 1)[1].strip(". ")
    return domain or None


def _same_company_domain(first: str | None, second: str | None) -> bool:
    first_domain = _domain(first)
    second_domain = _domain(second)
    return bool(
        first_domain
        and first_domain == second_domain
        and first_domain not in FREE_MAIL_DOMAINS
    )


def _recipient_header_referral_candidates(
    row: EmailMessage,
    *,
    authored_text: str,
) -> tuple[str, ...]:
    """Use a copied same-domain recipient only when the new body says it did so."""

    if not any(
        pattern.search(authored_text)
        for pattern in RECIPIENT_HEADER_REFERRAL_CUES
    ):
        return ()
    sender = row.from_address.strip().casefold()
    candidates: list[str] = []
    for raw_address in row.to_addresses or []:
        validation = validate_address_format(raw_address)
        address = validation.normalized if validation.valid else None
        if (
            address
            and address != sender
            and _same_company_domain(sender, address)
            and address not in candidates
        ):
            candidates.append(address)
    return tuple(candidates)


def _parse_return_until(
    return_hint: str | None,
    *,
    received_at: datetime,
) -> datetime | None:
    if not return_hint:
        return None
    cleaned = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b", "", return_hint, flags=re.I)
    cleaned = re.sub(r"\bof\b", " ", cleaned, flags=re.I)
    range_parts = re.split(
        r"\s+(?:to|until|till|through)\s+|\s+[\-–—]\s+",
        cleaned,
        flags=re.I,
    )
    if len(range_parts) > 1:
        cleaned = range_parts[-1]
    cleaned = cleaned.replace(",", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-")
    parsed: datetime | None = None
    try:
        parsed = parsedate_to_datetime(cleaned)
    except (TypeError, ValueError, OverflowError):
        pass
    if parsed is None:
        for format_string in MONTH_FORMATS:
            try:
                if "%Y" in format_string:
                    parsed = datetime.strptime(cleaned, format_string)
                else:
                    parsed = datetime.strptime(
                        f"{cleaned} {received_at.year}",
                        f"{format_string} %Y",
                    )
            except ValueError:
                continue
            if "%Y" not in format_string:
                if parsed.date() < received_at.date() - timedelta(days=7):
                    parsed = parsed.replace(year=received_at.year + 1)
            break
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=received_at.tzinfo or UTC)
    # Resume after the stated return/closure day, never during it.
    return datetime.combine(parsed.date() + timedelta(days=1), time.min, parsed.tzinfo).astimezone(UTC)


async def _unique_sender_contact(
    session: AsyncSession,
    row: EmailMessage,
) -> Contact | None:
    if row.contact_id is not None:
        return await session.get(Contact, row.contact_id)
    sender = row.from_address.strip().casefold()
    contacts = (
        (
            await session.execute(
                select(Contact).where(func.lower(Contact.email) == sender).limit(2)
            )
        )
        .scalars()
        .all()
    )
    return contacts[0] if len(contacts) == 1 else None


async def _disposition_contact(
    session: AsyncSession,
    row: EmailMessage,
    disposition: InboundDisposition,
    sender_contact: Contact | None,
) -> Contact | None:
    """Resolve the endpoint affected by a verified personnel message."""

    metadata = row.disposition_metadata or {}
    if (
        disposition.disposition_type is not InboundDispositionType.DEPARTED
        or not (
            (
                metadata.get("verified_reactivation_parent")
                and metadata.get("sender_changed")
            )
            or metadata.get("reviewed_parent_source")
        )
    ):
        return sender_contact
    raw_original_contact_id = metadata.get("original_contact_id")
    if not isinstance(raw_original_contact_id, (int, str)) or isinstance(
        raw_original_contact_id, bool
    ):
        return sender_contact
    try:
        original_contact_id = int(raw_original_contact_id)
    except (TypeError, ValueError):
        return sender_contact
    original = await session.get(Contact, original_contact_id)
    expected_customer_id = row.customer_id or (
        sender_contact.customer_id if sender_contact is not None else None
    )
    if (
        original is None
        or expected_customer_id is None
        or original.customer_id != expected_customer_id
    ):
        return sender_contact
    return original


async def _resolved_customer(
    session: AsyncSession,
    row: EmailMessage,
    contact: Contact | None,
) -> Customer | None:
    customer_id = row.customer_id or (contact.customer_id if contact else None)
    return await session.get(Customer, customer_id) if customer_id else None


@dataclass(frozen=True)
class _ParentResources:
    contact: Contact
    customer: Customer
    source: str
    parent_email_id: int | None = None
    parent_message_id: str | None = None


@dataclass(frozen=True)
class _ResolvedDispositionResources:
    sender_contact: Contact | None
    contact: Contact | None
    customer: Customer | None
    parent: _ParentResources | None = None


async def _exact_reactivation_parent_resources(
    session: AsyncSession,
    row: EmailMessage,
) -> _ParentResources | None:
    """Resolve a unique sent reactivation parent without trusting sender identity."""

    if not row.in_reply_to:
        return None
    matches = (
        await session.execute(
            select(Outbox, Contact, Customer)
            .select_from(Outbox)
            .join(
                ReactivationRecipient,
                ReactivationRecipient.outbox_id == Outbox.id,
            )
            .join(Contact, Contact.id == ReactivationRecipient.contact_id)
            .join(Customer, Customer.id == ReactivationRecipient.customer_id)
            .where(
                Outbox.message_id == row.in_reply_to,
                Outbox.message_kind == "REACTIVATION",
                Outbox.status == DeliveryStatus.SENT,
                Outbox.sent_at.is_not(None),
                Outbox.sent_at <= row.received_at,
                ReactivationRecipient.status.in_(["QUEUED", "SENT", "REPLIED"]),
                ReactivationRecipient.customer_id == Contact.customer_id,
                func.lower(Outbox.recipient) == func.lower(Contact.email),
            )
            .limit(2)
        )
    ).all()
    if len(matches) != 1:
        return None
    outbox, contact, customer = matches[0]
    return _ParentResources(
        contact=contact,
        customer=customer,
        source="EXACT_REACTIVATION_PARENT",
        parent_message_id=outbox.message_id,
    )


async def _quoted_outbound_parent_resources(
    session: AsyncSession,
    row: EmailMessage,
) -> _ParentResources | None:
    quoted = await resolve_quoted_outbound_parent(
        session,
        row,
        excluded_domains=FREE_MAIL_DOMAINS,
    )
    if quoted is None:
        return None
    contact = await session.get(Contact, quoted.contact_id)
    customer = await session.get(Customer, quoted.customer_id)
    if contact is None or customer is None or contact.customer_id != customer.id:
        return None
    return _ParentResources(
        contact=contact,
        customer=customer,
        source="QUOTED_OUTBOUND_PARENT",
        parent_email_id=quoted.email_id,
        parent_message_id=quoted.message_id,
    )


async def _resolved_disposition_resources(
    session: AsyncSession,
    row: EmailMessage,
    disposition: InboundDisposition,
) -> _ResolvedDispositionResources:
    sender_contact = await _unique_sender_contact(session, row)
    contact = await _disposition_contact(session, row, disposition, sender_contact)
    customer = await _resolved_customer(session, row, contact)
    parent: _ParentResources | None = None
    if (
        customer is None
        and disposition.disposition_type
        in {
            InboundDispositionType.DEPARTED,
            InboundDispositionType.CONTACT_REFERRAL,
            InboundDispositionType.FORWARDED_TO_COLLEAGUE,
        }
    ):
        parent = await _exact_reactivation_parent_resources(session, row)
        if parent is None:
            parent = await _quoted_outbound_parent_resources(session, row)
        if parent is not None:
            contact, customer = parent.contact, parent.customer
    return _ResolvedDispositionResources(
        sender_contact=sender_contact,
        contact=contact,
        customer=customer,
        parent=parent,
    )


async def build_disposition_plan(
    session: AsyncSession,
    row: EmailMessage,
    *,
    settings: Settings | None = None,
    disposition: InboundDisposition | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    disposition = disposition or await classify_email_disposition(
        row,
        settings=settings,
    )
    resources = await _resolved_disposition_resources(session, row, disposition)
    sender_contact = resources.sender_contact
    contact = resources.contact
    customer = resources.customer
    parent = resources.parent
    return_until = _parse_return_until(
        disposition.return_hint,
        received_at=row.received_at,
    )
    observed_at = at or datetime.now(UTC)
    sender_differs_from_contact = bool(
        contact is not None
        and row.from_address.strip().casefold() != contact.email.strip().casefold()
    )
    same_domain_reply_candidate = bool(
        parent is not None
        and sender_differs_from_contact
        and _same_company_domain(row.from_address, contact.email if contact else None)
    )
    absence_already_ended = bool(
        return_until is not None and return_until <= observed_at
    )
    proposed_actions: list[str] = []
    blockers: list[str] = []
    mutating_types = {
        InboundDispositionType.TEMPORARY_ABSENCE,
        InboundDispositionType.DEPARTED,
        InboundDispositionType.CONTACT_REFERRAL,
        InboundDispositionType.FORWARDED_TO_COLLEAGUE,
        InboundDispositionType.NON_TARGET,
    }
    if disposition.disposition_type in mutating_types:
        if disposition.classifier_source == "deterministic_fallback":
            blockers.append("AI_CLASSIFICATION_UNAVAILABLE")
        if (
            disposition.classifier_source == "anthropic"
            and disposition.confidence
            < settings.inbound_disposition_ai_min_confidence
        ):
            blockers.append("AI_CONFIDENCE_BELOW_THRESHOLD")
        if (
            disposition.classifier_source == "anthropic"
            and not disposition.evidence
        ):
            blockers.append("AI_EVIDENCE_MISSING")

    if disposition.disposition_type is InboundDispositionType.TEMPORARY_ABSENCE:
        proposed_actions.extend(
            ["IGNORE_AUTOREPLY", "RECORD_EXPIRED_ABSENCE"]
            if absence_already_ended
            else ["IGNORE_AUTOREPLY", "PAUSE_CONTACT"]
        )
        if disposition.replacement_emails:
            proposed_actions.append("SAVE_REFERRALS")
        if contact is None and not absence_already_ended:
            blockers.append("SENDER_CONTACT_NOT_UNIQUE")
        if return_until is None:
            blockers.append("RETURN_DATE_NOT_RELIABLE")
    elif disposition.disposition_type is InboundDispositionType.DEPARTED:
        proposed_actions.extend(["SUPPRESS_DEPARTED_CONTACT", "SAVE_REFERRALS"])
        verified_original_contact = bool(
            contact is not None
            and (
                disposition.automated_transport_signal
                or parent is not None
                or (
                    sender_contact is not None
                    and contact.id != sender_contact.id
                    and (row.disposition_metadata or {}).get(
                        "verified_reactivation_parent"
                    )
                )
            )
        )
        if not verified_original_contact:
            blockers.append("ORIGINAL_CONTACT_NOT_VERIFIED")
        verified_reply_contact = bool(
            verified_original_contact
            and contact is not None
            and (
                (
                    sender_contact is not None
                    and sender_contact.id != contact.id
                    and sender_contact.customer_id == contact.customer_id
                )
                or same_domain_reply_candidate
            )
        )
        if verified_reply_contact:
            proposed_actions.append("KEEP_VERIFIED_REPLY_CONTACT")
            if sender_contact is None:
                proposed_actions.append("CREATE_REPLY_CONTACT")
            if disposition.continue_business_processing:
                proposed_actions.append("CONTINUE_BUSINESS_PIPELINE")
        if not disposition.replacement_emails and not verified_reply_contact:
            blockers.append("NO_REPLACEMENT_CONTACT")
        if parent is not None and parent.source == "QUOTED_OUTBOUND_PARENT":
            blockers.append("QUOTED_PARENT_REQUIRES_REVIEW")
        elif parent is not None:
            blockers.append("PARENT_RESOLVED_DEPARTURE_REQUIRES_REVIEW")
        if parent is not None and sender_differs_from_contact and not same_domain_reply_candidate:
            blockers.append("CROSS_DOMAIN_DEPARTURE_REQUIRES_CONTACT_SELECTION")
    elif disposition.disposition_type in {
        InboundDispositionType.CONTACT_REFERRAL,
        InboundDispositionType.FORWARDED_TO_COLLEAGUE,
    }:
        proposed_actions.append("SAVE_REFERRALS")
        if disposition.forwarded_to_replacement:
            proposed_actions.append("WAIT_FOR_FORWARDED_REPLY")
        else:
            proposed_actions.append("REVIEW_NEW_CONTACT_OUTREACH")
        if customer is None:
            blockers.append("CUSTOMER_NOT_RESOLVED")
        if not disposition.replacement_emails:
            blockers.append("NO_REPLACEMENT_CONTACT")
        if (
            parent is not None
            and contact is not None
            and _domain(row.from_address) != _domain(contact.email)
        ):
            blockers.append("CROSS_DOMAIN_REACTIVATION_PARENT_REQUIRES_REVIEW")
    elif disposition.disposition_type is InboundDispositionType.NON_TARGET:
        proposed_actions.extend(["MARK_CUSTOMER_NON_TARGET", "STOP_REACTIVATION"])
        if customer is None:
            blockers.append("CUSTOMER_NOT_RESOLVED")
    elif (
        disposition.disposition_type
        is InboundDispositionType.CONTACT_IDENTITY_MISMATCH
    ):
        proposed_actions.append("REVIEW_CONTACT_IDENTITY")
        blockers.append("CONTACT_IDENTITY_REQUIRES_REVIEW")
    elif disposition.disposition_type is InboundDispositionType.UNCERTAIN:
        proposed_actions.append("REVIEW_CLASSIFICATION")
        blockers.append("AI_CLASSIFICATION_UNCERTAIN")
    elif disposition.disposition_type in {
        InboundDispositionType.AUTOMATED_ACKNOWLEDGEMENT,
        InboundDispositionType.SYSTEM_NOTIFICATION,
    }:
        proposed_actions.append("IGNORE_AUTOREPLY")
    else:
        proposed_actions.append("CONTINUE_BUSINESS_PIPELINE")

    referral_candidates = []
    for address in disposition.replacement_emails:
        same_domain = _same_company_domain(
            row.from_address,
            address,
        )
        referral_candidates.append(
            {
                "email": address,
                "same_company_domain": same_domain,
                "auto_contact_eligible": bool(
                    same_domain
                    and len(disposition.replacement_emails) == 1
                    and not disposition.forwarded_to_replacement
                ),
            }
        )
    if disposition.replacement_emails and not any(
        candidate["same_company_domain"] for candidate in referral_candidates
    ):
        blockers.append("NO_SAME_DOMAIN_REFERRAL")
    if len(disposition.replacement_emails) > 1:
        blockers.append("MULTIPLE_REFERRALS_REQUIRE_REVIEW")

    application_blockers: list[str] = []
    if disposition.disposition_type is InboundDispositionType.TEMPORARY_ABSENCE:
        application_blockers.extend(
            blocker
            for blocker in ("SENDER_CONTACT_NOT_UNIQUE",)
            if blocker in blockers
        )
    elif disposition.disposition_type is InboundDispositionType.DEPARTED:
        application_blockers.extend(
            blocker
            for blocker in (
                "ORIGINAL_CONTACT_NOT_VERIFIED",
                "CROSS_DOMAIN_DEPARTURE_REQUIRES_CONTACT_SELECTION",
            )
            if blocker in blockers
        )
    elif disposition.disposition_type in {
        InboundDispositionType.CONTACT_REFERRAL,
        InboundDispositionType.FORWARDED_TO_COLLEAGUE,
    }:
        application_blockers.extend(
            blocker
            for blocker in ("CUSTOMER_NOT_RESOLVED", "NO_REPLACEMENT_CONTACT")
            if blocker in blockers
        )
    elif disposition.disposition_type is InboundDispositionType.NON_TARGET:
        if "CUSTOMER_NOT_RESOLVED" in blockers:
            application_blockers.append("CUSTOMER_NOT_RESOLVED")

    latest_action = await session.scalar(
        select(InboundDispositionAction)
        .where(InboundDispositionAction.source_email_id == row.id)
        .order_by(InboundDispositionAction.id.desc())
        .limit(1)
    )
    action_summary = (
        {
            "id": latest_action.id,
            "status": latest_action.status,
            "disposition_type": latest_action.disposition_type,
            "applied_by": latest_action.applied_by,
            "applied_at": _iso(latest_action.applied_at),
            "rolled_back_by": latest_action.rolled_back_by,
            "rolled_back_at": _iso(latest_action.rolled_back_at),
            "rollback_reason": latest_action.rollback_reason,
            "applied_actions": (
                (
                    (latest_action.after_json.get("email") or {}).get(
                        "disposition_metadata"
                    )
                    or {}
                ).get("applied_actions", [])
            ),
            "outboxes": latest_action.after_json.get("outboxes", []),
        }
        if latest_action is not None
        else None
    )

    plan = {
        "email_id": row.id,
        "received_at": row.received_at.isoformat(),
        "from_address": row.from_address,
        "subject": row.subject,
        "case_id": row.case_id,
        "customer_id": customer.id if customer else None,
        "customer_name": customer.company_name if customer else None,
        "customer_qualification_status": (
            customer.qualification_status if customer else None
        ),
        "contact_id": contact.id if contact else None,
        "contact_name": contact.name if contact else None,
        "contact_email": contact.email if contact else None,
        "contact_lifecycle_status": contact.lifecycle_status if contact else None,
        "contact_suppressed": contact.suppressed if contact else None,
        "sender_contact_id": sender_contact.id if sender_contact else None,
        "sender_contact_email": sender_contact.email if sender_contact else None,
        "sender_contact_lifecycle_status": (
            sender_contact.lifecycle_status if sender_contact else None
        ),
        "sender_contact_suppressed": (
            sender_contact.suppressed if sender_contact else None
        ),
        "contact_resolution_source": (
            parent.source if parent is not None else "DIRECT_IDENTITY"
        ),
        "reactivation_parent_message_id": (
            parent.parent_message_id if parent is not None else None
        ),
        "parent_email_id": parent.parent_email_id if parent is not None else None,
        "reply_contact_candidate_email": (
            row.from_address.strip().casefold()
            if same_domain_reply_candidate and sender_contact is None
            else None
        ),
        "reply_contact_candidate_name": (
            reply_contact_name(None, disposition.authored_text)
            if same_domain_reply_candidate and sender_contact is None
            else None
        ),
        "disposition_handled_at": _iso(row.disposition_handled_at),
        "disposition_type": disposition.disposition_type.value,
        "confidence": disposition.confidence,
        "classifier_source": disposition.classifier_source,
        "classifier_model": disposition.classifier_model,
        "classifier_request_hash": disposition.classifier_request_hash,
        "classifier_request_id": disposition.classifier_request_id,
        "evidence": list(disposition.evidence),
        "classification_error": disposition.classification_error,
        "normalization_notes": list(disposition.normalization_notes),
        "reason": disposition.reason,
        "return_hint": disposition.return_hint,
        "unavailable_until": return_until.isoformat() if return_until else None,
        "absence_already_ended": absence_already_ended,
        "replacement_emails": list(disposition.replacement_emails),
        "referral_candidates": referral_candidates,
        "forwarded_to_replacement": disposition.forwarded_to_replacement,
        "non_target_reason": disposition.non_target_reason,
        "product_list_requested": disposition.product_list_requested,
        "continue_business_processing": disposition.continue_business_processing,
        "proposed_actions": proposed_actions,
        "blockers": list(dict.fromkeys(blockers)),
        "can_apply": not application_blockers,
        "application_blockers": list(dict.fromkeys(application_blockers)),
        "body_preview": re.sub(r"\s+", " ", disposition.authored_text)[:500],
        "latest_action": action_summary,
    }
    fingerprint_payload = {
        key: value for key, value in plan.items() if key != "latest_action"
    }
    plan["plan_token"] = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return plan


async def _save_referrals(
    session: AsyncSession,
    *,
    row: EmailMessage,
    disposition: InboundDisposition,
    contact: Contact | None,
    customer: Customer | None,
) -> list[ContactReferral]:
    saved: list[ContactReferral] = []
    for address in disposition.replacement_emails:
        source_location = (
            "recipient_header"
            if f"REPLACEMENT_FROM_RECIPIENT_HEADER:{address}"
            in disposition.normalization_notes
            else "authored_body"
        )
        existing = await session.scalar(
            select(ContactReferral).where(
                ContactReferral.source_email_id == row.id,
                func.lower(ContactReferral.referred_email) == address.casefold(),
            )
        )
        if existing is not None:
            saved.append(existing)
            continue
        referral = ContactReferral(
                source_email_id=row.id,
                customer_id=customer.id if customer else None,
                original_contact_id=contact.id if contact else None,
                referred_email=address,
                relationship_type=(
                    "TEMPORARY_BACKUP"
                    if disposition.disposition_type
                    is InboundDispositionType.TEMPORARY_ABSENCE
                    else "REPLACEMENT"
                ),
                status=(
                    "WAITING_FOR_FORWARDED_REPLY"
                    if disposition.forwarded_to_replacement
                    else "CANDIDATE"
                ),
                forwarded_already=disposition.forwarded_to_replacement,
                confidence=Decimal(str(disposition.confidence)),
                metadata_json={
                    "same_company_domain": _same_company_domain(
                        row.from_address,
                        address,
                    ),
                    "source_disposition": disposition.disposition_type.value,
                    "source_location": source_location,
                },
            )
        session.add(referral)
        saved.append(referral)
    await session.flush()
    return saved


async def _save_verified_reply_contact_referral(
    session: AsyncSession,
    *,
    row: EmailMessage,
    disposition: InboundDisposition,
    original_contact: Contact,
    reply_contact: Contact,
    customer: Customer,
) -> ContactReferral:
    existing = await session.scalar(
        select(ContactReferral).where(
            ContactReferral.source_email_id == row.id,
            func.lower(ContactReferral.referred_email)
            == reply_contact.email.strip().casefold(),
        )
    )
    if existing is not None:
        existing.customer_id = customer.id
        existing.original_contact_id = original_contact.id
        existing.new_contact_id = reply_contact.id
        existing.referred_name = reply_contact.name
        existing.status = "ACTIVE_CONTACT"
        existing.metadata_json = {
            **(existing.metadata_json or {}),
            "verified_reactivation_parent": True,
            "reply_contact_already_engaged": True,
        }
        return existing
    referral = ContactReferral(
        source_email_id=row.id,
        customer_id=customer.id,
        original_contact_id=original_contact.id,
        new_contact_id=reply_contact.id,
        referred_email=reply_contact.email.strip().casefold(),
        referred_name=reply_contact.name,
        relationship_type="REPLACEMENT",
        status="ACTIVE_CONTACT",
        forwarded_already=False,
        confidence=Decimal(str(disposition.confidence)),
        metadata_json={
            "same_company_domain": _same_company_domain(
                original_contact.email,
                reply_contact.email,
            ),
            "source_disposition": disposition.disposition_type.value,
            "verified_reactivation_parent": True,
            "reply_contact_already_engaged": True,
        },
    )
    session.add(referral)
    await session.flush()
    return referral


async def _ensure_reviewed_reply_contact(
    session: AsyncSession,
    *,
    row: EmailMessage,
    disposition: InboundDisposition,
    customer: Customer,
    original_contact: Contact,
    existing_sender_contact: Contact | None,
) -> tuple[Contact | None, bool]:
    """Create a same-company reply endpoint only after human confirmation."""

    sender = row.from_address.strip().casefold()
    if not sender or not _same_company_domain(sender, original_contact.email):
        return None, False
    if existing_sender_contact is not None:
        return (
            (existing_sender_contact, False)
            if existing_sender_contact.customer_id == customer.id
            else (None, False)
        )
    matches = (
        (
            await session.execute(
                select(Contact).where(func.lower(Contact.email) == sender).limit(2)
            )
        )
        .scalars()
        .all()
    )
    if matches:
        return (
            (matches[0], False)
            if len(matches) == 1 and matches[0].customer_id == customer.id
            else (None, False)
        )
    contact = Contact(
        customer_id=customer.id,
        name=reply_contact_name(None, disposition.authored_text),
        email=sender,
        language=original_contact.language,
        metadata_json={
            "source": "inbound_contact_referral",
            "source_email_id": row.id,
            "relationship": "verified_same_domain_reply",
        },
    )
    session.add(contact)
    await session.flush()
    return contact, True


async def _continue_reviewed_business_reply(
    session: AsyncSession,
    *,
    row: EmailMessage,
    disposition: InboundDisposition,
    customer: Customer,
    reply_contact: Contact,
) -> tuple[SalesCase | None, bool]:
    """Attach a reviewed mixed-intent reply to an open, draft-only case."""

    if not disposition.continue_business_processing:
        return None, False
    subject_key = normalized_subject(row.subject)[:255]
    candidates = (
        (
            await session.execute(
                select(SalesCase)
                .where(
                    SalesCase.contact_id == reply_contact.id,
                    SalesCase.subject_key == subject_key,
                    SalesCase.status.not_in(
                        [CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST]
                    ),
                )
                .order_by(SalesCase.id.desc())
                .limit(2)
            )
        )
        .scalars()
        .all()
    )
    if len(candidates) > 1:
        return None, False
    created = not candidates
    sales_case = candidates[0] if candidates else SalesCase(
        customer_id=customer.id,
        contact_id=reply_contact.id,
        product_id=None,
        currency="USD",
        stage=CaseStage.FOLLOW_UP,
        status=CaseStatus.WAITING_HUMAN,
        subject_key=subject_key,
        last_activity_at=row.received_at,
    )
    if created:
        session.add(sales_case)
        await session.flush()
    row.case_id = sales_case.id
    row.customer_id = customer.id
    row.contact_id = reply_contact.id
    handoff = await session.scalar(
        select(Handoff).where(Handoff.source_email_id == row.id)
    )
    if handoff is not None:
        handoff.case_id = sales_case.id
        if disposition.product_list_requested and explicit_product_list_requested(
            f"{row.subject}\n{row.body_text}"
        ):
            handoff.reason_code = HandoffReason.PRODUCT_LIST_REVIEW.value
            handoff.summary = (
                "Product-list request recovered from a reviewed personnel reply"
            )
        handoff.extracted_facts = {
            **(handoff.extracted_facts or {}),
            "inbound_disposition_continuation": {
                "source_email_id": row.id,
                "customer_id": customer.id,
                "contact_id": reply_contact.id,
                "case_id": sales_case.id,
                "product_list_requested": disposition.product_list_requested,
            },
        }
        run = await session.scalar(
            select(AgentRun).where(AgentRun.handoff_id == handoff.id)
        )
        if run is not None:
            run.case_id = sales_case.id
    return sales_case, created


async def _stage_referral_outreach(
    session: AsyncSession,
    *,
    row: EmailMessage,
    disposition: InboundDisposition,
    source_contact: Contact,
    customer: Customer,
    referrals: list[ContactReferral],
    settings: Settings,
) -> Outbox | None:
    """Queue one bounded referral introduction behind three independent gates."""

    if (
        not settings.referral_auto_contact_enabled
        or disposition.forwarded_to_replacement
        or len(referrals) != 1
        or customer.do_not_contact
        or customer.qualification_status == "NON_TARGET"
        or not customer.auto_send_allowed
    ):
        return None
    referral = referrals[0]
    target_email = referral.referred_email.strip().casefold()
    if (
        not _same_company_domain(source_contact.email, target_email)
        or not validate_address_format(target_email).valid
    ):
        return None
    contacts = (
        (
            await session.execute(
                select(Contact).where(func.lower(Contact.email) == target_email).limit(2)
            )
        )
        .scalars()
        .all()
    )
    if any(item.customer_id != customer.id for item in contacts):
        return None
    target = contacts[0] if contacts else None
    if target is None:
        target = Contact(
            customer_id=customer.id,
            name="Customer",
            email=target_email,
            language=source_contact.language,
            metadata_json={
                "source": "inbound_contact_referral",
                "source_email_id": row.id,
                "original_contact_id": source_contact.id,
            },
        )
        session.add(target)
        await session.flush()
    if target.suppressed or target.lifecycle_status == "DEPARTED":
        return None

    business_key = f"referral-outreach:{row.id}:{target_email}"
    existing = await session.scalar(
        select(Outbox).where(Outbox.business_key == business_key)
    )
    if existing is not None:
        referral.new_contact_id = target.id
        referral.status = "OUTREACH_QUEUED"
        return existing

    bundle = load_content(settings.content_dir)
    subject = "Lanya Chem product contact"
    business_text = (
        "Dear Sir/Madam,\n\n"
        "The previous contact at your company directed future correspondence to "
        "this address. This is Shreya from Lanya Chem.\n\n"
        "Could you please confirm whether you are the appropriate contact for "
        "product sourcing? If so, we can share our current product list and follow "
        "up on any products you require."
    )
    text_body = "\n".join([business_text, "", bundle.signature_text.strip()])
    html_body = (
        "<p>"
        + "</p><p>".join(
            html.escape(line) if line else "&nbsp;"
            for line in business_text.splitlines()
        )
        + "</p>"
        + bundle.signature_html
    )
    message_id, raw = build_message(
        from_address=settings.mail_from,
        recipient=target_email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        stable_key=business_key,
    )
    parsed = parse_mime(raw.encode("utf-8"))
    now = datetime.now(UTC)
    outbox = Outbox(
        case_id=None,
        quote_id=None,
        message_kind="REFERRAL_OUTREACH",
        business_key=business_key,
        message_id=message_id,
        recipient=target_email,
        raw_message=raw,
        status=DeliveryStatus.PENDING,
        available_at=now,
    )
    session.add(outbox)
    session.add(
        EmailMessage(
            case_id=None,
            customer_id=customer.id,
            contact_id=target.id,
            direction="OUTBOUND",
            message_id=message_id,
            from_address=parseaddr(settings.mail_from)[1],
            to_addresses=[target_email],
            subject=subject,
            body_text=text_body,
            body_html=html_body,
            attachment_metadata=[],
            raw_sha256=parsed.raw_sha256,
            is_history=False,
            received_at=now,
        )
    )
    referral.new_contact_id = target.id
    referral.status = "OUTREACH_QUEUED"
    await session.flush()
    return outbox


async def _resolve_terminal_handoff(
    session: AsyncSession,
    *,
    row: EmailMessage,
    disposition: InboundDisposition,
    actor: str,
) -> None:
    handoff = await session.scalar(
        select(Handoff).where(Handoff.source_email_id == row.id).with_for_update()
    )
    if handoff is None or handoff.status != "OPEN":
        return
    handoff.status = "RESOLVED"
    handoff.resolution_note = (
        f"Automatically resolved by inbound disposition: "
        f"{disposition.disposition_type.value}"
    )
    if handoff.dingtalk_status != "SENT":
        handoff.dingtalk_status = "CANCELLED"
    await finalize_handoff_agent_run(
        session,
        handoff_id=handoff.id,
        actor=actor,
        outcome=f"disposition-{disposition.disposition_type.value.casefold()}",
    )
    notify_job = await session.scalar(
        select(Job).where(Job.idempotency_key == f"handoff-notify:{handoff.id}")
    )
    if notify_job is not None and notify_job.status in {
        JobStatus.PENDING,
        JobStatus.FAILED,
    }:
        notify_job.status = JobStatus.DONE
        notify_job.last_error = "Cancelled: inbound disposition resolved the handoff"
        notify_job.locked_at = None
        notify_job.locked_by = None


async def _sync_open_handoff_facts(
    session: AsyncSession,
    *,
    row: EmailMessage,
    disposition: InboundDisposition,
) -> None:
    handoff = await session.scalar(
        select(Handoff).where(Handoff.source_email_id == row.id).with_for_update()
    )
    if handoff is None or handoff.status != "OPEN":
        return
    handoff.extracted_facts = {
        **(handoff.extracted_facts or {}),
        "inbound_disposition": {
            "type": disposition.disposition_type.value,
            "confidence": disposition.confidence,
            **disposition.metadata(),
        },
    }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.isoformat()


def _decimal_text(value: Decimal | None) -> str | None:
    return format(value.normalize(), "f") if value is not None else None


async def _lock_disposition_related_resources(
    session: AsyncSession,
    row: EmailMessage,
) -> None:
    await session.execute(
        select(ContactReferral)
        .where(ContactReferral.source_email_id == row.id)
        .with_for_update()
    )
    await session.execute(
        select(Outbox)
        .where(Outbox.business_key.like(f"referral-outreach:{row.id}:%"))
        .with_for_update()
    )
    handoff = await session.scalar(
        select(Handoff)
        .where(Handoff.source_email_id == row.id)
        .with_for_update()
    )
    if handoff is None:
        return
    run = await session.scalar(
        select(AgentRun).where(AgentRun.handoff_id == handoff.id).with_for_update()
    )
    await session.execute(
        select(Job)
        .where(Job.idempotency_key == f"handoff-notify:{handoff.id}")
        .with_for_update()
    )
    if run is None:
        return
    await session.execute(
        select(AssistanceRequest)
        .where(AssistanceRequest.run_id == run.id)
        .with_for_update()
    )
    await session.execute(
        select(AgentStep).where(AgentStep.run_id == run.id).with_for_update()
    )


async def _disposition_state_snapshot(
    session: AsyncSession,
    row: EmailMessage,
    *,
    disposition: InboundDisposition | None = None,
) -> dict[str, Any]:
    disposition = disposition or _stored_or_rule_disposition(row)
    resources = await _resolved_disposition_resources(session, row, disposition)
    contact = resources.contact
    customer = resources.customer
    referrals = (
        (
            await session.execute(
                select(ContactReferral)
                .where(ContactReferral.source_email_id == row.id)
                .order_by(ContactReferral.id)
            )
        )
        .scalars()
        .all()
    )
    outboxes = (
        (
            await session.execute(
                select(Outbox)
                .where(Outbox.business_key.like(f"referral-outreach:{row.id}:%"))
                .order_by(Outbox.id)
            )
        )
        .scalars()
        .all()
    )
    handoff = await session.scalar(
        select(Handoff).where(Handoff.source_email_id == row.id)
    )
    case_id = row.case_id or (handoff.case_id if handoff is not None else None)
    sales_case = await session.get(SalesCase, case_id) if case_id is not None else None
    run = (
        await session.scalar(select(AgentRun).where(AgentRun.handoff_id == handoff.id))
        if handoff is not None
        else None
    )
    notify_job = (
        await session.scalar(
            select(Job).where(Job.idempotency_key == f"handoff-notify:{handoff.id}")
        )
        if handoff is not None
        else None
    )
    assistance_requests = (
        (
            (
                await session.execute(
                    select(AssistanceRequest)
                    .where(AssistanceRequest.run_id == run.id)
                    .order_by(AssistanceRequest.id)
                )
            )
            .scalars()
            .all()
        )
        if run is not None
        else []
    )
    agent_steps = (
        (
            (
                await session.execute(
                    select(AgentStep)
                    .where(AgentStep.run_id == run.id)
                    .order_by(AgentStep.id)
                )
            )
            .scalars()
            .all()
        )
        if run is not None
        else []
    )
    target_contact_ids = sorted(
        {
            referral.new_contact_id
            for referral in referrals
            if referral.new_contact_id is not None
        }
    )
    target_contacts = []
    for contact_id in target_contact_ids:
        target = await session.get(Contact, contact_id)
        if target is not None:
            target_contacts.append(
                {
                    "id": target.id,
                    "customer_id": target.customer_id,
                    "name": target.name,
                    "email": target.email,
                    "suppressed": target.suppressed,
                    "lifecycle_status": target.lifecycle_status,
                    "unavailable_until": _iso(target.unavailable_until),
                    "metadata_json": target.metadata_json or {},
                }
            )
    return {
        "email": {
            "id": row.id,
            "case_id": row.case_id,
            "customer_id": row.customer_id,
            "contact_id": row.contact_id,
            "disposition_type": row.disposition_type,
            "disposition_confidence": (
                _decimal_text(row.disposition_confidence)
            ),
            "disposition_metadata": row.disposition_metadata or {},
            "disposition_handled_at": _iso(row.disposition_handled_at),
            "automated_reply_handled_at": _iso(row.automated_reply_handled_at),
        },
        "contact": (
            {
                "id": contact.id,
                "suppressed": contact.suppressed,
                "lifecycle_status": contact.lifecycle_status,
                "unavailable_until": _iso(contact.unavailable_until),
            }
            if contact is not None
            else None
        ),
        "customer": (
            {
                "id": customer.id,
                "qualification_status": customer.qualification_status,
                "qualification_reason": customer.qualification_reason,
                "qualified_at": _iso(customer.qualified_at),
            }
            if customer is not None
            else None
        ),
        "case": (
            {
                "id": sales_case.id,
                "customer_id": sales_case.customer_id,
                "contact_id": sales_case.contact_id,
                "product_id": sales_case.product_id,
                "category_id": sales_case.category_id,
                "currency": sales_case.currency,
                "stage": sales_case.stage.value,
                "status": sales_case.status.value,
                "subject_key": sales_case.subject_key,
                "negotiation_round": sales_case.negotiation_round,
                "last_activity_at": _iso(sales_case.last_activity_at),
            }
            if sales_case is not None
            else None
        ),
        "referrals": [
            {
                "id": referral.id,
                "customer_id": referral.customer_id,
                "original_contact_id": referral.original_contact_id,
                "new_contact_id": referral.new_contact_id,
                "referred_email": referral.referred_email,
                "referred_name": referral.referred_name,
                "relationship_type": referral.relationship_type,
                "status": referral.status,
                "forwarded_already": referral.forwarded_already,
                "confidence": _decimal_text(referral.confidence),
                "metadata_json": referral.metadata_json or {},
            }
            for referral in referrals
        ],
        "target_contacts": target_contacts,
        "outboxes": [
            {
                "id": outbox.id,
                "status": outbox.status.value,
                "recipient": outbox.recipient,
                "message_kind": outbox.message_kind,
                "message_id": outbox.message_id,
                "last_error": outbox.last_error,
                "sent_at": _iso(outbox.sent_at),
            }
            for outbox in outboxes
        ],
        "handoff": (
            {
                "id": handoff.id,
                "case_id": handoff.case_id,
                "reason_code": handoff.reason_code,
                "summary": handoff.summary,
                "status": handoff.status,
                "dingtalk_status": handoff.dingtalk_status,
                "resolution_note": handoff.resolution_note,
                "extracted_facts": handoff.extracted_facts or {},
            }
            if handoff is not None
            else None
        ),
        "agent_run": (
            {
                "id": run.id,
                "case_id": run.case_id,
                "status": run.status.value,
                "current_step": run.current_step,
                "last_error": run.last_error,
                "completed_at": _iso(run.completed_at),
            }
            if run is not None
            else None
        ),
        "assistance_requests": [
            {
                "id": request.id,
                "status": request.status.value,
            }
            for request in assistance_requests
        ],
        "agent_steps": [
            {
                "id": step.id,
                "status": step.status.value,
                "completed_at": _iso(step.completed_at),
            }
            for step in agent_steps
        ],
        "notify_job": (
            {
                "id": notify_job.id,
                "status": notify_job.status.value,
                "last_error": notify_job.last_error,
                "locked_at": _iso(notify_job.locked_at),
                "locked_by": notify_job.locked_by,
            }
            if notify_job is not None
            else None
        ),
    }


async def apply_email_disposition(
    session: AsyncSession,
    row: EmailMessage,
    *,
    settings: Settings | None = None,
    allow_referral_outreach: bool = True,
    actor: str = "inbound_disposition",
    force_manual: bool = False,
    disposition: InboundDisposition | None = None,
    at: datetime | None = None,
) -> bool:
    """Record/apply one disposition; return True when no business work remains."""

    settings = settings or get_settings()
    observed_at = at or datetime.now(UTC)
    if not settings.inbound_disposition_enabled or row.is_bounce:
        return False
    disposition = disposition or await classify_email_disposition(
        row,
        settings=settings,
    )
    previous_metadata = dict(row.disposition_metadata or {})
    classification_metadata = {
        **disposition.metadata(),
        **{
            key: value
            for key, value in previous_metadata.items()
            if key.startswith("applied_") or key in DISPOSITION_PROVENANCE_KEYS
        },
    }
    if disposition.disposition_type in {
        InboundDispositionType.CONTACT_IDENTITY_MISMATCH,
        InboundDispositionType.UNCERTAIN,
    }:
        row.disposition_type = disposition.disposition_type.value
        row.disposition_confidence = Decimal(str(disposition.confidence))
        row.disposition_metadata = classification_metadata
        await _ensure_classification_review_handoff(
            session,
            row=row,
            disposition=disposition,
        )
        return True
    if not settings.inbound_disposition_apply_enabled and not force_manual:
        row.disposition_type = disposition.disposition_type.value
        row.disposition_confidence = Decimal(str(disposition.confidence))
        row.disposition_metadata = classification_metadata
        return False
    locked_row = await session.scalar(
        select(EmailMessage).where(EmailMessage.id == row.id).with_for_update()
    )
    if locked_row is None:
        return False
    row = locked_row
    # The email body is immutable; retain the reviewed model decision after
    # acquiring locks instead of paying for or trusting a second model sample.
    previous_metadata = dict(row.disposition_metadata or {})
    classification_metadata = {
        **disposition.metadata(),
        **{
            key: value
            for key, value in previous_metadata.items()
            if key.startswith("applied_") or key in DISPOSITION_PROVENANCE_KEYS
        },
    }
    if row.disposition_handled_at is not None:
        return bool(previous_metadata.get("applied_terminal"))
    active_action = await session.scalar(
        select(InboundDispositionAction).where(
            InboundDispositionAction.source_email_id == row.id,
            InboundDispositionAction.status == "APPLIED",
        ).with_for_update()
    )
    if active_action is not None:
        action_email = active_action.after_json.get("email") or {}
        action_metadata = action_email.get("disposition_metadata") or {}
        return bool(action_metadata.get("applied_terminal"))
    resources = await _resolved_disposition_resources(session, row, disposition)
    sender_contact = resources.sender_contact
    contact = resources.contact
    customer = resources.customer
    if customer is not None:
        await session.scalar(
            select(Customer).where(Customer.id == customer.id).with_for_update()
        )
    contact_ids = sorted(
        {
            item.id
            for item in (contact, sender_contact)
            if item is not None
        }
    )
    if contact_ids:
        await session.execute(
            select(Contact).where(Contact.id.in_(contact_ids)).with_for_update()
        )
    resources = await _resolved_disposition_resources(session, row, disposition)
    sender_contact = resources.sender_contact
    contact = resources.contact
    customer = resources.customer
    parent = resources.parent
    if not force_manual:
        automatic_plan = await build_disposition_plan(
            session,
            row,
            settings=settings,
            disposition=disposition,
            at=observed_at,
        )
        if automatic_plan["blockers"]:
            row.disposition_type = disposition.disposition_type.value
            row.disposition_confidence = Decimal(str(disposition.confidence))
            row.disposition_metadata = classification_metadata
            return False
    await _lock_disposition_related_resources(session, row)
    before_snapshot = await _disposition_state_snapshot(
        session,
        row,
        disposition=disposition,
    )
    if parent is not None:
        classification_metadata = {
            **classification_metadata,
            "reviewed_parent_source": parent.source,
            "reviewed_parent_email_id": parent.parent_email_id,
            "reviewed_parent_message_id": parent.parent_message_id,
            "original_contact_id": contact.id if contact is not None else None,
            "customer_id": customer.id if customer is not None else None,
        }
    row.disposition_type = disposition.disposition_type.value
    row.disposition_confidence = Decimal(str(disposition.confidence))
    row.disposition_metadata = classification_metadata

    terminal = False
    applied_actions: list[str] = []

    if disposition.disposition_type in {
        InboundDispositionType.AUTOMATED_ACKNOWLEDGEMENT,
        InboundDispositionType.SYSTEM_NOTIFICATION,
    }:
        terminal = True
        applied_actions.append("IGNORE_AUTOREPLY")
    elif disposition.disposition_type is InboundDispositionType.TEMPORARY_ABSENCE:
        return_until = _parse_return_until(
            disposition.return_hint,
            received_at=row.received_at,
        )
        absence_already_ended = bool(
            return_until is not None and return_until <= observed_at
        )
        if contact is None and not absence_already_ended:
            return False
        if not absence_already_ended:
            assert contact is not None
            contact.lifecycle_status = "TEMPORARILY_UNAVAILABLE"
            contact.unavailable_until = return_until
        referrals = await _save_referrals(
            session,
            row=row,
            disposition=disposition,
            contact=contact,
            customer=customer,
        )
        terminal = True
        applied_actions.extend(
            ["IGNORE_AUTOREPLY", "RECORD_EXPIRED_ABSENCE"]
            if absence_already_ended
            else ["IGNORE_AUTOREPLY", "PAUSE_CONTACT"]
        )
        if referrals:
            applied_actions.append("SAVE_REFERRALS")
    elif disposition.disposition_type is InboundDispositionType.DEPARTED:
        # A human reply may mention somebody else leaving.  Suppress the sender
        # only when the transport itself proves this is that endpoint's auto-reply.
        verified_original_contact = bool(
            contact is not None
            and (
                disposition.automated_transport_signal
                or parent is not None
                or (
                    sender_contact is not None
                    and contact.id != sender_contact.id
                    and (row.disposition_metadata or {}).get(
                        "verified_reactivation_parent"
                    )
                )
            )
        )
        if not verified_original_contact:
            return False
        assert contact is not None
        reply_contact_created = False
        if (
            parent is not None
            and row.from_address.strip().casefold()
            != contact.email.strip().casefold()
        ):
            if customer is None:
                return False
            sender_contact, reply_contact_created = (
                await _ensure_reviewed_reply_contact(
                    session,
                    row=row,
                    disposition=disposition,
                    customer=customer,
                    original_contact=contact,
                    existing_sender_contact=sender_contact,
                )
            )
            if sender_contact is None:
                return False
        contact.lifecycle_status = "DEPARTED"
        contact.suppressed = True
        contact.unavailable_until = None
        referrals = await _save_referrals(
            session,
            row=row,
            disposition=disposition,
            contact=contact,
            customer=customer,
        )
        verified_reply_contact = bool(
            customer is not None
            and sender_contact is not None
            and contact.id != sender_contact.id
        )
        if verified_reply_contact:
            assert customer is not None
            assert sender_contact is not None
            verified_referral = await _save_verified_reply_contact_referral(
                session,
                row=row,
                disposition=disposition,
                original_contact=contact,
                reply_contact=sender_contact,
                customer=customer,
            )
            referrals.append(verified_referral)
            applied_actions.append("KEEP_VERIFIED_REPLY_CONTACT")
            if reply_contact_created:
                applied_actions.append("CREATE_REPLY_CONTACT")
            if disposition.continue_business_processing:
                sales_case, case_created = await _continue_reviewed_business_reply(
                    session,
                    row=row,
                    disposition=disposition,
                    customer=customer,
                    reply_contact=sender_contact,
                )
                if sales_case is None:
                    return False
                applied_actions.append("CONTINUE_BUSINESS_PIPELINE")
                if case_created:
                    applied_actions.append("CREATE_REVIEW_CASE")
        outreach = None
        if (
            customer is not None
            and allow_referral_outreach
            and not verified_reply_contact
        ):
            outreach = await _stage_referral_outreach(
                session,
                row=row,
                disposition=disposition,
                source_contact=contact,
                customer=customer,
                referrals=referrals,
                settings=settings,
            )
            if outreach is not None:
                applied_actions.append("QUEUE_REFERRAL_OUTREACH")
        terminal = bool(
            not disposition.continue_business_processing
            and (
                disposition.forwarded_to_replacement
                or outreach is not None
                or verified_reply_contact
            )
        )
        applied_actions.extend(["SUPPRESS_DEPARTED_CONTACT", "SAVE_REFERRALS"])
    elif disposition.disposition_type is InboundDispositionType.NON_TARGET:
        if customer is None:
            return False
        customer.qualification_status = "NON_TARGET"
        customer.qualification_reason = disposition.non_target_reason
        customer.qualified_at = datetime.now(UTC)
        terminal = True
        applied_actions.extend(["MARK_CUSTOMER_NON_TARGET", "STOP_REACTIVATION"])
    elif disposition.disposition_type in {
        InboundDispositionType.CONTACT_REFERRAL,
        InboundDispositionType.FORWARDED_TO_COLLEAGUE,
    }:
        if customer is None or not disposition.replacement_emails:
            return False
        referrals = await _save_referrals(
            session,
            row=row,
            disposition=disposition,
            contact=contact,
            customer=customer,
        )
        outreach = None
        if contact is not None and allow_referral_outreach:
            outreach = await _stage_referral_outreach(
                session,
                row=row,
                disposition=disposition,
                source_contact=contact,
                customer=customer,
                referrals=referrals,
                settings=settings,
            )
            if outreach is not None:
                applied_actions.append("QUEUE_REFERRAL_OUTREACH")
        terminal = disposition.forwarded_to_replacement or outreach is not None
        applied_actions.append("SAVE_REFERRALS")

    if not applied_actions:
        return False
    row.disposition_handled_at = datetime.now(UTC)
    row.disposition_metadata = {
        **row.disposition_metadata,
        "applied_actions": applied_actions,
        "applied_terminal": terminal,
    }
    await _sync_open_handoff_facts(
        session,
        row=row,
        disposition=disposition,
    )
    if terminal and row.is_automated_reply:
        row.automated_reply_handled_at = row.disposition_handled_at
    if terminal:
        await _resolve_terminal_handoff(
            session,
            row=row,
            disposition=disposition,
            actor=actor,
        )
    session.add(
        AuditEvent(
            case_id=row.case_id,
            actor=actor[:128],
            event_type="inbound.disposition_applied",
            data={
                "email_id": row.id,
                "disposition_type": disposition.disposition_type.value,
                "actions": applied_actions,
                **disposition.metadata(),
            },
        )
    )
    await session.flush()
    after_snapshot = await _disposition_state_snapshot(
        session,
        row,
        disposition=disposition,
    )
    session.add(
        InboundDispositionAction(
            source_email_id=row.id,
            disposition_type=disposition.disposition_type.value,
            status="APPLIED",
            applied_by=actor[:128],
            before_json=before_snapshot,
            after_json=after_snapshot,
        )
    )
    return terminal


def _parse_snapshot_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


async def rollback_email_disposition(
    session: AsyncSession,
    *,
    action_id: int,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """Restore one disposition action if no irreversible/later change exists."""

    action = await session.scalar(
        select(InboundDispositionAction)
        .where(InboundDispositionAction.id == action_id)
        .with_for_update()
    )
    if action is None:
        raise ValueError("disposition action was not found")
    if action.status != "APPLIED":
        raise ValueError("only an applied disposition action can be rolled back")
    row = await session.scalar(
        select(EmailMessage)
        .where(EmailMessage.id == action.source_email_id)
        .with_for_update()
    )
    if row is None:
        raise ValueError("source email no longer exists")

    before = action.before_json or {}
    after = action.after_json or {}

    def snapshot_matches(current_value: Any, expected_value: Any) -> bool:
        """Compare against the action schema while tolerating newer snapshot keys."""

        if isinstance(expected_value, dict):
            return isinstance(current_value, dict) and all(
                key in current_value
                and snapshot_matches(current_value[key], expected)
                for key, expected in expected_value.items()
            )
        if isinstance(expected_value, list):
            return isinstance(current_value, list) and len(current_value) == len(
                expected_value
            ) and all(
                snapshot_matches(current_item, expected_item)
                for current_item, expected_item in zip(
                    current_value,
                    expected_value,
                    strict=True,
                )
            )
        return current_value == expected_value

    def snapshot_ids(key: str) -> set[int]:
        ids: set[int] = set()
        for snapshot in (before.get(key), after.get(key)):
            if isinstance(snapshot, dict) and isinstance(snapshot.get("id"), int):
                ids.add(snapshot["id"])
        return ids

    def snapshot_list_ids(key: str) -> set[int]:
        return {
            item["id"]
            for snapshot in (before.get(key) or [], after.get(key) or [])
            for item in [snapshot]
            if isinstance(item, dict) and isinstance(item.get("id"), int)
        }

    customer_ids = snapshot_ids("customer")
    contact_ids = snapshot_ids("contact") | snapshot_list_ids("target_contacts")
    case_ids = snapshot_ids("case")
    referral_ids = snapshot_list_ids("referrals")
    outbox_ids = snapshot_list_ids("outboxes")
    handoff_ids = snapshot_ids("handoff")
    run_ids = snapshot_ids("agent_run")
    assistance_ids = snapshot_list_ids("assistance_requests")
    step_ids = snapshot_list_ids("agent_steps")
    job_ids = snapshot_ids("notify_job")
    for model, ids in (
        (Customer, customer_ids),
        (Contact, contact_ids),
        (SalesCase, case_ids),
        (ContactReferral, referral_ids),
        (Outbox, outbox_ids),
        (Handoff, handoff_ids),
        (AgentRun, run_ids),
        (AssistanceRequest, assistance_ids),
        (AgentStep, step_ids),
        (Job, job_ids),
    ):
        if ids:
            await session.execute(
                select(model).where(model.id.in_(sorted(ids))).with_for_update()
            )
    current = await _disposition_state_snapshot(session, row)
    conflicts: list[str] = []

    for key in (
        "email",
        "contact",
        "customer",
        "case",
        "referrals",
        "target_contacts",
        "assistance_requests",
        "agent_steps",
    ):
        if before.get(key) != after.get(key) and not snapshot_matches(
            current.get(key), after.get(key)
        ):
            conflicts.append(f"{key.upper()}_CHANGED_AFTER_APPLY")
    for key in ("handoff", "agent_run", "notify_job"):
        # A new handoff may legitimately be created after a non-terminal apply;
        # restore only resources that this action itself changed.
        if before.get(key) != after.get(key) and not snapshot_matches(
            current.get(key), after.get(key)
        ):
            conflicts.append(f"{key.upper()}_CHANGED_AFTER_APPLY")

    before_outbox_ids = {item["id"] for item in before.get("outboxes") or []}
    after_outboxes = {
        item["id"]: item for item in after.get("outboxes") or []
    }
    created_outbox_ids = sorted(set(after_outboxes) - before_outbox_ids)
    created_outbox_message_ids = {
        after_outboxes[outbox_id]["message_id"]
        for outbox_id in created_outbox_ids
        if after_outboxes[outbox_id].get("message_id")
    }
    for outbox_id in created_outbox_ids:
        outbox = await session.get(Outbox, outbox_id)
        if outbox is None:
            conflicts.append(f"OUTBOX_{outbox_id}_MISSING_AFTER_APPLY")
        elif outbox.status in {
            DeliveryStatus.CLAIMED,
            DeliveryStatus.SENT,
            DeliveryStatus.UNKNOWN,
        }:
            conflicts.append(f"OUTBOX_{outbox_id}_{outbox.status.value}_IRREVERSIBLE")

    before_targets = {item["id"]: item for item in before.get("target_contacts") or []}
    after_targets = {item["id"]: item for item in after.get("target_contacts") or []}
    created_target_ids = sorted(set(after_targets) - set(before_targets))
    action_metadata = ((after.get("email") or {}).get("disposition_metadata") or {})
    before_case = before.get("case") or {}
    before_case_id = before_case.get("id") if isinstance(before_case, dict) else None
    before_case_ids = {before_case_id} if isinstance(before_case_id, int) else set()
    created_case_ids = (
        sorted(case_ids - before_case_ids)
        if "CREATE_REVIEW_CASE" in (action_metadata.get("applied_actions") or [])
        else []
    )
    removed_contact_ids: list[int] = []
    for contact_id in created_target_ids:
        target = await session.get(Contact, contact_id)
        if target is None:
            continue
        metadata = target.metadata_json or {}
        created_by_action = bool(
            metadata.get("source") == "inbound_contact_referral"
            and metadata.get("source_email_id") == row.id
        )
        if not created_by_action:
            continue
        later_email_filters = [
            EmailMessage.contact_id == contact_id,
            EmailMessage.id != row.id,
        ]
        if created_outbox_message_ids:
            later_email_filters.append(
                ~(
                    (EmailMessage.direction == "OUTBOUND")
                    & EmailMessage.message_id.in_(created_outbox_message_ids)
                )
            )
        later_email_count = await session.scalar(
            select(func.count())
            .select_from(EmailMessage)
            .where(*later_email_filters)
        )
        case_conditions = [SalesCase.contact_id == contact_id]
        if created_case_ids:
            case_conditions.append(SalesCase.id.not_in(created_case_ids))
        case_count = await session.scalar(
            select(func.count()).select_from(SalesCase).where(*case_conditions)
        )
        if (later_email_count or 0) > 0 or (case_count or 0) > 0:
            conflicts.append(f"NEW_CONTACT_{contact_id}_HAS_LATER_ACTIVITY")

    for case_id in created_case_ids:
        related_email_count = await session.scalar(
            select(func.count())
            .select_from(EmailMessage)
            .where(EmailMessage.case_id == case_id, EmailMessage.id != row.id)
        )
        related_outbox_count = await session.scalar(
            select(func.count()).select_from(Outbox).where(Outbox.case_id == case_id)
        )
        related_quote_count = await session.scalar(
            select(func.count()).select_from(Quote).where(Quote.case_id == case_id)
        )
        if any(
            count or 0
            for count in (
                related_email_count,
                related_outbox_count,
                related_quote_count,
            )
        ):
            conflicts.append(f"NEW_CASE_{case_id}_HAS_LATER_ACTIVITY")

    if conflicts:
        raise ValueError("rollback blocked: " + ", ".join(conflicts))

    removed_outbound_email_ids: list[int] = []
    for outbox_id in created_outbox_ids:
        outbox = await session.get(Outbox, outbox_id)
        if outbox is not None:
            outbox.status = DeliveryStatus.CANCELLED
            outbox.last_error = f"Rolled back by {actor[:128]}: {reason}"[:2000]
    for message_id in sorted(created_outbox_message_ids):
        staged_email = await session.scalar(
            select(EmailMessage).where(
                EmailMessage.direction == "OUTBOUND",
                EmailMessage.message_id == message_id,
            )
        )
        if staged_email is not None:
            removed_outbound_email_ids.append(staged_email.id)
            await session.delete(staged_email)

    current_referrals = {
        referral.id: referral
        for referral in (
            (
                await session.execute(
                    select(ContactReferral).where(
                        ContactReferral.source_email_id == row.id
                    )
                )
            )
            .scalars()
            .all()
        )
    }
    before_referrals = {item["id"]: item for item in before.get("referrals") or []}
    for referral_id, referral in list(current_referrals.items()):
        snapshot = before_referrals.get(referral_id)
        if snapshot is None:
            await session.delete(referral)
            continue
        referral.customer_id = snapshot["customer_id"]
        referral.original_contact_id = snapshot["original_contact_id"]
        referral.new_contact_id = snapshot["new_contact_id"]
        referral.referred_email = snapshot["referred_email"]
        referral.referred_name = snapshot["referred_name"]
        referral.relationship_type = snapshot["relationship_type"]
        referral.status = snapshot["status"]
        referral.forwarded_already = snapshot["forwarded_already"]
        referral.confidence = Decimal(snapshot["confidence"])
        referral.metadata_json = snapshot["metadata_json"]
    await session.flush()

    email_snapshot = before["email"]
    if "case_id" in email_snapshot:
        row.case_id = email_snapshot["case_id"]
    if "customer_id" in email_snapshot:
        row.customer_id = email_snapshot["customer_id"]
    if "contact_id" in email_snapshot:
        row.contact_id = email_snapshot["contact_id"]
    handoff_snapshot = before.get("handoff")
    if handoff_snapshot is not None and before.get("handoff") != after.get("handoff"):
        handoff = await session.get(Handoff, handoff_snapshot["id"])
        if handoff is not None:
            if "case_id" in handoff_snapshot:
                handoff.case_id = handoff_snapshot["case_id"]
    run_snapshot = before.get("agent_run")
    if run_snapshot is not None and before.get("agent_run") != after.get("agent_run"):
        run = await session.get(AgentRun, run_snapshot["id"])
        if run is not None:
            if "case_id" in run_snapshot:
                run.case_id = run_snapshot["case_id"]
    await session.flush()

    for case_id in created_case_ids:
        sales_case = await session.get(SalesCase, case_id)
        if sales_case is not None:
            await session.delete(sales_case)
    await session.flush()

    for contact_id in created_target_ids:
        target = await session.get(Contact, contact_id)
        if target is None:
            continue
        metadata = target.metadata_json or {}
        if (
            metadata.get("source") == "inbound_contact_referral"
            and metadata.get("source_email_id") == row.id
        ):
            await session.delete(target)
            removed_contact_ids.append(contact_id)

    contact_snapshot = before.get("contact")
    if contact_snapshot is not None and before.get("contact") != after.get("contact"):
        contact = await session.get(Contact, contact_snapshot["id"])
        if contact is not None:
            contact.suppressed = contact_snapshot["suppressed"]
            contact.lifecycle_status = contact_snapshot["lifecycle_status"]
            contact.unavailable_until = _parse_snapshot_datetime(
                contact_snapshot["unavailable_until"]
            )
    customer_snapshot = before.get("customer")
    if customer_snapshot is not None and before.get("customer") != after.get("customer"):
        customer = await session.get(Customer, customer_snapshot["id"])
        if customer is not None:
            customer.qualification_status = customer_snapshot["qualification_status"]
            customer.qualification_reason = customer_snapshot["qualification_reason"]
            customer.qualified_at = _parse_snapshot_datetime(
                customer_snapshot["qualified_at"]
            )

    if handoff_snapshot is not None and before.get("handoff") != after.get("handoff"):
        handoff = await session.get(Handoff, handoff_snapshot["id"])
        if handoff is not None:
            if "case_id" in handoff_snapshot:
                handoff.case_id = handoff_snapshot["case_id"]
            handoff.reason_code = handoff_snapshot.get(
                "reason_code", handoff.reason_code
            )
            handoff.summary = handoff_snapshot.get("summary", handoff.summary)
            handoff.status = handoff_snapshot["status"]
            handoff.dingtalk_status = handoff_snapshot["dingtalk_status"]
            handoff.resolution_note = handoff_snapshot["resolution_note"]
            handoff.extracted_facts = handoff_snapshot["extracted_facts"]
    if run_snapshot is not None and before.get("agent_run") != after.get("agent_run"):
        run = await session.get(AgentRun, run_snapshot["id"])
        if run is not None:
            if "case_id" in run_snapshot:
                run.case_id = run_snapshot["case_id"]
            run.status = AgentRunStatus(run_snapshot["status"])
            run.current_step = run_snapshot["current_step"]
            run.last_error = run_snapshot["last_error"]
            run.completed_at = _parse_snapshot_datetime(run_snapshot["completed_at"])
    if before.get("assistance_requests") != after.get("assistance_requests"):
        for snapshot in before.get("assistance_requests") or []:
            request = await session.get(AssistanceRequest, snapshot["id"])
            if request is not None:
                request.status = AssistanceStatus(snapshot["status"])
    if before.get("agent_steps") != after.get("agent_steps"):
        for snapshot in before.get("agent_steps") or []:
            step = await session.get(AgentStep, snapshot["id"])
            if step is not None:
                step.status = AgentStepStatus(snapshot["status"])
                step.completed_at = _parse_snapshot_datetime(snapshot["completed_at"])
    job_snapshot = before.get("notify_job")
    if job_snapshot is not None and before.get("notify_job") != after.get("notify_job"):
        job = await session.get(Job, job_snapshot["id"])
        if job is not None:
            job.status = JobStatus(job_snapshot["status"])
            job.last_error = job_snapshot["last_error"]
            job.locked_at = _parse_snapshot_datetime(job_snapshot["locked_at"])
            job.locked_by = job_snapshot["locked_by"]

    row.disposition_type = email_snapshot["disposition_type"]
    row.disposition_confidence = (
        Decimal(email_snapshot["disposition_confidence"])
        if email_snapshot["disposition_confidence"] is not None
        else None
    )
    row.disposition_metadata = email_snapshot["disposition_metadata"]
    row.disposition_handled_at = _parse_snapshot_datetime(
        email_snapshot["disposition_handled_at"]
    )
    row.automated_reply_handled_at = _parse_snapshot_datetime(
        email_snapshot["automated_reply_handled_at"]
    )
    action.status = "ROLLED_BACK"
    action.rolled_back_by = actor[:128]
    action.rolled_back_at = datetime.now(UTC)
    action.rollback_reason = reason[:2000]
    session.add(
        AuditEvent(
            case_id=row.case_id,
            actor=actor[:128],
            event_type="inbound.disposition_rolled_back",
            data={
                "email_id": row.id,
                "action_id": action.id,
                "disposition_type": action.disposition_type,
                "reason": reason[:2000],
                "cancelled_outbox_ids": created_outbox_ids,
                "removed_outbound_email_ids": removed_outbound_email_ids,
                "removed_contact_ids": removed_contact_ids,
            },
        )
    )
    await session.commit()
    return {
        "action_id": action.id,
        "email_id": row.id,
        "status": action.status,
        "cancelled_outbox_ids": created_outbox_ids,
        "removed_outbound_email_ids": removed_outbound_email_ids,
        "removed_contact_ids": removed_contact_ids,
    }


async def backfill_inbound_dispositions(
    session: AsyncSession,
    *,
    apply: bool = False,
    limit: int = 1000,
    include_business: bool = False,
    include_synced_history: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    if apply and not settings.inbound_disposition_apply_enabled:
        raise ValueError(
            "INBOUND_DISPOSITION_APPLY_ENABLED must be true before applying a backfill"
        )
    filters = [
        EmailMessage.direction == "INBOUND",
        EmailMessage.is_bounce.is_(False),
    ]
    if not include_synced_history:
        filters.append(EmailMessage.is_history.is_(False))
    rows = (
        (
            await session.execute(
                select(EmailMessage)
                .where(*filters)
                .order_by(EmailMessage.received_at.desc(), EmailMessage.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    plans: list[dict[str, Any]] = []
    applied_count = 0
    for row in rows:
        disposition = None
        plan = await build_disposition_plan(
            session,
            row,
            settings=settings,
            disposition=disposition,
        )
        if (
            include_business
            or plan["disposition_type"] != InboundDispositionType.BUSINESS.value
        ):
            plans.append(plan)
        if apply:
            was_unhandled = row.disposition_handled_at is None
            await apply_email_disposition(
                session,
                row,
                settings=settings,
                allow_referral_outreach=False,
                disposition=disposition,
            )
            if was_unhandled and row.disposition_handled_at is not None:
                applied_count += 1
    if apply:
        await session.commit()
    counts = Counter(plan["disposition_type"] for plan in plans)
    return {
        "mode": "apply" if apply else "dry-run",
        "scanned_count": len(rows),
        "candidate_count": len(plans),
        "applied_count": applied_count,
        "counts": dict(sorted(counts.items())),
        "plans": plans,
    }
