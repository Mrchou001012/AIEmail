"Historical product-list request backfill workflow."

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalogs.backfill_types import BackfillCandidate
from app.common.email_threading import _reply_references
from app.common.service_core import audit, stage_outbox
from app.db import CaseStage, CaseStatus, DeliveryStatus, Outbox, SalesCase
from app.handoffs.agent_runtime import finalize_handoff_agent_run
from app.mail import normalized_subject
from app.research.company_research_service import _maybe_research_and_send_product_list


async def apply_backfill_candidate(
    session: AsyncSession,
    plan: BackfillCandidate,
    exclusions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Apply an already checked request under the caller's business transaction."""
    handoff = plan.handoff
    source = plan.source
    sales_case = plan.sales_case
    contact = plan.contact
    customer = plan.customer
    category = plan.category
    analysis = plan.analysis
    research_required = plan.research_required
    existing_product_list = plan.existing_product_list
    prepared = plan.prepared
    candidate = plan.candidate
    if research_required and existing_product_list is None:
        if sales_case is None:
            exclusions.append(
                {
                    "handoff_id": handoff.id,
                    "email_id": source.id,
                    "reason": "COMPANY_RESEARCH_CASE_REQUIRED",
                }
            )
            return None
        if sales_case.status == CaseStatus.WAITING_HUMAN:
            sales_case.status = CaseStatus.ACTIVE
        await _maybe_research_and_send_product_list(
            session,
            case=sales_case,
            email_row=source,
            analysis=analysis,
            analysis_facts={
                **(handoff.extracted_facts or {}),
                **analysis.model_dump(mode="json"),
                "company_research_backfill": True,
            },
            existing_handoff=handoff,
        )
        existing_product_list = await session.scalar(
            select(Outbox).where(
                Outbox.business_key == f"inbound-product-list:{source.id}",
                Outbox.status != DeliveryStatus.CANCELLED,
            )
        )
        if existing_product_list is None:
            await session.refresh(handoff)
            exclusions.append(
                {
                    "handoff_id": handoff.id,
                    "email_id": source.id,
                    "reason": "COMPANY_RESEARCH_REQUIRES_HUMAN",
                    "handoff_reason": handoff.reason_code,
                    "summary": handoff.summary,
                }
            )
            return None
        await session.refresh(sales_case, ["category"])
        category = sales_case.category
        if category is None:
            raise RuntimeError("company research queued a product list without assigning a category")
        candidate["category_id"] = category.id
        candidate["category_key"] = category.key

    if existing_product_list is None:
        matched_product = prepared["matched_product"]
        if sales_case is None:
            sales_case = SalesCase(
                customer_id=customer.id,
                contact_id=contact.id,
                product_id=(matched_product.id if matched_product is not None else None),
                category_id=category.id,
                currency="INR",
                stage=CaseStage.QUOTING,
                status=CaseStatus.ACTIVE,
                subject_key=normalized_subject(source.subject)[:255],
                customer=customer,
                contact=contact,
                product=matched_product,
                category=category,
            )
            session.add(sales_case)
            await session.flush()
        else:
            sales_case.category_id = category.id
            sales_case.category = category
            if sales_case.product is None and matched_product is not None:
                sales_case.product_id = matched_product.id
                sales_case.product = matched_product
            if sales_case.status == CaseStatus.WAITING_HUMAN:
                sales_case.status = CaseStatus.ACTIVE
        source.case_id = sales_case.id
        source.customer_id = customer.id
        source.contact_id = contact.id
        handoff.case_id = sales_case.id
        existing_product_list = await stage_outbox(
            session,
            case=sales_case,
            message_kind="PRODUCT_LIST",
            subject=prepared["subject"],
            text_body=prepared["text_body"],
            html_body=prepared["html_body"],
            business_key=f"inbound-product-list:{source.id}",
            in_reply_to=source.message_id,
            references=_reply_references(source),
            inline_images=prepared["inline_images"],
            attachments=prepared["attachments"],
        )
        if existing_product_list is None:
            existing_product_list = await session.scalar(
                select(Outbox).where(
                    Outbox.business_key == f"inbound-product-list:{source.id}",
                    Outbox.status != DeliveryStatus.CANCELLED,
                )
            )
        if existing_product_list is None:
            exclusions.append(
                {
                    "handoff_id": candidate["handoff_id"],
                    "email_id": candidate["email_id"],
                    "reason": "OUTBOX_IDEMPOTENCY_CONFLICT",
                }
            )
            await session.rollback()
            return None

    if sales_case is not None and sales_case.status == CaseStatus.WAITING_HUMAN:
        sales_case.status = CaseStatus.ACTIVE
    handoff.status = "RESOLVED"
    handoff.resolution_note = f"Automatically backfilled product list for {category.key}; outbox_id={existing_product_list.id}"
    await finalize_handoff_agent_run(
        session,
        handoff_id=handoff.id,
        actor="product-list-backfill",
        outcome="product-list-backfilled",
    )
    if handoff.dingtalk_status != "SENT":
        handoff.dingtalk_status = "CANCELLED"
    await audit(
        session,
        "handoff.product_list_backfilled",
        case_id=existing_product_list.case_id,
        actor="product-list-backfill",
        data={
            "handoff_id": handoff.id,
            "email_id": source.id,
            "outbox_id": existing_product_list.id,
            "category_id": category.id,
            "category_key": category.key,
            "attachment_filename": (prepared.get("attachment_filename")),
        },
    )
    await session.commit()
    return {
        **candidate,
        "case_id": existing_product_list.case_id,
        "outbox_id": existing_product_list.id,
        "outbox_status": existing_product_list.status.value,
    }
