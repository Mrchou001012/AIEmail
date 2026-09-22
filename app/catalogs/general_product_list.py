"""General product list workflow."""

import html
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import InboundAnalysis
from app.catalogs.product_list_drafting import generate_product_list_ai_preview
from app.catalogs.product_list_service import (
    official_product_catalog_attachment,
    official_product_catalog_sha256,
)
from app.common.email_identity import (
    reply_contact_name as _reply_contact_name,
)
from app.common.email_identity import (
    strip_duplicate_signature_lead as _strip_duplicate_signature_lead,
)
from app.common.email_threading import _reply_references, _reply_source
from app.common.service_core import (
    _all_products_catalog_category,
    _customer_payment_term,
    _payment_details_requested,
    _payment_term_sentence,
    create_handoff,
    stage_outbox,
)
from app.db import EmailMessage, SalesCase
from app.domain import HandoffReason, SendContext, evaluate_send_policy
from app.imports import load_content
from app.mail import OutboundAttachment, append_quoted_reply
from app.settings import get_settings


async def _maybe_send_general_product_list(
    session: AsyncSession,
    *,
    case: SalesCase,
    email_row: EmailMessage,
    analysis: InboundAnalysis,
    analysis_facts: dict[str, Any],
) -> bool:
    """Reply to a product-list request with the official company PDF."""

    request_text = f"{email_row.subject}\n{email_row.body_text}"
    try:
        catalog_file = official_product_catalog_attachment()
    except ValueError as exc:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.PRODUCT_LIST_REVIEW,
            summary=str(exc),
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return True
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
            reason=send_decision.reason or HandoffReason.PRODUCT_LIST_REVIEW,
            summary="Generic product-list request requires human review",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return True
    category = _all_products_catalog_category()
    contact_name = _reply_contact_name(case.contact.name, email_row.body_text)
    payment_requested = _payment_details_requested(request_text)
    payment = await _customer_payment_term(session, customer_id=case.customer_id) if payment_requested else None
    payment_term = payment.term if payment is not None else None
    payment_sentence = _payment_term_sentence(payment) if payment is not None else None
    try:
        draft_preview = await generate_product_list_ai_preview(
            settings=settings,
            subject=email_row.subject,
            contact_name=contact_name,
            customer_message=email_row.body_text,
            category=category,
            products=[],
            attachment_filename=catalog_file.filename,
            actor="system",
            payment_sentence=payment_sentence,
        )
    except Exception as exc:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.AI_FAILURE,
            summary=f"Company-wide product-list AI drafting failed: {type(exc).__name__}",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return True
    draft_body = str(draft_preview["body_text"])
    subject = str(draft_preview["subject"])
    prepared = {
        "scope": "official_catalog",
        "category_id": None,
        "category_key": "all_products",
        "category_name": "All Products",
        "product_ids": [],
        "product_codes": [],
        "product_count": None,
        "category_breakdown": [],
        "file_format": "pdf",
        "attachment_kind": "official_catalog_pdf",
        "attachment_filename": catalog_file.filename,
        "attachment_sha256": official_product_catalog_sha256(catalog_file),
        "payment_requested": payment_requested,
        "payment_term": payment_term,
        "payment_term_source": payment.source if payment is not None else None,
        "payment_term_quote_id": payment.quote_id if payment is not None else None,
        "missing_business_facts": [],
    }
    if not settings.product_list_auto_send_enabled:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.PRODUCT_LIST_REVIEW,
            summary="Company-wide product catalog draft prepared; human approval is required",
            facts={
                **analysis_facts,
                "prepared_product_list": prepared,
                "ai_draft_preview": draft_preview,
            },
            source_email_id=email_row.id,
        )
        return True
    bundle = load_content(settings.content_dir)
    draft_body = _strip_duplicate_signature_lead(
        draft_body,
        bundle.signature_text,
    )
    signed_text = "\n".join([draft_body, "", bundle.signature_text.strip()])
    signed_html = "".join(f"<p>{html.escape(line) if line else '&nbsp;'}</p>" for line in draft_body.splitlines()) + bundle.signature_html
    source = _reply_source(email_row)
    signed_text, signed_html = append_quoted_reply(
        signed_text,
        signed_html,
        from_address=email_row.from_address,
        source_body=source.body_text,
        source_html=source.body_html,
        occurred_at=email_row.received_at,
    )
    outbox = await stage_outbox(
        session,
        case=case,
        message_kind="PRODUCT_LIST",
        subject=subject,
        text_body=signed_text,
        html_body=signed_html,
        business_key=f"inbound-product-list:{email_row.id}:all",
        in_reply_to=email_row.message_id,
        references=_reply_references(email_row),
        inline_images=source.inline_images,
        attachments=(
            OutboundAttachment(
                filename=catalog_file.filename,
                content_type=catalog_file.content_type,
                payload=catalog_file.payload,
            ),
        ),
    )
    return outbox is not None
