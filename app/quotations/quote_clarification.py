"""Quote clarification workflow."""

import html
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import InboundAnalysis
from app.catalogs.products import product_text_key
from app.common.email_identity import reply_contact_name as _reply_contact_name
from app.common.email_threading import _reply_references, _reply_source
from app.common.service_core import (
    active_policy,
    audit,
    create_handoff,
    stage_outbox,
)
from app.db import (
    DeliveryStatus,
    EmailMessage,
    Outbox,
    PricePolicy,
    Product,
    SalesCase,
)
from app.domain import HandoffReason, Intent, SendContext, evaluate_send_policy
from app.imports import load_content
from app.inbound.inquiry_matching import _product_lookup_conditions
from app.mail import append_quoted_reply
from app.settings import get_settings


def _quantity_unit_label(unit: str | None) -> str:
    """Human-readable quantity unit for clarification questions."""
    normalized = (unit or "").strip().lower()
    if normalized in {"kg", "kgs", "kilogram", "kilograms"}:
        return "kilograms"
    if normalized in {"piece", "pieces"}:
        return "pieces"
    if normalized in {"unit", "units"}:
        return "units"
    return normalized or "kilograms"


def _pluralized_unit(unit: str | None) -> str:
    """Pluralize the product unit when it is a countable noun."""
    normalized = (unit or "").strip().lower()
    if normalized in {"piece", "unit"}:
        return f"{normalized}s"
    return normalized or "unit"


async def _moq_price_rows(
    session: AsyncSession,
    *,
    codes: list[str],
    currency: str,
) -> list[tuple[Product, PricePolicy]]:
    """Return one active MOQ reference price per product that has a policy.

    Products without a matching policy are simply skipped: the clarification
    still asks for their quantity, and callers never crash when catalog or
    commercial data changes underneath the request. Callers format the rows
    with the wording appropriate for single- or multi-product requests.
    """
    unique_codes = list(dict.fromkeys(code for code in codes if code))
    if not unique_codes:
        return []
    products = (
        (
            await session.execute(
                select(Product).where(
                    _product_lookup_conditions(unique_codes),
                    Product.active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    product_by_key = {product_text_key(product.code): product for product in products}
    rows: list[tuple[Product, PricePolicy]] = []
    for code in unique_codes:
        product = product_by_key.get(product_text_key(code))
        if product is None:
            continue
        policy = await active_policy(session, product.id, currency)
        if (
            policy is None
            or policy.min_quantity is None
            or policy.min_quantity <= 0
            or policy.standard_price is None
            or policy.standard_price <= 0
        ):
            continue
        rows.append((product, policy))
    return rows


async def _maybe_send_quote_clarification(
    session: AsyncSession,
    *,
    case: SalesCase,
    email_row: EmailMessage,
    analysis: InboundAnalysis,
    analysis_facts: dict[str, Any],
    missing_product_codes: list[str] | None = None,
) -> bool:
    """Ask once for missing product/quantity details instead of handing off."""
    product_known = case.product_id is not None
    quantity_known = analysis.quantity is not None
    needs_product = not product_known and not (missing_product_codes or analysis.product_requests)
    needs_quantity = product_known and not quantity_known
    needs_multi_quantities = bool(missing_product_codes)
    if analysis.intent != Intent.QUOTE_REQUEST or not (needs_product or needs_quantity or needs_multi_quantities):
        return False

    previous_clarification = await session.scalar(
        select(Outbox.id).where(
            Outbox.case_id == case.id,
            Outbox.message_kind == "QUOTE_CLARIFICATION",
            Outbox.status != DeliveryStatus.CANCELLED,
        )
    )
    if previous_clarification is not None:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.HUMAN_CONTROL,
            summary="Product is still unclear after one automated clarification",
            facts={
                **analysis_facts,
                "product_pending": True,
                "previous_clarification_outbox_id": previous_clarification,
            },
            source_email_id=email_row.id,
        )
        return True

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
            reason=send_decision.reason or HandoffReason.HUMAN_CONTROL,
            summary="Product clarification requires human review",
            facts={**analysis_facts, "product_pending": True},
            source_email_id=email_row.id,
        )
        return True

    settings = get_settings()
    bundle = load_content(settings.content_dir)
    greeting = f"Dear {_reply_contact_name(case.contact.name, email_row.body_text)},"
    if needs_multi_quantities:
        opening = "Thank you for your quotation request."
        names = ", ".join(missing_product_codes)
        moq_rows = await _moq_price_rows(
            session,
            codes=missing_product_codes,
            currency=case.currency,
        )
        reference_lines = []
        for product, policy in moq_rows:
            raw_unit = (product.unit or "unit").strip() or "unit"
            reference_lines.append(
                f"{product.code}: MOQ {policy.min_quantity} {_pluralized_unit(raw_unit)} at {case.currency} {policy.standard_price:.2f}"
            )
        question = (
            f"Could you please let us know the quantity you require for {names}? Better pricing is available for larger volumes."
            if moq_rows
            else f"Could you please confirm the required quantity for {names}?"
        )
    elif needs_quantity:
        opening = (
            f"Thank you for your quotation request for {case.product.name}."
            if case.product is not None
            else "Thank you for your quotation request."
        )
        reference_lines = []
        if case.product is not None:
            moq_rows = await _moq_price_rows(
                session,
                codes=[case.product.code],
                currency=case.currency,
            )
            for product, policy in moq_rows:
                raw_unit = (product.unit or "unit").strip() or "unit"
                reference_lines.append(
                    f"Our minimum order quantity is {policy.min_quantity} "
                    f"{_pluralized_unit(raw_unit)} at {case.currency} "
                    f"{policy.standard_price:.2f} per {raw_unit}."
                )
        if reference_lines:
            question = "Could you please let us know the quantity you require? Better pricing is available for larger volumes."
        else:
            question = (
                f"Could you please confirm the required quantity in "
                f"{_quantity_unit_label(case.product.unit if case.product is not None else None)}?"
            )
    else:
        opening = (
            f"Thank you for your quotation request for {analysis.quantity} kg."
            if analysis.quantity is not None
            else "Thank you for your quotation request."
        )
        reference_lines = []
        question = "Could you please confirm the product name or Lanya product code, together with the required quantity?"
    closing = "Once confirmed, we will confirm availability and prepare your quotation accordingly."
    business_lines = [greeting, "", opening]
    for line in reference_lines:
        business_lines.extend(["", line])
    business_lines.extend(["", question, closing])
    text = "\n".join([*business_lines, "", bundle.signature_text.strip()])
    html_body = "<p>" + "</p><p>".join(html.escape(line) if line else "&nbsp;" for line in business_lines) + "</p>" + bundle.signature_html
    try:
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
            summary=f"Product clarification rendering failed: {type(exc).__name__}",
            facts={**analysis_facts, "product_pending": True},
            source_email_id=email_row.id,
        )
        return True

    if not settings.quote_auto_send_enabled:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.QUOTE_REVIEW,
            summary="Quotation clarification draft prepared; human approval is required",
            facts={
                **analysis_facts,
                "product_pending": case.product is None,
                "ai_draft_preview": {
                    "subject": f"Re: {email_row.subject}",
                    "body_text": "\n".join(business_lines),
                    "generated_at": datetime.now(UTC).isoformat(),
                    "provider": "deterministic-quote-clarification",
                    "model": "current-catalog-moq-v1",
                    "rag_matches": [],
                },
            },
            source_email_id=email_row.id,
        )
        return True

    outbox = await stage_outbox(
        session,
        case=case,
        message_kind="QUOTE_CLARIFICATION",
        subject=f"Re: {email_row.subject}",
        text_body=text,
        html_body=html_body,
        business_key=f"inbound-reply:{email_row.id}:clarification",
        in_reply_to=email_row.message_id,
        references=_reply_references(email_row),
        inline_images=source.inline_images,
    )
    if outbox is None:
        await session.rollback()
        return True
    if outbox is not None:
        await audit(
            session,
            "inbound.quote_clarification_queued",
            case_id=case.id,
            actor="system",
            data={
                "email_id": email_row.id,
                "outbox_id": outbox.id,
                "requested_quantity": analysis.quantity,
            },
        )
    return True
