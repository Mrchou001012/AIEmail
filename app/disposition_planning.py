"""Build deterministic, reviewable plans from an inbound disposition."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import EmailMessage, InboundDispositionAction
from app.disposition_resolution import (
    email_domain as _domain,
)
from app.disposition_resolution import (
    parse_return_until as _parse_return_until,
)
from app.disposition_resolution import (
    resolve_disposition_resources as _resolved_disposition_resources,
)
from app.disposition_resolution import (
    same_company_domain as _same_company_domain,
)
from app.email_identity import reply_contact_name
from app.inbound_disposition import InboundDisposition, InboundDispositionType
from app.settings import Settings


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.isoformat()


async def build_disposition_plan_data(
    session: AsyncSession,
    row: EmailMessage,
    *,
    settings: Settings,
    disposition: InboundDisposition,
    at: datetime | None = None,
) -> dict[str, Any]:
    resources = await _resolved_disposition_resources(session, row, disposition)
    sender_contact = resources.sender_contact
    contact = resources.contact
    customer = resources.customer
    parent = resources.parent
    disposition_metadata = row.disposition_metadata or {}
    reviewed_parent_source = str(
        disposition_metadata.get("reviewed_parent_source") or ""
    ) or None
    parent_source = parent.source if parent is not None else reviewed_parent_source
    parent_email_id = (
        parent.parent_email_id
        if parent is not None
        else disposition_metadata.get("reviewed_parent_email_id")
    )
    parent_message_id = (
        parent.parent_message_id
        if parent is not None
        else disposition_metadata.get("reviewed_parent_message_id")
    )
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
        if return_until is None:
            proposed_actions.extend(["IGNORE_AUTOREPLY", "REVIEW_RETURN_DATE"])
        else:
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
                or parent_source is not None
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
        if row.disposition_handled_at is None:
            if parent_source == "QUOTED_OUTBOUND_PARENT":
                blockers.append("QUOTED_PARENT_REQUIRES_REVIEW")
            elif parent_source is not None:
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
            for blocker in (
                "SENDER_CONTACT_NOT_UNIQUE",
                "RETURN_DATE_NOT_RELIABLE",
            )
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
            parent_source or "DIRECT_IDENTITY"
        ),
        "reactivation_parent_message_id": parent_message_id,
        "parent_email_id": parent_email_id,
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

