"""Human handoff review, draft preview, assignment, and reply workflows."""

import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.common.service_core import audit
from app.db import CaseStage, CaseStatus, Contact, EmailMessage, Handoff, Outbox, Product, SalesCase
from app.mail import normalized_subject

logger = logging.getLogger(__name__)


async def assign_handoff_case(
    session: AsyncSession,
    *,
    handoff_id: int,
    case_id: int,
    actor: str,
) -> Handoff:
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None:
        raise ValueError("handoff not found")
    if handoff.source_email_id is None:
        raise ValueError("handoff has no source email to associate")
    email_row = await session.get(EmailMessage, handoff.source_email_id)
    case = await session.scalar(select(SalesCase).options(selectinload(SalesCase.contact)).where(SalesCase.id == case_id))
    if email_row is None or case is None:
        raise ValueError("source email or case not found")
    if email_row.direction != "INBOUND":
        raise ValueError("only inbound email can be associated with a handoff case")
    if case.status in {CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST}:
        raise ValueError("closed case cannot accept a new inbound email")
    if email_row.from_address.casefold() != case.contact.email.casefold():
        raise ValueError("source sender does not match the selected case contact")
    if email_row.case_id not in {None, case.id}:
        raise ValueError("source email is already associated with a different case")

    previous_case_id = handoff.case_id
    email_row.case_id = case.id
    email_row.customer_id = case.customer_id
    email_row.contact_id = case.contact_id
    handoff.case_id = case.id
    case.status = CaseStatus.WAITING_HUMAN
    await audit(
        session,
        "handoff.case_assigned",
        case_id=case.id,
        actor=actor,
        data={
            "handoff_id": handoff.id,
            "email_id": email_row.id,
            "previous_case_id": previous_case_id,
        },
    )
    await session.commit()
    return handoff


async def create_case_for_handoff(
    session: AsyncSession,
    *,
    handoff_id: int,
    contact_id: int,
    product_id: int | None,
    currency: str,
    actor: str,
) -> SalesCase:
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None:
        raise ValueError("handoff not found")
    if handoff.source_email_id is None:
        raise ValueError("handoff has no source email")
    email_row = await session.get(EmailMessage, handoff.source_email_id)
    contact = await session.get(Contact, contact_id)
    product = await session.get(Product, product_id) if product_id is not None else None
    normalized_currency = currency.strip().upper()
    if email_row is None or contact is None:
        raise ValueError("source email or contact not found")
    if product_id is not None and product is None:
        raise ValueError("product not found")
    if email_row.direction != "INBOUND":
        raise ValueError("only inbound email can create a reviewed case")
    if email_row.from_address.casefold() != contact.email.casefold():
        raise ValueError("source sender does not match the selected contact")
    if product is not None and not product.active:
        raise ValueError("inactive product cannot be selected")
    if not re.fullmatch(r"[A-Z]{3}", normalized_currency):
        raise ValueError("currency must be a three-letter code")
    if email_row.case_id is not None or handoff.case_id is not None:
        raise ValueError("handoff is already associated with a case")

    sales_case = SalesCase(
        customer_id=contact.customer_id,
        contact_id=contact.id,
        product_id=product.id if product is not None else None,
        currency=normalized_currency,
        stage=CaseStage.QUOTING if product is not None else CaseStage.FOLLOW_UP,
        status=CaseStatus.WAITING_HUMAN,
        subject_key=normalized_subject(email_row.subject)[:255],
    )
    session.add(sales_case)
    await session.flush()
    email_row.case_id = sales_case.id
    email_row.customer_id = sales_case.customer_id
    email_row.contact_id = sales_case.contact_id
    handoff.case_id = sales_case.id
    await audit(
        session,
        "handoff.case_created",
        case_id=sales_case.id,
        actor=actor,
        data={
            "handoff_id": handoff.id,
            "email_id": email_row.id,
            "contact_id": contact.id,
            "product_id": product.id if product is not None else None,
            "product_pending": product is None,
            "currency": normalized_currency,
        },
    )
    await session.commit()
    return sales_case


async def update_handoff_case_product(
    session: AsyncSession,
    *,
    handoff_id: int,
    product_id: int,
    actor: str,
) -> SalesCase:
    """Set or replace the product for an open, human-reviewed case."""
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None:
        raise ValueError("handoff not found")
    if handoff.status != "OPEN":
        raise ValueError("only an open handoff can update its case product")
    if handoff.case_id is None:
        raise ValueError("handoff must be associated with a case")
    if await session.scalar(select(Outbox.id).where(Outbox.approval_handoff_id == handoff.id)):
        raise ValueError("handoff already has an approved outbound email")

    sales_case = await session.get(SalesCase, handoff.case_id)
    product = await session.get(Product, product_id)
    if sales_case is None or product is None:
        raise ValueError("case or product not found")
    if sales_case.status in {CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST}:
        raise ValueError("closed case product cannot be changed")
    if not product.active:
        raise ValueError("inactive product cannot be selected")

    previous_product_id = sales_case.product_id
    sales_case.product_id = product.id
    sales_case.stage = CaseStage.QUOTING
    sales_case.status = CaseStatus.WAITING_HUMAN
    await audit(
        session,
        "handoff.case_product_updated",
        case_id=sales_case.id,
        actor=actor,
        data={
            "handoff_id": handoff.id,
            "previous_product_id": previous_product_id,
            "product_id": product.id,
        },
    )
    await session.commit()
    return sales_case
