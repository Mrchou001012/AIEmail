"""Category product list workflow."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import InboundAnalysis, requested_product_list_file_format
from app.catalogs.product_catalog import render_product_list_email
from app.catalogs.product_list_service import _product_list_outbound_attachments
from app.catalogs.products import product_codes_match
from app.common.email_identity import reply_contact_name as _reply_contact_name
from app.common.email_threading import _reply_references, _reply_source
from app.common.service_core import (
    _catalog_category_breakdown,
    _customer_payment_term,
    _payment_details_requested,
    _payment_term_sentence,
    audit,
    create_handoff,
    stage_outbox,
)
from app.db import EmailMessage, Product, ProductCategory, SalesCase
from app.domain import HandoffReason, SendContext, evaluate_send_policy
from app.imports import load_content
from app.mail import append_quoted_reply
from app.settings import get_settings


async def _maybe_send_product_list(
    session: AsyncSession,
    *,
    case: SalesCase,
    email_row: EmailMessage,
    analysis: InboundAnalysis,
    analysis_facts: dict[str, Any],
) -> bool:
    """Queue a deterministic category product-list reply when eligible.

    Returns ``True`` when the inbound email was handled (a product-list reply
    was queued or a handoff was created). Returns ``False`` when the case has
    no product category at all, so callers can continue the normal pipeline.
    """
    category = await session.get(ProductCategory, case.category_id) if case.category_id is not None else None
    if category is None and case.product_id is not None and case.product is not None and case.product.category_id is not None:
        category = await session.get(ProductCategory, case.product.category_id)
    if category is None or not category.active:
        if case.category_id is None:
            return False
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.HUMAN_CONTROL,
            summary="Case product category is no longer active",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return True

    send_decision = evaluate_send_policy(
        SendContext(
            intent=analysis.intent,
            stage=case.stage,
            status=case.status,
            intent_confidence=analysis.intent_confidence,
            # The product category comes from the CRM record or the matched
            # product, so the catalog target is deterministic rather than an
            # extracted product code.
            product_confidence=1.0,
            numeric_confidence=1.0,
            auto_send_allowed=case.customer.auto_send_allowed,
            contact_suppressed=case.contact.suppressed,
            do_not_contact=case.customer.do_not_contact,
            has_risky_attachment=analysis.risky_attachment,
            product_known=(
                analysis.product_code is None
                or (case.product is not None and product_codes_match(analysis.product_code, case.product.code))
            ),
            prebook_requested=analysis.prebook_requested,
            packaging_requested=analysis.packaging_requested,
            delivery_requested=analysis.shipping_requested,
        ),
        intent_threshold=get_settings().intent_confidence_threshold,
        product_threshold=get_settings().product_confidence_threshold,
        numeric_threshold=get_settings().numeric_confidence_threshold,
    )
    if not send_decision.allow_send:
        await create_handoff(
            session,
            case=case,
            reason=send_decision.reason or HandoffReason.LOW_CONFIDENCE,
            summary=f"Inbound {analysis.intent.value} requires human review",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return True
    if analysis.product_code is not None and not (
        case.product is not None and product_codes_match(analysis.product_code, case.product.code)
    ):
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.NONSTANDARD,
            summary="Email names a specific product; a category product list is not appropriate",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return True

    products = (
        (
            await session.execute(
                select(Product)
                .where(
                    Product.category_id == category.id,
                    Product.active.is_(True),
                    Product.catalog_visible.is_(True),
                )
                .order_by(Product.sort_order, Product.id)
            )
        )
        .scalars()
        .all()
    )
    if not products:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.HUMAN_CONTROL,
            summary=f"Product category {category.key} has no active products",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return True

    settings = get_settings()
    bundle = load_content(settings.content_dir)
    try:
        attachments, attachment_filename = _product_list_outbound_attachments(
            category=category,
            products=products,
            request_text=f"{email_row.subject}\n{email_row.body_text}",
        )
        text, html_body = render_product_list_email(
            contact_name=_reply_contact_name(case.contact.name, email_row.body_text),
            category=category,
            products=products,
            subject=email_row.subject,
            signature_text=bundle.signature_text,
            signature_html=bundle.signature_html,
            attachment_filename=attachment_filename,
        )
        signature_text = bundle.signature_text.strip()
        draft_body = text[: -len(signature_text)].rstrip() if signature_text and text.endswith(signature_text) else text.rstrip()
        source = _reply_source(email_row)
        text, html_body = append_quoted_reply(
            text,
            html_body,
            from_address=email_row.from_address,
            source_body=source.body_text,
            source_html=source.body_html,
            occurred_at=email_row.received_at,
        )
    except Exception as exc:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.AI_FAILURE,
            summary=f"Product list rendering failed: {type(exc).__name__}",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return True
    subject = f"Re: {email_row.subject}" if email_row.subject.strip() else f"Our {category.name} product list"
    product_list_request_text = f"{email_row.subject}\n{email_row.body_text}"
    payment_requested = _payment_details_requested(product_list_request_text)
    payment = await _customer_payment_term(session, customer_id=case.customer_id) if payment_requested else None
    payment_term = payment.term if payment is not None else None
    if payment is not None:
        draft_body = f"{draft_body}\n\n{_payment_term_sentence(payment)}"
    if not settings.product_list_auto_send_enabled:
        file_format = requested_product_list_file_format(f"{email_row.subject}\n{email_row.body_text}")
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.PRODUCT_LIST_REVIEW,
            summary=f"Product-list draft prepared for {category.name}; human approval is required",
            facts={
                **analysis_facts,
                "prepared_product_list": {
                    "category_id": category.id,
                    "category_key": category.key,
                    "category_name": category.name,
                    "product_ids": [product.id for product in products],
                    "product_codes": [product.code for product in products],
                    "product_count": len(products),
                    "category_breakdown": await _catalog_category_breakdown(session, products),
                    "file_format": file_format,
                    "attachment_filename": attachment_filename,
                    "payment_requested": payment_requested,
                    "payment_term": payment_term,
                    "payment_term_source": payment.source if payment is not None else None,
                    "payment_term_quote_id": payment.quote_id if payment is not None else None,
                    "missing_business_facts": [],
                },
                "ai_draft_preview": {
                    "subject": subject,
                    "body_text": draft_body,
                    "generated_at": datetime.now(UTC).isoformat(),
                    "provider": "deterministic-product-list",
                    "model": "active-product-catalog-v1",
                    "rag_matches": [],
                },
            },
            source_email_id=email_row.id,
            update_existing=True,
        )
        return True
    outbox = await stage_outbox(
        session,
        case=case,
        message_kind="PRODUCT_LIST",
        subject=subject,
        text_body=text,
        html_body=html_body,
        business_key=f"inbound-product-list:{email_row.id}",
        in_reply_to=email_row.message_id,
        references=_reply_references(email_row),
        inline_images=source.inline_images,
        attachments=attachments,
    )
    if outbox is None:
        await session.rollback()
        return True
    await audit(
        session,
        "inbound.product_list_queued",
        case_id=case.id,
        actor="system",
        data={
            "email_id": email_row.id,
            "outbox_id": outbox.id,
            "category_id": category.id,
            "category_key": category.key,
            "product_count": len(products),
            "attachment_filename": attachment_filename,
        },
    )
    return True
