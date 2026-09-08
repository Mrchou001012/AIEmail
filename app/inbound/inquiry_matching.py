"""Inquiry matching workflow."""

import re
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalogs.product_catalog import active_category_keys, customer_interest_keys
from app.catalogs.products import canonical_product_code, find_product_codes, product_text_key
from app.common.service_core import NewInquiryResolution
from app.db import CaseStage, CaseStatus, Contact, Product, ProductCategory, SalesCase


def _explicit_product_codes(text: str) -> list[str]:
    codes = find_product_codes(text)
    explicit = []
    for value in re.findall(
        r"\b(?:SKU|PRODUCT)\s*[:#-]?\s*([A-Z0-9][A-Z0-9_()%.\-]{1,63})",
        text,
        flags=re.IGNORECASE,
    ):
        # "PRODUCT LIST/CATALOG/BROCHURE/PRODUCTS" are category-catalog
        # requests, not explicit product codes. Only keep candidates that
        # look like real product codes (digit-bearing tokens, or brand-style
        # prefixes from the catalog) so prose fragments such as "LIST" or
        # "S." (from "PRODUCTS") are not treated as SKUs.
        candidate = value.rstrip(".,;:!?")
        if candidate.upper() in {
            "LIST",
            "LISTS",
            "CATALOG",
            "CATALOGUE",
            "BROCHURE",
            "RANGE",
            "PORTFOLIO",
        }:
            continue
        if re.search(r"\d", candidate) or re.match(
            r"^(YAC|LANNOX|UV|SBM|DBM|CAA|ZAA|THEIC|AAA)[-_A-Z0-9]*$",
            candidate,
            re.I,
        ):
            explicit.append(candidate)
    return list(dict.fromkeys([*codes, *(canonical_product_code(value) for value in explicit)]))


def _product_lookup_conditions(codes: list[str]) -> Any:
    """Match catalog codes by exact value or canonical normalization.

    Product codes are normalized with the same separator-insensitive key used
    for lookups, so WIDGET-100, WIDGET_100 and widget_100 all resolve to the
    same catalog row. Exact equality is kept as a fast path.
    """
    conditions = []
    for code in codes:
        conditions.append(Product.code == code)
        conditions.append(func.lower(func.regexp_replace(Product.code, r"[^a-zA-Z0-9]", "_", "g")) == product_text_key(code))
    return or_(*conditions)


def _prior_thread_marker(text: str) -> str | None:
    lowered = text.casefold()
    return next((marker for marker in PRIOR_THREAD_MARKERS if marker in lowered), None)


async def _resolve_category_inquiry_case(
    session: AsyncSession,
    *,
    contact: Contact,
    incoming_subject_key: str,
    facts: dict[str, Any],
) -> NewInquiryResolution | None:
    """Create or reuse a category case when the CRM knows exactly one interest."""
    interest_keys = customer_interest_keys(contact.customer)
    active_keys = await active_category_keys(session)
    known = [key for key in interest_keys if key in active_keys]
    if len(known) != 1:
        facts.update(
            {
                "interest_categories": interest_keys,
                "active_interest_categories": known,
            }
        )
        return None
    category_key = known[0]
    category = await session.scalar(
        select(ProductCategory).where(
            ProductCategory.key == category_key,
            ProductCategory.active.is_(True),
        )
    )
    if category is None:
        return None
    customer_currency_rows = await session.execute(
        select(SalesCase.currency).where(
            SalesCase.customer_id == contact.customer_id,
            SalesCase.status.not_in([CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST]),
        )
    )
    customer_currencies = set(customer_currency_rows.scalars().all())
    currency = next(iter(customer_currencies)) if len(customer_currencies) == 1 else "USD"
    existing = await session.scalar(
        select(SalesCase)
        .where(
            SalesCase.contact_id == contact.id,
            SalesCase.category_id == category.id,
            SalesCase.status == CaseStatus.ACTIVE,
        )
        .order_by(SalesCase.id.desc())
    )
    if existing is not None:
        return NewInquiryResolution(
            existing,
            facts={
                **facts,
                "category_id": category.id,
                "category_key": category.key,
                "currency": currency,
                "match_basis": "customer_interest_category",
                "reused_category_case": True,
            },
        )
    sales_case = SalesCase(
        customer_id=contact.customer_id,
        contact_id=contact.id,
        product_id=None,
        category_id=category.id,
        currency=currency,
        stage=CaseStage.QUOTING,
        status=CaseStatus.ACTIVE,
        subject_key=incoming_subject_key,
    )
    session.add(sales_case)
    await session.flush()
    return NewInquiryResolution(
        sales_case,
        facts={
            **facts,
            "category_id": category.id,
            "category_key": category.key,
            "currency": currency,
            "match_basis": "customer_interest_category",
        },
    )


PRIOR_THREAD_MARKERS = (
    "previous quote",
    "previous quotation",
    "earlier quote",
    "earlier quotation",
    "last quote",
    "last price",
    "as discussed",
    "as agreed",
    "same as before",
    "revised quote",
    "revised quotation",
    "revise your quote",
    "follow up on",
    "our previous conversation",
    "your previous offer",
)
