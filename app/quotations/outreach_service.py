"""Outreach service workflow."""

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.ai import AIClient
from app.common.service_core import _atomic_business_operation, _pricing_policy, active_policy, create_handoff, stage_outbox
from app.db import CaseStatus, EmailMessage, Handoff, Outbox, Quote, SalesCase
from app.domain import HandoffReason, initial_quote
from app.imports import load_content
from app.quotations.commercial import QuoteContextStatus
from app.quotations.commercial_service import _commercial_quote_context
from app.quotations.quote_rendering import render_quote, standard_quote_valid_until
from app.settings import get_settings


@_atomic_business_operation
async def create_case_outreach(session: AsyncSession, payload: dict[str, Any]) -> None:
    case_id = int(payload["case_id"])
    quantity = int(payload.get("quantity") or 1)
    reprice = bool(payload.get("reprice"))
    settings = get_settings()
    case = await session.scalar(
        select(SalesCase)
        .options(
            selectinload(SalesCase.customer),
            selectinload(SalesCase.contact),
            selectinload(SalesCase.product),
        )
        .where(SalesCase.id == case_id)
    )
    if case is None:
        raise RuntimeError(f"case {case_id} not found")
    if case.product_id is None or case.product is None:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.HUMAN_CONTROL,
            summary="Case product is still pending human selection",
            facts={"product_pending": True},
        )
        return
    historical_outbound = await session.scalar(
        select(EmailMessage)
        .where(
            or_(
                EmailMessage.case_id == case.id,
                EmailMessage.contact_id == case.contact_id,
            ),
            EmailMessage.direction == "OUTBOUND",
            EmailMessage.is_history.is_(True),
        )
        .order_by(EmailMessage.received_at.desc(), EmailMessage.id.desc())
        .limit(1)
    )
    if historical_outbound is not None:
        summary = "Historical Gmail outreach exists; initial outreach is blocked"
        existing_review = await session.scalar(
            select(Handoff.id).where(
                Handoff.case_id == case.id,
                Handoff.reason_code == HandoffReason.HUMAN_CONTROL.value,
                Handoff.summary == summary,
                Handoff.status == "OPEN",
            )
        )
        if existing_review is None:
            await create_handoff(
                session,
                case=case,
                reason=HandoffReason.HUMAN_CONTROL,
                summary=summary,
                facts={
                    "history_import": True,
                    "latest_outbound_email_id": historical_outbound.id,
                    "latest_outbound_at": historical_outbound.received_at.isoformat(),
                },
            )
        return
    if case.status != CaseStatus.ACTIVE:
        raise RuntimeError(f"case {case_id} is not active")
    if case.customer.do_not_contact or case.contact.suppressed or not case.customer.auto_send_allowed:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.SUPPRESSED,
            summary="Initial outreach blocked by customer/contact send eligibility",
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
            summary=f"Current commercial data cannot quote {case.product.code}: {commercial_context.reason}",
            facts={"commercial_cycle_id": commercial_context.cycle.id},
        )
        return
    cycle_id = commercial_context.cycle.id if commercial_context is not None else None
    business_key = f"initial-quote:case:{case.id}:cycle:{cycle_id}" if cycle_id is not None else f"initial-quote:case:{case.id}"
    if await session.scalar(select(Outbox.id).where(Outbox.business_key == business_key)) is not None:
        return
    existing_quote = await session.scalar(select(Quote).where(Quote.case_id == case.id).order_by(Quote.round_number.desc()).limit(1))
    if existing_quote is not None and not reprice:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.NONSTANDARD,
            summary="Case already has a quotation but no matching initial-outreach outbox record",
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
            summary=f"No active {case.currency} price policy is available for {case.product.code}",
        )
        return
    decision = initial_quote(_pricing_policy(policy_row), quantity)
    if not decision.approved or decision.unit_price is None:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.NONSTANDARD,
            summary=f"Initial quotation rejected by pricing policy: {decision.reason}",
            facts={"quantity": quantity, "hard_minimum": str(decision.hard_minimum)},
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
        )
        return
    try:
        plan = await AIClient().draft_plan(
            {
                "subject": f"{case.product.name} quotation",
                "contact_name": case.contact.name,
                "approved_product_key": case.product.approved_text_key,
            }
        )
        text, html_body = render_quote(
            plan=plan,
            bundle=bundle,
            product_key=case.product.approved_text_key,
            product_name=case.product.name,
            price=decision.unit_price,
            currency=policy_row.currency,
            quantity=quantity,
            unit=case.product.unit,
            incoterm=policy_row.standard_incoterm,
            payment_term=policy_row.standard_payment_term,
            valid_until=valid_until,
            taxes_included=policy_row.taxes_included,
            freight_included=policy_row.freight_included,
        )
    except Exception as exc:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.AI_FAILURE,
            summary=f"Initial outreach drafting failed: {type(exc).__name__}",
        )
        return
    round_number = existing_quote.round_number + 1 if existing_quote is not None else 0
    quote = Quote(
        case_id=case.id,
        price_policy_id=policy_row.id,
        commercial_cycle_id=cycle_id,
        round_number=round_number,
        unit_price=decision.unit_price,
        currency=policy_row.currency,
        quantity=quantity,
        incoterm=policy_row.standard_incoterm,
        payment_term=policy_row.standard_payment_term,
        valid_until=valid_until,
        pricing_snapshot={
            "standard_price": str(policy_row.standard_price),
            "absolute_floor": str(policy_row.absolute_floor),
            "hard_minimum": str(decision.hard_minimum),
            "max_discount_pct": str(policy_row.max_discount_pct),
            "applied_markup_pct": str(decision.applied_markup_pct),
            "pricing_tier": decision.reason,
        },
    )
    session.add(quote)
    case.negotiation_round = round_number
    await session.flush()
    subject = f"{case.product.name} quotation"
    case.subject_key = subject.lower()
    outbox = await stage_outbox(
        session,
        case=case,
        quote=quote,
        subject=subject,
        text_body=text,
        html_body=html_body,
        business_key=business_key,
    )
    if outbox is None:
        await session.rollback()
