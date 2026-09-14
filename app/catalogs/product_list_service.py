"""Product-list preparation, delivery, and human approval workflows."""

import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.service_core import _all_products_catalog_category, _customer_payment_term, _payment_details_requested
from app.db import EmailMessage, Handoff, Outbox, Product, ProductCategory, SalesCase
from app.domain import HandoffReason
from app.handoffs.human_reply_service import queue_human_reply
from app.mail import OutboundAttachment
from app.settings import get_settings

OFFICIAL_PRODUCT_CATALOG_FILENAME = "Catalog.pdf"


def official_product_catalog_attachment() -> OutboundAttachment:
    """Load the version-controlled official catalog without generating a file."""

    path = get_settings().content_dir / OFFICIAL_PRODUCT_CATALOG_FILENAME
    try:
        payload = path.read_bytes()
    except FileNotFoundError as exc:
        raise ValueError(f"official product catalog is missing: {path}") from exc
    if not payload.startswith(b"%PDF-"):
        raise ValueError("official product catalog is not a valid PDF")
    return OutboundAttachment(
        filename=OFFICIAL_PRODUCT_CATALOG_FILENAME,
        content_type="application/pdf",
        payload=payload,
    )


def official_product_catalog_sha256(attachment: OutboundAttachment) -> str:
    return hashlib.sha256(attachment.payload).hexdigest()


def _product_list_outbound_attachments(
    *,
    category: ProductCategory,
    products: list[Product],
    request_text: str,
    default_file_format: str | None = None,
) -> tuple[tuple[OutboundAttachment, ...], str | None]:
    # Keep the legacy arguments while callers migrate; attachment selection is
    # intentionally no longer affected by category or requested spreadsheet format.
    _ = category, products, request_text, default_file_format
    attachment = official_product_catalog_attachment()
    return (attachment,), attachment.filename


async def _validated_prepared_product_list(
    session: AsyncSession,
    *,
    handoff_id: int,
) -> tuple[Handoff, dict[str, Any], ProductCategory, list[Product]]:
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None:
        raise ValueError("handoff not found")
    prepared = (handoff.extracted_facts or {}).get("prepared_product_list")
    if handoff.reason_code != HandoffReason.PRODUCT_LIST_REVIEW.value or not isinstance(prepared, dict):
        raise ValueError("handoff has no prepared product-list draft")
    if prepared.get("scope") == "official_catalog":
        category = _all_products_catalog_category()
        products = []
    elif prepared.get("scope") == "all":
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
                    .order_by(ProductCategory.sort_order, Product.sort_order, Product.id)
                )
            ).all()
        )
    else:
        category_id = int(prepared.get("category_id") or 0)
        loaded_category = await session.get(ProductCategory, category_id)
        if loaded_category is None or not loaded_category.active:
            raise ValueError("prepared product category is missing or inactive")
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
    expected_ids = [int(value) for value in prepared.get("product_ids") or []]
    expected_codes = [str(value) for value in prepared.get("product_codes") or []]
    if [product.id for product in products] != expected_ids or [product.code for product in products] != expected_codes:
        raise ValueError("active product list changed after draft creation; regenerate the draft")
    return handoff, prepared, category, products


async def product_list_missing_business_facts(
    session: AsyncSession,
    *,
    handoff: Handoff,
    source_email: EmailMessage | None,
) -> list[str]:
    prepared = (handoff.extracted_facts or {}).get("prepared_product_list")
    if not isinstance(prepared, dict):
        return []
    missing = {str(value) for value in (prepared.get("missing_business_facts") or []) if str(value) != "payment_terms"}
    if source_email is not None and _payment_details_requested(f"{source_email.subject}\n{source_email.body_text}"):
        sales_case = await session.get(SalesCase, handoff.case_id) if handoff.case_id is not None else None
        if sales_case is None:
            missing.add("payment_terms")
        else:
            current = await _customer_payment_term(
                session,
                customer_id=sales_case.customer_id,
            )
            prepared_quote_id = prepared.get("payment_term_quote_id")
            if (
                str(prepared.get("payment_term") or "").strip().casefold() != current.term.casefold()
                or str(prepared.get("payment_term_source") or "") != current.source
                or (int(prepared_quote_id) if prepared_quote_id is not None else None) != current.quote_id
            ):
                missing.add("payment_terms")
    return sorted(missing)


async def prepared_product_list_attachment(
    session: AsyncSession,
    *,
    handoff_id: int,
) -> OutboundAttachment:
    """Build a review-only catalog download without creating delivery work."""

    _, prepared, _, _ = await _validated_prepared_product_list(
        session,
        handoff_id=handoff_id,
    )
    attachment = official_product_catalog_attachment()
    expected_hash = str(prepared.get("attachment_sha256") or "")
    if expected_hash and official_product_catalog_sha256(attachment) != expected_hash:
        raise ValueError("official product catalog changed after draft creation; regenerate the draft")
    return attachment


async def queue_prepared_product_list_reply(
    session: AsyncSession,
    *,
    handoff_id: int,
    subject: str,
    body_text: str,
    actor: str,
    note: str = "",
    resume_automation: bool = False,
) -> Outbox:
    """Approve a catalog draft after confirming its active product snapshot."""

    handoff, prepared, _, _ = await _validated_prepared_product_list(
        session,
        handoff_id=handoff_id,
    )
    source_email = await session.get(EmailMessage, handoff.source_email_id) if handoff.source_email_id is not None else None
    missing_business_facts = await product_list_missing_business_facts(
        session,
        handoff=handoff,
        source_email=source_email,
    )
    if missing_business_facts:
        raise ValueError(
            "prepared product-list reply is incomplete; missing approved business facts: "
            + ", ".join(str(value) for value in missing_business_facts)
        )
    attachment = official_product_catalog_attachment()
    expected_hash = str(prepared.get("attachment_sha256") or "")
    if not expected_hash:
        raise ValueError("prepared product-list draft predates the official PDF; regenerate the draft")
    if official_product_catalog_sha256(attachment) != expected_hash:
        raise ValueError("official product catalog changed after draft creation; regenerate the draft")
    return await queue_human_reply(
        session,
        handoff_id=handoff_id,
        subject=subject,
        body_text=body_text,
        actor=actor,
        note=note,
        resume_automation=resume_automation,
        attachments=(attachment,),
    )
