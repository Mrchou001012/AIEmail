"""Human-approved and prepared quotation workflows."""

import re
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.ai import AIClient
from app.common.email_identity import reply_contact_name as _reply_contact_name
from app.common.email_threading import _reply_references, _reply_source
from app.common.service_core import HumanApproval, _atomic_business_operation, stage_outbox
from app.db import AuditEvent, CaseStatus, EmailAddressStatus, EmailMessage, Handoff, Outbox, PricePolicy, Product, Quote, SalesCase
from app.handoffs.agent_runtime import finalize_handoff_agent_run
from app.imports import load_content
from app.mail import append_quoted_reply
from app.quotations.quote_rendering import render_multi_quote, standard_quote_valid_until
from app.settings import get_settings


async def _manual_pricing_policy(
    session: AsyncSession,
    *,
    product: Product,
    currency: str,
    standard_price: Decimal,
    handoff_id: int,
    actor: str,
    today: date,
) -> PricePolicy:
    latest_policy = await session.scalar(
        select(PricePolicy)
        .where(
            PricePolicy.product_id == product.id,
            PricePolicy.currency == currency,
        )
        .order_by(PricePolicy.valid_from.desc(), PricePolicy.id.desc())
        .limit(1)
    )
    policy = PricePolicy(
        commercial_cycle_id=None,
        product_id=product.id,
        currency=currency,
        standard_price=standard_price,
        # Counteroffers stay human-only; the floor equals the human-set price
        # so no accidental automatic discount is possible.
        absolute_floor=standard_price,
        max_discount_pct=Decimal("0"),
        max_negotiation_rounds=(latest_policy.max_negotiation_rounds if latest_policy is not None else 2),
        concession_step_pct=(latest_policy.concession_step_pct if latest_policy is not None else Decimal("0.02")),
        min_quantity=latest_policy.min_quantity if latest_policy is not None else 1,
        max_quantity=(latest_policy.max_quantity if latest_policy is not None else None),
        tier_1_max_multiple=(latest_policy.tier_1_max_multiple if latest_policy is not None else None),
        tier_1_markup_pct=(latest_policy.tier_1_markup_pct if latest_policy is not None else Decimal("0")),
        tier_2_max_multiple=(latest_policy.tier_2_max_multiple if latest_policy is not None else None),
        tier_2_markup_pct=(latest_policy.tier_2_markup_pct if latest_policy is not None else Decimal("0")),
        quote_valid_days=(latest_policy.quote_valid_days if latest_policy is not None else 30),
        quote_valid_weekday=(latest_policy.quote_valid_weekday if latest_policy is not None else None),
        standard_incoterm=(latest_policy.standard_incoterm if latest_policy is not None else "EXW"),
        allowed_incoterms=list(latest_policy.allowed_incoterms or ["EXW"]) if latest_policy is not None else ["EXW"],
        standard_payment_term=(latest_policy.standard_payment_term if latest_policy is not None else "100% before shipment"),
        allowed_payment_terms=list(latest_policy.allowed_payment_terms or ["100% before shipment"])
        if latest_policy is not None
        else ["100% before shipment"],
        taxes_included=latest_policy.taxes_included if latest_policy is not None else False,
        freight_included=(latest_policy.freight_included if latest_policy is not None else False),
        valid_from=today,
        valid_to=None,
        source_hash=(f"manual-price:{handoff_id}:{actor}:{today.isoformat()}:{product.id}:{currency}"),
        # Historical reference only: prices change frequently, so a
        # human-set price must never feed the next autonomous quotation.
        # The review screen shows this record; the next inquiry routes to a
        # human again who can price with the history in view.
        active=False,
    )
    session.add(policy)
    await session.flush()
    return policy


@_atomic_business_operation
async def quote_with_manual_price(
    session: AsyncSession,
    *,
    handoff_id: int,
    lines: list[tuple[int, Decimal, int]],
    currency: str,
    actor: str,
    note: str = "",
) -> Outbox:
    """Persist human-set prices and send one quotation covering every line.

    Each price is stored as an inactive historical price policy (inheriting
    commercial terms from the most recent policy of the same product when
    available). Prices change frequently, so the records are for reference
    only: the next inquiry for the same products still routes to a human, and
    the review screen shows this stored price history. All lines are sent in
    one quotation email with one round number; partial quotations are never
    sent.
    """
    if not lines:
        raise ValueError("at least one product line is required")
    handoff = await session.scalar(select(Handoff).where(Handoff.id == handoff_id).with_for_update())
    if handoff is None:
        raise ValueError("handoff not found")
    if handoff.status != "OPEN":
        raise ValueError("handoff is already resolved")
    if handoff.case_id is None or handoff.source_email_id is None:
        raise ValueError("associate the handoff with a case before quoting")
    case = await session.scalar(
        select(SalesCase)
        .options(
            selectinload(SalesCase.customer),
            selectinload(SalesCase.contact),
        )
        .where(SalesCase.id == handoff.case_id)
    )
    if case is None:
        raise ValueError("handoff case no longer exists")
    source_email = await session.get(EmailMessage, handoff.source_email_id)
    if source_email is None:
        raise ValueError("handoff source email no longer exists")
    if source_email.direction != "INBOUND":
        raise ValueError("manual quotation requires an inbound source email")
    if source_email.from_address.casefold() != case.contact.email.casefold():
        raise ValueError("source sender does not match the associated case contact")
    if case.status in {CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST}:
        raise ValueError("closed case cannot send a reviewed quotation")
    if case.customer.do_not_contact or case.contact.suppressed:
        raise ValueError("recipient is suppressed or marked do-not-contact")
    address_status = await session.get(EmailAddressStatus, case.contact.email.casefold())
    if address_status is not None and address_status.suppressed:
        raise ValueError("recipient address is permanently suppressed")
    currency = currency.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("currency must be a three-letter code")
    product_ids = [line[0] for line in lines]
    if len(set(product_ids)) != len(product_ids):
        raise ValueError("the same product cannot be priced twice in one quotation")
    products = (
        (
            await session.execute(
                select(Product).where(
                    Product.id.in_(product_ids),
                    Product.active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    product_by_id = {product.id: product for product in products}
    if len(product_by_id) != len(set(product_ids)):
        raise ValueError("one or more products are not active in the catalog")
    if case.product_id is not None:
        if len(lines) != 1 or lines[0][0] != case.product_id:
            raise ValueError("the selected product does not match the case product; choose the case product to price it")
    for _product_id, standard_price, quantity in lines:
        if standard_price <= 0:
            raise ValueError("standard price must be positive")
        if type(quantity) is not int or quantity <= 0:
            raise ValueError("quantity must be a positive integer")

    today = date.today()
    policies: dict[int, PricePolicy] = {}
    for product_id, standard_price, _quantity in lines:
        policies[product_id] = await _manual_pricing_policy(
            session,
            product=product_by_id[product_id],
            currency=currency,
            standard_price=standard_price,
            handoff_id=handoff_id,
            actor=actor,
            today=today,
        )

    if case.product_id is None and len(lines) == 1:
        single_product = product_by_id[lines[0][0]]
        case.product_id = single_product.id
        case.product = single_product
    if case.currency != currency:
        case.currency = currency
    settings = get_settings()
    valid_until = standard_quote_valid_until(settings)
    bundle = load_content(settings.content_dir)
    try:
        first_product = product_by_id[lines[0][0]]
        plan = await AIClient().draft_plan(
            {
                "subject": "Quotation",
                "contact_name": _reply_contact_name(
                    case.contact.name,
                    source_email.body_text,
                ),
                "approved_product_key": first_product.approved_text_key,
            }
        )
        source = _reply_source(source_email)
        rendered_lines = []
        for product_id, standard_price, quantity in lines:
            product = product_by_id[product_id]
            policy = policies[product_id]
            rendered_lines.append(
                {
                    "product_name": product.name,
                    "quantity": quantity,
                    "unit": product.unit,
                    "unit_price": standard_price,
                    "incoterm": policy.standard_incoterm,
                    "payment_term": policy.standard_payment_term,
                    "taxes_included": policy.taxes_included,
                    "freight_included": policy.freight_included,
                }
            )
        text, html_body = render_multi_quote(
            plan=plan,
            bundle=bundle,
            lines=rendered_lines,
            currency=currency,
            valid_until=valid_until,
            availability_note="Subject to confirmation at order placement",
        )
        text, html_body = append_quoted_reply(
            text,
            html_body,
            from_address=source_email.from_address,
            source_body=source.body_text,
            source_html=source.body_html,
            occurred_at=source_email.received_at,
        )
    except Exception as exc:
        raise ValueError(f"quotation rendering failed: {type(exc).__name__}") from exc

    latest_quote = await session.scalar(
        select(Quote).where(Quote.case_id == case.id).order_by(Quote.round_number.desc(), Quote.id.desc()).limit(1)
    )
    round_number = latest_quote.round_number + 1 if latest_quote is not None else 0
    created_quotes: list[Quote] = []
    for product_id, standard_price, quantity in lines:
        product = product_by_id[product_id]
        policy = policies[product_id]
        quote = Quote(
            case_id=case.id,
            product_id=product.id,
            price_policy_id=policy.id,
            commercial_cycle_id=None,
            round_number=round_number,
            unit_price=standard_price,
            currency=currency,
            quantity=quantity,
            incoterm=policy.standard_incoterm,
            payment_term=policy.standard_payment_term,
            valid_until=valid_until,
            pricing_snapshot={
                "hard_minimum": str(standard_price),
                "pricing_reason": "manual-price",
                "applied_markup_pct": "0",
                "requested_price": None,
            },
        )
        session.add(quote)
        created_quotes.append(quote)
    await session.flush()
    case.negotiation_round = round_number
    approved_at = datetime.now(UTC)
    outbox = await stage_outbox(
        session,
        case=case,
        quote=created_quotes[0],
        message_kind="HUMAN_QUOTE",
        subject=f"Re: {source_email.subject}",
        text_body=text,
        html_body=html_body,
        business_key=f"handoff-reply:{handoff.id}:manual-price",
        in_reply_to=source_email.message_id,
        references=_reply_references(source_email),
        inline_images=source.inline_images,
        approval=HumanApproval(
            handoff_id=handoff.id,
            approved_by=actor[:128],
            approved_at=approved_at,
        ),
    )
    if outbox is None:
        raise ValueError("a quotation is already queued for this handoff")
    handoff.status = "RESOLVED"
    await finalize_handoff_agent_run(
        session,
        handoff_id=handoff.id,
        actor=actor,
        outcome="manual-quotation",
    )
    priced_summary = "; ".join(
        (f"{product_by_id[product_id].code} {quantity} {product_by_id[product_id].unit} at {currency} {standard_price}")
        for product_id, standard_price, quantity in lines
    )
    handoff.resolution_note = note.strip() or f"Priced by {actor}: {priced_summary}"
    if handoff.dingtalk_status != "SENT":
        handoff.dingtalk_status = "CANCELLED"
    session.add(
        AuditEvent(
            case_id=case.id,
            actor=actor,
            event_type="handoff.manual_price_quoted",
            data={
                "handoff_id": handoff.id,
                "outbox_id": outbox.id,
                "currency": currency,
                "lines": [
                    {
                        "product_id": product_id,
                        "product_code": product_by_id[product_id].code,
                        "standard_price": str(standard_price),
                        "quantity": quantity,
                        "policy_id": policies[product_id].id,
                        "quote_id": created_quotes[index].id,
                    }
                    for index, (product_id, standard_price, quantity) in enumerate(lines)
                ],
            },
        )
    )
    await session.commit()
    return outbox
