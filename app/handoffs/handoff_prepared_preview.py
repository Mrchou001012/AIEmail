"""Handoff prepared preview workflow."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import InboundAnalysis, generic_product_list_requested
from app.catalogs.product_catalog import build_product_list_attachment, render_product_list_email
from app.coa.coa_preview import prepare_detected_coa_preview
from app.common.email_identity import reply_contact_name as _reply_contact_name
from app.common.service_core import (
    _all_products_catalog_category,
    _catalog_category_breakdown,
    _customer_payment_term,
    _payment_details_requested,
    _payment_term_sentence,
)
from app.db import EmailMessage, Handoff, Product, ProductCategory, SalesCase
from app.domain import HandoffReason, Intent
from app.imports import load_content
from app.settings import Settings


async def _prepared_handoff_draft_preview(
    session: AsyncSession,
    *,
    handoff: Handoff,
    source_email: EmailMessage,
    sales_case: SalesCase,
    analysis: InboundAnalysis,
    actor: str,
    settings: Settings,
) -> dict[str, Any] | None:
    """Use deterministic prepared work before falling back to free-form AI.

    The review button used to discard prepared catalog/quote/COA facts and send
    an empty commercial-facts object to the model.  Besides producing vague
    holding replies, that made an old product-list handoff look as though the
    application had no catalog.  This helper upgrades generic legacy catalog
    handoffs in place and preserves every already-prepared typed draft.
    """

    stored_facts = dict(handoff.extracted_facts or {})
    if analysis.intent == Intent.COA_REQUEST or analysis.coa_requested:
        result = await prepare_detected_coa_preview(
            session,
            handoff=handoff,
            email_row=source_email,
            sales_case=sales_case,
            analysis=analysis,
            analysis_metadata={},
            actor=actor,
            settings=settings,
            persist=False,
        )
        return result["preview"]
    stored_preview = stored_facts.get("ai_draft_preview")
    prepared_keys = (
        "prepared_coa",
        "prepared_product_list",
        "prepared_quote",
        "prepared_multi_quote",
    )
    has_prepared_work = any(isinstance(stored_facts.get(key), dict) for key in prepared_keys)
    request_text = f"{source_email.subject}\n{source_email.body_text}"
    generic_catalog_request = bool(analysis.product_list_requested and generic_product_list_requested(request_text))

    if not has_prepared_work and not generic_catalog_request:
        return None

    prepared_product_list = stored_facts.get("prepared_product_list")
    if generic_catalog_request and not isinstance(prepared_product_list, dict):
        products = list(
            (
                await session.scalars(
                    select(Product)
                    .join(ProductCategory, Product.category_id == ProductCategory.id)
                    .where(
                        Product.active.is_(True),
                        Product.catalog_visible.is_(True),
                        ProductCategory.active.is_(True),
                    )
                    .order_by(
                        ProductCategory.sort_order,
                        Product.sort_order,
                        Product.id,
                    )
                )
            ).all()
        )
        if not products:
            raise ValueError("客户要求产品清单，但当前没有已启用的产品目录；请先导入或启用产品数据")
        prepared_product_list = {
            "scope": "all",
            "category_id": None,
            "category_key": "all_products",
            "category_name": "All Products",
            "product_ids": [product.id for product in products],
            "product_codes": [product.code for product in products],
            "file_format": "xlsx",
        }
        stored_facts["prepared_product_list"] = prepared_product_list

    missing_business_facts: list[str] = []
    if isinstance(prepared_product_list, dict):
        file_format: str | None
        if prepared_product_list.get("scope") == "all":
            category = _all_products_catalog_category()
            products = list(
                (
                    await session.scalars(
                        select(Product)
                        .join(ProductCategory, Product.category_id == ProductCategory.id)
                        .where(
                            Product.active.is_(True),
                            Product.catalog_visible.is_(True),
                            ProductCategory.active.is_(True),
                        )
                        .order_by(
                            ProductCategory.sort_order,
                            Product.sort_order,
                            Product.id,
                        )
                    )
                ).all()
            )
            file_format = "xlsx"
        else:
            category_id = int(prepared_product_list.get("category_id") or 0)
            loaded_category = await session.get(ProductCategory, category_id)
            if loaded_category is None or not loaded_category.active:
                raise ValueError("已准备的产品分类不存在或已经停用")
            category = loaded_category
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
            raw_file_format = prepared_product_list.get("file_format")
            file_format = str(raw_file_format) if raw_file_format is not None else None
        if not products:
            raise ValueError("已准备的产品清单中没有启用的产品")

        attachment_filename: str | None = None
        if file_format is not None:
            catalog_file = build_product_list_attachment(
                category=category,
                products=products,
                file_format=str(file_format),
            )
            attachment_filename = catalog_file.filename
        contact_name = _reply_contact_name(
            sales_case.contact.name,
            source_email.body_text,
        )
        subject = source_email.subject if source_email.subject.casefold().startswith("re:") else f"Re: {source_email.subject}"
        if prepared_product_list.get("scope") == "all":
            body_text = (
                f"Dear {contact_name},\n\n"
                "Thank you for your email. Please find attached our current English product "
                "catalogue, including the approved product codes, product names, CAS numbers, "
                "and available content specifications.\n\n"
                "Please let us know the products and quantities you require so we can confirm "
                "current availability and pricing."
            )
            model = "active-full-product-catalog-v1"
        else:
            bundle = load_content(settings.content_dir)
            rendered_text, _ = render_product_list_email(
                contact_name=contact_name,
                category=category,
                products=products,
                subject=source_email.subject,
                signature_text=bundle.signature_text,
                signature_html=bundle.signature_html,
                attachment_filename=attachment_filename,
            )
            signature_text = bundle.signature_text.strip()
            body_text = (
                rendered_text[: -len(signature_text)].rstrip()
                if signature_text and rendered_text.endswith(signature_text)
                else rendered_text.rstrip()
            )
            model = "active-product-catalog-v1"

        payment_requested = _payment_details_requested(request_text)
        payment_term: str | None = None
        payment_term_source: str | None = None
        payment_term_quote_id: int | None = None
        if payment_requested:
            payment = await _customer_payment_term(
                session,
                customer_id=sales_case.customer_id,
            )
            payment_term = payment.term
            payment_term_source = payment.source
            payment_term_quote_id = payment.quote_id
            body_text = f"{body_text.rstrip()}\n\n{_payment_term_sentence(payment)}"

        prepared_product_list = {
            **prepared_product_list,
            "product_ids": [product.id for product in products],
            "product_codes": [product.code for product in products],
            "product_count": len(products),
            "category_breakdown": await _catalog_category_breakdown(session, products),
            "file_format": file_format,
            "attachment_filename": attachment_filename,
            "payment_requested": payment_requested,
            "payment_term": payment_term,
            "payment_term_source": payment_term_source,
            "payment_term_quote_id": payment_term_quote_id,
            "missing_business_facts": missing_business_facts,
        }
        stored_facts["prepared_product_list"] = prepared_product_list
        handoff.reason_code = HandoffReason.PRODUCT_LIST_REVIEW.value
        handoff.summary = "Product catalog draft prepared; human approval is required"
        stored_preview = {
            "subject": subject,
            "body_text": body_text,
            "provider": "deterministic-product-list",
            "model": model,
            "rag_matches": [],
        }

    if not isinstance(stored_preview, dict):
        return None
    subject = str(stored_preview.get("subject") or "").strip()
    body_text = str(stored_preview.get("body_text") or "").strip()
    if not subject or not body_text:
        return None

    generated_at = datetime.now(UTC)
    preview_facts = {
        **stored_preview,
        "subject": subject[:998],
        "body_text": body_text[:50_000],
        "generated_at": generated_at.isoformat(),
        "generated_by": actor,
        "delivery_created": False,
        "rag_enabled": False,
        "rag_matches": [],
        "missing_business_facts": missing_business_facts,
    }
    stored_facts["ai_draft_preview"] = preview_facts
    handoff.extracted_facts = stored_facts
    return preview_facts
