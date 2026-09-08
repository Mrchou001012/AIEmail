"""Shared primitives for capability-focused sales service modules."""

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parseaddr
from functools import wraps
from typing import Any, Concatenate
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import (
    AuditEvent,
    CaseStatus,
    Contact,
    DeliveryStatus,
    EmailMessage,
    Handoff,
    Outbox,
    PricePolicy,
    Product,
    ProductCategory,
    Quote,
    ReactivationRecipient,
    SalesCase,
)
from app.domain import HandoffReason, PricingPolicy
from app.handoffs.agent_runtime import ensure_handoff_agent_run
from app.jobs import enqueue_job
from app.mail import InlineImageAsset, OutboundAttachment, build_message, parse_mime
from app.rag_retrieval import LocalRAGRetriever
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


PAYMENT_DETAILS_REQUEST_PATTERN = re.compile(
    r"\b(?:payment\s+(?:support|terms?|methods?|options?|details?)|"
    r"accepted\s+payment(?:\s+(?:methods?|options?))?|"
    r"how\s+(?:can|do|should)\s+(?:we|i)\s+pay)\b",
    re.IGNORECASE,
)


def _retrieve_historical_style_examples(
    settings: Settings,
    *,
    subject: str,
    body: str,
    intent: str,
) -> list[dict[str, Any]]:
    retriever = LocalRAGRetriever(settings.rag_index_path)
    matches = retriever.retrieve(
        f"Intent: {intent}\nSubject: {subject}\nCustomer request:\n{body}",
        intent=intent,
        top_k=settings.rag_top_k,
        min_similarity=settings.rag_min_similarity,
    )
    return [match.prompt_document() for match in matches]


def _payment_details_requested(text: str) -> bool:
    return PAYMENT_DETAILS_REQUEST_PATTERN.search(text) is not None


@dataclass(frozen=True)
class CustomerPaymentTerm:
    term: str
    source: str
    quote_id: int | None = None


async def _customer_payment_term(
    session: AsyncSession,
    *,
    customer_id: int,
) -> CustomerPaymentTerm:
    """Use the latest actually-sent structured quote, otherwise prepayment.

    Historical email prose and RAG examples are intentionally excluded: they
    are useful for writing style, but are not an approved commercial ledger.
    """

    historical = await session.execute(
        select(Quote.id, Quote.payment_term)
        .join(SalesCase, Quote.case_id == SalesCase.id)
        .join(Outbox, Outbox.quote_id == Quote.id)
        .where(
            SalesCase.customer_id == customer_id,
            Outbox.status == DeliveryStatus.SENT,
            Outbox.sent_at.is_not(None),
        )
        .order_by(Outbox.sent_at.desc(), Quote.created_at.desc(), Quote.id.desc())
        .limit(1)
    )
    row = historical.first()
    if row is not None:
        quote_id, value = row
        clean = str(value or "").strip()
        if clean:
            return CustomerPaymentTerm(
                term=clean,
                source="latest_sent_quote",
                quote_id=int(quote_id),
            )
    return CustomerPaymentTerm(term="Prepayment", source="new_customer_default")


def _payment_term_sentence(payment: CustomerPaymentTerm) -> str:
    if payment.source == "new_customer_default":
        return "For a first order, our standard payment term is prepayment."
    return f"We can continue with the payment term used previously: {payment.term}."


async def _catalog_category_breakdown(
    session: AsyncSession,
    products: Sequence[Product],
) -> list[dict[str, Any]]:
    category_ids = sorted({int(product.category_id) for product in products if product.category_id is not None})
    if not category_ids:
        return []
    categories = list(
        (
            await session.scalars(
                select(ProductCategory).where(ProductCategory.id.in_(category_ids)).order_by(ProductCategory.sort_order, ProductCategory.id)
            )
        ).all()
    )
    counts: dict[int, int] = {}
    for product in products:
        if product.category_id is not None:
            counts[int(product.category_id)] = counts.get(int(product.category_id), 0) + 1
    return [
        {
            "category_id": category.id,
            "category_key": category.key,
            "category_name": category.name,
            "product_count": counts.get(category.id, 0),
        }
        for category in categories
        if counts.get(category.id, 0)
    ]


@dataclass(frozen=True)
class NewInquiryResolution:
    case: SalesCase | None
    reason: HandoffReason | None = None
    summary: str | None = None
    facts: dict[str, Any] | None = None


@dataclass(frozen=True)
class CaseLessReactivationParent:
    outbox: Outbox
    recipient: ReactivationRecipient
    original_contact: Contact
    reply_contact: Contact
    sender_changed: bool = False
    reply_contact_created: bool = False
    matched_domain: str | None = None


@dataclass(frozen=True)
class HumanApproval:
    handoff_id: int
    approved_by: str
    approved_at: datetime


def _human_approval_from_outbox(row: Outbox) -> HumanApproval | None:
    """Return the one authoritative approval value for an outbox row."""
    approved_by = (row.human_approved_by or "").strip()
    if row.approval_handoff_id is None or not approved_by or row.human_approved_at is None:
        return None
    return HumanApproval(
        handoff_id=row.approval_handoff_id,
        approved_by=approved_by,
        approved_at=row.human_approved_at,
    )


def _atomic_business_operation[**P, T](
    operation: Callable[Concatenate[AsyncSession, P], Awaitable[T]],
) -> Callable[Concatenate[AsyncSession, P], Awaitable[T]]:
    """Roll back all staged business state when a top-level operation fails."""

    @wraps(operation)
    async def wrapped(session: AsyncSession, *args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return await operation(session, *args, **kwargs)
        except BaseException:
            await session.rollback()
            raise

    return wrapped


def _pricing_policy(row: PricePolicy) -> PricingPolicy:
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
        tier_1_max_multiple=Decimal(row.tier_1_max_multiple) if row.tier_1_max_multiple is not None else None,
        tier_1_markup_pct=Decimal(row.tier_1_markup_pct),
        tier_2_max_multiple=Decimal(row.tier_2_max_multiple) if row.tier_2_max_multiple is not None else None,
        tier_2_markup_pct=Decimal(row.tier_2_markup_pct),
    )


async def audit(
    session: AsyncSession,
    event_type: str,
    *,
    case_id: int | None,
    actor: str,
    data: dict[str, Any] | None = None,
) -> None:
    session.add(AuditEvent(case_id=case_id, actor=actor, event_type=event_type, data=data or {}))


async def create_handoff(
    session: AsyncSession,
    *,
    case: SalesCase | None,
    reason: HandoffReason,
    summary: str,
    facts: dict[str, Any] | None = None,
    source_email_id: int | None = None,
    update_existing: bool = False,
    pause_case: bool = True,
) -> Handoff:
    created = False
    try:
        async with session.begin_nested():
            handoff = Handoff(
                case_id=case.id if case else None,
                source_email_id=source_email_id,
                reason_code=reason.value,
                summary=summary,
                extracted_facts=facts or {},
            )
            session.add(handoff)
            await session.flush()
            created = True
    except IntegrityError as exc:
        if source_email_id is None:
            raise
        handoff = await session.scalar(select(Handoff).where(Handoff.source_email_id == source_email_id))
        if handoff is None:
            raise
        expected_case_id = case.id if case else None
        if handoff.case_id != expected_case_id:
            raise RuntimeError(f"email {source_email_id} is already attached to a different case handoff") from exc
        if update_existing:
            if handoff.status != "OPEN":
                raise ValueError("cannot update a resolved handoff") from exc
            handoff.reason_code = reason.value
            handoff.summary = summary
            handoff.extracted_facts = facts or {}

    # Every inbound handoff is a durable paused Agent task.  Only handoff
    # types with a strict response schema receive typed assistance; all other
    # high-risk cases remain waiting for normal human resolution.
    if source_email_id is not None:
        await ensure_handoff_agent_run(session, handoff=handoff)

    if created:
        if pause_case and case and case.status == CaseStatus.ACTIVE:
            case.status = CaseStatus.WAITING_HUMAN
        await audit(
            session,
            "handoff.created",
            case_id=case.id if case else None,
            actor="system",
            data={"handoff_id": handoff.id, "reason": reason.value, "source_email_id": source_email_id},
        )
        await session.commit()
    elif update_existing:
        await audit(
            session,
            "handoff.updated_for_resume",
            case_id=case.id if case else None,
            actor="agent-runtime",
            data={
                "handoff_id": handoff.id,
                "reason": reason.value,
                "source_email_id": source_email_id,
            },
        )
        await session.commit()
    await enqueue_job(
        session,
        "notify_handoff",
        {"handoff_id": handoff.id},
        f"handoff-notify:{handoff.id}",
    )
    return handoff


async def active_policy(session: AsyncSession, product_id: int, currency: str) -> PricePolicy | None:
    settings = get_settings()
    today = datetime.now(UTC).astimezone(ZoneInfo(settings.business_timezone)).date()
    return await session.scalar(
        select(PricePolicy)
        .where(
            PricePolicy.product_id == product_id,
            PricePolicy.currency == currency,
            PricePolicy.active.is_(True),
            PricePolicy.valid_from <= today,
            (PricePolicy.valid_to.is_(None) | (PricePolicy.valid_to >= today)),
        )
        .order_by(PricePolicy.valid_from.desc())
    )


async def stage_outbox(
    session: AsyncSession,
    *,
    case: SalesCase,
    quote: Quote | None = None,
    message_kind: str = "AUTO_QUOTE",
    recipient: str | None = None,
    subject: str,
    text_body: str,
    html_body: str,
    business_key: str,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    inline_images: tuple[InlineImageAsset, ...] = (),
    attachments: tuple[OutboundAttachment, ...] = (),
    approval: HumanApproval | None = None,
) -> Outbox | None:
    """Stage immutable outbound records without owning the caller transaction."""
    mail_recipient = recipient or case.contact.email
    message_id, raw = build_message(
        from_address=get_settings().mail_from,
        recipient=mail_recipient,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        stable_key=business_key,
        in_reply_to=in_reply_to,
        references=references,
        inline_images=inline_images,
        attachments=attachments,
    )
    parsed_outbound = parse_mime(raw.encode("utf-8"))
    try:
        async with session.begin_nested():
            row = Outbox(
                case_id=case.id,
                quote_id=quote.id if quote is not None else None,
                message_kind=message_kind,
                business_key=business_key,
                message_id=message_id,
                recipient=mail_recipient,
                raw_message=raw,
                approval_handoff_id=(approval.handoff_id if approval else None),
                human_approved_by=(approval.approved_by if approval else None),
                human_approved_at=(approval.approved_at if approval else None),
            )
            session.add(row)
            await session.flush()
            session.add(
                EmailMessage(
                    case_id=case.id,
                    customer_id=case.customer_id,
                    contact_id=case.contact_id,
                    direction="OUTBOUND",
                    message_id=message_id,
                    in_reply_to=in_reply_to,
                    references_json=references or [],
                    from_address=parseaddr(get_settings().mail_from)[1],
                    to_addresses=[mail_recipient],
                    subject=subject,
                    body_text=text_body,
                    body_html=html_body,
                    attachment_metadata=parsed_outbound.attachments,
                    raw_sha256=parsed_outbound.raw_sha256,
                )
            )
            await audit(
                session,
                "outbox.frozen",
                case_id=case.id,
                actor="system",
                data={
                    "outbox_id": row.id,
                    "message_id": message_id,
                    "message_kind": message_kind,
                    "approval_handoff_id": approval.handoff_id if approval else None,
                    "attachments": [
                        {
                            "filename": item["filename"],
                            "content_type": item["content_type"],
                            "size": item["size"],
                            "sha256": item["sha256"],
                        }
                        for item in parsed_outbound.attachments
                        if item.get("disposition") == "attachment"
                    ],
                    **({"quote_id": quote.id} if quote is not None else {}),
                },
            )
            await session.flush()
        return row
    except IntegrityError:
        return None


def _all_products_catalog_category() -> ProductCategory:
    return ProductCategory(
        key="all_products",
        name="All Products",
        name_zh=None,
        active=True,
        sort_order=0,
    )
