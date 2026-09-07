"""Review-only COA preview generation for existing inbound handoffs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agent_runtime import ensure_handoff_agent_run
from app.ai import AIClient, InboundAnalysis
from app.coa_delivery import COAResponseError, prepare_coa_response
from app.coa_requests import plan_coa_request, prepared_coa_facts
from app.db import AuditEvent, CaseStatus, EmailMessage, Handoff, SalesCase
from app.domain import HandoffReason, Intent
from app.email_identity import reply_contact_name
from app.settings import Settings


async def prepare_handoff_coa_preview(
    session: AsyncSession,
    *,
    handoff_id: int,
    actor: str,
    settings: Settings,
) -> dict[str, Any]:
    """Re-analyze one historical email and save a COA draft without an Outbox row."""

    handoff = await session.scalar(select(Handoff).where(Handoff.id == handoff_id).with_for_update())
    if handoff is None:
        raise ValueError("handoff not found")
    if handoff.status != "OPEN":
        raise ValueError("only an open handoff can generate a COA preview")
    if handoff.source_email_id is None or handoff.case_id is None:
        raise ValueError("handoff must have an inbound email and associated case")
    email_row = await session.get(EmailMessage, handoff.source_email_id)
    sales_case = await session.scalar(
        select(SalesCase)
        .options(
            selectinload(SalesCase.customer),
            selectinload(SalesCase.contact),
            selectinload(SalesCase.product),
        )
        .where(SalesCase.id == handoff.case_id)
    )
    if email_row is None or sales_case is None or email_row.direction != "INBOUND":
        raise ValueError("handoff source email or case is unavailable")

    analysis, metadata = await AIClient(settings).analyze(
        email_row.subject,
        email_row.body_text,
        email_row.attachment_metadata,
    )
    return await prepare_detected_coa_preview(
        session,
        handoff=handoff,
        email_row=email_row,
        sales_case=sales_case,
        analysis=analysis,
        analysis_metadata=metadata,
        actor=actor,
        settings=settings,
    )


async def prepare_detected_coa_preview(
    session: AsyncSession,
    *,
    handoff: Handoff,
    email_row: EmailMessage,
    sales_case: SalesCase,
    analysis: InboundAnalysis,
    analysis_metadata: dict[str, Any],
    actor: str,
    settings: Settings,
    persist: bool = True,
) -> dict[str, Any]:
    """Save a detected COA request as a review-only draft."""

    if analysis.intent != Intent.COA_REQUEST and not analysis.coa_requested:
        raise ValueError("AI did not identify an explicit COA request in this email")
    analysis_facts = analysis.model_dump(mode="json")
    plan = plan_coa_request(
        analysis=analysis,
        analysis_facts=analysis_facts,
        case_product_code=sales_case.product.code if sales_case.product else "",
        case_product_name=sales_case.product.name if sales_case.product else "",
    )
    lookup_facts = plan.lookup_facts(
        analysis_facts=analysis_facts,
        catalog_path=settings.coa_catalog_path,
    )
    lookup_facts["coa_preview_analysis"] = analysis_metadata
    lookup_facts["coa_preview_delivery_created"] = False

    if not settings.coa_catalog_enabled:
        summary = "AI identified a COA request, but the approved catalog is disabled"
        facts = lookup_facts
        prepared_count = 0
        missing_queries = list(plan.product_queries)
    else:
        try:
            response = prepare_coa_response(
                settings=settings,
                contact_name=reply_contact_name(
                    sales_case.contact.name,
                    email_row.body_text,
                ),
                original_subject=email_row.subject,
                product_queries=list(plan.product_queries),
                cas_number=plan.cas_number,
            )
        except COAResponseError as exc:
            summary = exc.summary
            facts = {
                **lookup_facts,
                **exc.facts,
                "coa_help_needed": exc.help_needed,
            }
            prepared_count = 0
            missing_queries = list(facts.get("missing_coa_queries") or plan.product_queries)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            summary = "Approved COA catalog or attachment is unavailable"
            facts = {**lookup_facts, "coa_catalog_error": type(exc).__name__}
            prepared_count = 0
            missing_queries = list(plan.product_queries)
        else:
            facts = prepared_coa_facts(
                lookup_facts=lookup_facts,
                response=response,
                generated_at=datetime.now(UTC).isoformat(),
            )
            prepared_count = len(response.prepared_coas)
            missing_queries = list(response.missing_queries)
            summary = f"Review-only COA draft prepared for {prepared_count} product(s)" + (
                f"; {len(missing_queries)} product(s) still require human handling" if missing_queries else ""
            )

    preview = facts.get("ai_draft_preview")
    if not isinstance(preview, dict):
        subject = email_row.subject.strip()
        if not subject.casefold().startswith("re:"):
            subject = f"Re: {subject}"
        contact_name = sales_case.contact.name.strip() or "Customer"
        preview = {
            "subject": subject[:998],
            "body_text": (
                f"Dear {contact_name},\n\n"
                "Thank you for your COA request. We are confirming the correct "
                "document and will follow up shortly."
            ),
            "generated_at": datetime.now(UTC).isoformat(),
            "provider": "deterministic-coa-review",
            "model": str(analysis_metadata.get("model") or "coa-review"),
            "rag_matches": [],
            "delivery_created": False,
        }
        facts["ai_draft_preview"] = preview

    handoff.reason_code = HandoffReason.COA_REVIEW.value
    handoff.summary = summary
    handoff.extracted_facts = facts
    sales_case.status = CaseStatus.WAITING_HUMAN
    await ensure_handoff_agent_run(session, handoff=handoff)
    if persist:
        session.add(
            AuditEvent(
                case_id=sales_case.id,
                actor=actor[:128],
                event_type="handoff.coa_preview_generated",
                data={
                    "handoff_id": handoff.id,
                    "source_email_id": email_row.id,
                    "prepared_count": prepared_count,
                    "missing_coa_queries": missing_queries,
                    "delivery_created": False,
                },
            )
        )
        await session.commit()
    return {
        "handoff_id": handoff.id,
        "coa_requested": True,
        "prepared_count": prepared_count,
        "missing_coa_queries": missing_queries,
        "delivery_created": False,
        "preview": preview,
    }
