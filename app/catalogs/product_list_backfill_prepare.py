"Historical product-list request backfill workflow."

from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.ai import explicit_product_list_requested, requested_product_list_file_format, stub_analyze
from app.catalogs.backfill_types import BackfillCandidate
from app.catalogs.product_catalog import customer_interest_keys, render_product_list_email
from app.catalogs.product_list_service import _product_list_outbound_attachments
from app.catalogs.products import canonical_product_code, product_codes_match
from app.common.email_identity import reply_contact_name as _reply_contact_name
from app.common.email_threading import _reply_source
from app.db import (
    CaseStage,
    CaseStatus,
    Contact,
    DeliveryStatus,
    EmailAddressStatus,
    EmailMessage,
    Handoff,
    Outbox,
    Product,
    ProductCategory,
    SalesCase,
)
from app.domain import Intent, SendContext, evaluate_send_policy
from app.imports import load_content
from app.mail import append_quoted_reply, attachments_require_review
from app.settings import Settings


async def prepare_backfill_candidate(
    session: AsyncSession,
    *,
    handoff: Handoff,
    include_history: bool,
    cutoff: datetime,
    company_research: bool,
    settings: Settings,
    categories_by_id: dict[int, ProductCategory],
    categories_by_key: dict[str, ProductCategory],
    exclude: Callable[..., None],
) -> BackfillCandidate | None:
    """Check one historical request and prepare a reply without changing business state."""
    prepared: dict[str, Any] = {}
    source = await session.get(EmailMessage, handoff.source_email_id)
    if source is None:
        exclude(handoff, "SOURCE_EMAIL_MISSING")
        return None
    if source.direction != "INBOUND" or (source.is_history and not include_history):
        exclude(handoff, "NOT_LIVE_INBOUND")
        return None
    if source.received_at < cutoff:
        exclude(
            handoff,
            "OLDER_THAN_MAX_AGE",
            received_at=source.received_at.isoformat(),
        )
        return None
    if source.is_bounce or source.is_automated_reply:
        exclude(handoff, "NON_CUSTOMER_MESSAGE")
        return None
    request_text = f"{source.subject}\n{source.body_text}"
    if not explicit_product_list_requested(request_text):
        exclude(handoff, "NOT_EXPLICIT_PRODUCT_LIST_REQUEST")
        return None
    if attachments_require_review(source.attachment_metadata, source.body_html):
        exclude(handoff, "RISKY_ATTACHMENT")
        return None
    if not source.message_id:
        exclude(handoff, "SOURCE_MESSAGE_ID_MISSING")
        return None

    case_id = handoff.case_id or source.case_id
    sales_case = (
        await session.scalar(
            select(SalesCase)
            .options(
                selectinload(SalesCase.customer),
                selectinload(SalesCase.contact),
                selectinload(SalesCase.product),
                selectinload(SalesCase.category),
            )
            .where(SalesCase.id == case_id)
        )
        if case_id is not None
        else None
    )
    facts = dict(handoff.extracted_facts or {})
    contact_id = sales_case.contact_id if sales_case is not None else source.contact_id or facts.get("contact_id")
    contact = (
        sales_case.contact
        if sales_case is not None
        else await session.scalar(select(Contact).options(selectinload(Contact.customer)).where(Contact.id == contact_id))
    )
    if contact is None:
        exclude(handoff, "CONTACT_NOT_UNIQUE_OR_MISSING")
        return None
    customer = sales_case.customer if sales_case is not None else contact.customer
    if source.from_address.strip().casefold() != contact.email.strip().casefold():
        exclude(handoff, "SOURCE_CONTACT_MISMATCH")
        return None
    address_status = await session.get(
        EmailAddressStatus,
        contact.email.strip().casefold(),
    )
    if (
        contact.suppressed
        or customer.do_not_contact
        or not customer.auto_send_allowed
        or (address_status is not None and address_status.suppressed)
    ):
        exclude(handoff, "RECIPIENT_NOT_AUTHORIZED")
        return None

    category = None
    research_required = False
    if sales_case is not None:
        category = sales_case.category
        if category is None and sales_case.product is not None and sales_case.product.category_id is not None:
            category = categories_by_id.get(sales_case.product.category_id)
    if category is None:
        interest_categories = [categories_by_key[key] for key in customer_interest_keys(customer) if key in categories_by_key]
        interest_categories = list({item.id: item for item in interest_categories}.values())
        if len(interest_categories) != 1:
            if (
                company_research
                and not interest_categories
                and sales_case is not None
                and sales_case.product_id is None
                and sales_case.category_id is None
            ):
                if not settings.company_research_enabled:
                    exclude(handoff, "COMPANY_RESEARCH_DISABLED")
                    return None
                research_required = True
            else:
                exclude(
                    handoff,
                    "INTEREST_CATEGORY_NOT_UNIQUE",
                    category_keys=[item.key for item in interest_categories],
                )
                return None
        else:
            category = interest_categories[0]
    if category is not None and category.id not in categories_by_id:
        exclude(handoff, "CATEGORY_INACTIVE")
        return None

    existing_product_list = await session.scalar(
        select(Outbox).where(
            Outbox.business_key == f"inbound-product-list:{source.id}",
        )
    )
    if existing_product_list is not None and existing_product_list.status == DeliveryStatus.CANCELLED:
        exclude(
            handoff,
            "PREVIOUS_PRODUCT_LIST_CANCELLED",
            outbox_id=existing_product_list.id,
        )
        return None
    approved_reply = await session.scalar(select(Outbox.id).where(Outbox.approval_handoff_id == handoff.id))
    exact_thread_reply = await session.scalar(
        select(EmailMessage.id).where(
            EmailMessage.direction == "OUTBOUND",
            EmailMessage.in_reply_to == source.message_id,
        )
    )
    if approved_reply is not None or (exact_thread_reply is not None and existing_product_list is None):
        exclude(handoff, "ALREADY_REPLIED")
        return None

    if sales_case is not None:
        if sales_case.status not in {CaseStatus.ACTIVE, CaseStatus.WAITING_HUMAN}:
            exclude(
                handoff,
                "CASE_STATUS_UNSAFE",
                case_id=sales_case.id,
                case_status=sales_case.status.value,
            )
            return None
        if sales_case.stage not in {CaseStage.QUOTING, CaseStage.FOLLOW_UP}:
            exclude(
                handoff,
                "CASE_STAGE_UNSAFE",
                case_id=sales_case.id,
                case_stage=sales_case.stage.value,
            )
            return None
        other_open_handoffs = await session.scalar(
            select(func.count())
            .select_from(Handoff)
            .where(
                Handoff.case_id == sales_case.id,
                Handoff.status == "OPEN",
                Handoff.id != handoff.id,
            )
        )
        if other_open_handoffs:
            exclude(
                handoff,
                "CASE_HAS_OTHER_OPEN_HANDOFFS",
                case_id=sales_case.id,
                count=other_open_handoffs,
            )
            return None

    analysis = stub_analyze(
        source.subject,
        source.body_text,
        source.attachment_metadata,
    ).model_copy(update={"risky_attachment": False})
    matched_product = None
    if existing_product_list is None and research_required:
        if analysis.intent != Intent.PRODUCT_LIST_REQUEST:
            exclude(
                handoff,
                "UNSAFE_PRODUCT_LIST_INTENT",
                detected_intent=analysis.intent.value,
            )
            return None
        if analysis.product_code is not None:
            exclude(
                handoff,
                "SPECIFIC_PRODUCT_REQUIRES_CATALOG_MATCH",
                detected_product_code=canonical_product_code(analysis.product_code),
            )
            return None
    if existing_product_list is None and not research_required:
        if analysis.intent != Intent.PRODUCT_LIST_REQUEST:
            exclude(
                handoff,
                "UNSAFE_PRODUCT_LIST_INTENT",
                detected_intent=analysis.intent.value,
            )
            return None
        if analysis.product_code is not None:
            canonical_code = canonical_product_code(analysis.product_code)
            matched_product = await session.scalar(
                select(Product).where(
                    Product.code == canonical_code,
                    Product.active.is_(True),
                )
            )
            if matched_product is None:
                exclude(
                    handoff,
                    "SPECIFIC_PRODUCT_NOT_ACTIVE",
                    detected_product_code=canonical_code,
                )
                return None
            if matched_product.category_id != category.id:
                exclude(
                    handoff,
                    "SPECIFIC_PRODUCT_CATEGORY_MISMATCH",
                    detected_product_code=matched_product.code,
                    product_category_id=matched_product.category_id,
                    selected_category_id=category.id,
                )
                return None
            if (
                sales_case is not None
                and sales_case.product is not None
                and not product_codes_match(
                    matched_product.code,
                    sales_case.product.code,
                )
            ):
                exclude(
                    handoff,
                    "CASE_PRODUCT_MISMATCH",
                    detected_product_code=matched_product.code,
                    case_product_code=sales_case.product.code,
                )
                return None

        planned_status = CaseStatus.ACTIVE if sales_case is None or sales_case.status == CaseStatus.WAITING_HUMAN else sales_case.status
        send_decision = evaluate_send_policy(
            SendContext(
                intent=analysis.intent,
                stage=(sales_case.stage if sales_case is not None else CaseStage.QUOTING),
                status=planned_status,
                intent_confidence=analysis.intent_confidence,
                product_confidence=1.0,
                numeric_confidence=1.0,
                auto_send_allowed=customer.auto_send_allowed,
                contact_suppressed=contact.suppressed,
                do_not_contact=customer.do_not_contact,
                has_risky_attachment=analysis.risky_attachment,
                product_known=(analysis.product_code is None or matched_product is not None),
                prebook_requested=analysis.prebook_requested,
                packaging_requested=analysis.packaging_requested,
                delivery_requested=analysis.shipping_requested,
            ),
            intent_threshold=settings.intent_confidence_threshold,
            product_threshold=settings.product_confidence_threshold,
            numeric_threshold=settings.numeric_confidence_threshold,
        )
        if not send_decision.allow_send:
            exclude(
                handoff,
                "SEND_POLICY_BLOCKED",
                policy_reason=(send_decision.reason.value if send_decision.reason is not None else None),
            )
            return None

        products = list(
            (
                await session.scalars(
                    select(Product)
                    .where(
                        Product.category_id == category.id,
                        Product.active.is_(True),
                        Product.catalog_visible.is_(True),
                    )
                    .order_by(Product.sort_order, Product.id)
                )
            ).all()
        )
        if not products:
            exclude(handoff, "CATEGORY_HAS_NO_ACTIVE_PRODUCTS")
            return None
        try:
            bundle = load_content(settings.content_dir)
            attachments, attachment_filename = _product_list_outbound_attachments(
                category=category,
                products=products,
                request_text=request_text,
            )
            text_body, html_body = render_product_list_email(
                contact_name=_reply_contact_name(contact.name, source.body_text),
                category=category,
                products=products,
                subject=source.subject,
                signature_text=bundle.signature_text,
                signature_html=bundle.signature_html,
                attachment_filename=attachment_filename,
            )
            reply_source = _reply_source(source)
            text_body, html_body = append_quoted_reply(
                text_body,
                html_body,
                from_address=source.from_address,
                source_body=reply_source.body_text,
                source_html=reply_source.body_html,
                occurred_at=source.received_at,
            )
        except Exception as exc:
            exclude(
                handoff,
                "REPLY_SOURCE_OR_RENDER_UNAVAILABLE",
                error_type=type(exc).__name__,
                detail=str(exc)[:500],
            )
            return None
        prepared = {
            "analysis": analysis,
            "matched_product": matched_product,
            "subject": (f"Re: {source.subject}" if source.subject.strip() else f"Our {category.name} product list"),
            "text_body": text_body,
            "html_body": html_body,
            "inline_images": reply_source.inline_images,
            "attachments": attachments,
            "attachment_filename": attachment_filename,
            "product_count": len(products),
        }

    candidate = {
        "handoff_id": handoff.id,
        "email_id": source.id,
        "case_id": sales_case.id if sales_case is not None else None,
        "customer_id": customer.id,
        "contact_id": contact.id,
        "recipient": contact.email,
        "subject": source.subject,
        "received_at": source.received_at.isoformat(),
        "category_id": category.id if category is not None else None,
        "category_key": category.key if category is not None else None,
        "company_research_required": research_required,
        "detected_intent": analysis.intent.value,
        "detected_product_code": analysis.product_code,
        "matched_product_id": (matched_product.id if matched_product is not None else None),
        "requested_file_format": requested_product_list_file_format(request_text),
        "existing_outbox_id": (existing_product_list.id if existing_product_list is not None else None),
    }
    return BackfillCandidate(
        handoff=handoff,
        source=source,
        sales_case=sales_case,
        contact=contact,
        customer=customer,
        category=category,
        analysis=analysis,
        research_required=research_required,
        existing_product_list=existing_product_list,
        prepared=prepared,
        candidate=candidate,
    )
