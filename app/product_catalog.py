"""Product catalog storage, category classification, and product-list mail rendering.

The catalog is curated in ``config/product_catalog.yaml`` and upserted into the
database with :func:`import_product_catalog`. Customer interest categories come
from the original CRM workbook ("需要的Lanya产品" column) and are matched with
:func:`classify_category_interests`; the inbound pipeline then replies with the
matching category product list without a human handoff.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AuditEvent, Customer, Product, ProductCategory
from app.products import canonical_product_code, product_text_key

DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[1] / "config" / "product_catalog.yaml"

# Order matters: classification returns category keys in this order.
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "industrial_silanes": (
        "工业硅烷",
        "硅烷偶联剂",
        "有机硅",
        "industrial silanes",
        "industrial silane",
        "organosilane",
        "silane coupling",
        "硅烷",
        "silanes",
        "silicone",
    ),
    "pharmaceutical": (
        "医药",
        "制药",
        "药物",
        "原料药",
        "pharmaceutical intermediate",
        "api intermediate",
        "pharmaceutical",
        "pharma",
    ),
    "rubber_plastics": (
        "橡塑",
        "rubber & plastics",
        "rubber and plastics",
        "pvc stabilizer",
        "heat stabilizer",
        "uv stabilizer",
        "antioxidant",
        "橡胶",
        "塑料",
        "rubber",
        "plastic",
        "pvc",
    ),
}

INTEREST_METADATA_KEY = "interests"


def classify_category_interests(text: str) -> list[str]:
    """Return product-category keys mentioned in untrusted customer text."""
    lowered = (text or "").casefold()
    matched: list[str] = []
    for key, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword.casefold() in lowered for keyword in keywords):
            matched.append(key)
    return list(dict.fromkeys(matched))


def interest_entry(
    *,
    category_key: str,
    category_name: str,
    source: str,
    value: str,
    source_row: int | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "category_key": category_key,
        "category_name": category_name,
        "source": source,
        "value": value,
    }
    if source_row is not None:
        entry["source_row"] = source_row
    return entry


def customer_interests(customer: Customer) -> list[dict[str, Any]]:
    metadata = dict(customer.metadata_json or {})
    return [
        entry
        for entry in metadata.get(INTEREST_METADATA_KEY) or []
        if isinstance(entry, dict) and entry.get("category_key")
    ]


def customer_interest_keys(customer: Customer) -> list[str]:
    return list(
        dict.fromkeys(
            str(entry["category_key"])
            for entry in customer_interests(customer)
            if entry.get("category_key")
        )
    )


def merge_customer_interests(
    customer: Customer,
    entries: Iterable[dict[str, Any]],
) -> None:
    """Merge interest entries into ``Customer.metadata_json`` without duplicates."""
    metadata = dict(customer.metadata_json or {})
    existing = [
        entry
        for entry in metadata.get(INTEREST_METADATA_KEY) or []
        if isinstance(entry, dict)
    ]
    known = {
        (
            str(entry.get("category_key")),
            str(entry.get("source")),
            str(entry.get("value")),
        ): index
        for index, entry in enumerate(existing)
    }
    for entry in entries:
        key = (
            str(entry.get("category_key")),
            str(entry.get("source")),
            str(entry.get("value")),
        )
        if key in known:
            existing[known[key]] = entry
        else:
            existing.append(entry)
            known[key] = len(existing) - 1
    metadata[INTEREST_METADATA_KEY] = existing[-50:]
    customer.metadata_json = metadata


def load_catalog_yaml(path: Path = DEFAULT_CATALOG_PATH) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def _optional_catalog_value(
    item: dict[str, Any],
    key: str,
    current: str | None = None,
) -> str | None:
    """Apply explicit catalog values while preserving fields that are omitted."""
    if key not in item:
        return current
    return str(item.get(key) or "").strip() or None


def _catalog_categories(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    categories: dict[str, dict[str, Any]] = {}
    for item in payload.get("categories") or []:
        key = str(item.get("key") or "").strip()
        if not key:
            raise ValueError("catalog category requires key")
        if key in categories:
            raise ValueError(f"duplicate catalog category: {key}")
        categories[key] = item
    return categories


async def import_product_catalog(
    session: AsyncSession,
    *,
    path: Path = DEFAULT_CATALOG_PATH,
    apply: bool = True,
    actor: str = "product-catalog-import",
) -> dict[str, int]:
    """Upsert categories and products from the curated YAML catalog."""
    payload = load_catalog_yaml(path)
    categories = _catalog_categories(payload)
    products = payload.get("products") or []
    if not isinstance(products, list):
        raise ValueError("catalog products must be a list")

    result: dict[str, int] = {
        "categories_created": 0,
        "categories_updated": 0,
        "products_created": 0,
        "products_updated": 0,
        "products_inactive": 0,
    }
    if not apply:
        seen_codes: set[str] = set()
        for item in products:
            if not isinstance(item, dict):
                raise ValueError("catalog product entries must be objects")
            raw_code = str(item.get("code") or "").strip()
            if not raw_code:
                raise ValueError("catalog product requires code")
            category_key = str(item.get("category") or "").strip()
            if category_key not in categories:
                raise ValueError(
                    f"catalog product {raw_code} references unknown category {category_key}"
                )
            code = canonical_product_code(raw_code)
            if code in seen_codes:
                raise ValueError(
                    f"duplicate catalog product code after normalization: {raw_code}"
                )
            seen_codes.add(code)
            if not str(item.get("name") or "").strip():
                raise ValueError(f"catalog product {code} requires name")
        result["categories_created"] = len(categories)
        result["products_created"] = len(products)
        return result

    category_rows = (await session.execute(select(ProductCategory))).scalars().all()
    category_by_key = {category.key: category for category in category_rows}
    for sort_order, (key, item) in enumerate(categories.items(), start=1):
        category = category_by_key.get(key)
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError(f"catalog category {key} requires name")
        if category is None:
            category = ProductCategory(
                key=key,
                name=name,
                name_zh=str(item.get("name_zh") or "").strip() or None,
                sort_order=sort_order,
                active=True,
            )
            session.add(category)
            category_by_key[key] = category
            result["categories_created"] += 1
        else:
            category.name = name
            category.name_zh = str(item.get("name_zh") or "").strip() or category.name_zh
            category.sort_order = sort_order
            category.active = True
            result["categories_updated"] += 1
    await session.flush()

    product_rows = (await session.execute(select(Product))).scalars().all()
    product_by_code = {product.code: product for product in product_rows}
    seen_codes: set[str] = set()
    for sort_order, item in enumerate(products, start=1):
        if not isinstance(item, dict):
            raise ValueError("catalog product entries must be objects")
        raw_code = str(item.get("code") or "").strip()
        if not raw_code:
            raise ValueError("catalog product requires code")
        category_key = str(item.get("category") or "").strip()
        if category_key not in categories:
            raise ValueError(f"catalog product {raw_code} references unknown category {category_key}")
        code = canonical_product_code(raw_code)
        if code in seen_codes:
            raise ValueError(f"duplicate catalog product code after normalization: {raw_code}")
        seen_codes.add(code)
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError(f"catalog product {code} requires name")
        category = category_by_key[category_key]
        product = product_by_code.get(code)
        if product is None:
            product = Product(
                code=code,
                name=name,
                unit="kg",
                approved_text_key=product_text_key(code),
                active=True,
                category_id=category.id,
                brand=str(item.get("brand") or "").strip() or None,
                cas_no=str(item.get("cas_no") or "").strip() or None,
                content=str(item.get("content") or "").strip() or None,
                series=str(item.get("series") or "").strip() or None,
                sort_order=sort_order,
            )
            session.add(product)
            product_by_code[code] = product
            result["products_created"] += 1
        else:
            product.name = name
            product.category_id = category.id
            product.brand = _optional_catalog_value(item, "brand", product.brand)
            product.cas_no = _optional_catalog_value(item, "cas_no", product.cas_no)
            product.content = _optional_catalog_value(item, "content", product.content)
            product.series = _optional_catalog_value(item, "series", product.series)
            product.sort_order = sort_order
            product.active = True
            result["products_updated"] += 1
    await session.flush()

    session.add(
        AuditEvent(
            case_id=None,
            actor=actor,
            event_type="catalog.product_catalog_imported",
            data=result,
        )
    )
    await session.commit()
    return result


def _product_rows(products: Iterable[Product]) -> list[Product]:
    return sorted(
        products,
        key=lambda product: (product.sort_order or 0, product.id or 0),
    )


def _grouped_products(products: list[Product]) -> list[tuple[str | None, list[Product]]]:
    groups: list[tuple[str | None, list[Product]]] = []
    current_series: str | None = None
    current: list[Product] = []
    for product in products:
        if product.series != current_series:
            if current:
                groups.append((current_series, current))
            current_series = product.series
            current = []
        current.append(product)
    if current:
        groups.append((current_series, current))
    return groups


def _value(value: str | None) -> str:
    return value.strip() if value and value.strip() else "-"


def validate_product_list_email(text: str) -> None:
    if re.search(
        r"(?i)(?<![A-Z0-9])(?:USD|EUR|CNY|INR|Rs\.?|€|£)\s*"
        r"\d+(?:,[0-9]{3})*(?:\.\d+)?|"
        r"\d+(?:,[0-9]{3})*(?:\.\d+)?\s*(?:USD|EUR|CNY|INR|Rs\.?)",
        text,
    ):
        raise ValueError("product list email must not contain monetary values")
    forbidden = (
        "guarantee",
        "binding commitment",
        "we accept your order",
        "shipment confirmed",
        "please find attached",
        "quotation attached",
    )
    if any(term in text.casefold() for term in forbidden):
        raise ValueError("product list email contains an unsupported commitment or attachment claim")


def render_product_list_email(
    *,
    contact_name: str,
    category: ProductCategory,
    products: Iterable[Product],
    subject: str,
    signature_text: str,
    signature_html: str,
) -> tuple[str, str]:
    """Render a deterministic, price-free product-list reply for one category."""
    rows = _product_rows(products)
    greeting = f"Dear {contact_name.strip() or 'Customer'},"
    opening = f"Thank you for your interest in our {category.name}."
    lead_in = (
        "Please find below our current product list. For pricing, availability, "
        "or samples, please reply and our team will assist you."
    )
    body_lines: list[str] = [greeting, "", opening, lead_in]
    groups = _grouped_products(rows)
    for series, group in groups:
        if series:
            body_lines.extend(["", series])
        body_lines.append("No. | Code | Product Name | CAS No. | Content")
        for number, product in enumerate(group, start=1):
            body_lines.append(
                " | ".join(
                    [
                        str(number),
                        _value(product.code),
                        _value(product.name),
                        _value(product.cas_no),
                        _value(product.content),
                    ]
                )
            )
    business_text = "\n".join(body_lines)
    validate_product_list_email(business_text)
    text = "\n".join([business_text, "", signature_text.strip()])

    html_parts = [
        "<p>"
        + "</p><p>".join(
            html.escape(line) if line else "&nbsp;"
            for line in [greeting, opening, lead_in]
        )
        + "</p>"
    ]
    for series, group in groups:
        if series:
            html_parts.append(f"<h3>{html.escape(series)}</h3>")
        rows_html = [
            "<table border=\"1\" cellpadding=\"4\" cellspacing=\"0\" "
            "style=\"border-collapse:collapse\">",
            "<tr><th align=\"left\">No.</th><th align=\"left\">Code</th>"
            "<th align=\"left\">Product Name</th><th align=\"left\">CAS No.</th>"
            "<th align=\"left\">Content</th></tr>",
        ]
        for number, product in enumerate(group, start=1):
            rows_html.append(
                "<tr>"
                + "".join(
                    f"<td>{html.escape(value)}</td>"
                    for value in (
                        str(number),
                        _value(product.code),
                        _value(product.name),
                        _value(product.cas_no),
                        _value(product.content),
                    )
                )
                + "</tr>"
            )
        rows_html.append("</table>")
        html_parts.append("".join(rows_html))
    html_body = "".join(html_parts) + signature_html
    return text, html_body


def category_interest_entries(
    *,
    text: str,
    category_names: dict[str, str],
    source: str,
    source_row: int | None = None,
) -> list[dict[str, Any]]:
    return [
        interest_entry(
            category_key=key,
            category_name=category_names.get(key, key),
            source=source,
            value=text,
            source_row=source_row,
        )
        for key in classify_category_interests(text)
    ]


async def active_category_keys(session: AsyncSession) -> set[str]:
    rows = (
        await session.execute(
            select(ProductCategory.key).where(ProductCategory.active.is_(True))
        )
    ).scalars()
    return set(rows.all())


async def category_names_by_key(session: AsyncSession) -> dict[str, str]:
    rows = (
        await session.execute(
            select(ProductCategory.key, ProductCategory.name).where(
                ProductCategory.active.is_(True)
            )
        )
    ).all()
    return {key: name for key, name in rows}


async def audit_catalog_event(
    session: AsyncSession,
    *,
    event_type: str,
    case_id: int | None,
    data: dict[str, Any],
) -> None:
    session.add(
        AuditEvent(
            case_id=case_id,
            actor="product-list",
            event_type=event_type,
            data={**data, "at": datetime.now(UTC).isoformat()},
        )
    )
