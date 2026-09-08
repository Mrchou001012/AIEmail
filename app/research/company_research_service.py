"""Bounded company research for product-category selection."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import (
    AIClient,
    InboundAnalysis,
    explicit_product_list_requested,
)
from app.catalogs.category_product_list import _maybe_send_product_list
from app.common.service_core import audit, create_handoff
from app.db import (
    AIInvocation,
    CaseStatus,
    EmailMessage,
    Handoff,
    Product,
    ProductCategory,
    SalesCase,
)
from app.domain import (
    HandoffReason,
    Intent,
    SendContext,
    evaluate_send_policy,
)
from app.research.company_research_context import (
    _cached_company_research,
    _company_research_gate,
    _nonfree_email_domain,
    _store_company_research_cache,
)
from app.settings import get_settings


async def _company_research_catalog(
    session: AsyncSession,
) -> tuple[dict[str, ProductCategory], list[dict[str, Any]], str]:
    categories = (
        (
            await session.execute(
                select(ProductCategory).where(ProductCategory.active.is_(True)).order_by(ProductCategory.sort_order, ProductCategory.id)
            )
        )
        .scalars()
        .all()
    )
    products = (
        (
            await session.execute(
                select(Product)
                .where(
                    Product.active.is_(True),
                    Product.category_id.is_not(None),
                )
                .order_by(Product.category_id, Product.sort_order, Product.id)
            )
        )
        .scalars()
        .all()
    )
    examples_by_category: dict[int, list[str]] = {}
    for product in products:
        if product.category_id is None:
            continue
        examples = examples_by_category.setdefault(product.category_id, [])
        for value in (product.series, product.name):
            normalized = str(value or "").strip()
            if normalized and normalized not in examples and len(examples) < 12:
                examples.append(normalized)
    payload = [
        {
            "key": category.key,
            "name": category.name,
            "examples": examples_by_category.get(category.id, []),
        }
        for category in categories
    ]
    signature = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {category.key: category for category in categories}, payload, signature


async def _maybe_research_and_send_product_list(
    session: AsyncSession,
    *,
    case: SalesCase,
    email_row: EmailMessage,
    analysis: InboundAnalysis,
    analysis_facts: dict[str, Any],
    existing_handoff: Handoff | None = None,
) -> bool:
    """Use cited company research only for an explicit, category-less list request."""

    async def route_handoff(
        reason: HandoffReason,
        summary: str,
        facts: dict[str, Any],
    ) -> Handoff:
        if existing_handoff is None:
            return await create_handoff(
                session,
                case=case,
                reason=reason,
                summary=summary,
                facts=facts,
                source_email_id=email_row.id,
            )
        existing_handoff.reason_code = reason.value
        existing_handoff.summary = summary
        existing_handoff.extracted_facts = facts
        if case.status == CaseStatus.ACTIVE:
            case.status = CaseStatus.WAITING_HUMAN
        await audit(
            session,
            "handoff.reclassified",
            case_id=case.id,
            actor="company-research-backfill",
            data={
                "handoff_id": existing_handoff.id,
                "reason": reason.value,
                "source_email_id": email_row.id,
            },
        )
        await session.commit()
        return existing_handoff

    if analysis.intent != Intent.PRODUCT_LIST_REQUEST or not explicit_product_list_requested(f"{email_row.subject}\n{email_row.body_text}"):
        return False
    settings = get_settings()
    send_decision = evaluate_send_policy(
        SendContext(
            intent=analysis.intent,
            stage=case.stage,
            status=case.status,
            intent_confidence=analysis.intent_confidence,
            product_confidence=1.0,
            numeric_confidence=1.0,
            auto_send_allowed=case.customer.auto_send_allowed,
            contact_suppressed=case.contact.suppressed,
            do_not_contact=case.customer.do_not_contact,
            has_risky_attachment=analysis.risky_attachment,
            product_known=analysis.product_code is None,
            prebook_requested=analysis.prebook_requested,
            packaging_requested=analysis.packaging_requested,
            delivery_requested=analysis.shipping_requested,
        ),
        intent_threshold=settings.intent_confidence_threshold,
        product_threshold=settings.product_confidence_threshold,
        numeric_threshold=settings.numeric_confidence_threshold,
    )
    if not send_decision.allow_send:
        await route_handoff(
            send_decision.reason or HandoffReason.LOW_CONFIDENCE,
            f"Inbound {analysis.intent.value} requires human review",
            analysis_facts,
        )
        return True
    if not settings.company_research_enabled:
        await route_handoff(
            HandoffReason.PRODUCT_CATEGORY_REVIEW,
            "Product-list request has no unique CRM/Excel category; company research is disabled",
            {
                **analysis_facts,
                "product_pending": True,
                "company_research": {"status": "DISABLED"},
            },
        )
        return True

    categories_by_key, category_payload, catalog_signature = await _company_research_catalog(session)
    if not categories_by_key:
        await route_handoff(
            HandoffReason.PRODUCT_CATEGORY_REVIEW,
            "No active product category is available for a product-list reply",
            {**analysis_facts, "product_pending": True},
        )
        return True
    company_domain = _nonfree_email_domain(case.contact.email)
    observed_at = datetime.now(UTC)
    cached = _cached_company_research(
        case.customer,
        company_domain=company_domain,
        catalog_signature=catalog_signature,
        now=observed_at,
    )
    if cached is None:
        ai = AIClient(settings)
        try:
            decision, sources, metadata = await ai.research_company_category(
                company_name=case.customer.company_name,
                company_domain=company_domain,
                categories=category_payload,
            )
        except Exception as exc:
            session.add(
                AIInvocation(
                    case_id=case.id,
                    provider=settings.ai_provider,
                    model=settings.anthropic_model,
                    purpose="company_category_research",
                    request_hash=hashlib.sha256(f"{case.customer.id}:{catalog_signature}".encode()).hexdigest(),
                    parsed_output=None,
                    success=False,
                    error_type=type(exc).__name__,
                    input_tokens=None,
                    output_tokens=None,
                )
            )
            await audit(
                session,
                "company_research.failed",
                case_id=case.id,
                actor="system",
                data={
                    "email_id": email_row.id,
                    "customer_id": case.customer_id,
                    "error_type": type(exc).__name__,
                },
            )
            await route_handoff(
                HandoffReason.PRODUCT_CATEGORY_REVIEW,
                "Company research failed; product category requires human confirmation",
                {
                    **analysis_facts,
                    "product_pending": True,
                    "company_research": {
                        "status": "FAILED",
                        "error_type": type(exc).__name__,
                    },
                },
            )
            return True
        research_output = {
            "decision": decision.model_dump(mode="json"),
            "sources": [source.model_dump(mode="json") for source in sources],
        }
        session.add(
            AIInvocation(
                case_id=case.id,
                provider=str(metadata.get("provider") or settings.ai_provider),
                model=str(metadata.get("model") or settings.anthropic_model),
                purpose="company_category_research",
                request_hash=str(metadata.get("request_hash") or catalog_signature),
                parsed_output=research_output,
                success=True,
                input_tokens=metadata.get("input_tokens"),
                output_tokens=metadata.get("output_tokens"),
            )
        )
        _store_company_research_cache(
            case.customer,
            company_domain=company_domain,
            catalog_signature=catalog_signature,
            decision=decision,
            sources=sources,
            metadata=metadata,
            settings=settings,
            now=observed_at,
        )
        cache_hit = False
    else:
        decision, sources, metadata = cached
        cache_hit = True

    gate = _company_research_gate(
        decision,
        sources,
        company_domain=company_domain,
        active_category_keys=set(categories_by_key),
        settings=settings,
    )
    research_facts = {
        "status": "COMPLETED",
        "cache_hit": cache_hit,
        "company_name": case.customer.company_name,
        "company_domain": company_domain,
        "decision": decision.model_dump(mode="json"),
        "sources": [source.model_dump(mode="json") for source in sources],
        "gate": gate,
        "provider": metadata.get("provider"),
        "model": metadata.get("model"),
    }
    await audit(
        session,
        "company_research.completed",
        case_id=case.id,
        actor="system",
        data={
            "email_id": email_row.id,
            "customer_id": case.customer_id,
            "cache_hit": cache_hit,
            "recommended_category_key": decision.recommended_category_key,
            "eligible": gate["eligible"],
            "gate_reasons": gate["reasons"],
            "source_domains": gate["source_domains"],
        },
    )
    if not gate["eligible"] or not settings.company_research_auto_send_enabled:
        recommended = categories_by_key.get(decision.recommended_category_key or "")
        summary = (
            f"Company research suggests {recommended.name}; human confirmation is required"
            if recommended is not None
            else "Company research could not safely determine a product category"
        )
        if gate["eligible"] and not settings.company_research_auto_send_enabled:
            summary += " (observation mode)"
        await route_handoff(
            HandoffReason.PRODUCT_CATEGORY_REVIEW,
            summary,
            {
                **analysis_facts,
                "product_pending": True,
                "company_research": research_facts,
            },
        )
        return True

    category = categories_by_key[decision.recommended_category_key or ""]
    case.category_id = category.id
    case.category = category
    await audit(
        session,
        "company_research.category_selected",
        case_id=case.id,
        actor="system",
        data={
            "email_id": email_row.id,
            "category_id": category.id,
            "category_key": category.key,
            "research": research_facts,
        },
    )
    return await _maybe_send_product_list(
        session,
        case=case,
        email_row=email_row,
        analysis=analysis,
        analysis_facts={
            **analysis_facts,
            "company_research": research_facts,
        },
    )
