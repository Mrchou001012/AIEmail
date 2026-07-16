from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from pathlib import Path

from sqlalchemy import func, select

from app.db import (
    CaseStage,
    CaseStatus,
    Contact,
    Customer,
    DeliveryStatus,
    EmailMessage,
    Outbox,
    PricePolicy,
    Product,
    Quote,
    SalesCase,
    SessionLocal,
)
from app.domain import PricingPolicy, initial_quote, quote_valid_until
from app.imports import import_prices
from app.mail import normalized_subject
from app.services import active_policy, process_inbound, send_one_outbox
from app.settings import get_settings

PRICE_FILE = Path(
    os.getenv(
        "AIEMAIL_TEST_PRICE_FILE",
        "outputs/inr_price_policy_20260715/AIEmail_印度现货价格导入_20260715.xlsx",
    )
)
PREFERRED_PRODUCT = "YAC-TEOS40"
TEST_COMPANY = "AIEmail SMTP Self-Test Customer"


def pricing_policy(row: PricePolicy) -> PricingPolicy:
    return PricingPolicy(
        standard_price=Decimal(row.standard_price),
        absolute_floor=Decimal(row.absolute_floor),
        max_discount_pct=Decimal(row.max_discount_pct),
        concession_step_pct=Decimal(row.concession_step_pct),
        max_negotiation_rounds=row.max_negotiation_rounds,
        min_quantity=row.min_quantity,
        max_quantity=row.max_quantity,
        currency=row.currency,
        standard_incoterm=row.standard_incoterm,
        allowed_incoterms=tuple(row.allowed_incoterms),
        standard_payment_term=row.standard_payment_term,
        allowed_payment_terms=tuple(row.allowed_payment_terms),
        tier_1_max_multiple=(
            Decimal(row.tier_1_max_multiple) if row.tier_1_max_multiple is not None else None
        ),
        tier_1_markup_pct=Decimal(row.tier_1_markup_pct),
        tier_2_max_multiple=(
            Decimal(row.tier_2_max_multiple) if row.tier_2_max_multiple is not None else None
        ),
        tier_2_markup_pct=Decimal(row.tier_2_markup_pct),
    )


async def ensure_inr_prices() -> int:
    async with SessionLocal() as session:
        existing = await session.scalar(
            select(func.count(PricePolicy.id)).where(
                PricePolicy.currency == "INR",
                PricePolicy.active.is_(True),
            )
        )
        if existing:
            return 0
        if not PRICE_FILE.is_file():
            raise RuntimeError(f"INR price import file is missing: {PRICE_FILE}")
        preview = await import_prices(PRICE_FILE, session, apply=False)
        if not preview.ok:
            raise RuntimeError(
                f"INR price validation failed: {preview.errors[:5]}"
            )
        applied = await import_prices(PRICE_FILE, session, apply=True)
        if not applied.ok or applied.applied_rows != applied.valid_rows:
            raise RuntimeError(
                "INR price import did not apply every validated row: "
                f"valid={applied.valid_rows} applied={applied.applied_rows}"
            )
        return applied.applied_rows


async def create_reply_outbox(recipient: str) -> tuple[int, str, int, Decimal, str]:
    async with SessionLocal() as session:
        active_outbox = await session.scalar(
            select(func.count(Outbox.id)).where(
                Outbox.status.in_(
                    [
                        DeliveryStatus.PENDING,
                        DeliveryStatus.CLAIMED,
                        DeliveryStatus.FAILED,
                        DeliveryStatus.UNKNOWN,
                    ]
                )
            )
        )
        if active_outbox:
            raise RuntimeError(
                f"refusing test send because {active_outbox} active outbox record(s) already exist"
            )

        product = await session.scalar(select(Product).where(Product.code == PREFERRED_PRODUCT))
        if product is None:
            raise RuntimeError(f"product {PREFERRED_PRODUCT} was not imported")
        price_row = await active_policy(session, product.id, "INR")
        if price_row is None:
            raise RuntimeError(f"no active INR price exists for {PREFERRED_PRODUCT}")

        quantity = price_row.min_quantity * 4
        if price_row.max_quantity is not None and quantity > price_row.max_quantity:
            quantity = price_row.min_quantity
        price_decision = initial_quote(pricing_policy(price_row), quantity)
        if not price_decision.approved or price_decision.unit_price is None:
            raise RuntimeError(f"test quotation was rejected: {price_decision.reason}")

        customer = await session.scalar(
            select(Customer).where(Customer.company_name == TEST_COMPANY)
        )
        if customer is None:
            customer = Customer(
                company_name=TEST_COMPANY,
                language="en",
                auto_send_allowed=True,
                consent_basis="explicit owner-authorized SMTP self-test",
            )
            session.add(customer)
            await session.flush()
        else:
            customer.auto_send_allowed = True
            customer.do_not_contact = False

        contact = await session.scalar(
            select(Contact).where(
                Contact.customer_id == customer.id,
                Contact.email == recipient,
            )
        )
        if contact is None:
            contact = Contact(
                customer_id=customer.id,
                name="Test Customer",
                email=recipient,
                language="en",
            )
            session.add(contact)
            await session.flush()
        else:
            contact.suppressed = False

        run_token = uuid.uuid4().hex
        subject = f"[TEST] Inquiry for {product.code} ({run_token[:8]})"
        sales_case = SalesCase(
            customer_id=customer.id,
            contact_id=contact.id,
            product_id=product.id,
            currency="INR",
            stage=CaseStage.QUOTING,
            status=CaseStatus.ACTIVE,
            subject_key=normalized_subject(subject),
        )
        session.add(sales_case)
        await session.flush()

        valid_until = quote_valid_until(
            quote_valid_days=price_row.quote_valid_days,
            quote_valid_weekday=price_row.quote_valid_weekday,
        )
        session.add(
            Quote(
                case_id=sales_case.id,
                price_policy_id=price_row.id,
                round_number=0,
                unit_price=price_decision.unit_price,
                currency="INR",
                quantity=quantity,
                incoterm=price_row.standard_incoterm,
                payment_term=price_row.standard_payment_term,
                valid_until=valid_until,
                pricing_snapshot={
                    "test_fixture": True,
                    "pricing_reason": price_decision.reason,
                },
            )
        )

        inquiry_body = (
            "Dear Shreya,\n\n"
            f"We are interested in PRODUCT {product.code}, quantity {quantity} kg. "
            "Please quote your current price.\n\n"
            "Best regards,\nTest Customer"
        )
        inbound_message_id = f"<aiemail-self-test-{run_token}@example.test>"
        raw_hash = hashlib.sha256(
            f"{inbound_message_id}\n{subject}\n{inquiry_body}".encode()
        ).hexdigest()
        inbound = EmailMessage(
            case_id=sales_case.id,
            direction="INBOUND",
            mailbox="smtp-self-test",
            mailbox_folder="SIMULATED",
            message_id=inbound_message_id,
            from_address=recipient,
            to_addresses=[parseaddr(get_settings().mail_from)[1]],
            subject=subject,
            body_text=inquiry_body,
            attachment_metadata=[],
            raw_sha256=raw_hash,
            received_at=datetime.now(UTC),
        )
        session.add(inbound)
        await session.commit()

        await process_inbound(session, inbound.id)
        outbox = await session.scalar(
            select(Outbox).where(Outbox.business_key == f"inbound-reply:{inbound.id}")
        )
        if outbox is None:
            raise RuntimeError("the simulated inquiry did not produce an autonomous reply")
        if outbox.recipient.lower() != recipient:
            raise RuntimeError("generated recipient does not match the sole allowlist address")
        if outbox.status != DeliveryStatus.PENDING:
            raise RuntimeError(f"generated reply is not pending: {outbox.status.value}")

        parsed = BytesParser(policy=policy.default).parsebytes(outbox.raw_message.encode("utf-8"))
        html_part = parsed.get_body(preferencelist=("html",))
        html_body = html_part.get_content() if html_part is not None else ""
        if "lanyachem-logo" not in html_body or "Shreya Saxena" not in outbox.raw_message:
            raise RuntimeError("generated reply is missing the configured HTML signature or logo")
        return outbox.id, product.code, quantity, Decimal(price_decision.unit_price), subject


async def send_created_outbox(outbox_id: int) -> str:
    settings = get_settings()
    async with SessionLocal() as session:
        did_work = await send_one_outbox(session, settings)
        if not did_work:
            raise RuntimeError("SMTP sender found no eligible outbox record")
        row = await session.get(Outbox, outbox_id)
        if row is None:
            raise RuntimeError("test outbox disappeared")
        if row.status != DeliveryStatus.SENT:
            raise RuntimeError(
                f"SMTP test did not reach SENT: status={row.status.value} detail={row.last_error}"
            )
        return row.message_id


async def main() -> None:
    settings = get_settings()
    recipients = sorted({item.strip().lower() for item in settings.recipient_allowlist if item.strip()})
    if settings.mail_transport != "smtp":
        raise RuntimeError("MAIL_TRANSPORT must be smtp for this one-off test")
    if not settings.safe_mode or not settings.auto_send_enabled:
        raise RuntimeError("SAFE_MODE and AUTO_SEND_ENABLED must both be true for this one-off test")
    if len(recipients) != 1:
        raise RuntimeError("the one-off test requires exactly one allowlisted recipient")
    if not settings.gmail_address or not settings.gmail_app_password:
        raise RuntimeError("Gmail SMTP credentials are not configured")
    if parseaddr(settings.mail_from)[1].lower() != settings.gmail_address.lower():
        raise RuntimeError("MAIL_FROM must match GMAIL_ADDRESS for this SMTP test")

    imported = await ensure_inr_prices()
    outbox_id, product_code, quantity, price, inquiry_subject = await create_reply_outbox(
        recipients[0]
    )
    message_id = await send_created_outbox(outbox_id)
    print(f"price_rows_imported={imported}")
    print(f"test_product={product_code}")
    print(f"test_quantity_kg={quantity}")
    print(f"test_unit_price_inr={price:.4f}")
    print(f"test_inquiry_subject={inquiry_subject}")
    print(f"outbox_id={outbox_id}")
    print(f"message_id={message_id}")
    print("delivery_status=SENT")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"test_send_failed={type(exc).__name__}: {exc}", file=sys.stderr)
        raise
