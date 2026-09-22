"""Single-product quotation workflow."""

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import AIClient, InboundAnalysis
from app.catalogs.products import product_codes_match
from app.coa.coa_delivery import prepare_coa_attachments as _prepare_coa_attachments
from app.coa.coa_delivery import read_prepared_coa_attachments as _read_prepared_coa_attachments
from app.common.email_identity import reply_contact_name as _reply_contact_name
from app.common.email_threading import _reply_references, _reply_source
from app.common.service_core import (
    _pricing_policy,
    _retrieve_historical_style_examples,
    active_policy,
    create_handoff,
    stage_outbox,
)
from app.db import (
    CaseStage,
    EmailMessage,
    Quote,
    SalesCase,
)
from app.domain import HandoffReason, SendContext, evaluate_send_policy, initial_quote, transition
from app.imports import load_content
from app.mail import (
    append_quoted_reply,
)
from app.quotations.commercial import QuoteContextStatus
from app.quotations.commercial_service import _commercial_quote_context
from app.quotations.quote_clarification import _maybe_send_quote_clarification
from app.quotations.quote_rendering import render_quote, standard_quote_valid_until
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


async def prepare_single_product_quote(
    session: AsyncSession,
    *,
    case: SalesCase,
    email_row: EmailMessage,
    analysis: InboundAnalysis,
    analysis_facts: dict[str, Any],
    settings: Settings,
    ai: AIClient,
    metadata: dict[str, Any],
) -> None:
    """Validate, prepare and stage a single-product reply within the inbound transaction."""
    latest_quote = await session.scalar(select(Quote).where(Quote.case_id == case.id).order_by(Quote.round_number.desc()))
    if latest_quote is not None and latest_quote.currency != case.currency:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.NONSTANDARD,
            summary="The current case currency does not match its latest quotation",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return
    quantity = analysis.quantity or (latest_quote.quantity if latest_quote is not None else None)
    if quantity is None:
        if await _maybe_send_quote_clarification(
            session,
            case=case,
            email_row=email_row,
            analysis=analysis,
            analysis_facts=analysis_facts,
        ):
            return
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.LOW_CONFIDENCE,
            summary="Initial inquiry does not contain a reliable quotation quantity",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return
    commercial_context = await _commercial_quote_context(
        session,
        product_id=case.product_id,
        currency=case.currency,
        settings=settings,
        requested_quantity=quantity,
    )
    if commercial_context is not None and commercial_context.status is QuoteContextStatus.UNAVAILABLE:
        unavailable_reason = (
            HandoffReason.INVENTORY_UNAVAILABLE if commercial_context.reason.startswith("INVENTORY") else HandoffReason.NONSTANDARD
        )
        await create_handoff(
            session,
            case=case,
            reason=unavailable_reason,
            summary=(f"Current commercial data cannot quote {case.product.code}: {commercial_context.reason}"),
            facts={
                **analysis_facts,
                "commercial_cycle_id": commercial_context.cycle.id,
                "requested_quantity": quantity,
                "available_quantity": (
                    str(commercial_context.inventory.quantity)
                    if commercial_context.inventory is not None and commercial_context.inventory.quantity is not None
                    else None
                ),
            },
            source_email_id=email_row.id,
        )
        return
    policy_row = (
        commercial_context.policy if commercial_context is not None else await active_policy(session, case.product_id, case.currency)
    )
    if policy_row is None:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.NONSTANDARD,
            summary="No standard price policy matched the inbound request",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return
    currency_standard = analysis.currency is None or analysis.currency.upper() == case.currency
    incoterm_standard = analysis.incoterm is None or analysis.incoterm.upper() == policy_row.standard_incoterm.upper()
    payment_standard = analysis.payment_term is None or analysis.payment_term.casefold() == policy_row.standard_payment_term.casefold()
    quantity_standard = quantity >= policy_row.min_quantity and (policy_row.max_quantity is None or quantity <= policy_row.max_quantity)
    send_decision = evaluate_send_policy(
        SendContext(
            intent=analysis.intent,
            stage=case.stage,
            status=case.status,
            intent_confidence=analysis.intent_confidence,
            product_confidence=analysis.product_confidence,
            numeric_confidence=analysis.numeric_confidence,
            auto_send_allowed=case.customer.auto_send_allowed,
            contact_suppressed=case.contact.suppressed,
            do_not_contact=case.customer.do_not_contact,
            has_risky_attachment=analysis.risky_attachment,
            currency_standard=currency_standard,
            quantity_standard=quantity_standard,
            incoterm_standard=incoterm_standard,
            payment_standard=payment_standard,
            product_known=analysis.product_code is None or product_codes_match(analysis.product_code, case.product.code),
            prebook_requested=analysis.prebook_requested,
            packaging_requested=analysis.packaging_requested,
            delivery_requested=analysis.shipping_requested,
            ready_stock_available=(commercial_context.ready_stock_available if commercial_context is not None else True),
        ),
        intent_threshold=get_settings().intent_confidence_threshold,
        product_threshold=get_settings().product_confidence_threshold,
        numeric_threshold=get_settings().numeric_confidence_threshold,
    )
    if not send_decision.allow_send:
        await create_handoff(
            session,
            case=case,
            reason=send_decision.reason or HandoffReason.NONSTANDARD,
            summary=f"Inbound {analysis.intent.value} requires human review",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return
    price_decision = initial_quote(_pricing_policy(policy_row), quantity)
    if not price_decision.approved or price_decision.unit_price is None:
        reason = HandoffReason.BELOW_FLOOR if price_decision.reason and "floor" in price_decision.reason else HandoffReason.NONSTANDARD
        await create_handoff(
            session,
            case=case,
            reason=reason,
            summary=f"Pricing engine rejected autonomous reply: {price_decision.reason}",
            facts={
                **analysis_facts,
                "hard_minimum": str(price_decision.hard_minimum),
                "pricing_reason": price_decision.reason,
            },
            source_email_id=email_row.id,
        )
        return
    prepared_coas: list[dict[str, Any]] = []
    if analysis.coa_requested:
        try:
            prepared_coas = _prepare_coa_attachments(
                settings=settings,
                product_codes=[case.product.code],
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            await create_handoff(
                session,
                case=case,
                reason=HandoffReason.COA_REVIEW,
                summary=f"Quotation is possible, but the requested COA needs review: {exc}",
                facts={**analysis_facts, "quote_ready": True},
                source_email_id=email_row.id,
            )
            return
    valid_until = standard_quote_valid_until(settings)
    bundle = load_content(get_settings().content_dir)
    if not str(bundle.product_snippets.get(case.product.approved_text_key) or "").strip():
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.NONSTANDARD,
            summary=f"Approved product text is missing for key {case.product.approved_text_key}",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return
    historical_style_examples: list[dict[str, Any]] = []
    if settings.rag_enabled:
        try:
            historical_style_examples = await asyncio.to_thread(
                _retrieve_historical_style_examples,
                settings,
                subject=email_row.subject,
                body=email_row.body_text,
                intent=analysis.intent.value,
            )
        except Exception as exc:
            logger.warning(
                "Historical RAG retrieval skipped for email %s: %s",
                email_row.id,
                type(exc).__name__,
            )
    try:
        plan = await ai.draft_plan(
            {
                "subject": email_row.subject,
                "contact_name": _reply_contact_name(
                    case.contact.name,
                    email_row.body_text,
                ),
                "approved_product_key": case.product.approved_text_key,
                "historical_style_examples": historical_style_examples,
            }
        )
        text, html_body = render_quote(
            plan=plan,
            bundle=bundle,
            product_key=case.product.approved_text_key,
            product_name=case.product.name,
            price=price_decision.unit_price,
            currency=policy_row.currency,
            quantity=quantity,
            unit=case.product.unit,
            incoterm=policy_row.standard_incoterm,
            payment_term=policy_row.standard_payment_term,
            valid_until=valid_until,
            taxes_included=policy_row.taxes_included,
            freight_included=policy_row.freight_included,
            availability=("Subject to confirmation at order placement" if settings.quote_ignore_inventory else "Ready stock"),
        )
        signature_text = bundle.signature_text.strip()
        draft_body = text[: -len(signature_text)].rstrip() if signature_text and text.endswith(signature_text) else text.rstrip()
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
            summary=f"Reply drafting failed: {type(exc).__name__}",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return
    round_number = latest_quote.round_number + 1 if latest_quote is not None else 0
    if not settings.quote_auto_send_enabled:
        pricing_snapshot = {
            "hard_minimum": str(price_decision.hard_minimum),
            "pricing_reason": price_decision.reason,
            "applied_markup_pct": str(price_decision.applied_markup_pct),
            "requested_price": str(analysis.requested_unit_price),
        }
        commercial_cycle_id = commercial_context.cycle.id if commercial_context is not None else None
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.QUOTE_REVIEW,
            summary=(
                f"Quotation draft prepared for {case.product.code} at "
                f"{policy_row.currency} {price_decision.unit_price}; human approval is required"
            ),
            facts={
                **analysis_facts,
                "prepared_quote": {
                    "product_id": case.product.id,
                    "product_code": case.product.code,
                    "price_policy_id": policy_row.id,
                    "price_policy_source_hash": policy_row.source_hash,
                    "commercial_cycle_id": commercial_cycle_id,
                    "round_number": round_number,
                    "unit_price": str(price_decision.unit_price),
                    "currency": policy_row.currency,
                    "quantity": quantity,
                    "incoterm": policy_row.standard_incoterm,
                    "payment_term": policy_row.standard_payment_term,
                    "valid_until": valid_until.isoformat(),
                    "pricing_snapshot": pricing_snapshot,
                    "prepared_coas": prepared_coas,
                },
                "ai_draft_preview": {
                    "subject": f"Re: {email_row.subject}",
                    "body_text": draft_body,
                    "generated_at": datetime.now(UTC).isoformat(),
                    "provider": metadata["provider"],
                    "model": metadata["model"],
                    "rag_matches": historical_style_examples,
                },
            },
            source_email_id=email_row.id,
        )
        return
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
            summary=f"Prepared quotation COA changed or is unavailable: {exc}",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return
    case.negotiation_round = round_number
    if latest_quote is not None:
        case.stage = transition(case.stage, CaseStage.NEGOTIATING)
    quote = Quote(
        case_id=case.id,
        price_policy_id=policy_row.id,
        commercial_cycle_id=(commercial_context.cycle.id if commercial_context is not None else None),
        round_number=round_number,
        unit_price=price_decision.unit_price,
        currency=policy_row.currency,
        quantity=quantity,
        incoterm=policy_row.standard_incoterm,
        payment_term=policy_row.standard_payment_term,
        valid_until=valid_until,
        pricing_snapshot={
            "hard_minimum": str(price_decision.hard_minimum),
            "pricing_reason": price_decision.reason,
            "applied_markup_pct": str(price_decision.applied_markup_pct),
            "requested_price": str(analysis.requested_unit_price),
        },
    )
    session.add(quote)
    await session.flush()
    outbox = await stage_outbox(
        session,
        case=case,
        quote=quote,
        subject=f"Re: {email_row.subject}",
        text_body=text,
        html_body=html_body,
        business_key=(
            f"inbound-reply:{email_row.id}:quote:{commercial_context.cycle.id}"
            if commercial_context is not None
            else f"inbound-reply:{email_row.id}"
        ),
        in_reply_to=email_row.message_id,
        references=_reply_references(email_row),
        inline_images=source.inline_images,
        attachments=coa_attachments,
    )
    if outbox is None:
        await session.rollback()
