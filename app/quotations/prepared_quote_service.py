"""Prepared quote service workflow."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.coa.coa_delivery import read_prepared_coa_attachments as _read_prepared_coa_attachments
from app.common.service_core import _pricing_policy, active_policy
from app.db import (
    CaseStage,
    CommercialDataCycle,
    Handoff,
    Outbox,
    PricePolicy,
    Product,
    Quote,
    SalesCase,
)
from app.domain import HandoffReason, initial_quote, transition
from app.handoffs.human_reply_service import queue_human_reply
from app.settings import get_settings


async def queue_prepared_quote_reply(
    session: AsyncSession,
    *,
    handoff_id: int,
    subject: str,
    body_text: str,
    actor: str,
    note: str = "",
    resume_automation: bool = False,
) -> Outbox:
    """Approve a quote draft only while its price policy snapshot is current."""

    existing = await session.scalar(select(Outbox).where(Outbox.approval_handoff_id == handoff_id))
    if existing is not None:
        return existing
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None:
        raise ValueError("handoff not found")
    prepared = (handoff.extracted_facts or {}).get("prepared_quote")
    if handoff.reason_code != HandoffReason.QUOTE_REVIEW.value or not isinstance(prepared, dict):
        raise ValueError("handoff has no prepared quotation draft")
    if handoff.case_id is None:
        raise ValueError("quotation draft is not associated with a case")
    sales_case = await session.get(SalesCase, handoff.case_id)
    policy = await session.get(PricePolicy, int(prepared.get("price_policy_id") or 0))
    product_id = int(prepared.get("product_id") or 0)
    if sales_case is None or sales_case.product_id != product_id:
        raise ValueError("case product changed after quotation draft creation")
    if (
        policy is None
        or not policy.active
        or policy.product_id != product_id
        or policy.currency != str(prepared.get("currency") or "")
        or policy.source_hash != str(prepared.get("price_policy_source_hash") or "")
    ):
        raise ValueError("approved price policy changed after draft creation; regenerate the quote")
    current_policy = await active_policy(session, product_id, policy.currency)
    if current_policy is None or current_policy.id != policy.id:
        raise ValueError("quotation no longer uses the current active price policy")
    cycle_id = prepared.get("commercial_cycle_id")
    if cycle_id is not None:
        cycle = await session.get(CommercialDataCycle, int(cycle_id))
        settings = get_settings()
        if (
            cycle is None
            or cycle.price_status != "CONFIRMED"
            or (not settings.quote_ignore_inventory and cycle.inventory_status != "CONFIRMED")
            or policy.commercial_cycle_id != cycle.id
        ):
            raise ValueError("commercial price or inventory confirmation is no longer current")
    expected_round = int(prepared.get("round_number") or 0)
    latest_quote = await session.scalar(
        select(Quote).where(Quote.case_id == sales_case.id).order_by(Quote.round_number.desc(), Quote.id.desc()).limit(1)
    )
    actual_round = latest_quote.round_number + 1 if latest_quote is not None else 0
    if actual_round != expected_round:
        raise ValueError("quotation history changed after draft creation; regenerate the quote")
    valid_until = date.fromisoformat(str(prepared.get("valid_until") or ""))
    if valid_until < datetime.now(UTC).astimezone(ZoneInfo(get_settings().business_timezone)).date():
        raise ValueError("quotation draft has expired; regenerate it")
    quantity = int(prepared.get("quantity") or 0)
    current_price = initial_quote(_pricing_policy(policy), quantity)
    if (
        not current_price.approved
        or current_price.unit_price is None
        or current_price.unit_price != Decimal(str(prepared.get("unit_price")))
        or str(prepared.get("incoterm") or "") != policy.standard_incoterm
        or str(prepared.get("payment_term") or "") != policy.standard_payment_term
    ):
        raise ValueError("prepared quotation no longer matches current pricing rules")
    prepared_coas = prepared.get("prepared_coas") or []
    if not isinstance(prepared_coas, list):
        raise ValueError("prepared quotation COA metadata is invalid")
    try:
        coa_attachments = _read_prepared_coa_attachments(
            settings=get_settings(),
            prepared_coas=[dict(item) for item in prepared_coas],
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("prepared quotation COA is unavailable") from exc
    quote = Quote(
        case_id=sales_case.id,
        product_id=product_id,
        price_policy_id=policy.id,
        commercial_cycle_id=int(cycle_id) if cycle_id is not None else None,
        round_number=expected_round,
        unit_price=Decimal(str(prepared.get("unit_price"))),
        currency=policy.currency,
        quantity=quantity,
        incoterm=str(prepared.get("incoterm") or ""),
        payment_term=str(prepared.get("payment_term") or ""),
        valid_until=valid_until,
        pricing_snapshot=dict(prepared.get("pricing_snapshot") or {}),
    )
    session.add(quote)
    await session.flush()
    sales_case.negotiation_round = expected_round
    if latest_quote is not None:
        sales_case.stage = transition(sales_case.stage, CaseStage.NEGOTIATING)
    return await queue_human_reply(
        session,
        handoff_id=handoff_id,
        subject=subject,
        body_text=body_text,
        actor=actor,
        note=note,
        resume_automation=resume_automation,
        attachments=coa_attachments,
        quote=quote,
    )


async def queue_prepared_multi_quote_reply(
    session: AsyncSession,
    *,
    handoff_id: int,
    subject: str,
    body_text: str,
    actor: str,
    note: str = "",
    resume_automation: bool = False,
) -> Outbox:
    """Approve all lines of a multi-product quote atomically after revalidation."""

    existing = await session.scalar(select(Outbox).where(Outbox.approval_handoff_id == handoff_id))
    if existing is not None:
        return existing
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None or handoff.case_id is None:
        raise ValueError("handoff or quotation case not found")
    prepared = (handoff.extracted_facts or {}).get("prepared_multi_quote")
    if handoff.reason_code != HandoffReason.QUOTE_REVIEW.value or not isinstance(prepared, dict):
        raise ValueError("handoff has no prepared multi-product quotation")
    sales_case = await session.get(SalesCase, handoff.case_id)
    if sales_case is None:
        raise ValueError("quotation case not found")
    expected_round = int(prepared.get("round_number") or 0)
    latest_quote = await session.scalar(
        select(Quote).where(Quote.case_id == sales_case.id).order_by(Quote.round_number.desc(), Quote.id.desc()).limit(1)
    )
    actual_round = latest_quote.round_number + 1 if latest_quote is not None else 0
    if actual_round != expected_round:
        raise ValueError("quotation history changed after draft creation; regenerate the quote")
    settings = get_settings()
    prepared_lines = prepared.get("lines")
    if not isinstance(prepared_lines, list) or len(prepared_lines) < 2:
        raise ValueError("prepared multi-product quotation lines are invalid")
    quote_rows: list[Quote] = []
    seen_products: set[int] = set()
    for raw_line in prepared_lines:
        if not isinstance(raw_line, dict):
            raise ValueError("prepared multi-product quotation line is invalid")
        product_id = int(raw_line.get("product_id") or 0)
        if product_id in seen_products:
            raise ValueError("prepared quotation contains a duplicate product")
        seen_products.add(product_id)
        product = await session.get(Product, product_id)
        policy = await session.get(PricePolicy, int(raw_line.get("price_policy_id") or 0))
        if (
            product is None
            or not product.active
            or policy is None
            or not policy.active
            or policy.product_id != product.id
            or policy.currency != str(prepared.get("currency") or "")
            or policy.source_hash != str(raw_line.get("price_policy_source_hash") or "")
        ):
            raise ValueError("a product or price policy changed after draft creation")
        current_policy = await active_policy(session, product.id, policy.currency)
        if current_policy is None or current_policy.id != policy.id:
            raise ValueError("a quotation line no longer uses the current price policy")
        cycle_id = raw_line.get("commercial_cycle_id")
        if cycle_id is not None:
            cycle = await session.get(CommercialDataCycle, int(cycle_id))
            if (
                cycle is None
                or cycle.price_status != "CONFIRMED"
                or (not settings.quote_ignore_inventory and cycle.inventory_status != "CONFIRMED")
                or policy.commercial_cycle_id != cycle.id
            ):
                raise ValueError("commercial confirmation changed for a quotation line")
        quantity = int(raw_line.get("quantity") or 0)
        decision = initial_quote(_pricing_policy(policy), quantity)
        if not decision.approved or decision.unit_price is None or decision.unit_price != Decimal(str(raw_line.get("unit_price"))):
            raise ValueError("a prepared line no longer matches current pricing rules")
        valid_until = date.fromisoformat(str(raw_line.get("valid_until") or ""))
        if valid_until < datetime.now(UTC).astimezone(ZoneInfo(settings.business_timezone)).date():
            raise ValueError("multi-product quotation draft has expired")
        quote_rows.append(
            Quote(
                case_id=sales_case.id,
                product_id=product.id,
                price_policy_id=policy.id,
                commercial_cycle_id=int(cycle_id) if cycle_id is not None else None,
                round_number=expected_round,
                unit_price=decision.unit_price,
                currency=policy.currency,
                quantity=quantity,
                incoterm=policy.standard_incoterm,
                payment_term=policy.standard_payment_term,
                valid_until=valid_until,
                pricing_snapshot={
                    "hard_minimum": str(decision.hard_minimum),
                    "pricing_reason": decision.reason,
                    "applied_markup_pct": str(decision.applied_markup_pct),
                    "human_approved_multi_quote": True,
                },
            )
        )
    prepared_coas = prepared.get("prepared_coas") or []
    if not isinstance(prepared_coas, list):
        raise ValueError("prepared COA metadata is invalid")
    coa_attachments = _read_prepared_coa_attachments(
        settings=settings,
        prepared_coas=[dict(item) for item in prepared_coas],
    )
    session.add_all(quote_rows)
    await session.flush()
    sales_case.negotiation_round = expected_round
    if latest_quote is not None:
        sales_case.stage = transition(sales_case.stage, CaseStage.NEGOTIATING)
    return await queue_human_reply(
        session,
        handoff_id=handoff_id,
        subject=subject,
        body_text=body_text,
        actor=actor,
        note=note,
        resume_automation=resume_automation,
        attachments=coa_attachments,
        quote=quote_rows[0],
    )
