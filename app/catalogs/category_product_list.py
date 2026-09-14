"""Category product list workflow."""

import html
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import InboundAnalysis, requested_product_list_file_format
from app.catalogs.product_list_drafting import generate_product_list_ai_preview
from app.catalogs.product_list_service import _product_list_outbound_attachments
from app.catalogs.products import product_codes_match
from app.common.email_identity import (
    reply_contact_name as _reply_contact_name,
)
from app.common.email_identity import (
    strip_duplicate_signature_lead as _strip_duplicate_signature_lead,
)
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


def _verified_catalog_listing(products: list[Product]) -> tuple[str, str]:
    """Render approved catalog facts without prescribing the email wording."""

    header = "No. | Code | Product Name | CAS No. | Content"
    rows: list[str] = [header]
    html_rows = [
        '<table border="1" cellpadding="4" cellspacing="0" '
        'style="border-collapse:collapse">',
        '<tr><th align="left">No.</th><th align="left">Code</th>'
        '<th align="left">Product Name</th><th align="left">CAS No.</th>'
        '<th align="left">Content</th></tr>',
    ]
    for number, product in enumerate(products, start=1):
        values = (
            str(number),
            str(product.catalog_code or "-").strip(),
            str(product.name or "-").strip(),
            str(product.cas_no or "-").strip(),
            str(product.content or "-").strip(),
        )
        rows.append(" | ".join(values))
        html_rows.append(
            "<tr>" + "".join(f"<td>{html.escape(value)}</td>" for value in values) + "</tr>"
        )
    html_rows.append("</table>")
    return "\n".join(rows), "".join(html_rows)


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
    request_text = f"{email_row.subject}\n{email_row.body_text}"
    human_selected_category = isinstance(
        analysis_facts.get("human_selected_category"),
        dict,
    )
    requires_review = human_selected_category or not settings.product_list_auto_send_enabled
    try:
        attachments, attachment_filename = _product_list_outbound_attachments(
            category=category,
            products=products,
            request_text=request_text,
            # Every AI-authored product-list email carries the exact verified
            # catalog as an attachment; the model only writes the surrounding
            # prose and cannot alter the catalog rows.
            default_file_format="xlsx",
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

    payment_requested = _payment_details_requested(request_text)
    payment = await _customer_payment_term(session, customer_id=case.customer_id) if payment_requested else None
    payment_term = payment.term if payment is not None else None
    payment_sentence = _payment_term_sentence(payment) if payment is not None else None

    file_format = requested_product_list_file_format(request_text) or "xlsx"
    try:
        draft_preview = await generate_product_list_ai_preview(
            settings=settings,
            subject=email_row.subject,
            contact_name=_reply_contact_name(case.contact.name, email_row.body_text),
            customer_message=email_row.body_text,
            category=category,
            products=products,
            attachment_filename=attachment_filename,
            actor="agent-runtime" if human_selected_category else "system",
            payment_sentence=payment_sentence,
        )
    except Exception as exc:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.AI_FAILURE,
            summary=f"Product-list AI drafting failed: {type(exc).__name__}",
            facts=analysis_facts,
            source_email_id=email_row.id,
            update_existing=human_selected_category,
        )
        return True

    if requires_review:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.PRODUCT_LIST_REVIEW,
            summary=f"AI product-list draft prepared for {category.name}; human approval is required",
            facts={
                **analysis_facts,
                "product_list_draft_status": "READY",
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
                    "human_confirmation_required": True,
                },
                "ai_draft_preview": draft_preview,
            },
            source_email_id=email_row.id,
            update_existing=True,
        )
        return True

    try:
        draft_body = _strip_duplicate_signature_lead(
            str(draft_preview["body_text"]),
            bundle.signature_text,
        )
        catalog_text, catalog_html = _verified_catalog_listing(products)
        text = "\n".join(
            [draft_body, "", catalog_text, "", bundle.signature_text.strip()]
        )
        html_body = (
            "".join(
                f"<p>{html.escape(line) if line else '&nbsp;'}</p>"
                for line in draft_body.splitlines()
            )
            + catalog_html
            + bundle.signature_html
        )
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
    outbox = await stage_outbox(
        session,
        case=case,
        message_kind="PRODUCT_LIST",
        subject=str(draft_preview["subject"]),
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
