"""Inquiry resolution workflow."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.ai import explicit_product_list_requested, generic_product_list_requested
from app.common.service_core import CaseLessReactivationParent, NewInquiryResolution
from app.db import CaseStage, CaseStatus, Contact, EmailMessage, PricePolicy, Product, Quote, SalesCase
from app.domain import HandoffReason
from app.inbound.inquiry_matching import (
    _explicit_product_codes,
    _prior_thread_marker,
    _product_lookup_conditions,
    _resolve_category_inquiry_case,
)
from app.mail import ParsedEmail, has_thread_subject_prefix, normalized_subject
from app.settings import get_settings


async def _resolve_new_inquiry_case(
    session: AsyncSession,
    parsed: ParsedEmail,
    *,
    trusted_reactivation_parent: CaseLessReactivationParent | None = None,
) -> NewInquiryResolution:
    sender = parsed.from_address.strip().lower()
    facts: dict[str, Any] = {
        "new_thread": trusted_reactivation_parent is None,
        "sender": sender,
        "subject": parsed.subject,
    }
    if trusted_reactivation_parent is not None:
        facts.update(
            {
                "reactivation_outbox_id": trusted_reactivation_parent.outbox.id,
                "reactivation_recipient_id": trusted_reactivation_parent.recipient.id,
                "match_basis": (
                    "exact_case_less_reactivation_thread_same_company_domain"
                    if trusted_reactivation_parent.sender_changed
                    else "exact_case_less_reactivation_thread"
                ),
                "sender_changed": trusted_reactivation_parent.sender_changed,
                "original_contact_id": (trusted_reactivation_parent.original_contact.id),
                "reply_contact_id": trusted_reactivation_parent.reply_contact.id,
                "reply_contact_created": (trusted_reactivation_parent.reply_contact_created),
                "matched_domain": trusted_reactivation_parent.matched_domain,
            }
        )
    if not sender:
        return NewInquiryResolution(
            None,
            HandoffReason.NEW_INQUIRY_REVIEW,
            "New inbound thread has no reliable sender address",
            facts,
        )

    combined_text = f"{parsed.subject}\n{parsed.body_text}"
    marker = _prior_thread_marker(combined_text)
    if marker:
        return NewInquiryResolution(
            None,
            HandoffReason.THREAD_AMBIGUOUS,
            "New email thread refers to prior commercial history and requires manual linking",
            {**facts, "prior_context_marker": marker},
        )

    contacts = (
        (await session.execute(select(Contact).options(selectinload(Contact.customer)).where(func.lower(Contact.email) == sender)))
        .scalars()
        .all()
    )
    if len(contacts) != 1:
        return NewInquiryResolution(
            None,
            HandoffReason.NEW_INQUIRY_REVIEW,
            (
                "New inbound thread sender is not a known contact"
                if not contacts
                else "New inbound thread sender matches multiple customer records"
            ),
            {**facts, "matching_contact_count": len(contacts)},
        )
    contact = contacts[0]

    product_codes = _explicit_product_codes(combined_text)
    facts.update(
        {
            "contact_id": contact.id,
            "customer_id": contact.customer_id,
            "product_codes": product_codes,
        }
    )
    if len(product_codes) != 1:
        if not product_codes:
            category_resolution = await _resolve_category_inquiry_case(
                session,
                contact=contact,
                incoming_subject_key=normalized_subject(parsed.subject)[:255],
                facts=facts,
            )
            if category_resolution is not None:
                return category_resolution
            # An explicit list request from one known contact is safe to
            # represent as a product/category-pending case.  This lets the
            # normal AI job run bounded company research instead of creating a
            # premature case-less handoff.  Multiple internal interests remain
            # ambiguous and are never overridden by public web evidence.
            if (
                (get_settings().company_research_enabled or generic_product_list_requested(combined_text))
                and explicit_product_list_requested(combined_text)
                and not facts.get("active_interest_categories")
            ):
                currency_rows = await session.execute(
                    select(SalesCase.currency).where(
                        SalesCase.customer_id == contact.customer_id,
                        SalesCase.status.not_in([CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST]),
                    )
                )
                currencies = set(currency_rows.scalars().all())
                currency = next(iter(currencies)) if len(currencies) == 1 else "USD"
                sales_case = SalesCase(
                    customer_id=contact.customer_id,
                    contact_id=contact.id,
                    product_id=None,
                    category_id=None,
                    currency=currency,
                    stage=CaseStage.QUOTING,
                    status=CaseStatus.ACTIVE,
                    subject_key=normalized_subject(parsed.subject)[:255],
                )
                session.add(sales_case)
                await session.flush()
                return NewInquiryResolution(
                    sales_case,
                    facts={
                        **facts,
                        "currency": currency,
                        "product_pending": True,
                        "category_pending": True,
                        "match_basis": (
                            "generic_product_list_pending_catalog"
                            if generic_product_list_requested(combined_text)
                            else "explicit_product_list_pending_company_research"
                        ),
                    },
                )
            if explicit_product_list_requested(combined_text) and not facts.get("active_interest_categories"):
                return NewInquiryResolution(
                    None,
                    HandoffReason.PRODUCT_CATEGORY_REVIEW,
                    "Product-list request has no unique CRM/Excel category; company research is disabled",
                    {
                        **facts,
                        "product_pending": True,
                        "category_pending": True,
                        "company_research": {"status": "DISABLED"},
                    },
                )
        elif len(product_codes) > 1:
            # Multi-product quotation: keep the whole thread for automatic
            # processing only when every named product exists in the active
            # catalog. Any unknown product sends the entire request to a human.
            active_products = (
                (
                    await session.execute(
                        select(Product).where(
                            _product_lookup_conditions(product_codes),
                            Product.active.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if len(active_products) != len(set(product_codes)):
                return NewInquiryResolution(
                    None,
                    HandoffReason.NEW_INQUIRY_REVIEW,
                    "New inbound thread names a product that is not active in the catalog",
                    {**facts, "unknown_product": True},
                )
            currency_rows = await session.execute(
                select(SalesCase.currency).where(
                    SalesCase.customer_id == contact.customer_id,
                    SalesCase.status.not_in([CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST]),
                )
            )
            currencies = set(currency_rows.scalars().all())
            currency = next(iter(currencies)) if len(currencies) == 1 else "USD"
            sales_case = SalesCase(
                customer_id=contact.customer_id,
                contact_id=contact.id,
                product_id=None,
                category_id=None,
                currency=currency,
                stage=CaseStage.QUOTING,
                status=CaseStatus.ACTIVE,
                subject_key=normalized_subject(parsed.subject)[:255],
            )
            session.add(sales_case)
            await session.flush()
            return NewInquiryResolution(
                sales_case,
                facts={
                    **facts,
                    "currency": currency,
                    "product_pending": True,
                    "multi_product": True,
                    "match_basis": "explicit_multi_product_inquiry",
                },
            )
        return NewInquiryResolution(
            None,
            HandoffReason.NEW_INQUIRY_REVIEW,
            (
                "New inbound thread does not identify a supported product"
                if not product_codes
                else "New inbound thread mentions multiple products"
            ),
            facts,
        )

    product = await session.scalar(
        select(Product)
        .where(
            _product_lookup_conditions(product_codes),
            Product.active.is_(True),
        )
        .order_by(Product.id)
        .limit(1)
    )
    if product is None:
        return NewInquiryResolution(
            None,
            HandoffReason.NEW_INQUIRY_REVIEW,
            "New inbound thread names a product that is not active in the catalog",
            facts,
        )

    today = date.today()
    policy_rows = await session.execute(
        select(PricePolicy.currency).where(
            PricePolicy.product_id == product.id,
            PricePolicy.active.is_(True),
            PricePolicy.valid_from <= today,
            (PricePolicy.valid_to.is_(None) | (PricePolicy.valid_to >= today)),
        )
    )
    policy_currencies = set(policy_rows.scalars().all())
    customer_currency_rows = await session.execute(
        select(SalesCase.currency).where(
            SalesCase.customer_id == contact.customer_id,
            SalesCase.status.not_in([CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST]),
        )
    )
    customer_currencies = set(customer_currency_rows.scalars().all())
    if len(policy_currencies) == 1:
        currency = next(iter(policy_currencies))
    elif len(policy_currencies & customer_currencies) == 1:
        currency = next(iter(policy_currencies & customer_currencies))
    elif not policy_currencies and len(customer_currencies) == 1:
        # Manual-only products can still be represented as a case and routed to
        # a human using the customer's established market currency.
        currency = next(iter(customer_currencies))
    elif not policy_currencies and not customer_currencies and not explicit_product_list_requested(combined_text):
        # Unpriced product in a quotation context with no customer history:
        # still create the case with the default INR market so the human can
        # price it from the handoff (the review screen then sends the
        # quotation automatically). Explicit catalog requests stay case-less
        # for the product-list backfill pipeline.
        currency = "INR"
    else:
        return NewInquiryResolution(
            None,
            HandoffReason.NEW_INQUIRY_REVIEW,
            "New inbound thread currency cannot be selected unambiguously",
            {
                **facts,
                "policy_currencies": sorted(policy_currencies),
                "customer_currencies": sorted(customer_currencies),
            },
        )

    related_case_ids = (
        (
            await session.execute(
                select(SalesCase.id).where(
                    SalesCase.contact_id == contact.id,
                    SalesCase.product_id == product.id,
                    SalesCase.status.not_in([CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST]),
                )
            )
        )
        .scalars()
        .all()
    )
    recent_cutoff = datetime.now(UTC) - timedelta(days=7)
    recent_related_case_ids = (
        (
            await session.execute(
                select(SalesCase.id).where(
                    SalesCase.id.in_(related_case_ids),
                    or_(
                        SalesCase.id.in_(
                            select(EmailMessage.case_id).where(
                                EmailMessage.case_id.is_not(None),
                                EmailMessage.received_at >= recent_cutoff,
                            )
                        ),
                        SalesCase.id.in_(select(Quote.case_id).where(Quote.valid_until >= today)),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    incoming_subject_key = normalized_subject(parsed.subject)[:255]
    if recent_related_case_ids and has_thread_subject_prefix(parsed.subject):
        strong_matches = (
            (
                await session.execute(
                    select(SalesCase).where(
                        SalesCase.id.in_(recent_related_case_ids),
                        SalesCase.currency == currency,
                        SalesCase.subject_key == incoming_subject_key,
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(strong_matches) == 1:
            return NewInquiryResolution(
                strong_matches[0],
                facts={
                    **facts,
                    "product_id": product.id,
                    "currency": currency,
                    "possible_related_case_ids": related_case_ids,
                    "recent_related_case_ids": recent_related_case_ids,
                    "recovered_thread": True,
                    "match_basis": "unique_recent_contact_product_currency_subject",
                },
            )
    if recent_related_case_ids:
        return NewInquiryResolution(
            None,
            HandoffReason.THREAD_AMBIGUOUS,
            "New email thread may belong to a recent active case and requires manual linking",
            {
                **facts,
                "product_id": product.id,
                "currency": currency,
                "possible_related_case_ids": related_case_ids,
                "recent_related_case_ids": recent_related_case_ids,
                "recent_activity_cutoff": recent_cutoff.isoformat(),
            },
        )
    sales_case = SalesCase(
        customer_id=contact.customer_id,
        contact_id=contact.id,
        product_id=product.id,
        currency=currency,
        stage=CaseStage.QUOTING,
        status=CaseStatus.ACTIVE,
        subject_key=incoming_subject_key,
    )
    session.add(sales_case)
    await session.flush()
    return NewInquiryResolution(
        sales_case,
        facts={
            **facts,
            "product_id": product.id,
            "currency": currency,
            "possible_related_case_ids": related_case_ids,
        },
    )
