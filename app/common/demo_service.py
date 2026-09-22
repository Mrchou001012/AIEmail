"""Demo service workflow."""

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import AIClient
from app.common.service_core import _atomic_business_operation, _pricing_policy, active_policy, stage_outbox
from app.db import CaseStage, CaseStatus, Contact, Customer, Outbox, PricePolicy, Product, Quote, SalesCase
from app.domain import initial_quote, quote_valid_until
from app.imports import load_content
from app.quotations.quote_rendering import render_quote
from app.settings import get_settings


async def seed_demo_data(session: AsyncSession) -> dict[str, int]:
    if not get_settings().demo_mode:
        raise RuntimeError("demo mode is disabled")
    product = await session.scalar(select(Product).where(Product.code == "WIDGET-100"))
    if product is None:
        product = Product(
            code="WIDGET-100",
            name="Industrial Widget 100",
            unit="piece",
            approved_text_key="widget_100",
        )
        session.add(product)
        await session.flush()
    policy = await active_policy(session, product.id, "USD")
    if policy is None:
        policy = PricePolicy(
            product_id=product.id,
            currency="USD",
            standard_price=Decimal("100.0000"),
            absolute_floor=Decimal("82.0000"),
            max_discount_pct=Decimal("0.1500"),
            max_negotiation_rounds=2,
            concession_step_pct=Decimal("0.0300"),
            min_quantity=10,
            max_quantity=10000,
            quote_valid_days=30,
            standard_incoterm="EXW",
            allowed_incoterms=["EXW", "FCA", "FOB"],
            standard_payment_term="100% before shipment",
            allowed_payment_terms=[
                "100% before shipment",
                "30% deposit / 70% before shipment",
            ],
            valid_from=date.today(),
            source_hash="demo-seed-v1",
        )
        session.add(policy)
    customer = await session.scalar(select(Customer).where(Customer.company_name == "Demo Industrial Ltd"))
    if customer is None:
        customer = Customer(
            company_name="Demo Industrial Ltd",
            language="en",
            auto_send_allowed=True,
            consent_basis="demo fixture",
        )
        session.add(customer)
        await session.flush()
    contact = await session.scalar(select(Contact).where(Contact.customer_id == customer.id, Contact.email == "internal@example.com"))
    if contact is None:
        contact = Contact(
            customer_id=customer.id,
            name="Alex Buyer",
            email="internal@example.com",
            language="en",
        )
        session.add(contact)
        await session.flush()
    await session.commit()
    return {"product_id": product.id, "customer_id": customer.id, "contact_id": contact.id}


@_atomic_business_operation
async def create_demo_outreach(session: AsyncSession, payload: dict[str, Any]) -> None:
    ids = await seed_demo_data(session)
    customer = await session.get(Customer, ids["customer_id"])
    seed_contact = await session.get(Contact, ids["contact_id"])
    product = await session.get(Product, ids["product_id"])
    assert customer and seed_contact and product
    recipient = str(payload.get("recipient") or seed_contact.email).lower()
    contact = await session.scalar(select(Contact).where(Contact.customer_id == customer.id, Contact.email == recipient))
    if contact is None:
        contact = Contact(
            customer_id=customer.id,
            name="Demo Recipient",
            email=recipient,
            language=customer.language,
        )
        session.add(contact)
        await session.flush()
    quantity = int(payload.get("quantity") or 100)
    business_key = f"demo-outreach:{recipient}:{quantity}"
    if await session.scalar(select(Outbox.id).where(Outbox.business_key == business_key)) is not None:
        return
    policy_row = await active_policy(session, product.id, "USD")
    if policy_row is None:
        raise RuntimeError("no active demo policy")
    decision = initial_quote(_pricing_policy(policy_row), quantity)
    if not decision.approved or decision.unit_price is None:
        raise RuntimeError(decision.reason or "initial quote rejected")
    case = SalesCase(
        customer_id=customer.id,
        contact_id=contact.id,
        product_id=product.id,
        stage=CaseStage.QUOTING,
        status=CaseStatus.ACTIVE,
        subject_key="industrial widget 100 quotation",
    )
    session.add(case)
    await session.flush()
    valid_until = quote_valid_until(
        quote_valid_days=policy_row.quote_valid_days,
        quote_valid_weekday=policy_row.quote_valid_weekday,
    )
    quote = Quote(
        case_id=case.id,
        price_policy_id=policy_row.id,
        round_number=0,
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
    await session.flush()
    bundle = load_content(get_settings().content_dir)
    ai = AIClient()
    plan = await ai.draft_plan(
        {
            "subject": "Industrial Widget 100 quotation",
            "contact_name": contact.name,
            "approved_product_key": product.approved_text_key,
        }
    )
    text, html_body = render_quote(
        plan=plan,
        bundle=bundle,
        product_key=product.approved_text_key,
        product_name=product.name,
        price=decision.unit_price,
        currency=policy_row.currency,
        quantity=quantity,
        unit=product.unit,
        incoterm=policy_row.standard_incoterm,
        payment_term=policy_row.standard_payment_term,
        valid_until=valid_until,
        taxes_included=policy_row.taxes_included,
        freight_included=policy_row.freight_included,
    )
    outbox = await stage_outbox(
        session,
        case=case,
        quote=quote,
        subject="Industrial Widget 100 quotation",
        text_body=text,
        html_body=html_body,
        business_key=business_key,
    )
    if outbox is None:
        await session.rollback()
