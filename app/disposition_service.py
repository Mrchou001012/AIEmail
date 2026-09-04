from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import (
    AIClient,
    InboundDispositionDecision,
    inbound_disposition_message_params,
)
from app.auto_replies import AutomatedReplyType
from app.db import (
    AgentRun,
    AgentStep,
    AssistanceRequest,
    AuditEvent,
    Contact,
    ContactReferral,
    Customer,
    EmailMessage,
    Handoff,
    InboundDispositionAction,
    Job,
    JobStatus,
    Outbox,
    SalesCase,
)
from app.deliverability import validate_address_format
from app.disposition_actions import (
    _continue_reviewed_business_reply,
    _decimal_text,
    _ensure_reviewed_reply_contact,
    _iso,
    _lock_disposition_related_resources,
    _resolve_terminal_handoff,
    _save_referrals,
    _save_verified_reply_contact_referral,
    _stage_referral_outreach,
    _sync_open_handoff_facts,
)
from app.disposition_planning import build_disposition_plan_data
from app.disposition_resolution import (
    parse_return_until as _parse_return_until,
)
from app.disposition_resolution import (
    recipient_header_referral_candidates as _recipient_header_referral_candidates,
)
from app.disposition_resolution import (
    resolve_disposition_resources as _resolved_disposition_resources,
)
from app.domain import HandoffReason
from app.inbound_disposition import (
    InboundDisposition,
    InboundDispositionType,
    classify_inbound_disposition,
)
from app.settings import Settings, get_settings

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


async def build_disposition_plan(
    session: AsyncSession,
    row: EmailMessage,
    *,
    settings: Settings | None = None,
    disposition: InboundDisposition | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Classify when needed, then delegate deterministic plan construction."""

    settings = settings or get_settings()
    disposition = disposition or await classify_email_disposition(
        row,
        settings=settings,
    )
    return await build_disposition_plan_data(
        session,
        row,
        settings=settings,
        disposition=disposition,
        at=at,
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
    current_plan = await build_disposition_plan(
        session,
        row,
        settings=settings,
        disposition=disposition,
        at=observed_at,
    )
    if current_plan["application_blockers"] or (
        not force_manual and current_plan["blockers"]
    ):
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


async def rollback_email_disposition(
    session: AsyncSession,
    *,
    action_id: int,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """Delegate conflict-aware restoration to the audit module."""

    from app.disposition_audit import rollback_disposition_action

    return await rollback_disposition_action(
        session,
        action_id=action_id,
        actor=actor,
        reason=reason,
        snapshot_builder=_disposition_state_snapshot,
    )

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
