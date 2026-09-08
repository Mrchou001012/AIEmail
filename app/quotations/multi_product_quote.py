"""Multi product quote workflow."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import AIClient, InboundAnalysis
from app.catalogs.products import product_text_key
from app.coa.coa_delivery import prepare_coa_attachments as _prepare_coa_attachments
from app.coa.coa_delivery import read_prepared_coa_attachments as _read_prepared_coa_attachments
from app.common.email_identity import reply_contact_name as _reply_contact_name
from app.common.email_threading import _reply_references, _reply_source
from app.common.service_core import _pricing_policy, active_policy, audit, create_handoff, stage_outbox
from app.db import EmailMessage, PricePolicy, Product, Quote, SalesCase
from app.domain import HandoffReason, Intent, SendContext, evaluate_send_policy, initial_quote
from app.imports import load_content
from app.inbound.inquiry_matching import _product_lookup_conditions
from app.mail import append_quoted_reply
from app.quotations.commercial import QuoteContext, QuoteContextStatus
from app.quotations.commercial_service import _commercial_quote_context
from app.quotations.quote_clarification import _maybe_send_quote_clarification
from app.quotations.quote_rendering import render_multi_quote, standard_quote_valid_until
from app.settings import get_settings


async def _maybe_send_multi_product_quote(
    session: AsyncSession,
    *,
    case: SalesCase,
    email_row: EmailMessage,
    analysis: InboundAnalysis,
    analysis_facts: dict[str, Any],
) -> bool:
    """Quote every explicitly named product in one deterministic reply.

    The entire request goes to a human when any product is unknown, lacks a
    price policy, has a missing quantity after clarification, or fails the
    pricing/send gates. Partial quotations are never sent.
    """
    requests = [line for line in analysis.product_requests if line.product_code]
    if analysis.intent != Intent.QUOTE_REQUEST or len(requests) < 2:
        return False
    if case.product_id is not None:
        return False

    normalized_codes = list(dict.fromkeys(product_text_key(line.product_code) for line in requests))
    products = (
        (
            await session.execute(
                select(Product).where(
                    _product_lookup_conditions(list(dict.fromkeys(line.product_code for line in requests))),
                    Product.active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    product_by_key = {product_text_key(product.code): product for product in products}
    if len(product_by_key) != len(normalized_codes):
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.NEW_INQUIRY_REVIEW,
            summary="Multi-product request names a product that is not active in the catalog",
            facts={**analysis_facts, "unknown_product": True},
            source_email_id=email_row.id,
        )
        return True

    # The same product may be mentioned more than once. Resolve every mention
    # to its catalog row first, then keep one line per product only when all
    # mentions agree on the same quantity. Missing or conflicting quantities
    # are clarified instead of guessing, and duplicate rows can never violate
    # the (case, round, product) uniqueness constraint.
    grouped_quantities: dict[int, list[int | None]] = {}
    product_by_id: dict[int, Product] = {}
    for line in requests:
        product = product_by_key.get(product_text_key(line.product_code))
        if product is None:
            continue
        product_by_id[product.id] = product
        grouped_quantities.setdefault(product.id, []).append(line.quantity)

    missing_quantity_codes: list[str] = []
    resolved_requests: list[tuple[Product, int]] = []
    for product_id, quantities in grouped_quantities.items():
        product = product_by_id[product_id]
        distinct_quantities = set(quantities)
        if len(distinct_quantities) == 1 and None not in distinct_quantities:
            resolved_requests.append((product, next(iter(distinct_quantities))))
        else:
            missing_quantity_codes.append(product.code)
    if missing_quantity_codes:
        await _maybe_send_quote_clarification(
            session,
            case=case,
            email_row=email_row,
            analysis=analysis,
            analysis_facts=analysis_facts,
            missing_product_codes=missing_quantity_codes,
        )
        return True

    codes = [product.code for product in product_by_key.values()]
    settings = get_settings()
    valid_until = standard_quote_valid_until(settings)
    quote_rows: list[tuple[Product, PricePolicy, QuoteContext, Decimal, int, date]] = []
    for product, quantity in resolved_requests:
        context = await _commercial_quote_context(
            session,
            product_id=product.id,
            currency=case.currency,
            settings=settings,
            requested_quantity=quantity,
        )
        if context is not None and context.status is QuoteContextStatus.UNAVAILABLE:
            reason = HandoffReason.INVENTORY_UNAVAILABLE if context.reason.startswith("INVENTORY") else HandoffReason.NONSTANDARD
            await create_handoff(
                session,
                case=case,
                reason=reason,
                summary=(f"Current commercial data cannot quote {product.code}: {context.reason}"),
                facts={
                    **analysis_facts,
                    "commercial_cycle_id": context.cycle.id,
                    "product_pending": True,
                    "requested_quantity": quantity,
                },
                source_email_id=email_row.id,
            )
            return True
        policy_row = context.policy if context is not None else await active_policy(session, product.id, case.currency)
        if policy_row is None:
            await create_handoff(
                session,
                case=case,
                reason=HandoffReason.NONSTANDARD,
                summary=f"No standard price policy matched {product.code}",
                facts={**analysis_facts, "product_pending": True},
                source_email_id=email_row.id,
            )
            return True
        price_decision = initial_quote(_pricing_policy(policy_row), quantity)
        if not price_decision.approved or price_decision.unit_price is None:
            reason = HandoffReason.BELOW_FLOOR if price_decision.reason and "floor" in price_decision.reason else HandoffReason.NONSTANDARD
            await create_handoff(
                session,
                case=case,
                reason=reason,
                summary=f"Pricing engine rejected autonomous reply for {product.code}",
                facts={
                    **analysis_facts,
                    "hard_minimum": str(price_decision.hard_minimum),
                    "pricing_reason": price_decision.reason,
                },
                source_email_id=email_row.id,
            )
            return True
        quote_rows.append(
            (
                product,
                policy_row,
                context,
                price_decision.unit_price,
                quantity,
                valid_until,
            )
        )

    currency_standard = analysis.currency is None or analysis.currency.upper() == case.currency
    first_policy = quote_rows[0][1]
    incoterm_standard = analysis.incoterm is None or analysis.incoterm.upper() == first_policy.standard_incoterm.upper()
    payment_standard = analysis.payment_term is None or analysis.payment_term.casefold() == first_policy.standard_payment_term.casefold()
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
            currency_standard=currency_standard,
            quantity_standard=True,
            incoterm_standard=incoterm_standard,
            payment_standard=payment_standard,
            product_known=True,
            prebook_requested=analysis.prebook_requested,
            packaging_requested=analysis.packaging_requested,
            delivery_requested=analysis.shipping_requested,
            ready_stock_available=True,
        ),
        intent_threshold=settings.intent_confidence_threshold,
        product_threshold=settings.product_confidence_threshold,
        numeric_threshold=settings.numeric_confidence_threshold,
    )
    if not send_decision.allow_send:
        await create_handoff(
            session,
            case=case,
            reason=send_decision.reason or HandoffReason.NONSTANDARD,
            summary="Multi-product quotation requires human review",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return True

    prepared_coas: list[dict[str, Any]] = []
    if analysis.coa_requested:
        try:
            prepared_coas = _prepare_coa_attachments(
                settings=settings,
                product_codes=codes,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            await create_handoff(
                session,
                case=case,
                reason=HandoffReason.COA_REVIEW,
                summary=f"Multi-product quote is possible, but requested COAs need review: {exc}",
                facts={**analysis_facts, "quote_ready": True, "product_codes": codes},
                source_email_id=email_row.id,
            )
            return True

    bundle = load_content(settings.content_dir)
    try:
        plan = await AIClient().draft_plan(
            {
                "subject": email_row.subject,
                "contact_name": _reply_contact_name(
                    case.contact.name,
                    email_row.body_text,
                ),
                "approved_product_key": quote_rows[0][0].approved_text_key,
            }
        )
    except Exception as exc:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.AI_FAILURE,
            summary=f"Reply drafting failed: {type(exc).__name__}",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return True

    availability_note = "Subject to confirmation at order placement" if settings.quote_ignore_inventory else "Ready stock"
    lines = [
        {
            "product_name": product.name,
            "quantity": quantity,
            "unit": product.unit,
            "unit_price": unit_price,
            "incoterm": policy.standard_incoterm,
            "payment_term": policy.standard_payment_term,
            "taxes_included": policy.taxes_included,
            "freight_included": policy.freight_included,
        }
        for product, policy, _context, unit_price, quantity, _valid_until in quote_rows
    ]
    text, html_body = render_multi_quote(
        plan=plan,
        bundle=bundle,
        lines=lines,
        currency=case.currency,
        valid_until=valid_until,
        availability_note=availability_note,
    )
    signature_text = bundle.signature_text.strip()
    draft_body = text[: -len(signature_text)].rstrip() if signature_text and text.endswith(signature_text) else text.rstrip()
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
            summary=f"Multi-product quotation rendering failed: {type(exc).__name__}",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return True

    latest_quote = await session.scalar(
        select(Quote).where(Quote.case_id == case.id).order_by(Quote.round_number.desc(), Quote.id.desc()).limit(1)
    )
    round_number = latest_quote.round_number + 1 if latest_quote is not None else 0
    if not settings.quote_auto_send_enabled:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.QUOTE_REVIEW,
            summary="Multi-product quotation draft prepared; human approval is required",
            facts={
                **analysis_facts,
                "prepared_multi_quote": {
                    "product_codes": codes,
                    "round_number": round_number,
                    "currency": case.currency,
                    "lines": [
                        {
                            "product_id": product.id,
                            "product_code": product.code,
                            "price_policy_id": policy.id,
                            "price_policy_source_hash": policy.source_hash,
                            "commercial_cycle_id": (context.cycle.id if context is not None else None),
                            "unit_price": str(unit_price),
                            "quantity": quantity,
                            "valid_until": row_valid_until.isoformat(),
                        }
                        for (
                            product,
                            policy,
                            context,
                            unit_price,
                            quantity,
                            row_valid_until,
                        ) in quote_rows
                    ],
                    "prepared_coas": prepared_coas,
                    "requires_manual_revalidation": True,
                },
                "ai_draft_preview": {
                    "subject": f"Re: {email_row.subject}",
                    "body_text": draft_body,
                    "generated_at": datetime.now(UTC).isoformat(),
                    "provider": "deterministic-multi-quote",
                    "model": "current-commercial-policy-v1",
                    "rag_matches": [],
                },
            },
            source_email_id=email_row.id,
        )
        return True

    try:
        coa_attachments = _read_prepared_coa_attachments(
            settings=settings,
            prepared_coas=prepared_coas,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.COA_REVIEW,
            summary=f"Prepared multi-product COA changed or is unavailable: {exc}",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return True

    created_quotes: list[Quote] = []
    for product, policy, context, unit_price, quantity, valid_until in quote_rows:
        quote = Quote(
            case_id=case.id,
            product_id=product.id,
            price_policy_id=policy.id,
            commercial_cycle_id=(context.cycle.id if context is not None else None),
            round_number=round_number,
            unit_price=unit_price,
            currency=policy.currency,
            quantity=quantity,
            incoterm=policy.standard_incoterm,
            payment_term=policy.standard_payment_term,
            valid_until=valid_until,
            pricing_snapshot={
                "hard_minimum": str(_pricing_policy(policy).absolute_floor),
                "pricing_reason": "multi-product-autonomous",
                "applied_markup_pct": "0",
                "requested_price": str(analysis.requested_unit_price),
            },
        )
        session.add(quote)
        created_quotes.append(quote)
    await session.flush()
    case.negotiation_round = round_number
    outbox = await stage_outbox(
        session,
        case=case,
        quote=created_quotes[0],
        subject=f"Re: {email_row.subject}",
        text_body=text,
        html_body=html_body,
        business_key=f"inbound-reply:{email_row.id}:multi-quote",
        in_reply_to=email_row.message_id,
        references=_reply_references(email_row),
        inline_images=source.inline_images,
        attachments=coa_attachments,
    )
    if outbox is None:
        await session.rollback()
        return True
    await audit(
        session,
        "inbound.multi_product_quote_queued",
        case_id=case.id,
        actor="system",
        data={
            "email_id": email_row.id,
            "round_number": round_number,
            "product_codes": codes,
            "quote_ids": [quote.id for quote in created_quotes],
        },
    )
    return True
