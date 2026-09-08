"""Quote context workflow."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import InboundAnalysis, extract_quantity_kg
from app.catalogs.products import canonical_product_code, find_product_codes
from app.common.email_threading import _reply_source
from app.common.service_core import audit
from app.db import CaseStage, EmailMessage, Product, SalesCase
from app.domain import Intent


async def _augment_pending_quote_context(
    session: AsyncSession,
    *,
    case: SalesCase,
    email_row: EmailMessage,
    analysis: InboundAnalysis,
) -> tuple[InboundAnalysis, str | None]:
    """Recover a unique product/quantity from the current thread before asking."""
    if case.product_id is not None or analysis.intent != Intent.QUOTE_REQUEST:
        return analysis, None

    current_message = f"{email_row.subject}\n{email_row.body_text}"
    try:
        full_source = _reply_source(email_row)
        complete_thread = f"{email_row.subject}\n{full_source.body_text}"
    except RuntimeError:
        complete_thread = current_message

    candidate_code = canonical_product_code(analysis.product_code) if analysis.product_code else None
    candidate_source = "current_analysis" if candidate_code else None
    current_codes = find_product_codes(current_message)
    conflict: str | None = None
    if candidate_code is None:
        if len(current_codes) == 1:
            candidate_code = current_codes[0]
            candidate_source = "current_message"
        elif len(current_codes) > 1:
            conflict = "The current customer message contains multiple product codes"

    prior_inbound: list[EmailMessage] = []
    if candidate_code is None and conflict is None:
        prior_inbound = list(
            (
                await session.scalars(
                    select(EmailMessage)
                    .where(
                        EmailMessage.case_id == case.id,
                        EmailMessage.id != email_row.id,
                        EmailMessage.direction == "INBOUND",
                        EmailMessage.is_bounce.is_(False),
                        EmailMessage.is_automated_reply.is_(False),
                    )
                    .order_by(EmailMessage.received_at.desc(), EmailMessage.id.desc())
                    .limit(20)
                )
            ).all()
        )
        for prior in prior_inbound:
            prior_codes = find_product_codes(f"{prior.subject}\n{prior.body_text}")
            if len(prior_codes) == 1:
                candidate_code = prior_codes[0]
                candidate_source = f"prior_inbound:{prior.id}"
                break
            if len(prior_codes) > 1:
                conflict = f"Prior inbound email {prior.id} contains multiple product codes"
                break

    # The archived MIME keeps the complete quoted conversation and is useful
    # when an older inbound message was not stored as its own row. It is only a
    # fallback: prior customer-authored messages take precedence, so quoted
    # outbound catalogs and quotations cannot overwrite clearer customer
    # evidence.
    if candidate_code is None and conflict is None:
        thread_codes = find_product_codes(complete_thread)
        if len(thread_codes) == 1:
            candidate_code = thread_codes[0]
            candidate_source = "current_complete_thread"
        elif len(thread_codes) > 1:
            conflict = "The quoted email thread contains multiple product codes"

    quantity = analysis.quantity or extract_quantity_kg(current_message)
    if quantity is None:
        if not prior_inbound:
            prior_inbound = list(
                (
                    await session.scalars(
                        select(EmailMessage)
                        .where(
                            EmailMessage.case_id == case.id,
                            EmailMessage.id != email_row.id,
                            EmailMessage.direction == "INBOUND",
                            EmailMessage.is_bounce.is_(False),
                            EmailMessage.is_automated_reply.is_(False),
                        )
                        .order_by(EmailMessage.received_at.desc(), EmailMessage.id.desc())
                        .limit(20)
                    )
                ).all()
            )
        for prior in prior_inbound:
            quantity = extract_quantity_kg(f"{prior.subject}\n{prior.body_text}")
            if quantity is not None:
                break

    missing_fields = list(analysis.missing_fields)
    if quantity is not None:
        missing_fields = [item for item in missing_fields if item != "quantity"]
    updates: dict[str, Any] = {
        "quantity": quantity,
        "numeric_confidence": 1.0 if quantity is not None else analysis.numeric_confidence,
        "missing_fields": missing_fields,
    }
    if conflict is not None:
        return analysis.model_copy(update=updates), conflict
    if candidate_code is None:
        return analysis.model_copy(update=updates), None

    product = await session.scalar(
        select(Product).where(
            Product.code == candidate_code,
            Product.active.is_(True),
        )
    )
    if product is None:
        return (
            analysis.model_copy(update=updates),
            f"Referenced product {candidate_code} is not active in the catalog",
        )
    if case.category_id is not None and product.category_id != case.category_id:
        return (
            analysis.model_copy(update=updates),
            f"Referenced product {candidate_code} conflicts with the case product category",
        )

    case.product_id = product.id
    case.product = product
    if case.category_id is None:
        case.category_id = product.category_id
    if case.stage == CaseStage.FOLLOW_UP:
        case.stage = CaseStage.QUOTING
    updates.update(
        {
            "product_code": product.code,
            "product_confidence": 1.0,
            "missing_fields": [item for item in missing_fields if item != "product_code"],
        }
    )
    await audit(
        session,
        "case.product_inferred_from_thread",
        case_id=case.id,
        actor="system",
        data={
            "email_id": email_row.id,
            "product_id": product.id,
            "product_code": product.code,
            "source": candidate_source,
            "recovered_quantity": quantity,
        },
    )
    return analysis.model_copy(update=updates), None
