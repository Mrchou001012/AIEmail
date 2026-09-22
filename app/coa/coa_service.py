"""COA request orchestration and reviewed attachment approval."""

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

import app.coa.coa_requests as coa_requests
from app.ai import (
    InboundAnalysis,
)
from app.coa.coa_catalog import COACatalog
from app.coa.coa_delivery import (
    COAResponseError,
)
from app.coa.coa_delivery import (
    prepare_coa_response as _prepare_coa_response,
)
from app.common.email_identity import (
    reply_contact_name as _reply_contact_name,
)
from app.common.email_threading import _reply_references, _reply_source
from app.common.service_core import audit, create_handoff, stage_outbox
from app.db import (
    EmailMessage,
    Handoff,
    Outbox,
    SalesCase,
)
from app.domain import (
    HandoffReason,
    Intent,
    SendContext,
    evaluate_send_policy,
)
from app.handoffs.human_reply_service import queue_human_reply
from app.imports import load_content
from app.mail import (
    OutboundAttachment,
    append_quoted_reply,
)
from app.settings import get_settings


async def _maybe_handle_coa_request(
    session: AsyncSession,
    *,
    case: SalesCase,
    email_row: EmailMessage,
    analysis: InboundAnalysis,
    analysis_facts: dict[str, Any],
) -> bool:
    """Send every explicit COA deliverable independently from its primary intent."""
    if analysis.intent != Intent.COA_REQUEST and not analysis.coa_requested:
        return False
    settings = get_settings()
    plan = coa_requests.plan_coa_request(
        analysis=analysis,
        analysis_facts=analysis_facts,
        case_product_code=case.product.code if case.product is not None else "",
        case_product_name=case.product.name if case.product is not None else "",
    )
    lookup_facts = plan.lookup_facts(analysis_facts=analysis_facts, catalog_path=settings.coa_catalog_path)
    if not settings.coa_catalog_enabled:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.COA_REVIEW,
            summary="COA request is waiting because the approved COA catalog is disabled",
            facts=lookup_facts,
            source_email_id=email_row.id,
            pause_case=plan.primary_request,
        )
        return plan.primary_request
    try:
        response = _prepare_coa_response(
            settings=settings,
            contact_name=_reply_contact_name(
                case.contact.name,
                getattr(email_row, "body_text", ""),
            ),
            original_subject=email_row.subject,
            product_queries=list(plan.product_queries),
            cas_number=plan.cas_number,
        )
    except COAResponseError as exc:
        error_facts = {**lookup_facts, **exc.facts, "coa_help_needed": exc.help_needed}
        if plan.followup and plan.prior_missing_queries:
            error_facts["missing_coa_queries"] = list(plan.prior_missing_queries)
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.COA_REVIEW,
            summary=exc.summary,
            facts=error_facts,
            source_email_id=email_row.id,
            pause_case=plan.primary_request,
        )
        return plan.primary_request
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.COA_REVIEW,
            summary="Approved COA catalog or attachment is unavailable",
            facts={**lookup_facts, "coa_catalog_error": type(exc).__name__},
            source_email_id=email_row.id,
            pause_case=plan.primary_request,
        )
        return plan.primary_request
    prepared_facts = coa_requests.prepared_coa_facts(
        lookup_facts=lookup_facts, response=response, generated_at=datetime.now(UTC).isoformat()
    )
    outstanding_queries = coa_requests.outstanding_coa_queries(response_missing=response.missing_queries, plan=plan)
    if plan.followup:
        prepared_facts["missing_coa_queries"] = outstanding_queries
        prepared_facts["coa_partial"] = bool(outstanding_queries)
    if not settings.coa_auto_send_enabled:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.COA_REVIEW,
            summary=(f"COA draft prepared for {' and '.join(response.product_names)}; human approval is required"),
            facts=prepared_facts,
            source_email_id=email_row.id,
            update_existing=True,
            pause_case=plan.primary_request,
        )
        return plan.primary_request

    send_decision = evaluate_send_policy(
        SendContext(
            intent=analysis.intent,
            stage=case.stage,
            status=case.status,
            intent_confidence=analysis.intent_confidence,
            product_confidence=analysis.product_confidence,
            numeric_confidence=1.0,
            auto_send_allowed=case.customer.auto_send_allowed,
            contact_suppressed=case.contact.suppressed,
            do_not_contact=case.customer.do_not_contact,
            has_risky_attachment=analysis.risky_attachment,
            product_known=True,
        ),
        intent_threshold=settings.intent_confidence_threshold,
        product_threshold=settings.product_confidence_threshold,
        numeric_threshold=settings.numeric_confidence_threshold,
    )
    if not send_decision.allow_send:
        await create_handoff(
            session,
            case=case,
            reason=send_decision.reason or HandoffReason.COA_REVIEW,
            summary="COA was found, but current send policy requires human approval",
            facts=prepared_facts,
            source_email_id=email_row.id,
            pause_case=plan.primary_request,
        )
        return plan.primary_request

    bundle = load_content(settings.content_dir)
    signed_text, signed_html = coa_requests.signed_coa_body(
        response_body=response.body_text,
        signature_text=bundle.signature_text,
        signature_html=bundle.signature_html,
    )
    source = _reply_source(email_row)
    signed_text, signed_html = append_quoted_reply(
        signed_text,
        signed_html,
        from_address=email_row.from_address,
        source_body=source.body_text,
        source_html=source.body_html,
        occurred_at=email_row.received_at,
    )
    coa_business_key = coa_requests.coa_outbox_business_key(
        email_id=email_row.id, product_queries=plan.product_queries, followup=plan.followup
    )
    outbox = await stage_outbox(
        session,
        case=case,
        message_kind="COA",
        subject=response.subject,
        text_body=signed_text,
        html_body=signed_html,
        business_key=coa_business_key,
        in_reply_to=email_row.message_id,
        references=_reply_references(email_row),
        inline_images=source.inline_images,
        attachments=response.attachments,
    )
    if outbox is None:
        await session.rollback()
        return plan.primary_request
    await audit(
        session,
        "inbound.coa_queued",
        case_id=case.id,
        actor="system",
        data=coa_requests.coa_delivery_audit_data(
            email_id=email_row.id,
            outbox_id=outbox.id,
            business_key=coa_business_key,
            response=response,
            secondary_request=not plan.primary_request,
            outstanding_queries=outstanding_queries,
        ),
    )
    if outstanding_queries:
        partial_facts = coa_requests.partial_coa_handoff_facts(
            prepared_facts=prepared_facts,
            outbox_id=outbox.id,
            prepared_coas=response.prepared_coas,
            outstanding_queries=outstanding_queries,
        )
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.COA_REVIEW,
            summary=f"Sent {len(response.prepared_coas)} available COA(s); "
            f"{len(outstanding_queries)} requested COA(s) require human handling",
            facts=partial_facts,
            source_email_id=email_row.id,
            update_existing=True,
            pause_case=plan.primary_request,
        )
    return plan.primary_request


async def queue_prepared_coa_reply(
    session: AsyncSession,
    *,
    handoff_id: int,
    subject: str,
    body_text: str,
    actor: str,
    note: str = "",
    resume_automation: bool = False,
) -> Outbox:
    """Approve a prepared COA draft while rechecking the exact NAS file hash."""

    handoff = await session.get(Handoff, handoff_id)
    if handoff is None:
        raise ValueError("handoff not found")
    prepared = (handoff.extracted_facts or {}).get("prepared_coa")
    if handoff.reason_code != HandoffReason.COA_REVIEW.value or not isinstance(prepared, dict):
        raise ValueError("handoff has no prepared COA attachment")
    settings = get_settings()
    try:
        catalog = COACatalog(settings.coa_catalog_path)
        entry = catalog.entry_for_path(str(prepared.get("path") or ""))
        if str(entry.get("sha256") or "") != str(prepared.get("sha256") or ""):
            raise ValueError("prepared COA no longer matches the approved catalog")
        payload = catalog.read_verified_attachment(entry)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("approved COA catalog or attachment is unavailable") from exc
    return await queue_human_reply(
        session,
        handoff_id=handoff_id,
        subject=subject,
        body_text=body_text,
        actor=actor,
        note=note,
        resume_automation=resume_automation,
        attachments=(
            OutboundAttachment(
                filename=str(prepared.get("filename") or "COA.pdf"),
                content_type="application/pdf",
                payload=payload,
            ),
        ),
    )
