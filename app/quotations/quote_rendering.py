"""Deterministic quotation rendering.

This module contains no database or delivery side effects. Commercial facts
must be resolved before calling it, which keeps rendering independently
testable and prevents model prose from changing approved terms.
"""

from __future__ import annotations

import html
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.ai import validate_rendered_email
from app.imports import ContentBundle
from app.settings import Settings


def render_quote(
    *,
    plan: Any,
    bundle: ContentBundle,
    product_key: str,
    product_name: str,
    price: Decimal,
    currency: str,
    quantity: int,
    unit: str,
    incoterm: str,
    payment_term: str,
    valid_until: date,
    taxes_included: bool = False,
    freight_included: bool = False,
    availability: str = "Ready stock",
) -> tuple[str, str]:
    snippet = bundle.product_snippets[product_key]
    safe_greeting = plan.greeting.lower().startswith("dear ") and not any(
        character.isdigit() for character in plan.greeting
    )
    greeting = plan.greeting if safe_greeting else "Dear Customer,"
    body_lines = [
        greeting,
        "",
        "Thank you for your inquiry.",
        snippet,
        "",
        "Please find our standard quotation details below.",
        f"Product: {product_name}",
        f"Quantity: {quantity} {unit}",
        f"Unit price: {currency} {price:.4f} per {unit}",
        f"Availability: {availability}",
        f"Price basis: {incoterm} (ex-warehouse)",
        f"Taxes: {'included' if taxes_included else 'excluded'}",
        f"Freight: {'included' if freight_included else 'excluded'}",
        f"Payment term: {payment_term}",
        f"Quote valid until: {valid_until.isoformat()} ({valid_until.strftime('%A')})",
        "",
        "Please let us know if you have questions about this non-binding standard quotation.",
    ]
    business_text = "\n".join(body_lines)
    validate_rendered_email(
        business_text,
        exact_price=price,
        currency=currency,
        approved_fragments=[snippet],
    )
    text = "\n".join([business_text, "", bundle.signature_text.strip()])
    html_body = (
        "<p>"
        + "</p><p>".join(
            html.escape(line) if line else "&nbsp;" for line in body_lines
        )
        + "</p>"
        + bundle.signature_html
    )
    return text, html_body


def render_multi_quote(
    *,
    plan: Any,
    bundle: ContentBundle,
    lines: list[dict[str, object]],
    currency: str,
    valid_until: date,
    availability_note: str,
) -> tuple[str, str]:
    """Render one non-binding quotation covering several products."""

    safe_greeting = plan.greeting.lower().startswith("dear ") and not any(
        character.isdigit() for character in plan.greeting
    )
    greeting = plan.greeting if safe_greeting else "Dear Customer,"
    body_lines: list[str] = [
        greeting,
        "",
        "Thank you for your inquiry.",
        "",
        "Please find our standard quotation details below.",
    ]
    for index, line in enumerate(lines):
        body_lines.extend(
            [
                f"Product: {line['product_name']}",
                f"Quantity: {line['quantity']} {line['unit']}",
                f"Unit price: {currency} {line['unit_price']:.4f} per {line['unit']}",
                f"Availability: {availability_note}",
                f"Price basis: {line['incoterm']} (ex-warehouse)",
                f"Taxes: {'included' if line['taxes_included'] else 'excluded'}",
                f"Freight: {'included' if line['freight_included'] else 'excluded'}",
                f"Payment term: {line['payment_term']}",
            ]
        )
        if index < len(lines) - 1:
            body_lines.append("---")
    body_lines.extend(
        [
            f"Quote valid until: {valid_until.isoformat()} ({valid_until.strftime('%A')})",
            "",
            "Please let us know if you have questions about this non-binding standard quotation.",
        ]
    )
    business_text = "\n".join(body_lines)
    text = "\n".join([business_text, "", bundle.signature_text.strip()])
    html_body = (
        "<p>"
        + "</p><p>".join(
            html.escape(line) if line else "&nbsp;" for line in body_lines
        )
        + "</p>"
        + bundle.signature_html
    )
    return text, html_body


def standard_quote_valid_until(
    settings: Settings,
    at: datetime | None = None,
) -> date:
    """Return the next Monday in the configured business timezone."""

    observed = at or datetime.now(UTC)
    today = observed.astimezone(ZoneInfo(settings.business_timezone)).date()
    days_until_monday = (0 - today.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7
    return today + timedelta(days=days_until_monday)
