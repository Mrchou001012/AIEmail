import asyncio
import hashlib
import html
import json
import logging
import re
import smtplib
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from email.utils import parseaddr
from functools import wraps
from typing import Any, Concatenate
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy import case as sa_case
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agent_runtime import (
    COA_LOOKUP_REQUEST_TYPE,
    ensure_handoff_agent_run,
    finalize_handoff_agent_run,
)
from app.ai import (
    AIClient,
    CompanyCategoryDecision,
    CompanyResearchSource,
    InboundAnalysis,
    explicit_product_list_requested,
    extract_quantity_kg,
    generic_product_list_requested,
    render_draft_preview,
    requested_product_list_file_format,
    stub_analyze,
    validate_rendered_email,
)
from app.auto_replies import AutomatedReplyType, classify_automated_reply
from app.bounces import (
    BounceType,
    classify_bounce,
    classify_smtp_failure,
    has_permanent_failure_evidence,
)
from app.coa_catalog import COACatalog, COAFindStatus
from app.commercial import (
    QuoteContext,
    QuoteContextStatus,
    get_commercial_data_provider,
    get_or_create_current_cycle,
    is_business_day,
    is_commercial_day,
    is_commercial_open,
    lock_commercial_scope,
    next_business_open,
)
from app.db import (
    AgentRun,
    AgentRunStatus,
    AgentStep,
    AgentStepStatus,
    AIInvocation,
    AssistanceRequest,
    AssistanceStatus,
    AuditEvent,
    CaseStage,
    CaseStatus,
    CommercialDataCycle,
    Contact,
    Customer,
    DeliveryStatus,
    EmailAddressStatus,
    EmailDomainStatus,
    EmailMessage,
    ForwardRecipient,
    Handoff,
    Job,
    JobStatus,
    MailboxThrottle,
    Outbox,
    PricePolicy,
    Product,
    ProductCategory,
    Quote,
    ReactivationRecipient,
    SalesCase,
)
from app.deliverability import MXResult, MXStatus, lookup_mx, validate_address_format
from app.domain import (
    HandoffReason,
    Intent,
    PricingPolicy,
    SendContext,
    evaluate_send_policy,
    initial_quote,
    quote_valid_until,
    transition,
)
from app.history import resolve_unique_contact
from app.imports import ContentBundle, load_content
from app.integrations import DingTalkNotifier
from app.mail import (
    FullReplySource,
    GmailIMAPClient,
    InlineImageAsset,
    OutboundAttachment,
    ParsedEmail,
    _sanitize_quoted_html,
    append_quoted_reply,
    attachments_require_review,
    build_message,
    extract_full_reply_source,
    has_thread_subject_prefix,
    html_requires_mime_resources,
    match_case,
    normalized_subject,
    parse_mime,
    transport_for,
)
from app.product_catalog import (
    active_category_keys,
    build_product_list_attachment,
    customer_interest_keys,
    render_product_list_email,
)
from app.products import (
    canonical_product_code,
    find_product_codes,
    product_codes_match,
    product_text_key,
)
from app.rag_retrieval import LocalRAGRetriever
from app.reactivation import reactivation_send_guard, record_reactivation_reply
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

    historical = (
        await session.execute(
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
    category_ids = sorted(
        {int(product.category_id) for product in products if product.category_id is not None}
    )
    if not category_ids:
        return []
    categories = list(
        (
            await session.scalars(
                select(ProductCategory)
                .where(ProductCategory.id.in_(category_ids))
                .order_by(ProductCategory.sort_order, ProductCategory.id)
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


AUTO_SUPPRESS_PREFLIGHT_STATUSES = frozenset(
    {
        MXStatus.NO_DOMAIN.value,
        MXStatus.NULL_MX.value,
    }
)
DELIVERABILITY_BLOCK_STATUSES = frozenset(
    {
        *AUTO_SUPPRESS_PREFLIGHT_STATUSES,
        "INVALID_FORMAT",
        MXStatus.NO_MX.value,
        "SUPPRESSED",
    }
)

PRIOR_THREAD_MARKERS = (
    "previous quote",
    "previous quotation",
    "earlier quote",
    "earlier quotation",
    "last quote",
    "last price",
    "as discussed",
    "as agreed",
    "same as before",
    "revised quote",
    "revised quotation",
    "revise your quote",
    "follow up on",
    "our previous conversation",
    "your previous offer",
)

FREE_EMAIL_DOMAINS = frozenset(
    {
        "aol.com",
        "gmail.com",
        "googlemail.com",
        "hotmail.com",
        "icloud.com",
        "live.com",
        "outlook.com",
        "proton.me",
        "protonmail.com",
        "qq.com",
        "yahoo.com",
        "yahoo.co.in",
        "ymail.com",
    }
)
COMPANY_RESEARCH_CACHE_KEY = "company_category_research"
COMPANY_RESEARCH_CACHE_SCHEMA = "company-category-research.v1"


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
    if (
        row.approval_handoff_id is None
        or not approved_by
        or row.human_approved_at is None
    ):
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


class JobDeferred(RuntimeError):
    """A durable business wait that must not consume the job retry budget."""

    def __init__(self, reason: str, available_at: datetime):
        super().__init__(reason)
        self.reason = reason
        self.available_at = available_at


def _nonfree_email_domain(email_address: str) -> str | None:
    _, separator, domain = email_address.strip().casefold().rpartition("@")
    if not separator or not domain or domain in FREE_EMAIL_DOMAINS:
        return None
    return domain[:255]


def _source_hostname(url: str) -> str | None:
    try:
        host = (urlparse(url).hostname or "").casefold().rstrip(".")
    except ValueError:
        return None
    return host or None


def _company_research_gate(
    decision: CompanyCategoryDecision,
    sources: list[CompanyResearchSource],
    *,
    company_domain: str | None,
    active_category_keys: set[str],
    settings: Settings,
) -> dict[str, Any]:
    source_domains = sorted(
        {
            hostname
            for source in sources
            if (hostname := _source_hostname(source.url)) is not None
        }
    )
    exact_domain_source = bool(
        company_domain
        and any(
            hostname == company_domain or hostname.endswith(f".{company_domain}")
            for hostname in source_domains
        )
    )
    score_gap = max(
        0.0,
        float(decision.category_confidence) - float(decision.runner_up_confidence),
    )
    reasons: list[str] = []
    if decision.recommended_category_key not in active_category_keys:
        reasons.append("NO_ACTIVE_CATEGORY_RECOMMENDATION")
    if decision.identity_confidence < settings.company_research_min_identity_confidence:
        reasons.append("LOW_IDENTITY_CONFIDENCE")
    if decision.category_confidence < settings.company_research_min_category_confidence:
        reasons.append("LOW_CATEGORY_CONFIDENCE")
    if score_gap < settings.company_research_min_score_gap:
        reasons.append("CATEGORY_SCORE_GAP_TOO_SMALL")
    if decision.conflicting_evidence:
        reasons.append("CONFLICTING_EVIDENCE")
    if (
        len(source_domains) < settings.company_research_min_sources
        and not exact_domain_source
    ):
        reasons.append("INSUFFICIENT_INDEPENDENT_SOURCES")
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "score_gap": round(score_gap, 4),
        "source_domains": source_domains,
        "exact_domain_source": exact_domain_source,
    }


def _cached_company_research(
    customer: Customer,
    *,
    company_domain: str | None,
    catalog_signature: str,
    now: datetime,
) -> tuple[CompanyCategoryDecision, list[CompanyResearchSource], dict[str, Any]] | None:
    cache = (customer.metadata_json or {}).get(COMPANY_RESEARCH_CACHE_KEY)
    if not isinstance(cache, dict):
        return None
    if (
        cache.get("schema_version") != COMPANY_RESEARCH_CACHE_SCHEMA
        or cache.get("catalog_signature") != catalog_signature
        or cache.get("company_domain") != company_domain
    ):
        return None
    try:
        expires_at = datetime.fromisoformat(str(cache["expires_at"]))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= now:
            return None
        decision = CompanyCategoryDecision.model_validate(cache["decision"])
        sources = [
            CompanyResearchSource.model_validate(item)
            for item in cache.get("sources") or []
        ]
    except (KeyError, TypeError, ValueError):
        return None
    metadata = cache.get("metadata") if isinstance(cache.get("metadata"), dict) else {}
    return decision, sources, {**metadata, "cache_hit": True}


def _store_company_research_cache(
    customer: Customer,
    *,
    company_domain: str | None,
    catalog_signature: str,
    decision: CompanyCategoryDecision,
    sources: list[CompanyResearchSource],
    metadata: dict[str, Any],
    settings: Settings,
    now: datetime,
) -> None:
    safe_metadata = {
        key: metadata.get(key)
        for key in ("provider", "model", "request_hash", "input_tokens", "output_tokens")
        if metadata.get(key) is not None
    }
    cache = {
        "schema_version": COMPANY_RESEARCH_CACHE_SCHEMA,
        "researched_at": now.isoformat(),
        "expires_at": (now + timedelta(days=settings.company_research_cache_days)).isoformat(),
        "company_domain": company_domain,
        "catalog_signature": catalog_signature,
        "decision": decision.model_dump(mode="json"),
        "sources": [source.model_dump(mode="json") for source in sources],
        "metadata": safe_metadata,
    }
    customer.metadata_json = {
        **(customer.metadata_json or {}),
        COMPANY_RESEARCH_CACHE_KEY: cache,
    }


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


async def _email_address_status(session: AsyncSession, email_address: str) -> EmailAddressStatus:
    normalized = email_address.strip().casefold()[:320]
    row = await session.get(EmailAddressStatus, normalized)
    if row is None:
        row = EmailAddressStatus(email=normalized, suppressed=False)
        session.add(row)
        await session.flush()
    return row


def _recipient_delivery_gate_key(email_address: str) -> int:
    """Return a stable signed bigint key for PostgreSQL advisory locking."""
    normalized = email_address.strip().casefold()[:320]
    return int.from_bytes(
        hashlib.sha256(normalized.encode("utf-8")).digest()[:8],
        byteorder="big",
        signed=True,
    )


async def _lock_recipient_delivery_gate(
    session: AsyncSession,
    email_address: str,
) -> None:
    """Serialize final delivery and endpoint suppression for one address.

    The transaction holding this lock is the linearization boundary: a
    suppression committed before the final delivery transaction acquires the
    lock blocks the message; a suppression that waits behind an in-progress
    SMTP transaction is applied only after that already-started delivery.
    """
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                _recipient_delivery_gate_key(email_address)
            )
        )
    )


async def _suppress_email_address(
    session: AsyncSession,
    email_address: str,
    *,
    reason: str,
    source_email_id: int | None = None,
    bounce_type: str | None = None,
    diagnostic: str | None = None,
) -> EmailAddressStatus:
    await _lock_recipient_delivery_gate(session, email_address)
    now = datetime.now(UTC)
    status = await _email_address_status(session, email_address)
    status.suppressed = True
    status.suppression_reason = reason
    status.suppression_source_email_id = source_email_id
    status.suppressed_at = status.suppressed_at or now
    if bounce_type:
        status.last_bounce_at = now
        status.last_bounce_type = bounce_type
        status.last_bounce_diagnostic = diagnostic[:2000] if diagnostic else None
    contacts = (
        (
            await session.execute(
                select(Contact).where(func.lower(Contact.email) == status.email)
            )
        )
        .scalars()
        .all()
    )
    for contact in contacts:
        contact.suppressed = True
    return status


async def _recipient_preflight(
    session: AsyncSession,
    recipient: str,
    settings: Settings,
) -> tuple[str, str, dict[str, Any]]:
    """Return ALLOW, BLOCK, or DEFER plus a stable detail and audit facts."""
    if not settings.email_preflight_enabled:
        return "ALLOW", "recipient preflight disabled", {"preflight_status": "DISABLED"}

    now = datetime.now(UTC)
    format_result = validate_address_format(recipient)
    status = await _email_address_status(session, format_result.normalized)
    status.format_valid = format_result.valid
    status.domain = format_result.domain
    status.last_preflight_at = now
    if status.suppressed:
        status.preflight_status = "SUPPRESSED"
        detail = f"recipient permanently suppressed: {status.suppression_reason or 'unspecified'}"
        status.last_preflight_detail = detail
        return "BLOCK", detail, {
            "recipient": status.email,
            "preflight_status": "SUPPRESSED",
            "suppression_reason": status.suppression_reason,
            "auto_suppressed": True,
        }
    if not format_result.valid:
        detail = f"invalid recipient format: {format_result.error or 'invalid address'}"
        status.preflight_status = "INVALID_FORMAT"
        status.last_preflight_detail = detail
        await _suppress_email_address(session, status.email, reason="INVALID_FORMAT")
        return "BLOCK", detail, {
            "recipient": status.email,
            "preflight_status": "INVALID_FORMAT",
            "format_error": format_result.error,
            "suppression_reason": "INVALID_FORMAT",
        }
    if not settings.mx_check_enabled:
        status.preflight_status = MXStatus.UNCHECKED.value
        status.last_preflight_detail = "MX checking disabled"
        return "ALLOW", "MX checking disabled", {
            "recipient": status.email,
            "domain": status.domain,
            "preflight_status": MXStatus.UNCHECKED.value,
        }

    assert format_result.domain is not None
    domain_status = await session.get(EmailDomainStatus, format_result.domain)
    cache_ttl = (
        timedelta(minutes=settings.mx_temporary_retry_minutes)
        if domain_status and domain_status.mx_status == MXStatus.TEMPORARY_ERROR.value
        else timedelta(hours=settings.mx_cache_ttl_hours)
    )
    cache_fresh = bool(domain_status and domain_status.checked_at >= now - cache_ttl)
    if cache_fresh and domain_status is not None:
        mx_result = MXResult(
            MXStatus(domain_status.mx_status),
            domain_status.domain,
            tuple(domain_status.mx_records),
            domain_status.last_error,
        )
    else:
        mx_result = await asyncio.to_thread(
            lookup_mx,
            format_result.domain,
            timeout_seconds=settings.mx_lookup_timeout_seconds,
        )
        if domain_status is None:
            domain_status = EmailDomainStatus(
                domain=format_result.domain,
                mx_status=mx_result.status.value,
                mx_records=list(mx_result.records),
                checked_at=now,
                last_error=mx_result.error,
            )
            session.add(domain_status)
        else:
            domain_status.mx_status = mx_result.status.value
            domain_status.mx_records = list(mx_result.records)
            domain_status.checked_at = now
            domain_status.last_error = mx_result.error

    status.preflight_status = mx_result.status.value
    status.last_preflight_detail = mx_result.error
    facts = {
        "recipient": status.email,
        "domain": mx_result.domain,
        "preflight_status": mx_result.status.value,
        "mx_records": list(mx_result.records),
        "cache_hit": cache_fresh,
        "detail": mx_result.error,
    }
    if mx_result.deliverable:
        return "ALLOW", "recipient format and MX checks passed", facts
    if mx_result.temporary:
        return "DEFER", mx_result.error or "temporary DNS lookup failure", facts
    if mx_result.status.value in AUTO_SUPPRESS_PREFLIGHT_STATUSES:
        suppression_reason = f"PREFLIGHT_{mx_result.status.value}"
        await _suppress_email_address(session, status.email, reason=suppression_reason)
        facts["suppression_reason"] = suppression_reason
        facts["auto_suppressed"] = True
    return "BLOCK", mx_result.error or "recipient domain cannot receive email", facts


async def resolve_deliverability_handoff(
    session: AsyncSession,
    *,
    handoff_id: int,
    actor: str,
    note: str = "",
) -> Handoff:
    """Resolve an old deliverability handoff by suppressing only its exact recipient."""
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None:
        raise ValueError("handoff not found")
    if handoff.status != "OPEN":
        raise ValueError("handoff is already resolved")
    if handoff.reason_code != HandoffReason.EMAIL_DELIVERABILITY.value:
        raise ValueError("handoff is not an email deliverability review")

    facts = dict(handoff.extracted_facts or {})
    recipient = str(facts.get("recipient") or "").strip().casefold()
    preflight_status = str(facts.get("preflight_status") or "").strip().upper()
    if not recipient:
        raise ValueError("deliverability handoff has no recipient")
    address_status = await session.get(EmailAddressStatus, recipient)
    if preflight_status not in DELIVERABILITY_BLOCK_STATUSES and not (
        address_status and address_status.suppressed
    ):
        raise ValueError("deliverability result is not a permanent recipient block")

    suppression_reason = (
        address_status.suppression_reason
        if address_status and address_status.suppressed and address_status.suppression_reason
        else f"PREFLIGHT_{preflight_status or 'UNDELIVERABLE'}"
    )
    updated_status = await _suppress_email_address(session, recipient, reason=suppression_reason)
    if preflight_status:
        updated_status.preflight_status = preflight_status

    outbox_id = facts.get("outbox_id")
    outbox = await session.get(Outbox, outbox_id) if isinstance(outbox_id, int) else None
    if outbox is not None and outbox.status in {
        DeliveryStatus.PENDING,
        DeliveryStatus.FAILED,
        DeliveryStatus.CLAIMED,
        DeliveryStatus.UNKNOWN,
    }:
        outbox.status = DeliveryStatus.CANCELLED
        outbox.last_error = "recipient marked permanently undeliverable by operator"

    campaign_recipient = (
        await session.scalar(
            select(ReactivationRecipient).where(ReactivationRecipient.outbox_id == outbox_id)
        )
        if isinstance(outbox_id, int)
        else None
    )
    if campaign_recipient is not None and campaign_recipient.status not in {"SENT", "REPLIED"}:
        campaign_recipient.status = "SKIPPED"
        campaign_recipient.exclusion_reason = "EMAIL_UNDELIVERABLE"

    case = await session.get(SalesCase, handoff.case_id) if handoff.case_id else None
    if case is not None and case.status not in {CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST}:
        case.status = CaseStatus.PAUSED
    handoff.status = "RESOLVED"
    handoff.resolution_note = note.strip() or f"Recipient {recipient} marked permanently undeliverable"
    await finalize_handoff_agent_run(
        session,
        handoff_id=handoff.id,
        actor=actor,
        outcome="recipient-suppressed",
        cancelled=True,
    )
    await audit(
        session,
        "handoff.deliverability_recipient_suppressed",
        case_id=handoff.case_id,
        actor=actor,
        data={
            "handoff_id": handoff.id,
            "outbox_id": outbox_id,
            "recipient": recipient,
            "preflight_status": preflight_status,
            "suppression_reason": suppression_reason,
        },
    )
    await session.commit()
    return handoff


def _contact_metadata_with_manual_source(
    contact: Contact,
    *,
    actor: str,
    source: str,
    replaces_contact_id: int | None = None,
    replaces_email: str | None = None,
) -> None:
    metadata = dict(contact.metadata_json or {})
    metadata.setdefault("identity_kind", "EMAIL_ENDPOINT")
    metadata.setdefault("identity_verified", False)
    manual_entries = list(metadata.get("manual_entries") or [])
    entry: dict[str, Any] = {
        "actor": actor,
        "source": source,
        "created_at": datetime.now(UTC).isoformat(),
    }
    if replaces_contact_id is not None:
        entry["replaces_contact_id"] = replaces_contact_id
    if replaces_email:
        entry["replaces_email"] = replaces_email
    manual_entries.append(entry)
    metadata["manual_entries"] = manual_entries[-20:]
    contact.metadata_json = metadata


async def _ensure_customer_contact(
    session: AsyncSession,
    *,
    customer: Customer,
    email: str,
    name: str,
    actor: str,
    source: str,
    replaces_contact: Contact | None = None,
) -> tuple[Contact, bool]:
    format_result = validate_address_format(email)
    if not format_result.valid:
        raise ValueError(
            f"invalid email address: {format_result.error or 'invalid format'}"
        )
    normalized = format_result.normalized
    existing = (
        (
            await session.execute(
                select(Contact)
                .where(func.lower(Contact.email) == normalized)
                .order_by(Contact.id)
            )
        )
        .scalars()
        .all()
    )
    foreign = [row for row in existing if row.customer_id != customer.id]
    if foreign:
        raise ValueError(
            "email address already belongs to another customer; "
            "review that customer before creating a duplicate identity"
        )
    same_customer = next(
        (row for row in existing if row.customer_id == customer.id),
        None,
    )
    address_status = await session.get(EmailAddressStatus, normalized)
    if same_customer is not None:
        if same_customer.suppressed or (
            address_status is not None and address_status.suppressed
        ):
            raise ValueError(
                "email address already exists but is permanently suppressed"
            )
        return same_customer, False

    contact = Contact(
        customer_id=customer.id,
        name=name.strip() or "Customer",
        email=normalized,
        language=(replaces_contact.language if replaces_contact else customer.language)
        or "en",
        suppressed=False,
        metadata_json={},
        first_contact_at=(
            replaces_contact.first_contact_at if replaces_contact else None
        ),
        last_contact_at=None,
    )
    _contact_metadata_with_manual_source(
        contact,
        actor=actor,
        source=source,
        replaces_contact_id=replaces_contact.id if replaces_contact else None,
        replaces_email=replaces_contact.email if replaces_contact else None,
    )
    session.add(contact)
    await session.flush()
    return contact, True


async def _cancel_pending_recipient_delivery(
    session: AsyncSession,
    *,
    recipient: str,
    reason: str,
) -> list[int]:
    rows = (
        (
            await session.execute(
                select(Outbox)
                .where(
                    func.lower(Outbox.recipient) == recipient,
                    Outbox.status.in_(
                        [
                            DeliveryStatus.PENDING,
                            DeliveryStatus.FAILED,
                            DeliveryStatus.CLAIMED,
                            DeliveryStatus.UNKNOWN,
                        ]
                    ),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    outbox_ids: list[int] = []
    for row in rows:
        row.status = DeliveryStatus.CANCELLED
        row.last_error = reason
        outbox_ids.append(row.id)
    if outbox_ids:
        campaign_rows = (
            (
                await session.execute(
                    select(ReactivationRecipient).where(
                        ReactivationRecipient.outbox_id.in_(outbox_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        for campaign_row in campaign_rows:
            if campaign_row.status not in {"SENT", "REPLIED"}:
                campaign_row.status = "SKIPPED"
                campaign_row.exclusion_reason = "EMAIL_UNDELIVERABLE"
    return outbox_ids


async def _final_recipient_delivery_guard(
    session: AsyncSession,
    row: Outbox,
    *,
    settings: Settings,
    at: datetime,
) -> bool:
    """Re-check mutable recipient state after claiming and immediately before send.

    The advisory lock remains held by the transaction while the transport is
    called, so the API cannot commit a conflicting endpoint suppression between
    this check and SMTP delivery.
    """
    await _lock_recipient_delivery_gate(session, row.recipient)
    current = await session.scalar(
        select(Outbox)
        .where(Outbox.id == row.id)
        .execution_options(populate_existing=True)
    )
    if current is None or current.status != DeliveryStatus.CLAIMED:
        await session.commit()
        return False

    if current.message_kind == "FORWARD":
        complete_approval = _human_approval_from_outbox(current) is not None
        try:
            normalized_forward_recipient = _normalize_forward_recipient(current.recipient)
        except ValueError as exc:
            current.status = DeliveryStatus.CANCELLED
            current.last_error = f"final forward authorization failed: {exc}"[:2000]
            await session.commit()
            return False
        if not complete_approval or normalized_forward_recipient != current.recipient:
            current.status = DeliveryStatus.CANCELLED
            current.last_error = "final forward gate requires complete human approval metadata"
            await session.commit()
            return False

    address_status = await _email_address_status(session, current.recipient)
    if address_status.suppressed:
        current.status = DeliveryStatus.CANCELLED
        current.last_error = (
            "final delivery gate blocked suppressed recipient: "
            f"{address_status.suppression_reason or 'unspecified'}"
        )[:2000]
        campaign_recipient = await session.scalar(
            select(ReactivationRecipient).where(
                ReactivationRecipient.outbox_id == current.id
            )
        )
        if (
            campaign_recipient is not None
            and campaign_recipient.status not in {"SENT", "REPLIED"}
        ):
            campaign_recipient.status = "SKIPPED"
            campaign_recipient.exclusion_reason = "CONTACT_SUPPRESSED"
        await audit(
            session,
            "outbox.blocked_final_recipient_gate",
            case_id=current.case_id,
            actor="policy",
            data={
                "outbox_id": current.id,
                "recipient": current.recipient.strip().casefold(),
                "suppression_reason": address_status.suppression_reason,
            },
        )
        await session.commit()
        return False

    if current.message_kind == "REACTIVATION":
        guard = await reactivation_send_guard(
            session,
            current,
            settings=settings,
            at=at,
        )
        if guard.action == "DEFER":
            current.status = DeliveryStatus.PENDING
            current.attempts = max(0, current.attempts - 1)
            current.available_at = guard.available_at or (
                at + timedelta(minutes=15)
            )
            current.last_error = guard.reason
            await session.commit()
            return False
        if guard.action == "BLOCK":
            current.status = DeliveryStatus.CANCELLED
            current.last_error = guard.reason
            await session.commit()
            return False
    return True


async def suppress_contact_endpoint(
    session: AsyncSession,
    *,
    contact_id: int,
    actor: str,
    note: str = "",
) -> Contact:
    """Suppress one exact email endpoint without affecting sibling contacts."""
    contact = await session.get(Contact, contact_id)
    if contact is None:
        raise ValueError("contact not found")
    recipient = contact.email.strip().casefold()
    await _suppress_email_address(
        session,
        recipient,
        reason="MANUAL_CONTACT_ENDPOINT_SUPPRESSION",
    )
    outbox_ids = await _cancel_pending_recipient_delivery(
        session,
        recipient=recipient,
        reason="recipient endpoint manually marked undeliverable",
    )
    contact_ids = (
        (
            await session.execute(
                select(Contact.id).where(func.lower(Contact.email) == recipient)
            )
        )
        .scalars()
        .all()
    )
    cases = (
        (
            await session.execute(
                select(SalesCase).where(
                    SalesCase.contact_id.in_(contact_ids),
                    SalesCase.status.not_in(
                        [CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    for case in cases:
        case.status = CaseStatus.PAUSED
    await audit(
        session,
        "contact.endpoint_suppressed",
        case_id=None,
        actor=actor,
        data={
            "contact_id": contact.id,
            "customer_id": contact.customer_id,
            "recipient": recipient,
            "cancelled_outbox_ids": outbox_ids,
            "paused_case_ids": [case.id for case in cases],
            "note": note.strip(),
        },
    )
    await session.commit()
    return contact


async def add_customer_contact_endpoint(
    session: AsyncSession,
    *,
    customer_id: int,
    email: str,
    name: str,
    actor: str,
    note: str = "",
) -> tuple[Contact, bool]:
    """Add a separately deliverable address to one customer."""
    customer = await session.get(Customer, customer_id)
    if customer is None:
        raise ValueError("customer not found")
    contact, created = await _ensure_customer_contact(
        session,
        customer=customer,
        email=email,
        name=name,
        actor=actor,
        source="contact_directory",
    )
    await audit(
        session,
        "contact.endpoint_created" if created else "contact.endpoint_reused",
        case_id=None,
        actor=actor,
        data={
            "contact_id": contact.id,
            "customer_id": customer.id,
            "email": contact.email,
            "note": note.strip(),
        },
    )
    await session.commit()
    return contact, created


async def replace_handoff_recipient(
    session: AsyncSession,
    *,
    handoff_id: int,
    new_email: str,
    new_name: str,
    actor: str,
    note: str = "",
    resume_case: bool = False,
) -> tuple[Handoff, Contact, bool]:
    """Retire one failed endpoint and move only this handoff's case to a replacement."""
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None:
        raise ValueError("handoff not found")
    if handoff.status != "OPEN":
        raise ValueError("handoff is already resolved")
    if handoff.reason_code not in {
        HandoffReason.EMAIL_DELIVERABILITY.value,
        HandoffReason.BOUNCE_REVIEW.value,
    }:
        raise ValueError("handoff is not a recipient deliverability review")

    facts = dict(handoff.extracted_facts or {})
    old_email = str(facts.get("recipient") or "").strip().casefold()
    if not old_email:
        raise ValueError("deliverability handoff has no failed recipient")
    new_format = validate_address_format(new_email)
    if new_format.valid and new_format.normalized == old_email:
        raise ValueError("replacement email must differ from the failed address")

    case = await session.get(SalesCase, handoff.case_id) if handoff.case_id else None
    old_contact = await session.get(Contact, case.contact_id) if case is not None else None
    outbox_id = facts.get("outbox_id")
    campaign_recipient = (
        await session.scalar(
            select(ReactivationRecipient).where(
                ReactivationRecipient.outbox_id == outbox_id
            )
        )
        if isinstance(outbox_id, int)
        else None
    )
    if old_contact is None and campaign_recipient is not None:
        old_contact = await session.get(Contact, campaign_recipient.contact_id)
    if old_contact is None:
        matches = (
            (
                await session.execute(
                    select(Contact)
                    .where(func.lower(Contact.email) == old_email)
                    .order_by(Contact.id)
                )
            )
            .scalars()
            .all()
        )
        if len(matches) != 1:
            raise ValueError(
                "failed recipient does not map to exactly one customer contact"
            )
        old_contact = matches[0]
    if old_contact.email.strip().casefold() != old_email:
        raise ValueError("handoff case contact does not match the failed recipient")
    customer = await session.get(Customer, old_contact.customer_id)
    if customer is None:
        raise ValueError("customer not found")

    new_contact, created = await _ensure_customer_contact(
        session,
        customer=customer,
        email=new_email,
        name=new_name.strip() or old_contact.name,
        actor=actor,
        source="deliverability_handoff_replacement",
        replaces_contact=old_contact,
    )
    await _suppress_email_address(
        session,
        old_email,
        reason="REPLACED_UNDELIVERABLE_ENDPOINT",
        source_email_id=handoff.source_email_id,
    )
    cancelled_outbox_ids = await _cancel_pending_recipient_delivery(
        session,
        recipient=old_email,
        reason="recipient replaced after delivery failure",
    )
    old_metadata = dict(old_contact.metadata_json or {})
    old_metadata["replacement_contact_id"] = new_contact.id
    old_metadata["replacement_email"] = new_contact.email
    old_metadata["replaced_at"] = datetime.now(UTC).isoformat()
    old_metadata["replaced_by"] = actor
    old_contact.metadata_json = old_metadata

    if case is not None:
        case.contact_id = new_contact.id
        case.customer_id = customer.id
        if resume_case and case.status not in {
            CaseStatus.CLOSED_WON,
            CaseStatus.CLOSED_LOST,
        }:
            case.status = CaseStatus.ACTIVE
        elif case.status not in {CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST}:
            case.status = CaseStatus.PAUSED
    if campaign_recipient is not None and campaign_recipient.status not in {
        "SENT",
        "REPLIED",
    }:
        campaign_recipient.status = "SKIPPED"
        campaign_recipient.exclusion_reason = "EMAIL_REPLACED"

    handoff.status = "RESOLVED"
    handoff.resolution_note = note.strip() or (
        f"Replaced undeliverable recipient {old_email} with {new_contact.email}"
    )
    await finalize_handoff_agent_run(
        session,
        handoff_id=handoff.id,
        actor=actor,
        outcome="recipient-replaced",
        cancelled=True,
    )
    await audit(
        session,
        "handoff.deliverability_recipient_replaced",
        case_id=handoff.case_id,
        actor=actor,
        data={
            "handoff_id": handoff.id,
            "customer_id": customer.id,
            "old_contact_id": old_contact.id,
            "old_email": old_email,
            "new_contact_id": new_contact.id,
            "new_email": new_contact.email,
            "created_contact": created,
            "cancelled_outbox_ids": cancelled_outbox_ids,
            "case_resumed": bool(case is not None and resume_case),
        },
    )
    await session.commit()
    return handoff, new_contact, created


async def enqueue_job(
    session: AsyncSession,
    kind: str,
    payload: dict[str, Any],
    idempotency_key: str,
    available_at: datetime | None = None,
) -> Job | None:
    try:
        async with session.begin_nested():
            job = Job(
                kind=kind,
                payload=payload,
                idempotency_key=idempotency_key,
                available_at=available_at or datetime.now(UTC),
            )
            session.add(job)
            await session.flush()
        await session.commit()
        return job
    except IntegrityError:
        # The nested transaction already rolled back the conflicting insert.
        # Commit the still-valid outer transaction without expiring unrelated
        # ORM instances that callers may continue to use.
        await session.commit()
        return None


async def ensure_weekly_commercial_refresh(
    session: AsyncSession,
    settings: Settings | None = None,
    *,
    at: datetime | None = None,
) -> bool:
    """Durably request one DingTalk price/inventory reminder per business week."""

    settings = settings or get_settings()
    observed_at = at or datetime.now(UTC)
    if (
        settings.demo_mode
        or not settings.commercial_gate_enabled
        or not is_commercial_day(settings, observed_at)
        or not is_commercial_open(settings, observed_at)
    ):
        return False
    cycle = await get_or_create_current_cycle(session, settings, at=observed_at)
    if cycle.price_status == "CONFIRMED" and cycle.inventory_status == "CONFIRMED":
        await session.commit()
        return False
    job = await enqueue_job(
        session,
        "notify_commercial_refresh",
        {"cycle_id": cycle.id},
        f"weekly-commercial-refresh:{cycle.scope}:{cycle.week_start.isoformat()}",
    )
    return job is not None


async def _commercial_quote_context(
    session: AsyncSession,
    *,
    product_id: int,
    currency: str,
    settings: Settings,
    requested_quantity: Decimal | int | None = None,
    at: datetime | None = None,
) -> QuoteContext | None:
    if settings.demo_mode or not settings.commercial_gate_enabled:
        return None
    context = await get_commercial_data_provider(settings).get_quote_context(
        session,
        product_id=product_id,
        currency=currency,
        requested_quantity=requested_quantity,
        at=at,
    )
    if context.status is QuoteContextStatus.WAITING:
        await ensure_weekly_commercial_refresh(session, settings, at=at)
        raise JobDeferred(
            f"commercial data waiting: {context.reason}",
            context.next_check_at
            or (datetime.now(UTC) + timedelta(minutes=settings.commercial_retry_minutes)),
        )
    return context


async def create_handoff(
    session: AsyncSession,
    *,
    case: SalesCase | None,
    reason: HandoffReason,
    summary: str,
    facts: dict[str, Any] | None = None,
    source_email_id: int | None = None,
    update_existing: bool = False,
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
        if case and case.status == CaseStatus.ACTIVE:
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


async def _prepared_handoff_draft_preview(
    session: AsyncSession,
    *,
    handoff: Handoff,
    source_email: EmailMessage,
    sales_case: SalesCase,
    analysis: InboundAnalysis,
    actor: str,
    settings: Settings,
) -> dict[str, Any] | None:
    """Use deterministic prepared work before falling back to free-form AI.

    The review button used to discard prepared catalog/quote/COA facts and send
    an empty commercial-facts object to the model.  Besides producing vague
    holding replies, that made an old product-list handoff look as though the
    application had no catalog.  This helper upgrades generic legacy catalog
    handoffs in place and preserves every already-prepared typed draft.
    """

    stored_facts = dict(handoff.extracted_facts or {})
    stored_preview = stored_facts.get("ai_draft_preview")
    prepared_keys = (
        "prepared_coa",
        "prepared_product_list",
        "prepared_quote",
        "prepared_multi_quote",
    )
    has_prepared_work = any(
        isinstance(stored_facts.get(key), dict) for key in prepared_keys
    )
    request_text = f"{source_email.subject}\n{source_email.body_text}"
    generic_catalog_request = bool(
        analysis.product_list_requested
        and generic_product_list_requested(request_text)
    )

    if not has_prepared_work and not generic_catalog_request:
        return None

    prepared_product_list = stored_facts.get("prepared_product_list")
    if generic_catalog_request and not isinstance(prepared_product_list, dict):
        products = list(
            (
                await session.scalars(
                    select(Product)
                    .join(ProductCategory, Product.category_id == ProductCategory.id)
                    .where(
                        Product.active.is_(True),
                        ProductCategory.active.is_(True),
                    )
                    .order_by(
                        ProductCategory.sort_order,
                        Product.sort_order,
                        Product.id,
                    )
                )
            ).all()
        )
        if not products:
            raise ValueError(
                "客户要求产品清单，但当前没有已启用的产品目录；请先导入或启用产品数据"
            )
        prepared_product_list = {
            "scope": "all",
            "category_id": None,
            "category_key": "all_products",
            "category_name": "All Products",
            "product_ids": [product.id for product in products],
            "product_codes": [product.code for product in products],
            "file_format": "xlsx",
        }
        stored_facts["prepared_product_list"] = prepared_product_list

    missing_business_facts: list[str] = []
    if isinstance(prepared_product_list, dict):
        file_format: str | None
        if prepared_product_list.get("scope") == "all":
            category = _all_products_catalog_category()
            products = list(
                (
                    await session.scalars(
                        select(Product)
                        .join(ProductCategory, Product.category_id == ProductCategory.id)
                        .where(
                            Product.active.is_(True),
                            ProductCategory.active.is_(True),
                        )
                        .order_by(
                            ProductCategory.sort_order,
                            Product.sort_order,
                            Product.id,
                        )
                    )
                ).all()
            )
            file_format = "xlsx"
        else:
            category_id = int(prepared_product_list.get("category_id") or 0)
            loaded_category = await session.get(ProductCategory, category_id)
            if loaded_category is None or not loaded_category.active:
                raise ValueError("已准备的产品分类不存在或已经停用")
            category = loaded_category
            products = list(
                (
                    await session.scalars(
                        select(Product)
                        .where(
                            Product.category_id == category.id,
                            Product.active.is_(True),
                        )
                        .order_by(Product.sort_order, Product.id)
                    )
                ).all()
            )
            raw_file_format = prepared_product_list.get("file_format")
            file_format = str(raw_file_format) if raw_file_format is not None else None
        if not products:
            raise ValueError("已准备的产品清单中没有启用的产品")

        attachment_filename: str | None = None
        if file_format is not None:
            catalog_file = build_product_list_attachment(
                category=category,
                products=products,
                file_format=str(file_format),
            )
            attachment_filename = catalog_file.filename
        contact_name = sales_case.contact.name.strip() or "Customer"
        subject = (
            source_email.subject
            if source_email.subject.casefold().startswith("re:")
            else f"Re: {source_email.subject}"
        )
        if prepared_product_list.get("scope") == "all":
            body_text = (
                f"Dear {contact_name},\n\n"
                "Thank you for your email. Please find attached our current English product "
                "catalogue, including the approved product codes, product names, CAS numbers, "
                "and available content specifications.\n\n"
                "Please let us know the products and quantities you require so we can confirm "
                "current availability and pricing."
            )
            model = "active-full-product-catalog-v1"
        else:
            bundle = load_content(settings.content_dir)
            rendered_text, _ = render_product_list_email(
                contact_name=contact_name,
                category=category,
                products=products,
                subject=source_email.subject,
                signature_text=bundle.signature_text,
                signature_html=bundle.signature_html,
                attachment_filename=attachment_filename,
            )
            signature_text = bundle.signature_text.strip()
            body_text = (
                rendered_text[: -len(signature_text)].rstrip()
                if signature_text and rendered_text.endswith(signature_text)
                else rendered_text.rstrip()
            )
            model = "active-product-catalog-v1"

        payment_requested = _payment_details_requested(request_text)
        payment_term: str | None = None
        payment_term_source: str | None = None
        payment_term_quote_id: int | None = None
        if payment_requested:
            payment = await _customer_payment_term(
                session,
                customer_id=sales_case.customer_id,
            )
            payment_term = payment.term
            payment_term_source = payment.source
            payment_term_quote_id = payment.quote_id
            body_text = f"{body_text.rstrip()}\n\n{_payment_term_sentence(payment)}"

        prepared_product_list = {
            **prepared_product_list,
            "product_ids": [product.id for product in products],
            "product_codes": [product.code for product in products],
            "product_count": len(products),
            "category_breakdown": await _catalog_category_breakdown(session, products),
            "file_format": file_format,
            "attachment_filename": attachment_filename,
            "payment_requested": payment_requested,
            "payment_term": payment_term,
            "payment_term_source": payment_term_source,
            "payment_term_quote_id": payment_term_quote_id,
            "missing_business_facts": missing_business_facts,
        }
        stored_facts["prepared_product_list"] = prepared_product_list
        handoff.reason_code = HandoffReason.PRODUCT_LIST_REVIEW.value
        handoff.summary = (
            "Product catalog draft prepared; human approval is required"
        )
        stored_preview = {
            "subject": subject,
            "body_text": body_text,
            "provider": "deterministic-product-list",
            "model": model,
            "rag_matches": [],
        }

    if not isinstance(stored_preview, dict):
        return None
    subject = str(stored_preview.get("subject") or "").strip()
    body_text = str(stored_preview.get("body_text") or "").strip()
    if not subject or not body_text:
        return None

    generated_at = datetime.now(UTC)
    preview_facts = {
        **stored_preview,
        "subject": subject[:998],
        "body_text": body_text[:50_000],
        "generated_at": generated_at.isoformat(),
        "generated_by": actor,
        "delivery_created": False,
        "rag_enabled": False,
        "rag_matches": [],
        "missing_business_facts": missing_business_facts,
    }
    stored_facts["ai_draft_preview"] = preview_facts
    handoff.extracted_facts = stored_facts
    return preview_facts


async def stream_handoff_draft_preview(
    session: AsyncSession,
    *,
    handoff_id: int,
    actor: str,
) -> AsyncIterator[dict[str, Any]]:
    """Stream and save a review-only draft without creating delivery work."""
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None:
        raise ValueError("handoff not found")
    if handoff.status != "OPEN":
        raise ValueError("only an open handoff can generate a draft preview")
    if handoff.source_email_id is None:
        raise ValueError("handoff has no source email")
    if handoff.case_id is None:
        raise ValueError("handoff must be associated with a case")
    if await session.scalar(
        select(Outbox.id).where(Outbox.approval_handoff_id == handoff.id)
    ):
        raise ValueError("handoff already has an approved outbound email")

    source_email = await session.get(EmailMessage, handoff.source_email_id)
    sales_case = await session.scalar(
        select(SalesCase)
        .options(
            selectinload(SalesCase.contact),
            selectinload(SalesCase.product),
        )
        .where(SalesCase.id == handoff.case_id)
    )
    if source_email is None or sales_case is None:
        raise ValueError("handoff source email or case not found")
    if source_email.direction != "INBOUND":
        raise ValueError("draft previews can only be generated for inbound email")

    settings = get_settings()
    ai = AIClient(settings)
    yield {
        "type": "status",
        "stage": "analysis",
        "message": "正在分析客户邮件…",
    }
    analysis, analysis_metadata = await ai.analyze(
        source_email.subject,
        source_email.body_text,
        source_email.attachment_metadata,
    )
    prepared_preview = await _prepared_handoff_draft_preview(
        session,
        handoff=handoff,
        source_email=source_email,
        sales_case=sales_case,
        analysis=analysis,
        actor=actor,
        settings=settings,
    )
    if prepared_preview is not None:
        yield {
            "type": "status",
            "stage": "prepared",
            "message": "正在使用已确认的产品目录生成可核对草稿…",
        }
        yield {"type": "subject", "value": prepared_preview["subject"]}
        yield {"type": "body_reset"}
        for index, block in enumerate(
            str(prepared_preview["body_text"]).split("\n\n")
        ):
            if block.strip():
                yield {
                    "type": "body_block",
                    "kind": "greeting" if index == 0 else "paragraph",
                    "value": block.strip(),
                }
        missing_business_facts = list(
            prepared_preview.get("missing_business_facts") or []
        )
        if missing_business_facts:
            yield {
                "type": "status",
                "stage": "blocked",
                "message": "产品列表已准备，但仍有商业条件未写入当前草稿；暂时禁止发送。",
            }
        session.add(
            AIInvocation(
                case_id=sales_case.id,
                provider=analysis_metadata["provider"],
                model=analysis_metadata["model"],
                purpose="handoff_preview_analysis",
                request_hash=analysis_metadata["request_hash"],
                parsed_output=analysis.model_dump(mode="json"),
                success=True,
                input_tokens=analysis_metadata.get("input_tokens"),
                output_tokens=analysis_metadata.get("output_tokens"),
            )
        )
        await audit(
            session,
            "handoff.prepared_draft_preview_generated",
            case_id=sales_case.id,
            actor=actor,
            data={
                "handoff_id": handoff.id,
                "source_email_id": source_email.id,
                "intent": analysis.intent.value,
                "provider": prepared_preview.get("provider"),
                "model": prepared_preview.get("model"),
                "missing_business_facts": missing_business_facts,
                "delivery_created": False,
            },
        )
        await session.commit()
        yield {"type": "complete", "preview": prepared_preview}
        return
    yield {
        "type": "status",
        "stage": "retrieval",
        "message": "正在检索历史邮件表达方式…" if settings.rag_enabled else "历史邮件 RAG 未启用，正在准备草稿…",
    }
    historical_style_examples: list[dict[str, Any]] = []
    retrieval_error: str | None = None
    if settings.rag_enabled:
        try:
            historical_style_examples = await asyncio.to_thread(
                _retrieve_historical_style_examples,
                settings,
                subject=source_email.subject,
                body=source_email.body_text,
                intent=analysis.intent.value,
            )
        except Exception as exc:
            retrieval_error = type(exc).__name__
            logger.warning(
                "RAG retrieval failed while generating handoff %s preview: %s",
                handoff.id,
                exc,
            )

    yield {
        "type": "status",
        "stage": "drafting",
        "message": "正在流式生成邮件草稿…",
    }
    preview = None
    preview_metadata: dict[str, Any] | None = None
    async for event in ai.draft_preview_stream(
        {
            "subject": source_email.subject,
            "contact_name": sales_case.contact.name,
            "customer_message": source_email.body_text[:12_000],
            "intent": analysis.intent.value,
            "product_code": sales_case.product.code if sales_case.product is not None else None,
            "quantity": analysis.quantity,
            "requested_information": analysis.missing_fields,
            "approved_commercial_facts": {},
            "historical_style_examples": historical_style_examples,
        }
    ):
        if event["type"] == "complete":
            preview = event["preview"]
            preview_metadata = event["metadata"]
            logger.error(
                "AI draft preview final value: type=%s, keys=%s",
                type(preview).__name__,
                list(preview.keys()) if isinstance(preview, dict) else None,
            )
            continue
        yield event
    if preview is None or preview_metadata is None:
        raise RuntimeError("AI draft preview stream ended without a final result")

    generated_at = datetime.now(UTC)
    preview_facts: dict[str, Any] = {
        "subject": preview.subject,
        "body_text": render_draft_preview(preview),
        "analysis": analysis.model_dump(mode="json"),
        "provider": preview_metadata["provider"],
        "model": preview_metadata["model"],
        "generated_at": generated_at.isoformat(),
        "generated_by": actor,
        "input_tokens": preview_metadata.get("input_tokens"),
        "output_tokens": preview_metadata.get("output_tokens"),
        "rag_enabled": settings.rag_enabled,
        "rag_matches": [
            {
                "example_id": item.get("example_id"),
                "similarity": item.get("similarity"),
                "boss_anchor": bool(item.get("boss_anchor")),
                "intent": item.get("intent"),
            }
            for item in historical_style_examples
        ],
        "rag_error": retrieval_error,
        "delivery_created": False,
    }
    stored_facts = dict(handoff.extracted_facts or {})
    stored_facts["ai_draft_preview"] = preview_facts
    handoff.extracted_facts = stored_facts

    session.add_all(
        [
            AIInvocation(
                case_id=sales_case.id,
                provider=analysis_metadata["provider"],
                model=analysis_metadata["model"],
                purpose="handoff_preview_analysis",
                request_hash=analysis_metadata["request_hash"],
                parsed_output=analysis.model_dump(mode="json"),
                success=True,
                input_tokens=analysis_metadata.get("input_tokens"),
                output_tokens=analysis_metadata.get("output_tokens"),
            ),
            AIInvocation(
                case_id=sales_case.id,
                provider=preview_metadata["provider"],
                model=preview_metadata["model"],
                purpose="handoff_draft_preview",
                request_hash=preview_metadata["request_hash"],
                parsed_output=preview.model_dump(mode="json"),
                success=True,
                input_tokens=preview_metadata.get("input_tokens"),
                output_tokens=preview_metadata.get("output_tokens"),
            ),
        ]
    )
    await audit(
        session,
        "handoff.draft_preview_generated",
        case_id=sales_case.id,
        actor=actor,
        data={
            "handoff_id": handoff.id,
            "source_email_id": source_email.id,
            "intent": analysis.intent.value,
            "provider": preview_metadata["provider"],
            "model": preview_metadata["model"],
            "rag_match_count": len(historical_style_examples),
            "delivery_created": False,
        },
    )
    await session.commit()
    yield {
        "type": "complete",
        "preview": preview_facts,
    }


async def generate_handoff_draft_preview(
    session: AsyncSession,
    *,
    handoff_id: int,
    actor: str,
) -> dict[str, Any]:
    """Generate and save a review-only draft without creating any delivery work."""
    completed: dict[str, Any] | None = None
    async for event in stream_handoff_draft_preview(
        session,
        handoff_id=handoff_id,
        actor=actor,
    ):
        if event["type"] == "complete":
            completed = event["preview"]
    if completed is None:
        raise RuntimeError("AI draft preview stream ended without a saved result")
    return completed


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
    case = await session.scalar(
        select(SalesCase)
        .options(selectinload(SalesCase.contact))
        .where(SalesCase.id == case_id)
    )
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
    if await session.scalar(
        select(Outbox.id).where(Outbox.approval_handoff_id == handoff.id)
    ):
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


def _strip_duplicate_signature_lead(
    body_text: str,
    signature_text: str,
) -> str:
    signature_lead = next(
        (
            line.strip()
            for line in signature_text.splitlines()
            if line.strip()
        ),
        "",
    )
    body_lines = body_text.splitlines()
    if (
        signature_lead
        and body_lines
        and body_lines[-1].strip().casefold() == signature_lead.casefold()
    ):
        return "\n".join(body_lines[:-1]).rstrip()
    return body_text


async def queue_human_reply(
    session: AsyncSession,
    *,
    handoff_id: int,
    subject: str,
    body_text: str,
    actor: str,
    note: str = "",
    resume_automation: bool = False,
    attachments: tuple[OutboundAttachment, ...] = (),
    quote: Quote | None = None,
) -> Outbox:
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None:
        raise ValueError("handoff not found")
    existing = await session.scalar(
        select(Outbox).where(
            or_(
                Outbox.approval_handoff_id == handoff.id,
                Outbox.business_key == f"handoff-reply:{handoff.id}",
            )
        )
    )
    if existing is not None:
        return existing
    if handoff.status != "OPEN":
        raise ValueError("handoff is already resolved")
    if handoff.case_id is None or handoff.source_email_id is None:
        raise ValueError("associate the handoff with a case before replying")
    source_email = await session.get(EmailMessage, handoff.source_email_id)
    case = await session.scalar(
        select(SalesCase)
        .options(
            selectinload(SalesCase.customer),
            selectinload(SalesCase.contact),
            selectinload(SalesCase.product),
        )
        .where(SalesCase.id == handoff.case_id)
    )
    if source_email is None or case is None:
        raise ValueError("source email or associated case not found")
    if source_email.direction != "INBOUND":
        raise ValueError("human reply requires an inbound source email")
    if source_email.from_address.casefold() != case.contact.email.casefold():
        raise ValueError("source sender does not match the associated case contact")
    if case.status in {CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST}:
        raise ValueError("closed case cannot send a reviewed reply")
    if case.customer.do_not_contact or case.contact.suppressed:
        raise ValueError("recipient is suppressed or marked do-not-contact")
    address_status = await session.get(EmailAddressStatus, case.contact.email.casefold())
    if address_status is not None and address_status.suppressed:
        raise ValueError("recipient address is permanently suppressed")

    clean_subject = subject.strip()
    clean_body = body_text.strip()
    if not clean_subject or "\r" in clean_subject or "\n" in clean_subject:
        raise ValueError("subject must be a single non-empty line")
    if not clean_body:
        raise ValueError("reply body cannot be empty")
    settings = get_settings()
    bundle = load_content(settings.content_dir)
    clean_body = _strip_duplicate_signature_lead(
        clean_body,
        bundle.signature_text,
    )
    if not clean_body:
        raise ValueError("reply body cannot contain only the signature sign-off")
    signed_text = "\n".join([clean_body, "", bundle.signature_text.strip()])
    html_lines = [
        f"<p>{html.escape(line) if line else '&nbsp;'}</p>"
        for line in clean_body.splitlines()
    ]
    signed_html = "".join(html_lines) + bundle.signature_html
    source = _reply_source(source_email)
    signed_text, signed_html = append_quoted_reply(
        signed_text,
        signed_html,
        from_address=source_email.from_address,
        source_body=source.body_text,
        source_html=source.body_html,
        occurred_at=source_email.received_at,
    )
    references = _reply_references(source_email)
    business_key = f"handoff-reply:{handoff.id}"
    message_id, raw = build_message(
        from_address=get_settings().mail_from,
        recipient=case.contact.email,
        subject=clean_subject,
        text_body=signed_text,
        html_body=signed_html,
        stable_key=business_key,
        in_reply_to=source_email.message_id,
        references=references,
        inline_images=source.inline_images,
        attachments=attachments,
    )
    parsed_outbound = parse_mime(raw.encode("utf-8"))
    now = datetime.now(UTC)
    outbox = Outbox(
        case_id=case.id,
        quote_id=quote.id if quote is not None else None,
        message_kind="HUMAN_REPLY",
        business_key=business_key,
        message_id=message_id,
        recipient=case.contact.email,
        raw_message=raw,
        approval_handoff_id=handoff.id,
        human_approved_by=actor[:128],
        human_approved_at=now,
    )
    session.add(outbox)
    await session.flush()
    session.add(
        EmailMessage(
            case_id=case.id,
            customer_id=case.customer_id,
            contact_id=case.contact_id,
            direction="OUTBOUND",
            message_id=message_id,
            in_reply_to=source_email.message_id,
            references_json=references,
            from_address=parseaddr(get_settings().mail_from)[1],
            to_addresses=[case.contact.email],
            subject=clean_subject,
            body_text=signed_text,
            body_html=signed_html,
            attachment_metadata=parsed_outbound.attachments,
            raw_sha256=parsed_outbound.raw_sha256,
        )
    )
    handoff.status = "RESOLVED"
    handoff.resolution_note = note.strip() or f"Reply approved by {actor}"
    case.status = CaseStatus.ACTIVE if resume_automation else CaseStatus.HUMAN_TAKEOVER
    await finalize_handoff_agent_run(
        session,
        handoff_id=handoff.id,
        actor=actor,
        outcome="human-reply",
    )
    await audit(
        session,
        "handoff.reply_approved",
        case_id=case.id,
        actor=actor,
        data={
            "handoff_id": handoff.id,
            "outbox_id": outbox.id,
            "message_id": message_id,
            "resume_automation": resume_automation,
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
        },
    )
    await session.commit()
    return outbox


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
        max_negotiation_rounds=(
            latest_policy.max_negotiation_rounds
            if latest_policy is not None
            else 2
        ),
        concession_step_pct=(
            latest_policy.concession_step_pct
            if latest_policy is not None
            else Decimal("0.02")
        ),
        min_quantity=latest_policy.min_quantity if latest_policy is not None else 1,
        max_quantity=(
            latest_policy.max_quantity if latest_policy is not None else None
        ),
        tier_1_max_multiple=(
            latest_policy.tier_1_max_multiple if latest_policy is not None else None
        ),
        tier_1_markup_pct=(
            latest_policy.tier_1_markup_pct
            if latest_policy is not None
            else Decimal("0")
        ),
        tier_2_max_multiple=(
            latest_policy.tier_2_max_multiple if latest_policy is not None else None
        ),
        tier_2_markup_pct=(
            latest_policy.tier_2_markup_pct
            if latest_policy is not None
            else Decimal("0")
        ),
        quote_valid_days=(
            latest_policy.quote_valid_days if latest_policy is not None else 30
        ),
        quote_valid_weekday=(
            latest_policy.quote_valid_weekday if latest_policy is not None else None
        ),
        standard_incoterm=(
            latest_policy.standard_incoterm if latest_policy is not None else "EXW"
        ),
        allowed_incoterms=list(latest_policy.allowed_incoterms or ["EXW"])
        if latest_policy is not None
        else ["EXW"],
        standard_payment_term=(
            latest_policy.standard_payment_term
            if latest_policy is not None
            else "100% before shipment"
        ),
        allowed_payment_terms=list(
            latest_policy.allowed_payment_terms or ["100% before shipment"]
        )
        if latest_policy is not None
        else ["100% before shipment"],
        taxes_included=latest_policy.taxes_included if latest_policy is not None else False,
        freight_included=(
            latest_policy.freight_included if latest_policy is not None else False
        ),
        valid_from=today,
        valid_to=None,
        source_hash=(
            f"manual-price:{handoff_id}:{actor}:{today.isoformat()}:"
            f"{product.id}:{currency}"
        ),
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
    handoff = await session.scalar(
        select(Handoff).where(Handoff.id == handoff_id).with_for_update()
    )
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
            raise ValueError(
                "the selected product does not match the case product; "
                "choose the case product to price it"
            )
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
                "contact_name": case.contact.name,
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
        select(Quote)
        .where(Quote.case_id == case.id)
        .order_by(Quote.round_number.desc(), Quote.id.desc())
        .limit(1)
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
        (
            f"{product_by_id[product_id].code} {quantity} "
            f"{product_by_id[product_id].unit} at {currency} {standard_price}"
        )
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


def _normalize_forward_recipient(value: str) -> str:
    result = validate_address_format(value)
    if not result.valid:
        raise ValueError("recipient must be a valid email address")
    return result.normalized


async def _touch_forward_recipient(
    session: AsyncSession,
    *,
    email: str,
    name: str = "",
) -> ForwardRecipient:
    now = datetime.now(UTC)
    row = await session.scalar(
        select(ForwardRecipient)
        .where(ForwardRecipient.email == email)
        .with_for_update()
    )
    if row is None:
        try:
            async with session.begin_nested():
                row = ForwardRecipient(
                    email=email,
                    name=name.strip() or None,
                    last_used_at=now,
                )
                session.add(row)
                await session.flush()
        except IntegrityError:
            row = await session.scalar(
                select(ForwardRecipient)
                .where(ForwardRecipient.email == email)
                .with_for_update()
            )
            if row is None:
                raise
    if name.strip():
        row.name = name.strip()
    row.last_used_at = now
    await session.flush()
    return row


def _extract_forward_attachments(raw: bytes) -> tuple[OutboundAttachment, ...]:
    """Collect non-inline MIME parts as forward attachments."""
    from email import policy as _email_policy
    from email.parser import BytesParser as _BytesParser

    message = _BytesParser(policy=_email_policy.default).parsebytes(raw)
    result: list[OutboundAttachment] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        content_id = str(part.get("Content-ID") or "").strip()
        content_location = str(part.get("Content-Location") or "").strip()
        inline_resource = bool(
            (content_id or content_location)
            and (disposition == "inline" or part.get_content_type().startswith("image/"))
        )
        if disposition == "attachment" or (filename and not inline_resource):
            payload = part.get_payload(decode=True) or b""
            if payload:
                result.append(
                    OutboundAttachment(
                        filename=filename or "unnamed",
                        content_type=part.get_content_type(),
                        payload=payload,
                    )
                )
    return tuple(result)


def _forwarded_message_bodies(
    *,
    note: str,
    source: FullReplySource,
    original_from: str,
    original_to: str,
    original_subject: str,
    occurred_at: datetime | None,
) -> tuple[str, str]:
    """Build a Gmail-style forwarded message with text and HTML preserved."""
    timestamp = occurred_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    date_line = timestamp.astimezone(UTC).strftime("%a, %d %b %Y %H:%M %z")
    body_text = source.body_text or ""
    header_lines = [
        "---------- Forwarded message ---------",
        f"From: {original_from}",
        f"Date: {date_line}",
        f"Subject: {original_subject}",
        f"To: {original_to}",
    ]
    text = "\n".join(header_lines) + "\n\n" + body_text
    if note.strip():
        text = f"{note.strip()}\n\n{text}"

    header_html = "".join(
        f"<b>{html.escape(label)}:</b> {html.escape(value)}<br>"
        for label, value in (
            ("From", original_from),
            ("Date", date_line),
            ("Subject", original_subject),
            ("To", original_to),
        )
    )
    if source.body_html:
        quoted_html = _sanitize_quoted_html(source.body_html)
    else:
        quoted_html = (
            '<div style="white-space:pre-wrap">' + html.escape(body_text) + "</div>"
        )
    note_html = f"<p>{html.escape(note.strip())}</p>" if note.strip() else ""
    html_body = (
        "<div>"
        f"{note_html}"
        "<p><b>---------- Forwarded message ---------</b></p>"
        f"<p>{header_html}</p>"
        f'<div class="gmail_quote">{quoted_html}</div>'
        "</div>"
    )
    return text, html_body


async def list_forward_recipients(
    session: AsyncSession,
    *,
    query: str = "",
    limit: int = 20,
) -> list[dict[str, Any]]:
    conditions = []
    q = query.strip()
    if q:
        pattern = f"%{q}%"
        conditions.append(
            or_(
                ForwardRecipient.email.ilike(pattern),
                ForwardRecipient.name.ilike(pattern),
            )
        )
    rows = (
        (
            await session.execute(
                select(ForwardRecipient)
                .where(*conditions)
                .order_by(
                    ForwardRecipient.last_used_at.desc().nullslast(),
                    ForwardRecipient.id.desc(),
                )
                .limit(max(1, min(limit, 50)))
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": row.id,
            "email": row.email,
            "name": row.name,
            "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        }
        for row in rows
    ]


async def save_forward_recipient(
    session: AsyncSession,
    *,
    email: str,
    name: str = "",
) -> dict[str, Any]:
    normalized = _normalize_forward_recipient(email)
    row = await _touch_forward_recipient(
        session,
        email=normalized,
        name=name,
    )
    await session.commit()
    return {"id": row.id, "email": row.email, "name": row.name}


@_atomic_business_operation
async def forward_handoff_email(
    session: AsyncSession,
    *,
    handoff_id: int,
    recipient: str,
    actor: str,
    note: str = "",
) -> Outbox:
    """Forward the source email to a salesperson and take the case over.

    The forward keeps the original message as a Gmail-style multipart
    alternative (plain text + sanitized HTML) and preserves non-inline
    attachments. The case is set to human takeover so the AI never replies to
    it again.
    """
    handoff = await session.scalar(
        select(Handoff).where(Handoff.id == handoff_id).with_for_update()
    )
    if handoff is None:
        raise ValueError("handoff not found")
    if handoff.status != "OPEN":
        raise ValueError("handoff is already resolved")
    if handoff.case_id is None or handoff.source_email_id is None:
        raise ValueError("associate the handoff with a case before forwarding")
    case = await session.scalar(
        select(SalesCase)
        .options(selectinload(SalesCase.contact))
        .where(SalesCase.id == handoff.case_id)
    )
    if case is None:
        raise ValueError("handoff case no longer exists")
    source_email = await session.get(EmailMessage, handoff.source_email_id)
    if source_email is None:
        raise ValueError("handoff source email no longer exists")
    settings = get_settings()
    recipient = _normalize_forward_recipient(recipient)
    address_status = await session.get(EmailAddressStatus, recipient)
    if address_status is not None and address_status.suppressed:
        raise ValueError("recipient address is permanently suppressed")
    archive_folder = "mail_archive" if source_email.is_history else "inbound_archive"
    archive_path = (
        settings.runtime_dir / archive_folder / f"{source_email.raw_sha256}.eml"
    )
    attachments: tuple[OutboundAttachment, ...] = ()
    try:
        raw = archive_path.read_bytes()
        source = extract_full_reply_source(raw)
        attachments = _extract_forward_attachments(raw)
    except OSError:
        if html_requires_mime_resources(source_email.body_html):
            raise ValueError(
                "the original email archive with inline images is unavailable "
                "for forwarding"
            ) from None
        source = FullReplySource(
            body_text=source_email.body_text,
            body_html=source_email.body_html,
        )

    text_body, html_body = _forwarded_message_bodies(
        note=note,
        source=source,
        original_from=source_email.from_address,
        original_to=", ".join(source_email.to_addresses),
        original_subject=source_email.subject,
        occurred_at=source_email.received_at,
    )
    subject = (
        f"Fwd: {source_email.subject}"
        if source_email.subject
        else "Fwd: (no subject)"
    )
    approved_at = datetime.now(UTC)
    outbox = await stage_outbox(
        session,
        case=case,
        message_kind="FORWARD",
        recipient=recipient,
        subject=subject[:998],
        text_body=text_body,
        html_body=html_body,
        business_key=f"handoff-reply:{handoff.id}:forward",
        in_reply_to=None,
        references=[],
        inline_images=source.inline_images,
        attachments=attachments,
        approval=HumanApproval(
            handoff_id=handoff.id,
            approved_by=actor[:128],
            approved_at=approved_at,
        ),
    )
    if outbox is None:
        raise ValueError("a forward is already queued for this handoff")

    normalized = recipient
    await _touch_forward_recipient(session, email=normalized)

    handoff.status = "RESOLVED"
    handoff.resolution_note = (
        note.strip()
        or f"Forwarded by {actor} to {recipient} for human takeover"
    )
    await finalize_handoff_agent_run(
        session,
        handoff_id=handoff.id,
        actor=actor,
        outcome="forwarded-to-salesperson",
        cancelled=True,
    )
    if handoff.dingtalk_status != "SENT":
        handoff.dingtalk_status = "CANCELLED"
    case.status = CaseStatus.HUMAN_TAKEOVER
    session.add(
        AuditEvent(
            case_id=case.id,
            actor=actor,
            event_type="handoff.forwarded_to_salesperson",
            data={
                "handoff_id": handoff.id,
                "outbox_id": outbox.id,
                "recipient": normalized,
                "case_status": case.status.value,
                "attachments": len(attachments),
            },
        )
    )
    await session.commit()
    return outbox


def _reply_references(source_email: EmailMessage) -> list[str]:
    """Build a complete, ordered RFC reply chain for a response."""
    return list(
        dict.fromkeys(
            item
            for item in [
                *source_email.references_json,
                source_email.in_reply_to,
                source_email.message_id,
            ]
            if item
        )
    )


MAX_REPLY_SOURCE_ARCHIVE_BYTES = 30 * 1024 * 1024


def _reply_source(source_email: EmailMessage) -> FullReplySource:
    """Load the complete direct-parent display body and its inline resources."""
    archive_folder = "mail_archive" if source_email.is_history else "inbound_archive"
    archive_path = (
        get_settings().runtime_dir
        / archive_folder
        / f"{source_email.raw_sha256}.eml"
    )
    try:
        archive_size = archive_path.stat().st_size
        raw = archive_path.read_bytes()
    except OSError as exc:
        if html_requires_mime_resources(source_email.body_html):
            raise RuntimeError(
                f"complete reply source with inline images is unavailable for email_id={source_email.id}"
            ) from exc
        logger.warning(
            "Complete reply archive unavailable for email_id=%s; using stored body without MIME resources",
            source_email.id,
        )
        return FullReplySource(
            body_text=source_email.body_text,
            body_html=source_email.body_html,
        )
    if archive_size > MAX_REPLY_SOURCE_ARCHIVE_BYTES:
        raise RuntimeError(
            f"complete reply source exceeds {MAX_REPLY_SOURCE_ARCHIVE_BYTES} bytes"
        )
    try:
        return extract_full_reply_source(raw)
    except (ValueError, LookupError, RecursionError) as exc:
        raise RuntimeError(
            f"complete reply source could not preserve inline content for email_id={source_email.id}"
        ) from exc


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


def render_quote(
    *,
    plan: Any,
    bundle: ContentBundle,
    product_key: str,
    product_name: str,
    price: Decimal,
    currency: str,
    quantity: int,
    unit: str,
    incoterm: str,
    payment_term: str,
    valid_until: date,
    taxes_included: bool = False,
    freight_included: bool = False,
    availability: str = "Ready stock",
) -> tuple[str, str]:
    snippet = bundle.product_snippets[product_key]
    # Free-form model prose is deliberately not inserted into a commercial email.
    # The structured plan selects tone/snippet IDs; factual language remains local and reviewed.
    safe_greeting = plan.greeting.lower().startswith("dear ") and not any(ch.isdigit() for ch in plan.greeting)
    greeting = plan.greeting if safe_greeting else "Dear Customer,"
    opening = "Thank you for your inquiry."
    price_lead_in = "Please find our standard quotation details below."
    closing = "Please let us know if you have questions about this non-binding standard quotation."
    body_lines = [
        greeting,
        "",
        opening,
        snippet,
        "",
        price_lead_in,
        f"Product: {product_name}",
        f"Quantity: {quantity} {unit}",
        f"Unit price: {currency} {price:.4f} per {unit}",
        f"Availability: {availability}",
        f"Price basis: {incoterm} (ex-warehouse)",
        f"Taxes: {'included' if taxes_included else 'excluded'}",
        f"Freight: {'included' if freight_included else 'excluded'}",
        f"Payment term: {payment_term}",
        f"Quote valid until: {valid_until.isoformat()} ({valid_until.strftime('%A')})",
        "",
        closing,
    ]
    business_text = "\n".join(body_lines)
    validate_rendered_email(business_text, exact_price=price, currency=currency, approved_fragments=[snippet])
    text = "\n".join([business_text, "", bundle.signature_text.strip()])
    html_body = (
        "<p>"
        + "</p><p>".join(html.escape(line) if line else "&nbsp;" for line in body_lines)
        + "</p>"
        + bundle.signature_html
    )
    return text, html_body


def render_multi_quote(
    *,
    plan: Any,
    bundle: ContentBundle,
    lines: list[dict[str, object]],
    currency: str,
    valid_until: date,
    availability_note: str,
) -> tuple[str, str]:
    """Render one non-binding quotation covering several products."""
    safe_greeting = plan.greeting.lower().startswith("dear ") and not any(
        ch.isdigit() for ch in plan.greeting
    )
    greeting = plan.greeting if safe_greeting else "Dear Customer,"
    opening = "Thank you for your inquiry."
    price_lead_in = "Please find our standard quotation details below."
    closing = "Please let us know if you have questions about this non-binding standard quotation."
    body_lines: list[str] = [greeting, "", opening, "", price_lead_in]
    for index, line in enumerate(lines):
        body_lines.extend(
            [
                f"Product: {line['product_name']}",
                f"Quantity: {line['quantity']} {line['unit']}",
                f"Unit price: {currency} {line['unit_price']:.4f} per {line['unit']}",
                f"Availability: {availability_note}",
                f"Price basis: {line['incoterm']} (ex-warehouse)",
                f"Taxes: {'included' if line['taxes_included'] else 'excluded'}",
                f"Freight: {'included' if line['freight_included'] else 'excluded'}",
                f"Payment term: {line['payment_term']}",
            ]
        )
        if index < len(lines) - 1:
            body_lines.append("---")
    body_lines.extend(
        [
            f"Quote valid until: {valid_until.isoformat()} ({valid_until.strftime('%A')})",
            "",
            closing,
        ]
    )
    business_text = "\n".join(body_lines)
    text = "\n".join([business_text, "", bundle.signature_text.strip()])
    html_body = (
        "<p>"
        + "</p><p>".join(html.escape(line) if line else "&nbsp;" for line in body_lines)
        + "</p>"
        + bundle.signature_html
    )
    return text, html_body


def standard_quote_valid_until(
    settings: Settings,
    at: datetime | None = None,
) -> date:
    """Quotations expire on the coming Monday.

    The weekly price refresh covers only the current week, so every quotation
    is anchored to the Monday of the following week regardless of the product
    or the day it was issued.
    """
    observed = at or datetime.now(UTC)
    today = observed.astimezone(ZoneInfo(settings.business_timezone)).date()
    days_until_monday = (0 - today.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7
    return today + timedelta(days=days_until_monday)


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
            HandoffReason.INVENTORY_UNAVAILABLE
            if commercial_context.reason.startswith("INVENTORY")
            else HandoffReason.NONSTANDARD
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
    business_key = (
        f"initial-quote:case:{case.id}:cycle:{cycle_id}"
        if cycle_id is not None
        else f"initial-quote:case:{case.id}"
    )
    if await session.scalar(select(Outbox.id).where(Outbox.business_key == business_key)) is not None:
        return
    existing_quote = await session.scalar(
        select(Quote).where(Quote.case_id == case.id).order_by(Quote.round_number.desc()).limit(1)
    )
    if existing_quote is not None and not reprice:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.NONSTANDARD,
            summary="Case already has a quotation but no matching initial-outreach outbox record",
        )
        return
    policy_row = (
        commercial_context.policy
        if commercial_context is not None
        else await active_policy(session, case.product_id, case.currency)
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


async def _ensure_inbound_follow_up(
    session: AsyncSession,
    row: EmailMessage,
    *,
    ambiguous: bool = False,
    review_reason: HandoffReason | None = None,
    review_summary: str | None = None,
    review_facts: dict[str, Any] | None = None,
) -> None:
    if row.is_bounce:
        await enqueue_job(
            session,
            "process_inbound",
            {"email_id": row.id},
            f"process-inbound:{row.id}",
        )
        return
    if row.automated_reply_type == AutomatedReplyType.SYSTEM_NOTIFICATION.value:
        if row.automated_reply_handled_at is None:
            row.automated_reply_handled_at = datetime.now(UTC)
            await audit(
                session,
                "inbound.system_notification_ignored",
                case_id=row.case_id,
                actor="system",
                data={
                    "email_id": row.id,
                    "sender": row.from_address,
                    "subject": row.subject,
                    **(row.automated_reply_metadata or {}),
                },
            )
            await session.commit()
        return
    if row.case_id is not None:
        await enqueue_job(
            session,
            "process_inbound",
            {"email_id": row.id},
            f"process-inbound:{row.id}",
        )
        return
    summary_prefix = "Ambiguous thread" if ambiguous else "No case matched inbound email"
    await create_handoff(
        session,
        case=None,
        reason=review_reason or HandoffReason.THREAD_AMBIGUOUS,
        summary=review_summary or f"{summary_prefix} from {row.from_address}: {row.subject}",
        facts=review_facts,
        source_email_id=row.id,
    )


def _explicit_product_codes(text: str) -> list[str]:
    codes = find_product_codes(text)
    explicit = []
    for value in re.findall(
        r"\b(?:SKU|PRODUCT)\s*[:#-]?\s*([A-Z0-9][A-Z0-9_()%.\-]{1,63})",
        text,
        flags=re.IGNORECASE,
    ):
        # "PRODUCT LIST/CATALOG/BROCHURE/PRODUCTS" are category-catalog
        # requests, not explicit product codes. Only keep candidates that
        # look like real product codes (digit-bearing tokens, or brand-style
        # prefixes from the catalog) so prose fragments such as "LIST" or
        # "S." (from "PRODUCTS") are not treated as SKUs.
        candidate = value.rstrip(".,;:!?")
        if candidate.upper() in {
            "LIST",
            "LISTS",
            "CATALOG",
            "CATALOGUE",
            "BROCHURE",
            "RANGE",
            "PORTFOLIO",
        }:
            continue
        if re.search(r"\d", candidate) or re.match(
            r"^(YAC|LANNOX|UV|SBM|DBM|CAA|ZAA|THEIC|AAA)[-_A-Z0-9]*$",
            candidate,
            re.I,
        ):
            explicit.append(candidate)
    return list(dict.fromkeys([*codes, *(canonical_product_code(value) for value in explicit)]))


def _product_lookup_conditions(codes: list[str]) -> Any:
    """Match catalog codes by exact value or canonical normalization.

    Product codes are normalized with the same separator-insensitive key used
    for lookups, so WIDGET-100, WIDGET_100 and widget_100 all resolve to the
    same catalog row. Exact equality is kept as a fast path.
    """
    conditions = []
    for code in codes:
        conditions.append(Product.code == code)
        conditions.append(
            func.lower(
                func.regexp_replace(Product.code, r"[^a-zA-Z0-9]", "_", "g")
            )
            == product_text_key(code)
        )
    return or_(*conditions)


def _prior_thread_marker(text: str) -> str | None:
    lowered = text.casefold()
    return next((marker for marker in PRIOR_THREAD_MARKERS if marker in lowered), None)


async def _resolve_category_inquiry_case(
    session: AsyncSession,
    *,
    contact: Contact,
    incoming_subject_key: str,
    facts: dict[str, Any],
) -> NewInquiryResolution | None:
    """Create or reuse a category case when the CRM knows exactly one interest."""
    interest_keys = customer_interest_keys(contact.customer)
    active_keys = await active_category_keys(session)
    known = [key for key in interest_keys if key in active_keys]
    if len(known) != 1:
        facts.update(
            {
                "interest_categories": interest_keys,
                "active_interest_categories": known,
            }
        )
        return None
    category_key = known[0]
    category = await session.scalar(
        select(ProductCategory).where(
            ProductCategory.key == category_key,
            ProductCategory.active.is_(True),
        )
    )
    if category is None:
        return None
    customer_currency_rows = await session.execute(
        select(SalesCase.currency).where(
            SalesCase.customer_id == contact.customer_id,
            SalesCase.status.not_in([CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST]),
        )
    )
    customer_currencies = set(customer_currency_rows.scalars().all())
    currency = next(iter(customer_currencies)) if len(customer_currencies) == 1 else "USD"
    existing = await session.scalar(
        select(SalesCase)
        .where(
            SalesCase.contact_id == contact.id,
            SalesCase.category_id == category.id,
            SalesCase.status == CaseStatus.ACTIVE,
        )
        .order_by(SalesCase.id.desc())
    )
    if existing is not None:
        return NewInquiryResolution(
            existing,
            facts={
                **facts,
                "category_id": category.id,
                "category_key": category.key,
                "currency": currency,
                "match_basis": "customer_interest_category",
                "reused_category_case": True,
            },
        )
    sales_case = SalesCase(
        customer_id=contact.customer_id,
        contact_id=contact.id,
        product_id=None,
        category_id=category.id,
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
            "category_id": category.id,
            "category_key": category.key,
            "currency": currency,
            "match_basis": "customer_interest_category",
        },
    )


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
                "original_contact_id": (
                    trusted_reactivation_parent.original_contact.id
                ),
                "reply_contact_id": trusted_reactivation_parent.reply_contact.id,
                "reply_contact_created": (
                    trusted_reactivation_parent.reply_contact_created
                ),
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
        (
            await session.execute(
                select(Contact)
                .options(selectinload(Contact.customer))
                .where(func.lower(Contact.email) == sender)
            )
        )
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
                get_settings().company_research_enabled
                and explicit_product_list_requested(combined_text)
                and not facts.get("active_interest_categories")
            ):
                currency_rows = await session.execute(
                    select(SalesCase.currency).where(
                        SalesCase.customer_id == contact.customer_id,
                        SalesCase.status.not_in(
                            [CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST]
                        ),
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
                        "match_basis": "explicit_product_list_pending_company_research",
                    },
                )
            if (
                explicit_product_list_requested(combined_text)
                and not facts.get("active_interest_categories")
            ):
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
                    SalesCase.status.not_in(
                        [CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST]
                    ),
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
    elif (
        not policy_currencies
        and not customer_currencies
        and not explicit_product_list_requested(combined_text)
    ):
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
                        SalesCase.id.in_(
                            select(Quote.case_id).where(Quote.valid_until >= today)
                        ),
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


async def _case_less_reactivation_parent(
    session: AsyncSession,
    parsed: ParsedEmail,
) -> CaseLessReactivationParent | None:
    """Find a verified case-less reactivation message referenced by this reply.

    Exact sender matching remains the primary path. A changed sender address is
    accepted only when one exact ``In-Reply-To`` parent exists and both old and
    new addresses share the same non-free corporate domain. The new endpoint is
    stored as a separate contact; the historical recipient is never overwritten.
    """

    sender = parsed.from_address.strip().casefold()
    ordered_ids = list(dict.fromkeys(item for item in parsed.references if item))
    if parsed.in_reply_to:
        ordered_ids = [parsed.in_reply_to]
    else:
        ordered_ids.reverse()
    if not sender or not ordered_ids:
        return None
    occurred_at = parsed.occurred_at or datetime.now(UTC)
    rows = (
        await session.execute(
            select(Outbox, ReactivationRecipient, Contact)
                .join(ReactivationRecipient, ReactivationRecipient.outbox_id == Outbox.id)
                .join(Contact, Contact.id == ReactivationRecipient.contact_id)
                .where(
                    Outbox.message_id.in_(ordered_ids),
                    Outbox.case_id.is_(None),
                    Outbox.message_kind == "REACTIVATION",
                    Outbox.status == DeliveryStatus.SENT,
                    Outbox.sent_at.is_not(None),
                    Outbox.sent_at <= occurred_at,
                    ReactivationRecipient.status.in_(["QUEUED", "SENT"]),
                    ReactivationRecipient.customer_id == Contact.customer_id,
                )
                .with_for_update()
        )
    ).all()
    exact_matches = [
        CaseLessReactivationParent(
            outbox=outbox,
            recipient=recipient,
            original_contact=contact,
            reply_contact=contact,
        )
        for outbox, recipient, contact in rows
        if outbox.recipient.strip().casefold() == sender
        and contact.email.strip().casefold() == sender
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]
    if exact_matches or parsed.in_reply_to is None or len(rows) != 1:
        return None

    outbox, recipient, original_contact = rows[0]
    original_recipient = outbox.recipient.strip().casefold()
    original_contact_email = original_contact.email.strip().casefold()
    sender_domain = _nonfree_email_domain(sender)
    if (
        sender_domain is None
        or sender_domain != _nonfree_email_domain(original_recipient)
        or sender_domain != _nonfree_email_domain(original_contact_email)
        or original_recipient != original_contact_email
    ):
        return None
    customer = await session.get(Customer, recipient.customer_id)
    if customer is None or customer.id != original_contact.customer_id:
        return None
    try:
        reply_contact, created = await _ensure_customer_contact(
            session,
            customer=customer,
            email=sender,
            name="Customer",
            actor="thread_resolver",
            source="exact_reactivation_message_id_same_company_domain",
        )
    except ValueError:
        # A suppressed or cross-customer identity is never silently reassigned.
        return None

    metadata = dict(reply_contact.metadata_json or {})
    thread_links = list(metadata.get("reactivation_thread_links") or [])
    link = {
        "source": "exact_reactivation_message_id_same_company_domain",
        "original_contact_id": original_contact.id,
        "original_email": original_contact_email,
        "parent_outbox_id": outbox.id,
        "parent_message_id": outbox.message_id,
        "matched_domain": sender_domain,
        "linked_at": datetime.now(UTC).isoformat(),
    }
    if not any(
        item.get("parent_outbox_id") == outbox.id
        for item in thread_links
        if isinstance(item, dict)
    ):
        thread_links.append(link)
    metadata["reactivation_thread_links"] = thread_links[-20:]
    reply_contact.metadata_json = metadata
    return CaseLessReactivationParent(
        outbox=outbox,
        recipient=recipient,
        original_contact=original_contact,
        reply_contact=reply_contact,
        sender_changed=True,
        reply_contact_created=created,
        matched_domain=sender_domain,
    )


async def ingest_raw_email(
    session: AsyncSession,
    raw: bytes,
    *,
    mailbox: str = "demo",
    mailbox_folder: str | None = None,
    uid_validity: int | None = None,
    imap_uid: int | None = None,
    direction: str = "INBOUND",
    is_history: bool = False,
) -> EmailMessage | None:
    direction = direction.upper()
    if direction not in {"INBOUND", "OUTBOUND"}:
        raise ValueError(f"unsupported email direction: {direction}")
    parsed = parse_mime(raw)
    bounce = classify_bounce(
        raw,
        subject=parsed.subject,
        body=parsed.body_text,
        sender=parsed.from_address,
    ) if direction == "INBOUND" else None
    automated_reply = (
        classify_automated_reply(
            subject=parsed.subject,
            body=parsed.body_text,
            headers=parsed.header_metadata,
            sender=parsed.from_address,
        )
        if direction == "INBOUND" and not (bounce and bounce.is_bounce)
        else None
    )
    duplicate_query = select(EmailMessage).where(
        (EmailMessage.raw_sha256 == parsed.raw_sha256)
        | ((EmailMessage.message_id == parsed.message_id) & EmailMessage.message_id.is_not(None))
    )
    duplicate = await session.scalar(duplicate_query)
    if duplicate:
        if direction == "INBOUND" and duplicate.direction == "INBOUND" and not is_history:
            await _ensure_inbound_follow_up(session, duplicate)
        return duplicate
    # DEPARTED / CONTACT_CHANGE messages are normally written by a human
    # (for example "Pooja no longer works here, please send the product
    # list to this address"). They must enter the normal new-inquiry and
    # product-list pipeline instead of being swallowed as automated noise,
    # while keeping subject-based case matching for threadless replies.
    personnel_reply = bool(
        automated_reply
        and automated_reply.reply_type
        in {
            AutomatedReplyType.DEPARTED,
            AutomatedReplyType.CONTACT_CHANGE,
        }
    )
    live_human_inbound = bool(
        direction == "INBOUND"
        and not is_history
        and not (bounce and bounce.is_bounce)
        and not (
            automated_reply
            and automated_reply.is_automated
            and not personnel_reply
        )
    )
    is_system_notification = bool(
        automated_reply
        and automated_reply.reply_type is AutomatedReplyType.SYSTEM_NOTIFICATION
    )
    if is_system_notification:
        # Trusted infrastructure notifications are mailbox records, not sales
        # participants. Never attach them to a coincidentally similar thread.
        case, ambiguous = None, False
    else:
        case, ambiguous = await match_case(
            session,
            parsed,
            direction=direction,
            # A live human-authored message may inherit commercial history only
            # through Message-ID/References. Subject-only matching is retained for
            # history reconciliation, outbound mail, and non-sending auto replies.
            allow_subject_fallback=not live_human_inbound or personnel_reply,
        )
    new_inquiry = NewInquiryResolution(case)
    reactivation_parent: CaseLessReactivationParent | None = None
    personnel_change_handled = False
    if live_human_inbound and case is None and not ambiguous:
        has_thread_headers = bool(parsed.in_reply_to or parsed.references)
        if has_thread_headers:
            reactivation_parent = await _case_less_reactivation_parent(session, parsed)
            if reactivation_parent is not None:
                new_inquiry = await _resolve_new_inquiry_case(
                    session,
                    parsed,
                    trusted_reactivation_parent=reactivation_parent,
                )
                case = new_inquiry.case
            else:
                # A concurrent first reply can promote a case-less reactivation
                # while this transaction waits for the parent row lock. Re-run
                # the authoritative header match once before escalating.
                case, ambiguous = await match_case(
                    session,
                    parsed,
                    direction=direction,
                    allow_subject_fallback=False,
                )
                if case is not None:
                    new_inquiry = NewInquiryResolution(case)
                else:
                    new_inquiry = NewInquiryResolution(
                        None,
                        HandoffReason.THREAD_AMBIGUOUS,
                        "Inbound reply contains thread references that do not match a known case",
                        {
                            "new_thread": False,
                            "sender": parsed.from_address,
                            "subject": parsed.subject,
                            "in_reply_to": parsed.in_reply_to,
                            "references": parsed.references,
                        },
                    )
        else:
            new_inquiry = await _resolve_new_inquiry_case(session, parsed)
            case = new_inquiry.case
    if reactivation_parent is not None and case is not None:
        reactivation_parent.outbox.case_id = case.id
        reactivation_parent.recipient.case_id = case.id
        await session.execute(
            update(EmailMessage)
            .where(
                EmailMessage.message_id == reactivation_parent.outbox.message_id,
                EmailMessage.direction == "OUTBOUND",
                EmailMessage.case_id.is_(None),
            )
            .values(case_id=case.id)
        )
        session.add(
            AuditEvent(
                case_id=case.id,
                actor="thread_resolver",
                event_type="reactivation.thread_promoted",
                data={
                    "outbox_id": reactivation_parent.outbox.id,
                    "recipient_id": reactivation_parent.recipient.id,
                },
            )
        )
        if reactivation_parent.sender_changed:
            session.add(
                AuditEvent(
                    case_id=case.id,
                    actor="thread_resolver",
                    event_type="reactivation.sender_endpoint_linked",
                    data={
                        "outbox_id": reactivation_parent.outbox.id,
                        "recipient_id": reactivation_parent.recipient.id,
                        "original_contact_id": (
                            reactivation_parent.original_contact.id
                        ),
                        "original_email": (
                            reactivation_parent.original_contact.email
                        ),
                        "reply_contact_id": reactivation_parent.reply_contact.id,
                        "reply_email": reactivation_parent.reply_contact.email,
                        "reply_contact_created": (
                            reactivation_parent.reply_contact_created
                        ),
                        "matched_domain": reactivation_parent.matched_domain,
                    },
                )
            )
        if (
            automated_reply
            and automated_reply.reply_type
            in {
                AutomatedReplyType.DEPARTED,
                AutomatedReplyType.CONTACT_CHANGE,
            }
        ):
            original = reactivation_parent.original_contact
            if original.id != reactivation_parent.reply_contact.id:
                # The historical recipient left the company / changed roles;
                # retire that endpoint while keeping the new reply contact
                # active so the business request can still be handled.
                original.suppressed = True
                personnel_change_handled = True
                session.add(
                    AuditEvent(
                        case_id=case.id,
                        actor="thread_resolver",
                        event_type="contact.suppressed_for_personnel_change",
                        data={
                            "original_contact_id": original.id,
                            "original_email": original.email,
                            "reply_contact_id": reactivation_parent.reply_contact.id,
                            "reply_email": reactivation_parent.reply_contact.email,
                            "automated_reply_type": automated_reply.reply_type.value,
                        },
                    )
                )
            else:
                # Same endpoint is the one that left; suppress it as well.
                original.suppressed = True
                personnel_change_handled = True
                session.add(
                    AuditEvent(
                        case_id=case.id,
                        actor="thread_resolver",
                        event_type="contact.suppressed_for_personnel_change",
                        data={
                            "original_contact_id": original.id,
                            "original_email": original.email,
                            "reply_contact_id": reactivation_parent.reply_contact.id,
                            "reply_email": reactivation_parent.reply_contact.email,
                            "automated_reply_type": automated_reply.reply_type.value,
                        },
                    )
                )
    matched_outbox = None
    if bounce and bounce.is_bounce and bounce.original_message_id:
        matched_outbox = await session.scalar(
            select(Outbox).where(
                Outbox.message_id == bounce.original_message_id,
                Outbox.status == DeliveryStatus.SENT,
            )
        )
        if case is None and matched_outbox and matched_outbox.case_id:
            case = await session.get(SalesCase, matched_outbox.case_id)
            ambiguous = False
    bounce_metadata = bounce.metadata() if bounce and bounce.is_bounce else {}
    if matched_outbox is not None:
        bounce_metadata["matched_outbox_id"] = matched_outbox.id
    identity_contact = None
    if case is None and not is_system_notification:
        identity_addresses = (
            [parsed.from_address] if direction == "INBOUND" else parsed.to_addresses
        )
        identity_contact = await resolve_unique_contact(session, identity_addresses)
    identity_customer_id = (
        case.customer_id
        if case is not None
        else identity_contact.customer_id if identity_contact is not None else None
    )
    identity_contact_id = (
        case.contact_id
        if case is not None
        else identity_contact.id if identity_contact is not None else None
    )
    try:
        async with session.begin_nested():
            automated_metadata = (
                {**automated_reply.metadata(), "headers": parsed.header_metadata}
                if automated_reply and automated_reply.is_automated
                else {}
            )
            if personnel_change_handled and reactivation_parent is not None:
                automated_metadata["personnel_change_handled"] = True
                automated_metadata["original_contact_id"] = (
                    reactivation_parent.original_contact.id
                )
            row = EmailMessage(
                case_id=case.id if case else None,
                customer_id=identity_customer_id,
                contact_id=identity_contact_id,
                direction=direction,
                mailbox=mailbox,
                mailbox_folder=mailbox_folder,
                uid_validity=uid_validity,
                imap_uid=imap_uid,
                message_id=parsed.message_id,
                in_reply_to=parsed.in_reply_to,
                references_json=parsed.references,
                from_address=parsed.from_address,
                to_addresses=parsed.to_addresses,
                subject=parsed.subject,
                body_text=parsed.body_text,
                body_html=parsed.body_html,
                attachment_metadata=parsed.attachments,
                raw_sha256=parsed.raw_sha256,
                is_history=is_history,
                is_automated_reply=bool(automated_reply and automated_reply.is_automated),
                automated_reply_type=(
                    automated_reply.reply_type.value
                    if automated_reply and automated_reply.reply_type is not None
                    else None
                ),
                automated_reply_metadata=automated_metadata,
                is_bounce=bool(bounce and bounce.is_bounce),
                bounce_type=(
                    bounce.bounce_type.value
                    if bounce and bounce.bounce_type is not None
                    else None
                ),
                bounce_metadata=bounce_metadata,
                received_at=parsed.occurred_at or datetime.now(UTC),
            )
            session.add(row)
            await session.flush()
    except IntegrityError:
        # The personnel-change / thread-linking side effects above live
        # outside the savepoint and must not survive a duplicate-email
        # collision (for example two IMAP syncs racing on the same message).
        await session.rollback()
        duplicate = await session.scalar(duplicate_query)
        if duplicate is None:
            raise
        if direction == "INBOUND" and duplicate.direction == "INBOUND" and not is_history:
            await _ensure_inbound_follow_up(session, duplicate)
        return duplicate

    archive_dir = "mail_archive" if is_history or direction == "OUTBOUND" else "inbound_archive"
    archive = get_settings().runtime_dir / archive_dir / f"{parsed.raw_sha256}.eml"
    archive.write_bytes(raw)
    await audit(
        session,
        "email.history_ingested" if is_history else "email.ingested",
        case_id=case.id if case else None,
        actor="gmail_history" if is_history else ("imap" if mailbox != "demo" else "demo"),
        data={
            "email_id": row.id,
            "message_id": parsed.message_id,
            "direction": direction,
            "mailbox": mailbox,
            "mailbox_folder": mailbox_folder,
            "automated_reply_type": row.automated_reply_type,
            "bounce_type": row.bounce_type,
        },
    )
    if new_inquiry.case is not None and new_inquiry.facts is not None and live_human_inbound:
        await audit(
            session,
            (
                "email.thread_recovered"
                if new_inquiry.facts.get("recovered_thread")
                else "case.created_from_new_inquiry"
            ),
            case_id=new_inquiry.case.id,
            actor="thread_resolver",
            data=new_inquiry.facts,
        )
    if direction == "INBOUND" and not is_history and reactivation_parent is not None:
        await record_reactivation_reply(
            session,
            row,
            recipient_id=reactivation_parent.recipient.id,
            allow_changed_contact=reactivation_parent.sender_changed,
            commit=False,
        )
    await session.commit()
    if direction == "INBOUND" and not is_history:
        await _ensure_inbound_follow_up(
            session,
            row,
            ambiguous=ambiguous,
            review_reason=new_inquiry.reason,
            review_summary=new_inquiry.summary,
            review_facts=new_inquiry.facts,
        )
    return row


async def _match_bounce_outbox(
    session: AsyncSession,
    email_row: EmailMessage,
) -> tuple[Outbox | None, str | None]:
    metadata = email_row.bounce_metadata or {}
    recipient = str(metadata.get("recipient") or "").strip().casefold() or None
    outbox = None
    if metadata.get("matched_outbox_id"):
        outbox = await session.get(Outbox, int(metadata["matched_outbox_id"]))
    if outbox is None and metadata.get("original_message_id"):
        outbox = await session.scalar(
            select(Outbox).where(
                Outbox.message_id == str(metadata["original_message_id"]),
                Outbox.status == DeliveryStatus.SENT,
            )
        )
    if outbox is None and recipient:
        outbox = await session.scalar(
            select(Outbox)
            .where(
                func.lower(Outbox.recipient) == recipient,
                Outbox.status == DeliveryStatus.SENT,
            )
            .order_by(Outbox.sent_at.desc(), Outbox.id.desc())
        )
    if outbox is not None:
        recipient = recipient or outbox.recipient.casefold()
        if recipient != outbox.recipient.casefold():
            return None, recipient
    return outbox, recipient


async def _apply_correlated_hard_bounce(
    session: AsyncSession,
    *,
    email_row: EmailMessage,
    outbox: Outbox,
    recipient: str,
    case: SalesCase | None,
    audit_event: str,
) -> None:
    metadata = dict(email_row.bounce_metadata or {})
    diagnostic = str(metadata.get("diagnostic") or "")[:2000] or None
    await _suppress_email_address(
        session,
        recipient,
        reason="HARD_BOUNCE",
        source_email_id=email_row.id,
        bounce_type=BounceType.HARD.value,
        diagnostic=diagnostic,
    )
    if case and case.status not in {CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST}:
        case.status = CaseStatus.PAUSED
    campaign_recipient = await session.scalar(
        select(ReactivationRecipient).where(ReactivationRecipient.outbox_id == outbox.id)
    )
    if campaign_recipient is not None and campaign_recipient.status != "REPLIED":
        campaign_recipient.status = "FAILED"
        campaign_recipient.exclusion_reason = "HARD_BOUNCE"
    await audit(
        session,
        audit_event,
        case_id=case.id if case else None,
        actor="system",
        data={"email_id": email_row.id, "outbox_id": outbox.id, **metadata},
    )


async def _handle_bounce(session: AsyncSession, email_row: EmailMessage) -> None:
    if email_row.bounce_handled_at is not None:
        return
    outbox, recipient = await _match_bounce_outbox(session, email_row)
    case = await session.get(SalesCase, outbox.case_id) if outbox and outbox.case_id else None
    if case is None and email_row.case_id:
        case = await session.get(SalesCase, email_row.case_id)
    if case and email_row.case_id is None:
        email_row.case_id = case.id
    if case:
        email_row.customer_id = case.customer_id
        email_row.contact_id = case.contact_id

    metadata = dict(email_row.bounce_metadata or {})
    if outbox is not None:
        metadata["matched_outbox_id"] = outbox.id
    metadata["recipient"] = recipient
    email_row.bounce_metadata = metadata
    email_row.bounce_handled_at = datetime.now(UTC)
    diagnostic = str(metadata.get("diagnostic") or "")[:2000] or None

    if email_row.bounce_type == BounceType.HARD.value and outbox is not None and recipient:
        await _apply_correlated_hard_bounce(
            session,
            email_row=email_row,
            outbox=outbox,
            recipient=recipient,
            case=case,
            audit_event="inbound.hard_bounce_suppressed",
        )
        await session.commit()
        return

    if recipient:
        status = await _email_address_status(session, recipient)
        # A late or repeated delivery report is still useful endpoint history.
        # Preserve the newest bounce facts even when the address was already
        # suppressed, while avoiding another handoff/notification below.
        status.last_bounce_at = datetime.now(UTC)
        status.last_bounce_type = email_row.bounce_type
        status.last_bounce_diagnostic = diagnostic
        if status.suppressed:
            # The endpoint is already suppressed, so a late or repeated
            # delivery report must not create another human-review handoff
            # or DingTalk notification. Record it and move on.
            await audit(
                session,
                "inbound.bounce_ignored_suppressed",
                case_id=case.id if case else None,
                actor="system",
                data={
                    "email_id": email_row.id,
                    "outbox_id": outbox.id if outbox else None,
                    **metadata,
                },
            )
            await session.commit()
            return
    await audit(
        session,
        "inbound.bounce_review_required",
        case_id=case.id if case else None,
        actor="system",
        data={"email_id": email_row.id, "outbox_id": outbox.id if outbox else None, **metadata},
    )
    await create_handoff(
        session,
        case=case,
        reason=HandoffReason.BOUNCE_REVIEW,
        summary=(
            f"Review {email_row.bounce_type or 'unknown'} delivery failure for "
            f"{recipient or 'an unidentified recipient'}"
        ),
        facts={"email_id": email_row.id, "outbox_id": outbox.id if outbox else None, **metadata},
        source_email_id=email_row.id,
    )


async def reconcile_permanent_bounce_handoffs(session: AsyncSession) -> int:
    """Resolve legacy SOFT reviews whose content proves a permanent failure.

    This is deliberately limited to a bounce that can be correlated to an
    exact sent outbox recipient.  Uncorrelated delivery reports remain in
    review so an arbitrary inbound message cannot suppress a customer.
    """
    handoffs = (
        (
            await session.execute(
                select(Handoff)
                .where(
                    Handoff.status == "OPEN",
                    Handoff.reason_code == HandoffReason.BOUNCE_REVIEW.value,
                    Handoff.source_email_id.is_not(None),
                )
                .order_by(Handoff.id)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    resolved = 0
    for handoff in handoffs:
        email_row = await session.get(EmailMessage, handoff.source_email_id)
        if email_row is None or not email_row.is_bounce:
            continue
        facts = {**(email_row.bounce_metadata or {}), **(handoff.extracted_facts or {})}
        evidence = "\n".join(
            str(value)
            for value in (
                email_row.subject,
                email_row.body_text,
                handoff.summary,
                facts.get("diagnostic"),
                facts.get("detail"),
                facts.get("status_code"),
            )
            if value
        )
        if not has_permanent_failure_evidence(
            evidence,
            status_code=str(facts.get("status_code") or "") or None,
        ):
            continue

        outbox, recipient = await _match_bounce_outbox(session, email_row)
        if outbox is None or recipient is None:
            continue
        case_id = handoff.case_id or outbox.case_id or email_row.case_id
        case = await session.get(SalesCase, case_id) if case_id else None
        if case is not None:
            handoff.case_id = case.id
            email_row.case_id = case.id
            email_row.customer_id = case.customer_id
            email_row.contact_id = case.contact_id

        metadata = dict(email_row.bounce_metadata or {})
        detected_by = list(metadata.get("detected_by") or [])
        if "reconcile:permanent-failure-evidence" not in detected_by:
            detected_by.append("reconcile:permanent-failure-evidence")
        metadata.update(
            {
                "bounce_type": BounceType.HARD.value,
                "permanent": True,
                "recipient": recipient,
                "matched_outbox_id": outbox.id,
                "detected_by": detected_by,
            }
        )
        email_row.bounce_type = BounceType.HARD.value
        email_row.bounce_metadata = metadata
        email_row.bounce_handled_at = datetime.now(UTC)
        await _apply_correlated_hard_bounce(
            session,
            email_row=email_row,
            outbox=outbox,
            recipient=recipient,
            case=case,
            audit_event="inbound.bounce_review_auto_resolved",
        )
        handoff.status = "RESOLVED"
        handoff.resolution_note = (
            f"Automatically resolved: {recipient} has a permanent recipient/domain failure"
        )
        await finalize_handoff_agent_run(
            session,
            handoff_id=handoff.id,
            actor="bounce-reconciler",
            outcome="permanent-bounce",
            cancelled=True,
        )
        if handoff.dingtalk_status != "SENT":
            handoff.dingtalk_status = "CANCELLED"
        notify_job = await session.scalar(
            select(Job).where(Job.idempotency_key == f"handoff-notify:{handoff.id}")
        )
        if notify_job is not None and notify_job.status in {JobStatus.PENDING, JobStatus.FAILED}:
            notify_job.status = JobStatus.DONE
            notify_job.last_error = "Cancelled: permanent bounce was handled automatically"
            notify_job.locked_at = None
            notify_job.locked_by = None
            notify_job.updated_at = datetime.now(UTC)
        await session.commit()
        resolved += 1
    return resolved


async def _handle_automated_reply(
    session: AsyncSession,
    *,
    case: SalesCase,
    email_row: EmailMessage,
) -> bool:
    if not email_row.is_automated_reply:
        return False
    if email_row.automated_reply_handled_at is not None:
        return True

    reply_type = email_row.automated_reply_type
    facts = {
        "automated_reply_type": reply_type,
        **(email_row.automated_reply_metadata or {}),
    }
    if reply_type in {
        AutomatedReplyType.OUT_OF_OFFICE.value,
        AutomatedReplyType.GENERIC_AUTOREPLY.value,
        AutomatedReplyType.SYSTEM_NOTIFICATION.value,
    }:
        email_row.automated_reply_handled_at = datetime.now(UTC)
        await audit(
            session,
            "inbound.automated_reply_handled",
            case_id=case.id,
            actor="system",
            data={"email_id": email_row.id, **facts},
        )
        await session.commit()
        return True

    if reply_type in {
        AutomatedReplyType.DEPARTED.value,
        AutomatedReplyType.CONTACT_CHANGE.value,
    }:
        if email_row.automated_reply_metadata.get("personnel_change_handled"):
            # The old endpoint was already retired and a new contact was
            # linked during ingestion; record the personnel change and keep
            # processing the business request (quote / product list / ...).
            await audit(
                session,
                "inbound.personnel_change_recorded",
                case_id=case.id,
                actor="system",
                data={"email_id": email_row.id, **facts},
            )
            return False
        email_row.automated_reply_handled_at = datetime.now(UTC)
        # No reactivation context: conservative fallback keeps the original
        # behavior (retire the current contact and ask a human to verify).
        case.contact.suppressed = True
        summary = "Contact appears to have left the company; verify any replacement contact"
        reason = HandoffReason.PERSONNEL_CHANGE
    else:
        email_row.automated_reply_handled_at = datetime.now(UTC)
        summary = "Automated reply could not be handled safely"
        reason = HandoffReason.AUTOMATED_REPLY_REVIEW
    await audit(
        session,
        "inbound.automated_reply_escalated",
        case_id=case.id,
        actor="system",
        data={"email_id": email_row.id, **facts},
    )
    await create_handoff(
        session,
        case=case,
        reason=reason,
        summary=summary,
        facts=facts,
        source_email_id=email_row.id,
    )
    return True


async def _augment_pending_quote_context(
    session: AsyncSession,
    *,
    case: SalesCase,
    email_row: EmailMessage,
    analysis: InboundAnalysis,
) -> tuple[InboundAnalysis, str | None]:
    """Recover a unique product/quantity from the current thread before asking."""
    if case.product_id is not None or analysis.intent != Intent.QUOTE_REQUEST:
        return analysis, None

    current_message = f"{email_row.subject}\n{email_row.body_text}"
    try:
        full_source = _reply_source(email_row)
        complete_thread = f"{email_row.subject}\n{full_source.body_text}"
    except RuntimeError:
        complete_thread = current_message

    candidate_code = (
        canonical_product_code(analysis.product_code)
        if analysis.product_code
        else None
    )
    candidate_source = "current_analysis" if candidate_code else None
    current_codes = find_product_codes(current_message)
    conflict: str | None = None
    if candidate_code is None:
        if len(current_codes) == 1:
            candidate_code = current_codes[0]
            candidate_source = "current_message"
        elif len(current_codes) > 1:
            conflict = "The current customer message contains multiple product codes"

    prior_inbound: list[EmailMessage] = []
    if candidate_code is None and conflict is None:
        prior_inbound = list(
            (
                await session.scalars(
                    select(EmailMessage)
                    .where(
                        EmailMessage.case_id == case.id,
                        EmailMessage.id != email_row.id,
                        EmailMessage.direction == "INBOUND",
                        EmailMessage.is_bounce.is_(False),
                        EmailMessage.is_automated_reply.is_(False),
                    )
                    .order_by(EmailMessage.received_at.desc(), EmailMessage.id.desc())
                    .limit(20)
                )
            ).all()
        )
        for prior in prior_inbound:
            prior_codes = find_product_codes(f"{prior.subject}\n{prior.body_text}")
            if len(prior_codes) == 1:
                candidate_code = prior_codes[0]
                candidate_source = f"prior_inbound:{prior.id}"
                break
            if len(prior_codes) > 1:
                conflict = f"Prior inbound email {prior.id} contains multiple product codes"
                break

    # The archived MIME keeps the complete quoted conversation and is useful
    # when an older inbound message was not stored as its own row. It is only a
    # fallback: prior customer-authored messages take precedence, so quoted
    # outbound catalogs and quotations cannot overwrite clearer customer
    # evidence.
    if candidate_code is None and conflict is None:
        thread_codes = find_product_codes(complete_thread)
        if len(thread_codes) == 1:
            candidate_code = thread_codes[0]
            candidate_source = "current_complete_thread"
        elif len(thread_codes) > 1:
            conflict = "The quoted email thread contains multiple product codes"

    quantity = analysis.quantity or extract_quantity_kg(current_message)
    if quantity is None:
        if not prior_inbound:
            prior_inbound = list(
                (
                    await session.scalars(
                        select(EmailMessage)
                        .where(
                            EmailMessage.case_id == case.id,
                            EmailMessage.id != email_row.id,
                            EmailMessage.direction == "INBOUND",
                            EmailMessage.is_bounce.is_(False),
                            EmailMessage.is_automated_reply.is_(False),
                        )
                        .order_by(EmailMessage.received_at.desc(), EmailMessage.id.desc())
                        .limit(20)
                    )
                ).all()
            )
        for prior in prior_inbound:
            quantity = extract_quantity_kg(f"{prior.subject}\n{prior.body_text}")
            if quantity is not None:
                break

    missing_fields = list(analysis.missing_fields)
    if quantity is not None:
        missing_fields = [item for item in missing_fields if item != "quantity"]
    updates: dict[str, Any] = {
        "quantity": quantity,
        "numeric_confidence": 1.0 if quantity is not None else analysis.numeric_confidence,
        "missing_fields": missing_fields,
    }
    if conflict is not None:
        return analysis.model_copy(update=updates), conflict
    if candidate_code is None:
        return analysis.model_copy(update=updates), None

    product = await session.scalar(
        select(Product).where(
            Product.code == candidate_code,
            Product.active.is_(True),
        )
    )
    if product is None:
        return (
            analysis.model_copy(update=updates),
            f"Referenced product {candidate_code} is not active in the catalog",
        )
    if case.category_id is not None and product.category_id != case.category_id:
        return (
            analysis.model_copy(update=updates),
            f"Referenced product {candidate_code} conflicts with the case product category",
        )

    case.product_id = product.id
    case.product = product
    if case.category_id is None:
        case.category_id = product.category_id
    if case.stage == CaseStage.FOLLOW_UP:
        case.stage = CaseStage.QUOTING
    updates.update(
        {
            "product_code": product.code,
            "product_confidence": 1.0,
            "missing_fields": [
                item for item in missing_fields if item != "product_code"
            ],
        }
    )
    await audit(
        session,
        "case.product_inferred_from_thread",
        case_id=case.id,
        actor="system",
        data={
            "email_id": email_row.id,
            "product_id": product.id,
            "product_code": product.code,
            "source": candidate_source,
            "recovered_quantity": quantity,
        },
    )
    return analysis.model_copy(update=updates), None


def _quantity_unit_label(unit: str | None) -> str:
    """Human-readable quantity unit for clarification questions."""
    normalized = (unit or "").strip().lower()
    if normalized in {"kg", "kgs", "kilogram", "kilograms"}:
        return "kilograms"
    if normalized in {"piece", "pieces"}:
        return "pieces"
    if normalized in {"unit", "units"}:
        return "units"
    return normalized or "kilograms"


def _pluralized_unit(unit: str | None) -> str:
    """Pluralize the product unit when it is a countable noun."""
    normalized = (unit or "").strip().lower()
    if normalized in {"piece", "unit"}:
        return f"{normalized}s"
    return normalized or "unit"


async def _moq_price_rows(
    session: AsyncSession,
    *,
    codes: list[str],
    currency: str,
) -> list[tuple[Product, PricePolicy]]:
    """Return one active MOQ reference price per product that has a policy.

    Products without a matching policy are simply skipped: the clarification
    still asks for their quantity, and callers never crash when catalog or
    commercial data changes underneath the request. Callers format the rows
    with the wording appropriate for single- or multi-product requests.
    """
    unique_codes = list(dict.fromkeys(code for code in codes if code))
    if not unique_codes:
        return []
    products = (
        (
            await session.execute(
                select(Product).where(
                    _product_lookup_conditions(unique_codes),
                    Product.active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    product_by_key = {
        product_text_key(product.code): product for product in products
    }
    rows: list[tuple[Product, PricePolicy]] = []
    for code in unique_codes:
        product = product_by_key.get(product_text_key(code))
        if product is None:
            continue
        policy = await active_policy(session, product.id, currency)
        if (
            policy is None
            or policy.min_quantity is None
            or policy.min_quantity <= 0
            or policy.standard_price is None
            or policy.standard_price <= 0
        ):
            continue
        rows.append((product, policy))
    return rows


async def _maybe_send_quote_clarification(
    session: AsyncSession,
    *,
    case: SalesCase,
    email_row: EmailMessage,
    analysis: InboundAnalysis,
    analysis_facts: dict[str, Any],
    missing_product_codes: list[str] | None = None,
) -> bool:
    """Ask once for missing product/quantity details instead of handing off."""
    product_known = case.product_id is not None
    quantity_known = analysis.quantity is not None
    needs_product = not product_known and not (missing_product_codes or analysis.product_requests)
    needs_quantity = product_known and not quantity_known
    needs_multi_quantities = bool(missing_product_codes)
    if analysis.intent != Intent.QUOTE_REQUEST or not (
        needs_product or needs_quantity or needs_multi_quantities
    ):
        return False

    previous_clarification = await session.scalar(
        select(Outbox.id).where(
            Outbox.case_id == case.id,
            Outbox.message_kind == "QUOTE_CLARIFICATION",
            Outbox.status != DeliveryStatus.CANCELLED,
        )
    )
    if previous_clarification is not None:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.HUMAN_CONTROL,
            summary="Product is still unclear after one automated clarification",
            facts={
                **analysis_facts,
                "product_pending": True,
                "previous_clarification_outbox_id": previous_clarification,
            },
            source_email_id=email_row.id,
        )
        return True

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
            product_known=True,
            prebook_requested=analysis.prebook_requested,
            packaging_requested=analysis.packaging_requested,
            delivery_requested=analysis.shipping_requested,
        ),
        intent_threshold=get_settings().intent_confidence_threshold,
        product_threshold=get_settings().product_confidence_threshold,
        numeric_threshold=get_settings().numeric_confidence_threshold,
    )
    if not send_decision.allow_send:
        await create_handoff(
            session,
            case=case,
            reason=send_decision.reason or HandoffReason.HUMAN_CONTROL,
            summary="Product clarification requires human review",
            facts={**analysis_facts, "product_pending": True},
            source_email_id=email_row.id,
        )
        return True

    settings = get_settings()
    bundle = load_content(settings.content_dir)
    greeting = f"Dear {case.contact.name.strip() or 'Customer'},"
    if needs_multi_quantities:
        opening = "Thank you for your quotation request."
        names = ", ".join(missing_product_codes)
        moq_rows = await _moq_price_rows(
            session,
            codes=missing_product_codes,
            currency=case.currency,
        )
        reference_lines = []
        for product, policy in moq_rows:
            raw_unit = (product.unit or "unit").strip() or "unit"
            reference_lines.append(
                f"{product.code}: MOQ {policy.min_quantity} "
                f"{_pluralized_unit(raw_unit)} at {case.currency} "
                f"{policy.standard_price:.2f}"
            )
        question = (
            f"Could you please let us know the quantity you require for {names}? "
            "Better pricing is available for larger volumes."
            if moq_rows
            else f"Could you please confirm the required quantity for {names}?"
        )
    elif needs_quantity:
        opening = (
            f"Thank you for your quotation request for {case.product.name}."
            if case.product is not None
            else "Thank you for your quotation request."
        )
        reference_lines = []
        if case.product is not None:
            moq_rows = await _moq_price_rows(
                session,
                codes=[case.product.code],
                currency=case.currency,
            )
            for product, policy in moq_rows:
                raw_unit = (product.unit or "unit").strip() or "unit"
                reference_lines.append(
                    f"Our minimum order quantity is {policy.min_quantity} "
                    f"{_pluralized_unit(raw_unit)} at {case.currency} "
                    f"{policy.standard_price:.2f} per {raw_unit}."
                )
        if reference_lines:
            question = (
                "Could you please let us know the quantity you require? "
                "Better pricing is available for larger volumes."
            )
        else:
            question = (
                f"Could you please confirm the required quantity in "
                f"{_quantity_unit_label(case.product.unit if case.product is not None else None)}?"
            )
    else:
        opening = (
            f"Thank you for your quotation request for {analysis.quantity} kg."
            if analysis.quantity is not None
            else "Thank you for your quotation request."
        )
        reference_lines = []
        question = (
            "Could you please confirm the product name or Lanya product code, "
            "together with the required quantity?"
        )
    closing = "Once confirmed, we will confirm availability and prepare your quotation accordingly."
    business_lines = [greeting, "", opening]
    for line in reference_lines:
        business_lines.extend(["", line])
    business_lines.extend(["", question, closing])
    text = "\n".join([*business_lines, "", bundle.signature_text.strip()])
    html_body = (
        "<p>"
        + "</p><p>".join(
            html.escape(line) if line else "&nbsp;" for line in business_lines
        )
        + "</p>"
        + bundle.signature_html
    )
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
            summary=f"Product clarification rendering failed: {type(exc).__name__}",
            facts={**analysis_facts, "product_pending": True},
            source_email_id=email_row.id,
        )
        return True

    if not settings.quote_auto_send_enabled:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.QUOTE_REVIEW,
            summary="Quotation clarification draft prepared; human approval is required",
            facts={
                **analysis_facts,
                "product_pending": case.product is None,
                "ai_draft_preview": {
                    "subject": f"Re: {email_row.subject}",
                    "body_text": "\n".join(business_lines),
                    "generated_at": datetime.now(UTC).isoformat(),
                    "provider": "deterministic-quote-clarification",
                    "model": "current-catalog-moq-v1",
                    "rag_matches": [],
                },
            },
            source_email_id=email_row.id,
        )
        return True

    outbox = await stage_outbox(
        session,
        case=case,
        message_kind="QUOTE_CLARIFICATION",
        subject=f"Re: {email_row.subject}",
        text_body=text,
        html_body=html_body,
        business_key=f"inbound-reply:{email_row.id}:clarification",
        in_reply_to=email_row.message_id,
        references=_reply_references(email_row),
        inline_images=source.inline_images,
    )
    if outbox is None:
        await session.rollback()
        return True
    if outbox is not None:
        await audit(
            session,
            "inbound.quote_clarification_queued",
            case_id=case.id,
            actor="system",
            data={
                "email_id": email_row.id,
                "outbox_id": outbox.id,
                "requested_quantity": analysis.quantity,
            },
        )
    return True


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
    requests = [
        line
        for line in analysis.product_requests
        if line.product_code
    ]
    if analysis.intent != Intent.QUOTE_REQUEST or len(requests) < 2:
        return False
    if case.product_id is not None:
        return False

    normalized_codes = list(
        dict.fromkeys(product_text_key(line.product_code) for line in requests)
    )
    products = (
        (
            await session.execute(
                select(Product).where(
                    _product_lookup_conditions(
                        list(
                            dict.fromkeys(
                                line.product_code for line in requests
                            )
                        )
                    ),
                    Product.active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    product_by_key = {
        product_text_key(product.code): product for product in products
    }
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
            reason = (
                HandoffReason.INVENTORY_UNAVAILABLE
                if context.reason.startswith("INVENTORY")
                else HandoffReason.NONSTANDARD
            )
            await create_handoff(
                session,
                case=case,
                reason=reason,
                summary=(
                    f"Current commercial data cannot quote {product.code}: "
                    f"{context.reason}"
                ),
                facts={
                    **analysis_facts,
                    "commercial_cycle_id": context.cycle.id,
                    "product_pending": True,
                    "requested_quantity": quantity,
                },
                source_email_id=email_row.id,
            )
            return True
        policy_row = (
            context.policy
            if context is not None
            else await active_policy(session, product.id, case.currency)
        )
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
            reason = (
                HandoffReason.BELOW_FLOOR
                if price_decision.reason and "floor" in price_decision.reason
                else HandoffReason.NONSTANDARD
            )
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
    incoterm_standard = (
        analysis.incoterm is None
        or analysis.incoterm.upper() == first_policy.standard_incoterm.upper()
    )
    payment_standard = (
        analysis.payment_term is None
        or analysis.payment_term.casefold()
        == first_policy.standard_payment_term.casefold()
    )
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
                "contact_name": case.contact.name,
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

    availability_note = (
        "Subject to confirmation at order placement"
        if settings.quote_ignore_inventory
        else "Ready stock"
    )
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
    draft_body = (
        text[: -len(signature_text)].rstrip()
        if signature_text and text.endswith(signature_text)
        else text.rstrip()
    )
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
        select(Quote)
        .where(Quote.case_id == case.id)
        .order_by(Quote.round_number.desc(), Quote.id.desc())
        .limit(1)
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
                            "commercial_cycle_id": (
                                context.cycle.id if context is not None else None
                            ),
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
            commercial_cycle_id=(
                context.cycle.id if context is not None else None
            ),
            round_number=round_number,
            unit_price=unit_price,
            currency=policy.currency,
            quantity=quantity,
            incoterm=policy.standard_incoterm,
            payment_term=policy.standard_payment_term,
            valid_until=valid_until,
            pricing_snapshot={
                "hard_minimum": str(
                    _pricing_policy(policy).absolute_floor
                ),
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


async def _company_research_catalog(
    session: AsyncSession,
) -> tuple[dict[str, ProductCategory], list[dict[str, Any]], str]:
    categories = (
        (
            await session.execute(
                select(ProductCategory)
                .where(ProductCategory.active.is_(True))
                .order_by(ProductCategory.sort_order, ProductCategory.id)
            )
        )
        .scalars()
        .all()
    )
    products = (
        (
            await session.execute(
                select(Product)
                .where(
                    Product.active.is_(True),
                    Product.category_id.is_not(None),
                )
                .order_by(Product.category_id, Product.sort_order, Product.id)
            )
        )
        .scalars()
        .all()
    )
    examples_by_category: dict[int, list[str]] = {}
    for product in products:
        if product.category_id is None:
            continue
        examples = examples_by_category.setdefault(product.category_id, [])
        for value in (product.series, product.name):
            normalized = str(value or "").strip()
            if normalized and normalized not in examples and len(examples) < 12:
                examples.append(normalized)
    payload = [
        {
            "key": category.key,
            "name": category.name,
            "examples": examples_by_category.get(category.id, []),
        }
        for category in categories
    ]
    signature = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    return {category.key: category for category in categories}, payload, signature


async def _maybe_research_and_send_product_list(
    session: AsyncSession,
    *,
    case: SalesCase,
    email_row: EmailMessage,
    analysis: InboundAnalysis,
    analysis_facts: dict[str, Any],
    existing_handoff: Handoff | None = None,
) -> bool:
    """Use cited company research only for an explicit, category-less list request."""

    async def route_handoff(
        reason: HandoffReason,
        summary: str,
        facts: dict[str, Any],
    ) -> Handoff:
        if existing_handoff is None:
            return await create_handoff(
                session,
                case=case,
                reason=reason,
                summary=summary,
                facts=facts,
                source_email_id=email_row.id,
            )
        existing_handoff.reason_code = reason.value
        existing_handoff.summary = summary
        existing_handoff.extracted_facts = facts
        if case.status == CaseStatus.ACTIVE:
            case.status = CaseStatus.WAITING_HUMAN
        await audit(
            session,
            "handoff.reclassified",
            case_id=case.id,
            actor="company-research-backfill",
            data={
                "handoff_id": existing_handoff.id,
                "reason": reason.value,
                "source_email_id": email_row.id,
            },
        )
        await session.commit()
        return existing_handoff

    if analysis.intent != Intent.PRODUCT_LIST_REQUEST or not explicit_product_list_requested(
        f"{email_row.subject}\n{email_row.body_text}"
    ):
        return False
    settings = get_settings()
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
            product_known=analysis.product_code is None,
            prebook_requested=analysis.prebook_requested,
            packaging_requested=analysis.packaging_requested,
            delivery_requested=analysis.shipping_requested,
        ),
        intent_threshold=settings.intent_confidence_threshold,
        product_threshold=settings.product_confidence_threshold,
        numeric_threshold=settings.numeric_confidence_threshold,
    )
    if not send_decision.allow_send:
        await route_handoff(
            send_decision.reason or HandoffReason.LOW_CONFIDENCE,
            f"Inbound {analysis.intent.value} requires human review",
            analysis_facts,
        )
        return True
    if not settings.company_research_enabled:
        await route_handoff(
            HandoffReason.PRODUCT_CATEGORY_REVIEW,
            "Product-list request has no unique CRM/Excel category; company research is disabled",
            {
                **analysis_facts,
                "product_pending": True,
                "company_research": {"status": "DISABLED"},
            },
        )
        return True

    categories_by_key, category_payload, catalog_signature = await _company_research_catalog(
        session
    )
    if not categories_by_key:
        await route_handoff(
            HandoffReason.PRODUCT_CATEGORY_REVIEW,
            "No active product category is available for a product-list reply",
            {**analysis_facts, "product_pending": True},
        )
        return True
    company_domain = _nonfree_email_domain(case.contact.email)
    observed_at = datetime.now(UTC)
    cached = _cached_company_research(
        case.customer,
        company_domain=company_domain,
        catalog_signature=catalog_signature,
        now=observed_at,
    )
    if cached is None:
        ai = AIClient(settings)
        try:
            decision, sources, metadata = await ai.research_company_category(
                company_name=case.customer.company_name,
                company_domain=company_domain,
                categories=category_payload,
            )
        except Exception as exc:
            session.add(
                AIInvocation(
                    case_id=case.id,
                    provider=settings.ai_provider,
                    model=settings.anthropic_model,
                    purpose="company_category_research",
                    request_hash=hashlib.sha256(
                        f"{case.customer.id}:{catalog_signature}".encode()
                    ).hexdigest(),
                    parsed_output=None,
                    success=False,
                    error_type=type(exc).__name__,
                    input_tokens=None,
                    output_tokens=None,
                )
            )
            await audit(
                session,
                "company_research.failed",
                case_id=case.id,
                actor="system",
                data={
                    "email_id": email_row.id,
                    "customer_id": case.customer_id,
                    "error_type": type(exc).__name__,
                },
            )
            await route_handoff(
                HandoffReason.PRODUCT_CATEGORY_REVIEW,
                "Company research failed; product category requires human confirmation",
                {
                    **analysis_facts,
                    "product_pending": True,
                    "company_research": {
                        "status": "FAILED",
                        "error_type": type(exc).__name__,
                    },
                },
            )
            return True
        research_output = {
            "decision": decision.model_dump(mode="json"),
            "sources": [source.model_dump(mode="json") for source in sources],
        }
        session.add(
            AIInvocation(
                case_id=case.id,
                provider=str(metadata.get("provider") or settings.ai_provider),
                model=str(metadata.get("model") or settings.anthropic_model),
                purpose="company_category_research",
                request_hash=str(metadata.get("request_hash") or catalog_signature),
                parsed_output=research_output,
                success=True,
                input_tokens=metadata.get("input_tokens"),
                output_tokens=metadata.get("output_tokens"),
            )
        )
        _store_company_research_cache(
            case.customer,
            company_domain=company_domain,
            catalog_signature=catalog_signature,
            decision=decision,
            sources=sources,
            metadata=metadata,
            settings=settings,
            now=observed_at,
        )
        cache_hit = False
    else:
        decision, sources, metadata = cached
        cache_hit = True

    gate = _company_research_gate(
        decision,
        sources,
        company_domain=company_domain,
        active_category_keys=set(categories_by_key),
        settings=settings,
    )
    research_facts = {
        "status": "COMPLETED",
        "cache_hit": cache_hit,
        "company_name": case.customer.company_name,
        "company_domain": company_domain,
        "decision": decision.model_dump(mode="json"),
        "sources": [source.model_dump(mode="json") for source in sources],
        "gate": gate,
        "provider": metadata.get("provider"),
        "model": metadata.get("model"),
    }
    await audit(
        session,
        "company_research.completed",
        case_id=case.id,
        actor="system",
        data={
            "email_id": email_row.id,
            "customer_id": case.customer_id,
            "cache_hit": cache_hit,
            "recommended_category_key": decision.recommended_category_key,
            "eligible": gate["eligible"],
            "gate_reasons": gate["reasons"],
            "source_domains": gate["source_domains"],
        },
    )
    if not gate["eligible"] or not settings.company_research_auto_send_enabled:
        recommended = categories_by_key.get(decision.recommended_category_key or "")
        summary = (
            f"Company research suggests {recommended.name}; human confirmation is required"
            if recommended is not None
            else "Company research could not safely determine a product category"
        )
        if gate["eligible"] and not settings.company_research_auto_send_enabled:
            summary += " (observation mode)"
        await route_handoff(
            HandoffReason.PRODUCT_CATEGORY_REVIEW,
            summary,
            {
                **analysis_facts,
                "product_pending": True,
                "company_research": research_facts,
            },
        )
        return True

    category = categories_by_key[decision.recommended_category_key or ""]
    case.category_id = category.id
    case.category = category
    await audit(
        session,
        "company_research.category_selected",
        case_id=case.id,
        actor="system",
        data={
            "email_id": email_row.id,
            "category_id": category.id,
            "category_key": category.key,
            "research": research_facts,
        },
    )
    return await _maybe_send_product_list(
        session,
        case=case,
        email_row=email_row,
        analysis=analysis,
        analysis_facts={
            **analysis_facts,
            "company_research": research_facts,
        },
    )


def _product_list_outbound_attachments(
    *,
    category: ProductCategory,
    products: list[Product],
    request_text: str,
) -> tuple[tuple[OutboundAttachment, ...], str | None]:
    file_format = requested_product_list_file_format(request_text)
    if file_format is None:
        return (), None
    catalog_file = build_product_list_attachment(
        category=category,
        products=products,
        file_format=file_format,
    )
    return (
        (
            OutboundAttachment(
                filename=catalog_file.filename,
                content_type=catalog_file.content_type,
                payload=catalog_file.payload,
            ),
        ),
        catalog_file.filename,
    )


def _coa_reply_draft(
    *,
    contact_name: str,
    original_subject: str,
    product_name: str,
) -> tuple[str, str]:
    subject = (
        f"Re: {original_subject.strip()}"
        if original_subject.strip() and not original_subject.strip().casefold().startswith("re:")
        else (original_subject.strip() or f"COA for {product_name}")
    )
    greeting_name = contact_name.strip() or "Customer"
    body = (
        f"Dear {greeting_name},\n\n"
        f"Please find attached the Certificate of Analysis (COA) for {product_name}."
    )
    return subject[:998], body


def _prepare_coa_attachments(
    *,
    settings: Settings,
    product_codes: list[str],
) -> list[dict[str, Any]]:
    if not settings.coa_catalog_enabled:
        raise ValueError("approved COA catalog is disabled")
    catalog = COACatalog(settings.coa_catalog_path)
    prepared: list[dict[str, Any]] = []
    for code in product_codes:
        result = catalog.find(code)
        if (
            result.status is not COAFindStatus.FOUND
            or len(result.matches) != 1
            or not result.auto_send_eligible
        ):
            raise ValueError(f"no unique approved standard English COA for {code}")
        entry = dict(result.matches[0])
        relative_path = str(entry["path"])
        prepared.append(
            {
                "product_code": code,
                "path": relative_path,
                "filename": relative_path.replace("\\", "/").rsplit("/", 1)[-1],
                "sha256": str(entry["sha256"]),
                "size": int(entry["size"]),
                "match_basis": result.match_basis,
            }
        )
    return prepared


def _read_prepared_coa_attachments(
    *,
    settings: Settings,
    prepared_coas: list[dict[str, Any]],
) -> tuple[OutboundAttachment, ...]:
    if not prepared_coas:
        return ()
    catalog = COACatalog(settings.coa_catalog_path)
    attachments: list[OutboundAttachment] = []
    for prepared in prepared_coas:
        entry = catalog.entry_for_path(str(prepared.get("path") or ""))
        if str(entry.get("sha256") or "") != str(prepared.get("sha256") or ""):
            raise ValueError("prepared COA no longer matches the approved catalog")
        attachments.append(
            OutboundAttachment(
                filename=str(prepared.get("filename") or "COA.pdf"),
                content_type="application/pdf",
                payload=catalog.read_verified_attachment(entry),
            )
        )
    return tuple(attachments)


async def _maybe_handle_coa_request(
    session: AsyncSession,
    *,
    case: SalesCase,
    email_row: EmailMessage,
    analysis: InboundAnalysis,
    analysis_facts: dict[str, Any],
) -> bool:
    """Resolve a COA request against the approved catalog or pause for review."""

    if analysis.intent != Intent.COA_REQUEST:
        return False
    settings = get_settings()
    explicit_query = (analysis.requested_product_name or analysis.product_code or "").strip()
    query = explicit_query
    if not query and case.product is not None:
        query = (case.product.code or case.product.name).strip()
    cas_number = (analysis.requested_cas_number or "").strip() or None
    lookup_facts: dict[str, Any] = {
        **analysis_facts,
        "coa_query": query,
        "coa_cas_number": cas_number,
        "coa_catalog_path": str(settings.coa_catalog_path),
    }
    if not settings.coa_catalog_enabled:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.COA_REVIEW,
            summary="COA request is waiting because the approved COA catalog is disabled",
            facts=lookup_facts,
            source_email_id=email_row.id,
        )
        return True
    if not query and not cas_number:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.COA_REVIEW,
            summary="Customer requested a COA but the product or CAS number is missing",
            facts={**lookup_facts, "coa_help_needed": "product name, product code, or CAS number"},
            source_email_id=email_row.id,
        )
        return True
    try:
        catalog = COACatalog(settings.coa_catalog_path)
        result = catalog.find(query, cas_number=cas_number)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.COA_REVIEW,
            summary="Approved COA catalog is unavailable and must be rebuilt",
            facts={**lookup_facts, "coa_catalog_error": type(exc).__name__},
            source_email_id=email_row.id,
        )
        return True

    result_facts = {**lookup_facts, "coa_lookup": result.as_dict()}
    if (
        result.status is not COAFindStatus.FOUND
        or len(result.matches) != 1
        or not result.auto_send_eligible
    ):
        reason = (
            "COA match is ambiguous and requires human selection"
            if result.status is COAFindStatus.AMBIGUOUS
            else "No unique approved standard English COA was found"
        )
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.COA_REVIEW,
            summary=reason,
            facts={
                **result_facts,
                "coa_help_needed": (
                    "confirm the correct suffix-free standard English COA"
                    if result.status is COAFindStatus.AMBIGUOUS
                    else "provide or identify a suffix-free standard English COA"
                ),
            },
            source_email_id=email_row.id,
        )
        return True

    entry = dict(result.matches[0])
    product_name = str(entry.get("product_code") or entry.get("product_name") or query)
    subject, draft_body = _coa_reply_draft(
        contact_name=case.contact.name,
        original_subject=email_row.subject,
        product_name=product_name,
    )
    relative_path = str(entry["path"])
    prepared = {
        "path": relative_path,
        "filename": relative_path.replace("\\", "/").rsplit("/", 1)[-1],
        "sha256": str(entry["sha256"]),
        "size": int(entry["size"]),
        "product_name": product_name,
        "match_basis": result.match_basis,
        "catalog_schema": catalog.schema_version,
    }
    draft_preview = {
        "subject": subject,
        "body_text": draft_body,
        "generated_at": datetime.now(UTC).isoformat(),
        "provider": "deterministic-coa",
        "model": catalog.schema_version,
        "rag_matches": [],
    }
    prepared_facts = {
        **result_facts,
        "prepared_coa": prepared,
        "ai_draft_preview": draft_preview,
    }

    if not settings.coa_auto_send_enabled:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.COA_REVIEW,
            summary=f"COA draft prepared for {product_name}; human approval is required",
            facts=prepared_facts,
            source_email_id=email_row.id,
            update_existing=True,
        )
        return True

    send_decision = evaluate_send_policy(
        SendContext(
            intent=analysis.intent,
            stage=case.stage,
            status=case.status,
            intent_confidence=analysis.intent_confidence,
            product_confidence=analysis.product_confidence,
            numeric_confidence=1.0,
            auto_send_allowed=case.customer.auto_send_allowed,
            contact_suppressed=case.contact.suppressed,
            do_not_contact=case.customer.do_not_contact,
            has_risky_attachment=analysis.risky_attachment,
            product_known=True,
        ),
        intent_threshold=settings.intent_confidence_threshold,
        product_threshold=settings.product_confidence_threshold,
        numeric_threshold=settings.numeric_confidence_threshold,
    )
    if not send_decision.allow_send:
        await create_handoff(
            session,
            case=case,
            reason=send_decision.reason or HandoffReason.COA_REVIEW,
            summary="COA was found, but current send policy requires human approval",
            facts=prepared_facts,
            source_email_id=email_row.id,
        )
        return True

    try:
        payload = catalog.read_verified_attachment(entry)
    except (OSError, ValueError) as exc:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.COA_REVIEW,
            summary="Selected COA changed or became unavailable; rescan and review are required",
            facts={**prepared_facts, "coa_attachment_error": type(exc).__name__},
            source_email_id=email_row.id,
        )
        return True

    bundle = load_content(settings.content_dir)
    signed_text = "\n".join([draft_body, "", bundle.signature_text.strip()])
    signed_html = "".join(
        f"<p>{html.escape(line) if line else '&nbsp;'}</p>"
        for line in draft_body.splitlines()
    ) + bundle.signature_html
    source = _reply_source(email_row)
    signed_text, signed_html = append_quoted_reply(
        signed_text,
        signed_html,
        from_address=email_row.from_address,
        source_body=source.body_text,
        source_html=source.body_html,
        occurred_at=email_row.received_at,
    )
    outbox = await stage_outbox(
        session,
        case=case,
        message_kind="COA",
        subject=subject,
        text_body=signed_text,
        html_body=signed_html,
        business_key=f"inbound-coa:{email_row.id}",
        in_reply_to=email_row.message_id,
        references=_reply_references(email_row),
        inline_images=source.inline_images,
        attachments=(
            OutboundAttachment(
                filename=prepared["filename"],
                content_type="application/pdf",
                payload=payload,
            ),
        ),
    )
    if outbox is None:
        await session.rollback()
        return True
    await audit(
        session,
        "inbound.coa_queued",
        case_id=case.id,
        actor="system",
        data={
            "email_id": email_row.id,
            "outbox_id": outbox.id,
            "coa_path": relative_path,
            "coa_sha256": prepared["sha256"],
            "match_basis": result.match_basis,
        },
    )
    return True


async def queue_prepared_coa_reply(
    session: AsyncSession,
    *,
    handoff_id: int,
    subject: str,
    body_text: str,
    actor: str,
    note: str = "",
    resume_automation: bool = False,
) -> Outbox:
    """Approve a prepared COA draft while rechecking the exact NAS file hash."""

    handoff = await session.get(Handoff, handoff_id)
    if handoff is None:
        raise ValueError("handoff not found")
    prepared = (handoff.extracted_facts or {}).get("prepared_coa")
    if handoff.reason_code != HandoffReason.COA_REVIEW.value or not isinstance(prepared, dict):
        raise ValueError("handoff has no prepared COA attachment")
    settings = get_settings()
    try:
        catalog = COACatalog(settings.coa_catalog_path)
        entry = catalog.entry_for_path(str(prepared.get("path") or ""))
        if str(entry.get("sha256") or "") != str(prepared.get("sha256") or ""):
            raise ValueError("prepared COA no longer matches the approved catalog")
        payload = catalog.read_verified_attachment(entry)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("approved COA catalog or attachment is unavailable") from exc
    return await queue_human_reply(
        session,
        handoff_id=handoff_id,
        subject=subject,
        body_text=body_text,
        actor=actor,
        note=note,
        resume_automation=resume_automation,
        attachments=(
            OutboundAttachment(
                filename=str(prepared.get("filename") or "COA.pdf"),
                content_type="application/pdf",
                payload=payload,
            ),
        ),
    )


async def _validated_prepared_product_list(
    session: AsyncSession,
    *,
    handoff_id: int,
) -> tuple[Handoff, dict[str, Any], ProductCategory, list[Product]]:
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None:
        raise ValueError("handoff not found")
    prepared = (handoff.extracted_facts or {}).get("prepared_product_list")
    if (
        handoff.reason_code != HandoffReason.PRODUCT_LIST_REVIEW.value
        or not isinstance(prepared, dict)
    ):
        raise ValueError("handoff has no prepared product-list draft")
    if prepared.get("scope") == "all":
        category = _all_products_catalog_category()
        products = list(
            (
                await session.scalars(
                    select(Product)
                    .join(ProductCategory, Product.category_id == ProductCategory.id)
                    .where(Product.active.is_(True), ProductCategory.active.is_(True))
                    .order_by(ProductCategory.sort_order, Product.sort_order, Product.id)
                )
            ).all()
        )
    else:
        category_id = int(prepared.get("category_id") or 0)
        loaded_category = await session.get(ProductCategory, category_id)
        if loaded_category is None or not loaded_category.active:
            raise ValueError("prepared product category is missing or inactive")
        category = loaded_category
        products = list(
            (
                await session.scalars(
                    select(Product)
                    .where(
                        Product.category_id == category.id,
                        Product.active.is_(True),
                    )
                    .order_by(Product.sort_order, Product.id)
                )
            ).all()
        )
    expected_ids = [int(value) for value in prepared.get("product_ids") or []]
    expected_codes = [str(value) for value in prepared.get("product_codes") or []]
    if [product.id for product in products] != expected_ids or [
        product.code for product in products
    ] != expected_codes:
        raise ValueError("active product list changed after draft creation; regenerate the draft")
    return handoff, prepared, category, products


async def product_list_missing_business_facts(
    session: AsyncSession,
    *,
    handoff: Handoff,
    source_email: EmailMessage | None,
) -> list[str]:
    prepared = (handoff.extracted_facts or {}).get("prepared_product_list")
    if not isinstance(prepared, dict):
        return []
    missing = {
        str(value)
        for value in (prepared.get("missing_business_facts") or [])
        if str(value) != "payment_terms"
    }
    if source_email is not None and _payment_details_requested(
        f"{source_email.subject}\n{source_email.body_text}"
    ):
        sales_case = (
            await session.get(SalesCase, handoff.case_id)
            if handoff.case_id is not None
            else None
        )
        if sales_case is None:
            missing.add("payment_terms")
        else:
            current = await _customer_payment_term(
                session,
                customer_id=sales_case.customer_id,
            )
            prepared_quote_id = prepared.get("payment_term_quote_id")
            if (
                str(prepared.get("payment_term") or "").strip().casefold()
                != current.term.casefold()
                or str(prepared.get("payment_term_source") or "") != current.source
                or (
                    int(prepared_quote_id) if prepared_quote_id is not None else None
                )
                != current.quote_id
            ):
                missing.add("payment_terms")
    return sorted(missing)


async def prepared_product_list_attachment(
    session: AsyncSession,
    *,
    handoff_id: int,
) -> OutboundAttachment:
    """Build a review-only catalog download without creating delivery work."""

    _, prepared, category, products = await _validated_prepared_product_list(
        session,
        handoff_id=handoff_id,
    )
    file_format = prepared.get("file_format")
    if file_format not in {"xlsx", "csv"}:
        raise ValueError("prepared product list does not have a downloadable attachment")
    catalog_file = build_product_list_attachment(
        category=category,
        products=products,
        file_format=file_format,
    )
    return OutboundAttachment(
        filename=catalog_file.filename,
        content_type=catalog_file.content_type,
        payload=catalog_file.payload,
    )


async def queue_prepared_product_list_reply(
    session: AsyncSession,
    *,
    handoff_id: int,
    subject: str,
    body_text: str,
    actor: str,
    note: str = "",
    resume_automation: bool = False,
) -> Outbox:
    """Approve a catalog draft after confirming its active product snapshot."""

    handoff, prepared, category, products = await _validated_prepared_product_list(
        session,
        handoff_id=handoff_id,
    )
    source_email = (
        await session.get(EmailMessage, handoff.source_email_id)
        if handoff.source_email_id is not None
        else None
    )
    missing_business_facts = await product_list_missing_business_facts(
        session,
        handoff=handoff,
        source_email=source_email,
    )
    if missing_business_facts:
        raise ValueError(
            "prepared product-list reply is incomplete; missing approved business facts: "
            + ", ".join(str(value) for value in missing_business_facts)
        )
    file_format = prepared.get("file_format")
    attachments: tuple[OutboundAttachment, ...] = ()
    if file_format is not None:
        if file_format not in {"xlsx", "csv"}:
            raise ValueError("prepared product-list attachment format is invalid")
        catalog_file = build_product_list_attachment(
            category=category,
            products=products,
            file_format=file_format,
        )
        attachments = (
            OutboundAttachment(
                filename=catalog_file.filename,
                content_type=catalog_file.content_type,
                payload=catalog_file.payload,
            ),
        )
    return await queue_human_reply(
        session,
        handoff_id=handoff_id,
        subject=subject,
        body_text=body_text,
        actor=actor,
        note=note,
        resume_automation=resume_automation,
        attachments=attachments,
    )


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

    existing = await session.scalar(
        select(Outbox).where(Outbox.approval_handoff_id == handoff_id)
    )
    if existing is not None:
        return existing
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None:
        raise ValueError("handoff not found")
    prepared = (handoff.extracted_facts or {}).get("prepared_quote")
    if handoff.reason_code != HandoffReason.QUOTE_REVIEW.value or not isinstance(
        prepared, dict
    ):
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
        select(Quote)
        .where(Quote.case_id == sales_case.id)
        .order_by(Quote.round_number.desc(), Quote.id.desc())
        .limit(1)
    )
    actual_round = latest_quote.round_number + 1 if latest_quote is not None else 0
    if actual_round != expected_round:
        raise ValueError("quotation history changed after draft creation; regenerate the quote")
    valid_until = date.fromisoformat(str(prepared.get("valid_until") or ""))
    if valid_until < datetime.now(UTC).astimezone(
        ZoneInfo(get_settings().business_timezone)
    ).date():
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

    existing = await session.scalar(
        select(Outbox).where(Outbox.approval_handoff_id == handoff_id)
    )
    if existing is not None:
        return existing
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None or handoff.case_id is None:
        raise ValueError("handoff or quotation case not found")
    prepared = (handoff.extracted_facts or {}).get("prepared_multi_quote")
    if handoff.reason_code != HandoffReason.QUOTE_REVIEW.value or not isinstance(
        prepared, dict
    ):
        raise ValueError("handoff has no prepared multi-product quotation")
    sales_case = await session.get(SalesCase, handoff.case_id)
    if sales_case is None:
        raise ValueError("quotation case not found")
    expected_round = int(prepared.get("round_number") or 0)
    latest_quote = await session.scalar(
        select(Quote)
        .where(Quote.case_id == sales_case.id)
        .order_by(Quote.round_number.desc(), Quote.id.desc())
        .limit(1)
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
                or (
                    not settings.quote_ignore_inventory
                    and cycle.inventory_status != "CONFIRMED"
                )
                or policy.commercial_cycle_id != cycle.id
            ):
                raise ValueError("commercial confirmation changed for a quotation line")
        quantity = int(raw_line.get("quantity") or 0)
        decision = initial_quote(_pricing_policy(policy), quantity)
        if (
            not decision.approved
            or decision.unit_price is None
            or decision.unit_price != Decimal(str(raw_line.get("unit_price")))
        ):
            raise ValueError("a prepared line no longer matches current pricing rules")
        valid_until = date.fromisoformat(str(raw_line.get("valid_until") or ""))
        if valid_until < datetime.now(UTC).astimezone(
            ZoneInfo(settings.business_timezone)
        ).date():
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


def _all_products_catalog_category() -> ProductCategory:
    return ProductCategory(
        key="all_products",
        name="All Products",
        name_zh=None,
        active=True,
        sort_order=0,
    )


async def _maybe_send_general_product_list(
    session: AsyncSession,
    *,
    case: SalesCase,
    email_row: EmailMessage,
    analysis: InboundAnalysis,
    analysis_facts: dict[str, Any],
) -> bool:
    """Prepare the dynamic company-wide English catalog for a generic request."""

    request_text = f"{email_row.subject}\n{email_row.body_text}"
    if not generic_product_list_requested(request_text):
        return False
    products = list(
        (
            await session.scalars(
                select(Product)
                .join(ProductCategory, Product.category_id == ProductCategory.id)
                .where(Product.active.is_(True), ProductCategory.active.is_(True))
                .order_by(ProductCategory.sort_order, Product.sort_order, Product.id)
            )
        ).all()
    )
    if not products:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.PRODUCT_LIST_REVIEW,
            summary="Generic product-list request has no active approved catalog rows",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return True
    settings = get_settings()
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
            product_known=True,
        ),
        intent_threshold=settings.intent_confidence_threshold,
        product_threshold=settings.product_confidence_threshold,
        numeric_threshold=settings.numeric_confidence_threshold,
    )
    if not send_decision.allow_send:
        await create_handoff(
            session,
            case=case,
            reason=send_decision.reason or HandoffReason.PRODUCT_LIST_REVIEW,
            summary="Generic product-list request requires human review",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return True
    category = _all_products_catalog_category()
    catalog_file = build_product_list_attachment(
        category=category,
        products=products,
        file_format="xlsx",
    )
    contact_name = case.contact.name.strip() or "Customer"
    draft_body = (
        f"Dear {contact_name},\n\n"
        "Please find attached our current English product catalogue. It includes the approved "
        "product codes, product names, CAS numbers, and available content specifications.\n\n"
        "Please let us know the products and quantities you require so we can confirm current "
        "availability and pricing."
    )
    payment_requested = _payment_details_requested(request_text)
    payment = (
        await _customer_payment_term(session, customer_id=case.customer_id)
        if payment_requested
        else None
    )
    payment_term = payment.term if payment is not None else None
    if payment is not None:
        draft_body = f"{draft_body}\n\n{_payment_term_sentence(payment)}"
    subject = f"Re: {email_row.subject}" if email_row.subject.strip() else "Lanya Chem product catalogue"
    prepared = {
        "scope": "all",
        "category_id": None,
        "category_key": "all_products",
        "category_name": "All Products",
        "product_ids": [product.id for product in products],
        "product_codes": [product.code for product in products],
        "product_count": len(products),
        "category_breakdown": await _catalog_category_breakdown(session, products),
        "file_format": "xlsx",
        "attachment_filename": catalog_file.filename,
        "payment_requested": payment_requested,
        "payment_term": payment_term,
        "payment_term_source": payment.source if payment is not None else None,
        "payment_term_quote_id": payment.quote_id if payment is not None else None,
        "missing_business_facts": [],
    }
    if not settings.product_list_auto_send_enabled:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.PRODUCT_LIST_REVIEW,
            summary="Company-wide product catalog draft prepared; human approval is required",
            facts={
                **analysis_facts,
                "prepared_product_list": prepared,
                "ai_draft_preview": {
                    "subject": subject,
                    "body_text": draft_body,
                    "generated_at": datetime.now(UTC).isoformat(),
                    "provider": "deterministic-product-list",
                    "model": "active-full-product-catalog-v1",
                    "rag_matches": [],
                },
            },
            source_email_id=email_row.id,
        )
        return True
    bundle = load_content(settings.content_dir)
    signed_text = "\n".join([draft_body, "", bundle.signature_text.strip()])
    signed_html = "".join(
        f"<p>{html.escape(line) if line else '&nbsp;'}</p>"
        for line in draft_body.splitlines()
    ) + bundle.signature_html
    source = _reply_source(email_row)
    signed_text, signed_html = append_quoted_reply(
        signed_text,
        signed_html,
        from_address=email_row.from_address,
        source_body=source.body_text,
        source_html=source.body_html,
        occurred_at=email_row.received_at,
    )
    outbox = await stage_outbox(
        session,
        case=case,
        message_kind="PRODUCT_LIST",
        subject=subject,
        text_body=signed_text,
        html_body=signed_html,
        business_key=f"inbound-product-list:{email_row.id}:all",
        in_reply_to=email_row.message_id,
        references=_reply_references(email_row),
        inline_images=source.inline_images,
        attachments=(
            OutboundAttachment(
                filename=catalog_file.filename,
                content_type=catalog_file.content_type,
                payload=catalog_file.payload,
            ),
        ),
    )
    return outbox is not None


async def _maybe_send_product_list(
    session: AsyncSession,
    *,
    case: SalesCase,
    email_row: EmailMessage,
    analysis: InboundAnalysis,
    analysis_facts: dict[str, Any],
) -> bool:
    """Queue a deterministic category product-list reply when eligible.

    Returns ``True`` when the inbound email was handled (a product-list reply
    was queued or a handoff was created). Returns ``False`` when the case has
    no product category at all, so callers can continue the normal pipeline.
    """
    category = (
        await session.get(ProductCategory, case.category_id)
        if case.category_id is not None
        else None
    )
    if (
        category is None
        and case.product_id is not None
        and case.product is not None
        and case.product.category_id is not None
    ):
        category = await session.get(ProductCategory, case.product.category_id)
    if category is None or not category.active:
        if case.category_id is None:
            return False
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.HUMAN_CONTROL,
            summary="Case product category is no longer active",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return True

    send_decision = evaluate_send_policy(
        SendContext(
            intent=analysis.intent,
            stage=case.stage,
            status=case.status,
            intent_confidence=analysis.intent_confidence,
            # The product category comes from the CRM record or the matched
            # product, so the catalog target is deterministic rather than an
            # extracted product code.
            product_confidence=1.0,
            numeric_confidence=1.0,
            auto_send_allowed=case.customer.auto_send_allowed,
            contact_suppressed=case.contact.suppressed,
            do_not_contact=case.customer.do_not_contact,
            has_risky_attachment=analysis.risky_attachment,
            product_known=(
                analysis.product_code is None
                or (
                    case.product is not None
                    and product_codes_match(analysis.product_code, case.product.code)
                )
            ),
            prebook_requested=analysis.prebook_requested,
            packaging_requested=analysis.packaging_requested,
            delivery_requested=analysis.shipping_requested,
        ),
        intent_threshold=get_settings().intent_confidence_threshold,
        product_threshold=get_settings().product_confidence_threshold,
        numeric_threshold=get_settings().numeric_confidence_threshold,
    )
    if not send_decision.allow_send:
        await create_handoff(
            session,
            case=case,
            reason=send_decision.reason or HandoffReason.LOW_CONFIDENCE,
            summary=f"Inbound {analysis.intent.value} requires human review",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return True
    if analysis.product_code is not None and not (
        case.product is not None
        and product_codes_match(analysis.product_code, case.product.code)
    ):
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.NONSTANDARD,
            summary="Email names a specific product; a category product list is not appropriate",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return True

    products = (
        (
            await session.execute(
                select(Product)
                .where(
                    Product.category_id == category.id,
                    Product.active.is_(True),
                )
                .order_by(Product.sort_order, Product.id)
            )
        )
        .scalars()
        .all()
    )
    if not products:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.HUMAN_CONTROL,
            summary=f"Product category {category.key} has no active products",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return True

    settings = get_settings()
    bundle = load_content(settings.content_dir)
    try:
        attachments, attachment_filename = _product_list_outbound_attachments(
            category=category,
            products=products,
            request_text=f"{email_row.subject}\n{email_row.body_text}",
        )
        text, html_body = render_product_list_email(
            contact_name=case.contact.name,
            category=category,
            products=products,
            subject=email_row.subject,
            signature_text=bundle.signature_text,
            signature_html=bundle.signature_html,
            attachment_filename=attachment_filename,
        )
        signature_text = bundle.signature_text.strip()
        draft_body = (
            text[: -len(signature_text)].rstrip()
            if signature_text and text.endswith(signature_text)
            else text.rstrip()
        )
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
            summary=f"Product list rendering failed: {type(exc).__name__}",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return True
    subject = (
        f"Re: {email_row.subject}"
        if email_row.subject.strip()
        else f"Our {category.name} product list"
    )
    product_list_request_text = f"{email_row.subject}\n{email_row.body_text}"
    payment_requested = _payment_details_requested(product_list_request_text)
    payment = (
        await _customer_payment_term(session, customer_id=case.customer_id)
        if payment_requested
        else None
    )
    payment_term = payment.term if payment is not None else None
    if payment is not None:
        draft_body = f"{draft_body}\n\n{_payment_term_sentence(payment)}"
    if not settings.product_list_auto_send_enabled:
        file_format = requested_product_list_file_format(
            f"{email_row.subject}\n{email_row.body_text}"
        )
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.PRODUCT_LIST_REVIEW,
            summary=f"Product-list draft prepared for {category.name}; human approval is required",
            facts={
                **analysis_facts,
                "prepared_product_list": {
                    "category_id": category.id,
                    "category_key": category.key,
                    "category_name": category.name,
                    "product_ids": [product.id for product in products],
                    "product_codes": [product.code for product in products],
                    "product_count": len(products),
                    "category_breakdown": await _catalog_category_breakdown(
                        session, products
                    ),
                    "file_format": file_format,
                    "attachment_filename": attachment_filename,
                    "payment_requested": payment_requested,
                    "payment_term": payment_term,
                    "payment_term_source": payment.source if payment is not None else None,
                    "payment_term_quote_id": payment.quote_id if payment is not None else None,
                    "missing_business_facts": [],
                },
                "ai_draft_preview": {
                    "subject": subject,
                    "body_text": draft_body,
                    "generated_at": datetime.now(UTC).isoformat(),
                    "provider": "deterministic-product-list",
                    "model": "active-product-catalog-v1",
                    "rag_matches": [],
                },
            },
            source_email_id=email_row.id,
            update_existing=True,
        )
        return True
    outbox = await stage_outbox(
        session,
        case=case,
        message_kind="PRODUCT_LIST",
        subject=subject,
        text_body=text,
        html_body=html_body,
        business_key=f"inbound-product-list:{email_row.id}",
        in_reply_to=email_row.message_id,
        references=_reply_references(email_row),
        inline_images=source.inline_images,
        attachments=attachments,
    )
    if outbox is None:
        await session.rollback()
        return True
    await audit(
        session,
        "inbound.product_list_queued",
        case_id=case.id,
        actor="system",
        data={
            "email_id": email_row.id,
            "outbox_id": outbox.id,
            "category_id": category.id,
            "category_key": category.key,
            "product_count": len(products),
            "attachment_filename": attachment_filename,
        },
    )
    return True


@_atomic_business_operation
async def backfill_product_list_requests(
    session: AsyncSession,
    *,
    apply: bool = False,
    limit: int = 500,
    max_age_days: int = 30,
    handoff_ids: tuple[int, ...] = (),
    include_history: bool = False,
    company_research: bool = False,
) -> dict[str, Any]:
    """Safely queue replies for explicit, unresolved product-list requests.

    Preview mode is strictly read-only. Apply mode revalidates every delivery
    guard, normally requires a unique active catalog category, preserves the
    original reply thread, and resolves the obsolete handoff only after an
    idempotent PRODUCT_LIST outbox row exists. ``company_research`` is an
    explicit opt-in for selected, category-less handoffs and remains governed
    by both company-research feature switches.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if max_age_days <= 0:
        raise ValueError("max_age_days must be positive")
    if apply and not handoff_ids:
        raise ValueError("apply mode requires explicitly selected handoff_ids")

    query = (
        select(Handoff)
        .where(
            Handoff.status == "OPEN",
            Handoff.source_email_id.is_not(None),
        )
        .order_by(Handoff.id)
        .limit(limit)
    )
    if handoff_ids:
        query = query.where(Handoff.id.in_(handoff_ids))
    if apply:
        query = query.with_for_update(skip_locked=True)
    handoffs = list((await session.scalars(query)).all())

    active_categories = list(
        (
            await session.scalars(
                select(ProductCategory).where(ProductCategory.active.is_(True))
            )
        ).all()
    )
    categories_by_id = {category.id: category for category in active_categories}
    categories_by_key = {category.key: category for category in active_categories}
    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    candidates: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    queued: list[dict[str, Any]] = []
    prepared_replies: dict[int, dict[str, Any]] = {}

    def exclude(handoff: Handoff, reason: str, **details: Any) -> None:
        exclusions.append(
            {
                "handoff_id": handoff.id,
                "email_id": handoff.source_email_id,
                "reason": reason,
                **details,
            }
        )

    for handoff in handoffs:
        source = await session.get(EmailMessage, handoff.source_email_id)
        if source is None:
            exclude(handoff, "SOURCE_EMAIL_MISSING")
            continue
        if source.direction != "INBOUND" or (source.is_history and not include_history):
            exclude(handoff, "NOT_LIVE_INBOUND")
            continue
        if source.received_at < cutoff:
            exclude(
                handoff,
                "OLDER_THAN_MAX_AGE",
                received_at=source.received_at.isoformat(),
            )
            continue
        if source.is_bounce or source.is_automated_reply:
            exclude(handoff, "NON_CUSTOMER_MESSAGE")
            continue
        request_text = f"{source.subject}\n{source.body_text}"
        if not explicit_product_list_requested(request_text):
            exclude(handoff, "NOT_EXPLICIT_PRODUCT_LIST_REQUEST")
            continue
        if attachments_require_review(source.attachment_metadata, source.body_html):
            exclude(handoff, "RISKY_ATTACHMENT")
            continue
        if not source.message_id:
            exclude(handoff, "SOURCE_MESSAGE_ID_MISSING")
            continue

        case_id = handoff.case_id or source.case_id
        sales_case = (
            await session.scalar(
                select(SalesCase)
                .options(
                    selectinload(SalesCase.customer),
                    selectinload(SalesCase.contact),
                    selectinload(SalesCase.product),
                    selectinload(SalesCase.category),
                )
                .where(SalesCase.id == case_id)
            )
            if case_id is not None
            else None
        )
        facts = dict(handoff.extracted_facts or {})
        contact_id = (
            sales_case.contact_id
            if sales_case is not None
            else source.contact_id or facts.get("contact_id")
        )
        contact = (
            sales_case.contact
            if sales_case is not None
            else await session.scalar(
                select(Contact)
                .options(selectinload(Contact.customer))
                .where(Contact.id == contact_id)
            )
        )
        if contact is None:
            exclude(handoff, "CONTACT_NOT_UNIQUE_OR_MISSING")
            continue
        customer = sales_case.customer if sales_case is not None else contact.customer
        if source.from_address.strip().casefold() != contact.email.strip().casefold():
            exclude(handoff, "SOURCE_CONTACT_MISMATCH")
            continue
        address_status = await session.get(
            EmailAddressStatus,
            contact.email.strip().casefold(),
        )
        if (
            contact.suppressed
            or customer.do_not_contact
            or not customer.auto_send_allowed
            or (address_status is not None and address_status.suppressed)
        ):
            exclude(handoff, "RECIPIENT_NOT_AUTHORIZED")
            continue

        category = None
        research_required = False
        if sales_case is not None:
            category = sales_case.category
            if (
                category is None
                and sales_case.product is not None
                and sales_case.product.category_id is not None
            ):
                category = categories_by_id.get(sales_case.product.category_id)
        if category is None:
            interest_categories = [
                categories_by_key[key]
                for key in customer_interest_keys(customer)
                if key in categories_by_key
            ]
            interest_categories = list(
                {item.id: item for item in interest_categories}.values()
            )
            if len(interest_categories) != 1:
                if (
                    company_research
                    and not interest_categories
                    and sales_case is not None
                    and sales_case.product_id is None
                    and sales_case.category_id is None
                ):
                    if not settings.company_research_enabled:
                        exclude(handoff, "COMPANY_RESEARCH_DISABLED")
                        continue
                    research_required = True
                else:
                    exclude(
                        handoff,
                        "INTEREST_CATEGORY_NOT_UNIQUE",
                        category_keys=[item.key for item in interest_categories],
                    )
                    continue
            else:
                category = interest_categories[0]
        if category is not None and category.id not in categories_by_id:
            exclude(handoff, "CATEGORY_INACTIVE")
            continue

        existing_product_list = await session.scalar(
            select(Outbox).where(
                Outbox.business_key == f"inbound-product-list:{source.id}",
            )
        )
        if (
            existing_product_list is not None
            and existing_product_list.status == DeliveryStatus.CANCELLED
        ):
            exclude(
                handoff,
                "PREVIOUS_PRODUCT_LIST_CANCELLED",
                outbox_id=existing_product_list.id,
            )
            continue
        approved_reply = await session.scalar(
            select(Outbox.id).where(Outbox.approval_handoff_id == handoff.id)
        )
        exact_thread_reply = await session.scalar(
            select(EmailMessage.id).where(
                EmailMessage.direction == "OUTBOUND",
                EmailMessage.in_reply_to == source.message_id,
            )
        )
        if approved_reply is not None or (
            exact_thread_reply is not None and existing_product_list is None
        ):
            exclude(handoff, "ALREADY_REPLIED")
            continue

        if sales_case is not None:
            if sales_case.status not in {CaseStatus.ACTIVE, CaseStatus.WAITING_HUMAN}:
                exclude(
                    handoff,
                    "CASE_STATUS_UNSAFE",
                    case_id=sales_case.id,
                    case_status=sales_case.status.value,
                )
                continue
            if sales_case.stage not in {CaseStage.QUOTING, CaseStage.FOLLOW_UP}:
                exclude(
                    handoff,
                    "CASE_STAGE_UNSAFE",
                    case_id=sales_case.id,
                    case_stage=sales_case.stage.value,
                )
                continue
            other_open_handoffs = await session.scalar(
                select(func.count())
                .select_from(Handoff)
                .where(
                    Handoff.case_id == sales_case.id,
                    Handoff.status == "OPEN",
                    Handoff.id != handoff.id,
                )
            )
            if other_open_handoffs:
                exclude(
                    handoff,
                    "CASE_HAS_OTHER_OPEN_HANDOFFS",
                    case_id=sales_case.id,
                    count=other_open_handoffs,
                )
                continue

        analysis = stub_analyze(
            source.subject,
            source.body_text,
            source.attachment_metadata,
        ).model_copy(update={"risky_attachment": False})
        matched_product = None
        if existing_product_list is None and research_required:
            if analysis.intent != Intent.PRODUCT_LIST_REQUEST:
                exclude(
                    handoff,
                    "UNSAFE_PRODUCT_LIST_INTENT",
                    detected_intent=analysis.intent.value,
                )
                continue
            if analysis.product_code is not None:
                exclude(
                    handoff,
                    "SPECIFIC_PRODUCT_REQUIRES_CATALOG_MATCH",
                    detected_product_code=canonical_product_code(analysis.product_code),
                )
                continue
        if existing_product_list is None and not research_required:
            if analysis.intent != Intent.PRODUCT_LIST_REQUEST:
                exclude(
                    handoff,
                    "UNSAFE_PRODUCT_LIST_INTENT",
                    detected_intent=analysis.intent.value,
                )
                continue
            if analysis.product_code is not None:
                canonical_code = canonical_product_code(analysis.product_code)
                matched_product = await session.scalar(
                    select(Product).where(
                        Product.code == canonical_code,
                        Product.active.is_(True),
                    )
                )
                if matched_product is None:
                    exclude(
                        handoff,
                        "SPECIFIC_PRODUCT_NOT_ACTIVE",
                        detected_product_code=canonical_code,
                    )
                    continue
                if matched_product.category_id != category.id:
                    exclude(
                        handoff,
                        "SPECIFIC_PRODUCT_CATEGORY_MISMATCH",
                        detected_product_code=matched_product.code,
                        product_category_id=matched_product.category_id,
                        selected_category_id=category.id,
                    )
                    continue
                if (
                    sales_case is not None
                    and sales_case.product is not None
                    and not product_codes_match(
                        matched_product.code,
                        sales_case.product.code,
                    )
                ):
                    exclude(
                        handoff,
                        "CASE_PRODUCT_MISMATCH",
                        detected_product_code=matched_product.code,
                        case_product_code=sales_case.product.code,
                    )
                    continue

            planned_status = (
                CaseStatus.ACTIVE
                if sales_case is None or sales_case.status == CaseStatus.WAITING_HUMAN
                else sales_case.status
            )
            send_decision = evaluate_send_policy(
                SendContext(
                    intent=analysis.intent,
                    stage=(
                        sales_case.stage
                        if sales_case is not None
                        else CaseStage.QUOTING
                    ),
                    status=planned_status,
                    intent_confidence=analysis.intent_confidence,
                    product_confidence=1.0,
                    numeric_confidence=1.0,
                    auto_send_allowed=customer.auto_send_allowed,
                    contact_suppressed=contact.suppressed,
                    do_not_contact=customer.do_not_contact,
                    has_risky_attachment=analysis.risky_attachment,
                    product_known=(
                        analysis.product_code is None
                        or matched_product is not None
                    ),
                    prebook_requested=analysis.prebook_requested,
                    packaging_requested=analysis.packaging_requested,
                    delivery_requested=analysis.shipping_requested,
                ),
                intent_threshold=settings.intent_confidence_threshold,
                product_threshold=settings.product_confidence_threshold,
                numeric_threshold=settings.numeric_confidence_threshold,
            )
            if not send_decision.allow_send:
                exclude(
                    handoff,
                    "SEND_POLICY_BLOCKED",
                    policy_reason=(
                        send_decision.reason.value
                        if send_decision.reason is not None
                        else None
                    ),
                )
                continue

            products = list(
                (
                    await session.scalars(
                        select(Product)
                        .where(
                            Product.category_id == category.id,
                            Product.active.is_(True),
                        )
                        .order_by(Product.sort_order, Product.id)
                    )
                ).all()
            )
            if not products:
                exclude(handoff, "CATEGORY_HAS_NO_ACTIVE_PRODUCTS")
                continue
            try:
                bundle = load_content(settings.content_dir)
                attachments, attachment_filename = (
                    _product_list_outbound_attachments(
                        category=category,
                        products=products,
                        request_text=request_text,
                    )
                )
                text_body, html_body = render_product_list_email(
                    contact_name=contact.name,
                    category=category,
                    products=products,
                    subject=source.subject,
                    signature_text=bundle.signature_text,
                    signature_html=bundle.signature_html,
                    attachment_filename=attachment_filename,
                )
                reply_source = _reply_source(source)
                text_body, html_body = append_quoted_reply(
                    text_body,
                    html_body,
                    from_address=source.from_address,
                    source_body=reply_source.body_text,
                    source_html=reply_source.body_html,
                    occurred_at=source.received_at,
                )
            except Exception as exc:
                exclude(
                    handoff,
                    "REPLY_SOURCE_OR_RENDER_UNAVAILABLE",
                    error_type=type(exc).__name__,
                    detail=str(exc)[:500],
                )
                continue
            prepared_replies[handoff.id] = {
                "analysis": analysis,
                "matched_product": matched_product,
                "subject": (
                    f"Re: {source.subject}"
                    if source.subject.strip()
                    else f"Our {category.name} product list"
                ),
                "text_body": text_body,
                "html_body": html_body,
                "inline_images": reply_source.inline_images,
                "attachments": attachments,
                "attachment_filename": attachment_filename,
                "product_count": len(products),
            }

        candidate = {
            "handoff_id": handoff.id,
            "email_id": source.id,
            "case_id": sales_case.id if sales_case is not None else None,
            "customer_id": customer.id,
            "contact_id": contact.id,
            "recipient": contact.email,
            "subject": source.subject,
            "received_at": source.received_at.isoformat(),
            "category_id": category.id if category is not None else None,
            "category_key": category.key if category is not None else None,
            "company_research_required": research_required,
            "detected_intent": analysis.intent.value,
            "detected_product_code": analysis.product_code,
            "matched_product_id": (
                matched_product.id if matched_product is not None else None
            ),
            "requested_file_format": requested_product_list_file_format(
                request_text
            ),
            "existing_outbox_id": (
                existing_product_list.id if existing_product_list is not None else None
            ),
        }
        candidates.append(candidate)
        if not apply:
            continue

        if research_required and existing_product_list is None:
            if sales_case is None:
                exclusions.append(
                    {
                        "handoff_id": handoff.id,
                        "email_id": source.id,
                        "reason": "COMPANY_RESEARCH_CASE_REQUIRED",
                    }
                )
                continue
            if sales_case.status == CaseStatus.WAITING_HUMAN:
                sales_case.status = CaseStatus.ACTIVE
            await _maybe_research_and_send_product_list(
                session,
                case=sales_case,
                email_row=source,
                analysis=analysis,
                analysis_facts={
                    **(handoff.extracted_facts or {}),
                    **analysis.model_dump(mode="json"),
                    "company_research_backfill": True,
                },
                existing_handoff=handoff,
            )
            existing_product_list = await session.scalar(
                select(Outbox).where(
                    Outbox.business_key == f"inbound-product-list:{source.id}",
                    Outbox.status != DeliveryStatus.CANCELLED,
                )
            )
            if existing_product_list is None:
                await session.refresh(handoff)
                exclusions.append(
                    {
                        "handoff_id": handoff.id,
                        "email_id": source.id,
                        "reason": "COMPANY_RESEARCH_REQUIRES_HUMAN",
                        "handoff_reason": handoff.reason_code,
                        "summary": handoff.summary,
                    }
                )
                continue
            await session.refresh(sales_case, ["category"])
            category = sales_case.category
            if category is None:
                raise RuntimeError(
                    "company research queued a product list without assigning a category"
                )
            candidate["category_id"] = category.id
            candidate["category_key"] = category.key

        if existing_product_list is None:
            prepared = prepared_replies[handoff.id]
            matched_product = prepared["matched_product"]
            if sales_case is None:
                sales_case = SalesCase(
                    customer_id=customer.id,
                    contact_id=contact.id,
                    product_id=(
                        matched_product.id if matched_product is not None else None
                    ),
                    category_id=category.id,
                    currency="INR",
                    stage=CaseStage.QUOTING,
                    status=CaseStatus.ACTIVE,
                    subject_key=normalized_subject(source.subject)[:255],
                    customer=customer,
                    contact=contact,
                    product=matched_product,
                    category=category,
                )
                session.add(sales_case)
                await session.flush()
            else:
                sales_case.category_id = category.id
                sales_case.category = category
                if sales_case.product is None and matched_product is not None:
                    sales_case.product_id = matched_product.id
                    sales_case.product = matched_product
                if sales_case.status == CaseStatus.WAITING_HUMAN:
                    sales_case.status = CaseStatus.ACTIVE
            source.case_id = sales_case.id
            source.customer_id = customer.id
            source.contact_id = contact.id
            handoff.case_id = sales_case.id
            existing_product_list = await stage_outbox(
                session,
                case=sales_case,
                message_kind="PRODUCT_LIST",
                subject=prepared["subject"],
                text_body=prepared["text_body"],
                html_body=prepared["html_body"],
                business_key=f"inbound-product-list:{source.id}",
                in_reply_to=source.message_id,
                references=_reply_references(source),
                inline_images=prepared["inline_images"],
                attachments=prepared["attachments"],
            )
            if existing_product_list is None:
                existing_product_list = await session.scalar(
                    select(Outbox).where(
                        Outbox.business_key == f"inbound-product-list:{source.id}",
                        Outbox.status != DeliveryStatus.CANCELLED,
                    )
                )
            if existing_product_list is None:
                exclusions.append(
                    {
                        "handoff_id": candidate["handoff_id"],
                        "email_id": candidate["email_id"],
                        "reason": "OUTBOX_IDEMPOTENCY_CONFLICT",
                    }
                )
                await session.rollback()
                continue

        if sales_case is not None and sales_case.status == CaseStatus.WAITING_HUMAN:
            sales_case.status = CaseStatus.ACTIVE
        handoff.status = "RESOLVED"
        handoff.resolution_note = (
            f"Automatically backfilled product list for {category.key}; "
            f"outbox_id={existing_product_list.id}"
        )
        await finalize_handoff_agent_run(
            session,
            handoff_id=handoff.id,
            actor="product-list-backfill",
            outcome="product-list-backfilled",
        )
        if handoff.dingtalk_status != "SENT":
            handoff.dingtalk_status = "CANCELLED"
        await audit(
            session,
            "handoff.product_list_backfilled",
            case_id=existing_product_list.case_id,
            actor="product-list-backfill",
            data={
                "handoff_id": handoff.id,
                "email_id": source.id,
                "outbox_id": existing_product_list.id,
                "category_id": category.id,
                "category_key": category.key,
                "attachment_filename": (
                    prepared_replies.get(handoff.id, {}).get(
                        "attachment_filename"
                    )
                ),
            },
        )
        await session.commit()
        queued.append(
            {
                **candidate,
                "case_id": existing_product_list.case_id,
                "outbox_id": existing_product_list.id,
                "outbox_status": existing_product_list.status.value,
            }
        )

    exclusion_counts: dict[str, int] = {}
    for item in exclusions:
        reason = str(item["reason"])
        exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
    return {
        "apply": apply,
        "include_history": include_history,
        "company_research": company_research,
        "max_age_days": max_age_days,
        "scanned": len(handoffs),
        "candidate_count": len(candidates),
        "queued_count": len(queued),
        "exclusion_counts": exclusion_counts,
        "candidates": candidates,
        "queued": queued,
        "exclusions": exclusions,
    }


@_atomic_business_operation
async def process_inbound(session: AsyncSession, email_id: int) -> None:
    email_row = await session.get(EmailMessage, email_id)
    if email_row is None:
        return
    if email_row.is_bounce:
        await _handle_bounce(session, email_row)
        return
    # A reply to a reactivation is business-significant even when a mail client
    # omitted thread headers and the normal case matcher could not link it.
    if not email_row.is_automated_reply:
        await record_reactivation_reply(session, email_row, commit=False)
    if email_row.case_id is None:
        return
    case = await session.get(SalesCase, email_row.case_id)
    if case is None:
        return
    if case.status == CaseStatus.HUMAN_TAKEOVER:
        # The salesperson explicitly took the case over: the AI must not
        # reply, clarify, quote or create handoffs for this case again.
        return
    reply_key = f"inbound-reply:{email_row.id}"
    existing_reply = await session.scalar(
        select(Outbox.id).where(
            or_(
                Outbox.business_key == reply_key,
                Outbox.business_key.like(f"{reply_key}:%"),
            ),
            Outbox.status != DeliveryStatus.CANCELLED,
        )
    )
    if existing_reply is not None:
        return
    existing_handoff = await session.scalar(select(Handoff).where(Handoff.source_email_id == email_row.id))
    if existing_handoff is not None:
        await enqueue_job(
            session,
            "notify_handoff",
            {"handoff_id": existing_handoff.id},
            f"handoff-notify:{existing_handoff.id}",
        )
        return
    await session.refresh(case, ["customer", "contact", "product"])
    if await _handle_automated_reply(session, case=case, email_row=email_row):
        return
    settings = get_settings()
    commercial_context: QuoteContext | None = None
    ai = AIClient()
    try:
        analysis, metadata = await ai.analyze(email_row.subject, email_row.body_text, email_row.attachment_metadata)
    except Exception as exc:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.AI_FAILURE,
            summary=f"AI analysis failed: {type(exc).__name__}",
            source_email_id=email_row.id,
        )
        return
    analysis = analysis.model_copy(
        update={
            "risky_attachment": attachments_require_review(
                email_row.attachment_metadata,
                email_row.body_html,
            )
        }
    )
    analysis_facts = analysis.model_dump(mode="json")
    session.add(
        AIInvocation(
            case_id=case.id,
            provider=metadata["provider"],
            model=metadata["model"],
            purpose="inbound_analysis",
            request_hash=metadata["request_hash"],
            parsed_output=analysis_facts,
            success=True,
            input_tokens=metadata.get("input_tokens"),
            output_tokens=metadata.get("output_tokens"),
        )
    )
    if analysis.unsubscribe:
        case.contact.suppressed = True
        case.customer.do_not_contact = True
        case.status = CaseStatus.PAUSED
        await audit(session, "contact.unsubscribed", case_id=case.id, actor="customer")
        await session.commit()
        return
    if await _maybe_handle_coa_request(
        session,
        case=case,
        email_row=email_row,
        analysis=analysis,
        analysis_facts=analysis_facts,
    ):
        return
    multi_product_request = (
        len(
            [
                line
                for line in analysis.product_requests
                if line.product_code
            ]
        )
        >= 2
    )
    if (
        case.product_id is None
        and analysis.intent == Intent.QUOTE_REQUEST
        and not multi_product_request
    ):
        analysis, context_conflict = await _augment_pending_quote_context(
            session,
            case=case,
            email_row=email_row,
            analysis=analysis,
        )
        analysis_facts = analysis.model_dump(mode="json")
        if context_conflict is not None:
            await create_handoff(
                session,
                case=case,
                reason=HandoffReason.NONSTANDARD,
                summary=context_conflict,
                facts={
                    **analysis_facts,
                    "product_pending": True,
                    "context_conflict": context_conflict,
                },
                source_email_id=email_row.id,
            )
            return
    product_independent_risk = {
        Intent.COUNTEROFFER: HandoffReason.PRICE_NEGOTIATION,
        Intent.SAMPLE_REQUEST: HandoffReason.SAMPLE_REQUEST,
        Intent.ORDER: HandoffReason.ORDER_COMMITMENT,
        Intent.SHIPPING: HandoffReason.SHIPPING_REQUEST,
        Intent.TECHNICAL: HandoffReason.TECHNICAL_REQUEST,
        Intent.COMPLAINT: HandoffReason.COMPLAINT,
    }.get(analysis.intent)
    if (case.product_id is None or case.product is None) and product_independent_risk:
        await create_handoff(
            session,
            case=case,
            reason=product_independent_risk,
            summary=f"Inbound {analysis.intent.value} requires human review",
            facts={**analysis_facts, "product_pending": True},
            source_email_id=email_row.id,
        )
        return
    if (
        analysis.intent == Intent.PRODUCT_LIST_REQUEST
        and await _maybe_send_general_product_list(
            session,
            case=case,
            email_row=email_row,
            analysis=analysis,
            analysis_facts=analysis_facts,
        )
    ):
        return
    if case.product_id is None or case.product is None:
        if (
            case.category_id is not None
            and analysis.intent == Intent.PRODUCT_LIST_REQUEST
        ):
            if await _maybe_send_product_list(
                session,
                case=case,
                email_row=email_row,
                analysis=analysis,
                analysis_facts=analysis_facts,
            ):
                return
        if (
            case.category_id is None
            and analysis.intent == Intent.PRODUCT_LIST_REQUEST
            and await _maybe_research_and_send_product_list(
                session,
                case=case,
                email_row=email_row,
                analysis=analysis,
                analysis_facts=analysis_facts,
            )
        ):
            return
        if analysis.intent == Intent.QUOTE_REQUEST:
            if await _maybe_send_multi_product_quote(
                session,
                case=case,
                email_row=email_row,
                analysis=analysis,
                analysis_facts=analysis_facts,
            ):
                return
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
            reason=(
                HandoffReason.PRODUCT_CATEGORY_REVIEW
                if analysis.intent == Intent.PRODUCT_LIST_REQUEST
                else HandoffReason.HUMAN_CONTROL
            ),
            summary=(
                "Product-list request requires product category confirmation"
                if analysis.intent == Intent.PRODUCT_LIST_REQUEST
                else "Case product is still pending human selection"
            ),
            facts={**analysis_facts, "product_pending": True},
            source_email_id=email_row.id,
        )
        return
    # Weekly commercial-data readiness blocks only an autonomous quotation.
    # Unsubscribe, counteroffers, samples, orders, complaints, and all other
    # human-review paths must still be classified and surfaced immediately.
    if analysis.intent == Intent.PRODUCT_LIST_REQUEST:
        if await _maybe_send_product_list(
            session,
            case=case,
            email_row=email_row,
            analysis=analysis,
            analysis_facts=analysis_facts,
        ):
            return
    if analysis.intent != Intent.QUOTE_REQUEST:
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
                product_known=analysis.product_code is None or product_codes_match(analysis.product_code, case.product.code),
                prebook_requested=analysis.prebook_requested,
                packaging_requested=analysis.packaging_requested,
                delivery_requested=analysis.shipping_requested,
            ),
            intent_threshold=get_settings().intent_confidence_threshold,
            product_threshold=get_settings().product_confidence_threshold,
            numeric_threshold=get_settings().numeric_confidence_threshold,
        )
        await create_handoff(
            session,
            case=case,
            reason=send_decision.reason or HandoffReason.LOW_CONFIDENCE,
            summary=f"Inbound {analysis.intent.value} requires human review",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return
    latest_quote = await session.scalar(
        select(Quote).where(Quote.case_id == case.id).order_by(Quote.round_number.desc())
    )
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
            HandoffReason.INVENTORY_UNAVAILABLE
            if commercial_context.reason.startswith("INVENTORY")
            else HandoffReason.NONSTANDARD
        )
        await create_handoff(
            session,
            case=case,
            reason=unavailable_reason,
            summary=(
                f"Current commercial data cannot quote {case.product.code}: "
                f"{commercial_context.reason}"
            ),
            facts={
                **analysis_facts,
                "commercial_cycle_id": commercial_context.cycle.id,
                "requested_quantity": quantity,
                "available_quantity": (
                    str(commercial_context.inventory.quantity)
                    if commercial_context.inventory is not None
                    and commercial_context.inventory.quantity is not None
                    else None
                ),
            },
            source_email_id=email_row.id,
        )
        return
    policy_row = (
        commercial_context.policy
        if commercial_context is not None
        else await active_policy(session, case.product_id, case.currency)
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
    quantity_standard = quantity >= policy_row.min_quantity and (
        policy_row.max_quantity is None or quantity <= policy_row.max_quantity
    )
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
            ready_stock_available=(
                commercial_context.ready_stock_available
                if commercial_context is not None
                else True
            ),
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
                "contact_name": case.contact.name,
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
            availability=(
                "Subject to confirmation at order placement"
                if settings.quote_ignore_inventory
                else "Ready stock"
            ),
        )
        signature_text = bundle.signature_text.strip()
        draft_body = (
            text[: -len(signature_text)].rstrip()
            if signature_text and text.endswith(signature_text)
            else text.rstrip()
        )
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
        commercial_cycle_id = (
            commercial_context.cycle.id if commercial_context is not None else None
        )
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


@_atomic_business_operation
async def resume_agent_run(
    session: AsyncSession,
    *,
    run_id: int,
    expected_version: int,
    assistance_request_id: int,
) -> None:
    """Continue a paused product-catalog task after typed human assistance."""

    run = await session.scalar(
        select(AgentRun).where(AgentRun.id == run_id).with_for_update()
    )
    if run is None:
        return
    if run.status == AgentRunStatus.COMPLETED:
        return
    if run.version != expected_version:
        # A newer human answer/version supersedes this queued continuation.
        return

    request = await session.scalar(
        select(AssistanceRequest).where(
            AssistanceRequest.id == assistance_request_id,
            AssistanceRequest.run_id == run.id,
        )
    )
    handoff = await session.get(Handoff, run.handoff_id)
    case = (
        await session.scalar(
            select(SalesCase)
            .options(
                selectinload(SalesCase.customer),
                selectinload(SalesCase.contact),
                selectinload(SalesCase.product),
            )
            .where(SalesCase.id == run.case_id)
        )
        if run.case_id is not None
        else None
    )
    email_row = await session.get(EmailMessage, run.source_email_id)
    now = datetime.now(UTC)

    prior_step = await session.scalar(
        select(AgentStep).where(
            AgentStep.run_id == run.id,
            AgentStep.idempotency_key == f"resume:{expected_version}",
        )
    )
    if prior_step is None:
        sequence = int(
            await session.scalar(
                select(func.max(AgentStep.sequence)).where(AgentStep.run_id == run.id)
            )
            or 0
        ) + 1
        step = AgentStep(
            run_id=run.id,
            sequence=sequence,
            kind="RESUME_EXECUTION",
            idempotency_key=f"resume:{expected_version}",
            status=AgentStepStatus.RUNNING,
            input_json={
                "assistance_request_id": assistance_request_id,
                "run_version": expected_version,
            },
            started_at=now,
        )
        session.add(step)
        await session.flush()
    else:
        step = prior_step
        step.status = AgentStepStatus.RUNNING
        step.error = None
        step.started_at = step.started_at or now

    async def block(reason: str) -> None:
        run.status = AgentRunStatus.BLOCKED
        run.current_step = "human_review"
        run.last_error = reason[:2000]
        step.status = AgentStepStatus.BLOCKED
        step.error = reason[:2000]
        step.completed_at = datetime.now(UTC)
        if case is not None:
            case.status = CaseStatus.WAITING_HUMAN
        if handoff is not None:
            handoff.status = "OPEN"
            handoff.summary = reason
        await audit(
            session,
            "agent.run_blocked",
            case_id=run.case_id,
            actor="agent-runtime",
            data={
                "agent_run_id": run.id,
                "run_version": run.version,
                "assistance_request_id": assistance_request_id,
                "reason": reason,
            },
        )
        await session.commit()

    if (
        request is None
        or request.status != AssistanceStatus.ANSWERED
        or not request.answer_json
    ):
        await block("Agent resume is missing an answered assistance request")
        return
    if handoff is None or handoff.status != "OPEN":
        await block("The related human handoff is no longer open")
        return
    if email_row is None:
        await block("The source email is unavailable")
        return

    if request.request_type == COA_LOOKUP_REQUEST_TYPE:
        if case is None:
            await block("The COA request is no longer associated with a customer case")
            return
        try:
            analysis = InboundAnalysis.model_validate(handoff.extracted_facts)
        except Exception:
            await block("The stored COA request facts are invalid")
            return
        corrected_query = str(request.answer_json.get("product_query") or "").strip()
        corrected_cas = str(request.answer_json.get("cas_number") or "").strip() or None
        corrected_analysis = analysis.model_copy(
            update={
                "requested_product_name": corrected_query,
                "requested_cas_number": corrected_cas,
                "missing_fields": [
                    field
                    for field in analysis.missing_fields
                    if field != "requested_product_name"
                ],
            }
        )
        run.status = AgentRunStatus.RUNNING
        run.current_step = "recheck-coa-catalog"
        run.last_error = None
        case.status = CaseStatus.ACTIVE
        resume_facts = {
            **(handoff.extracted_facts or {}),
            **corrected_analysis.model_dump(mode="json"),
            "human_corrected_coa_lookup": {
                "product_query": corrected_query,
                "cas_number": corrected_cas,
                "assistance_request_id": request.id,
                "answered_by": request.answered_by,
            },
        }
        handoff.extracted_facts = resume_facts
        await _maybe_handle_coa_request(
            session,
            case=case,
            email_row=email_row,
            analysis=corrected_analysis,
            analysis_facts=resume_facts,
        )
        prepared = (handoff.extracted_facts or {}).get("prepared_coa")
        outbox = await session.scalar(
            select(Outbox).where(
                Outbox.business_key == f"inbound-coa:{email_row.id}",
                Outbox.status != DeliveryStatus.CANCELLED,
            )
        )
        completed_at = datetime.now(UTC)
        if isinstance(prepared, dict):
            request.status = AssistanceStatus.APPLIED
            request.applied_at = completed_at
            run.status = AgentRunStatus.WAITING_HUMAN
            run.current_step = "approve-coa-draft"
            run.last_error = None
            run.context_json = {
                **(run.context_json or {}),
                "coa_path": prepared.get("path"),
                "coa_sha256": prepared.get("sha256"),
            }
            step.status = AgentStepStatus.COMPLETED
            step.output_json = {
                "prepared_coa": {
                    "path": prepared.get("path"),
                    "sha256": prepared.get("sha256"),
                },
                "next_step": "human-draft-approval",
            }
            step.completed_at = completed_at
            await audit(
                session,
                "agent.coa_draft_prepared_after_assistance",
                case_id=case.id,
                actor="agent-runtime",
                data={
                    "agent_run_id": run.id,
                    "run_version": run.version,
                    "assistance_request_id": request.id,
                    "coa_path": prepared.get("path"),
                    "coa_sha256": prepared.get("sha256"),
                },
            )
            await session.commit()
            return
        if outbox is not None:
            request.status = AssistanceStatus.APPLIED
            request.applied_at = completed_at
            handoff.status = "RESOLVED"
            handoff.resolution_note = f"Agent resumed COA lookup; outbox_id={outbox.id}"
            run.status = AgentRunStatus.COMPLETED
            run.current_step = "completed"
            run.completed_at = completed_at
            step.status = AgentStepStatus.COMPLETED
            step.output_json = {"outbox_id": outbox.id}
            step.completed_at = completed_at
            await session.commit()
            return
        await block("The corrected product or CAS still has no unique standard English COA")
        return

    category_id = int(request.answer_json.get("category_id") or 0)
    category = await session.get(ProductCategory, category_id)
    if category is None or not category.active:
        await block("The selected product category is missing or inactive")
        return

    if case is None:
        contact_id = int((handoff.extracted_facts or {}).get("contact_id") or 0)
        contact = await session.scalar(
            select(Contact)
            .options(selectinload(Contact.customer))
            .where(Contact.id == contact_id)
        )
        if (
            contact is None
            or email_row.from_address.casefold() != contact.email.casefold()
        ):
            await block("The source sender cannot be safely linked to one customer contact")
            return
        currency_rows = await session.execute(
            select(SalesCase.currency).where(
                SalesCase.customer_id == contact.customer_id,
                SalesCase.status.not_in(
                    [CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST]
                ),
            )
        )
        currencies = set(currency_rows.scalars().all())
        currency = next(iter(currencies)) if len(currencies) == 1 else "USD"
        case = SalesCase(
            customer_id=contact.customer_id,
            contact_id=contact.id,
            product_id=None,
            category_id=category.id,
            currency=currency,
            stage=CaseStage.QUOTING,
            status=CaseStatus.ACTIVE,
            subject_key=normalized_subject(email_row.subject)[:255],
        )
        session.add(case)
        await session.flush()
        case.customer = contact.customer
        case.contact = contact
        case.product = None
        email_row.case_id = case.id
        email_row.customer_id = case.customer_id
        email_row.contact_id = case.contact_id
        handoff.case_id = case.id
        run.case_id = case.id

    try:
        analysis = InboundAnalysis.model_validate(handoff.extracted_facts)
        analysis_metadata = None
    except Exception:
        try:
            analysis, analysis_metadata = await AIClient().analyze(
                email_row.subject,
                email_row.body_text,
                email_row.attachment_metadata,
            )
        except Exception as exc:
            await block(f"Inbound analysis failed while resuming: {type(exc).__name__}")
            return
        session.add(
            AIInvocation(
                case_id=case.id,
                provider=analysis_metadata["provider"],
                model=analysis_metadata["model"],
                purpose="agent_resume_inbound_analysis",
                request_hash=analysis_metadata["request_hash"],
                parsed_output=analysis.model_dump(mode="json"),
                success=True,
                input_tokens=analysis_metadata.get("input_tokens"),
                output_tokens=analysis_metadata.get("output_tokens"),
            )
        )
    if analysis.intent != Intent.PRODUCT_LIST_REQUEST:
        await block("The stored inbound intent is no longer a product-list request")
        return

    run.status = AgentRunStatus.RUNNING
    run.current_step = "generate-product-list"
    run.last_error = None
    case.category_id = category.id
    case.category = category
    case.status = CaseStatus.ACTIVE
    resume_facts = {
        **(handoff.extracted_facts or {}),
        **analysis.model_dump(mode="json"),
        "human_selected_category": {
            "category_id": category.id,
            "category_key": category.key,
            "category_name": category.name,
            "assistance_request_id": request.id,
            "answered_by": request.answered_by,
        },
    }
    handoff.extracted_facts = resume_facts

    await _maybe_send_product_list(
        session,
        case=case,
        email_row=email_row,
        analysis=analysis,
        analysis_facts=resume_facts,
    )
    outbox = await session.scalar(
        select(Outbox).where(
            Outbox.business_key == f"inbound-product-list:{email_row.id}",
            Outbox.status != DeliveryStatus.CANCELLED,
        )
    )
    prepared_product_list = (handoff.extracted_facts or {}).get(
        "prepared_product_list"
    )
    if isinstance(prepared_product_list, dict):
        completed_at = datetime.now(UTC)
        request.status = AssistanceStatus.APPLIED
        request.applied_at = completed_at
        run.status = AgentRunStatus.WAITING_HUMAN
        run.current_step = "approve-product-list-draft"
        run.last_error = None
        run.context_json = {
            **(run.context_json or {}),
            "category_id": category.id,
            "category_key": category.key,
            "prepared_product_ids": prepared_product_list.get("product_ids") or [],
        }
        step.status = AgentStepStatus.COMPLETED
        step.output_json = {
            "category_id": category.id,
            "category_key": category.key,
            "next_step": "human-draft-approval",
        }
        step.completed_at = completed_at
        await audit(
            session,
            "agent.product_list_draft_prepared_after_assistance",
            case_id=case.id,
            actor="agent-runtime",
            data={
                "agent_run_id": run.id,
                "run_version": run.version,
                "assistance_request_id": request.id,
                "category_id": category.id,
                "category_key": category.key,
                "product_count": len(prepared_product_list.get("product_ids") or []),
            },
        )
        await session.commit()
        return
    if outbox is None:
        await block(
            "The category was applied, but current safety policy still requires human review"
        )
        return

    completed_at = datetime.now(UTC)
    request.status = AssistanceStatus.APPLIED
    request.applied_at = completed_at
    handoff.status = "RESOLVED"
    handoff.resolution_note = (
        f"Agent resumed after {request.answered_by or 'human'} selected "
        f"{category.name}; outbox_id={outbox.id}"
    )
    if handoff.dingtalk_status != "SENT":
        handoff.dingtalk_status = "CANCELLED"
    run.status = AgentRunStatus.COMPLETED
    run.current_step = "completed"
    run.last_error = None
    run.completed_at = completed_at
    run.context_json = {
        **(run.context_json or {}),
        "category_id": category.id,
        "category_key": category.key,
        "outbox_id": outbox.id,
    }
    step.status = AgentStepStatus.COMPLETED
    step.output_json = {
        "category_id": category.id,
        "category_key": category.key,
        "outbox_id": outbox.id,
        "outbox_status": outbox.status.value,
    }
    step.completed_at = completed_at
    await audit(
        session,
        "agent.run_completed",
        case_id=case.id,
        actor="agent-runtime",
        data={
            "agent_run_id": run.id,
            "run_version": run.version,
            "assistance_request_id": request.id,
            "category_id": category.id,
            "category_key": category.key,
            "outbox_id": outbox.id,
        },
    )
    await session.commit()


async def notify_handoff(session: AsyncSession, handoff_id: int) -> None:
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None or handoff.dingtalk_status == "SENT":
        return
    if handoff.status != "OPEN":
        handoff.dingtalk_status = "CANCELLED"
        await session.commit()
        return
    case = await session.get(SalesCase, handoff.case_id) if handoff.case_id else None
    try:
        handoff.dingtalk_status = await DingTalkNotifier().notify(handoff, case)
    except Exception as exc:
        handoff.dingtalk_status = "FAILED"
        raise RuntimeError(str(exc)) from exc
    finally:
        await session.commit()


async def notify_commercial_refresh(session: AsyncSession, cycle_id: int) -> None:
    cycle = await session.get(CommercialDataCycle, cycle_id)
    if cycle is None or cycle.reminder_status in {"SENT", "LOGGED", "NOT_REQUIRED"}:
        return
    if cycle.price_status == "CONFIRMED" and cycle.inventory_status == "CONFIRMED":
        cycle.reminder_status = "NOT_REQUIRED"
        await session.commit()
        return
    try:
        cycle.reminder_status = await DingTalkNotifier().notify_commercial_refresh(cycle)
        cycle.reminder_sent_at = datetime.now(UTC)
    except Exception as exc:
        cycle.reminder_status = "FAILED"
        raise RuntimeError(str(exc)) from exc
    finally:
        await session.commit()


def _message_activity_key(source: str, row_id: int, message_id: str | None) -> str:
    normalized = (message_id or "").strip().lower()
    return f"message-id:{normalized}" if normalized else f"{source}:{row_id}"


async def _mailbox_sent_events_since(
    session: AsyncSession,
    mailbox: str,
    since: datetime,
    until: datetime,
) -> dict[str, datetime]:
    events: dict[str, datetime] = {}
    email_rows = await session.execute(
        select(EmailMessage.id, EmailMessage.message_id, EmailMessage.received_at).where(
            EmailMessage.mailbox == mailbox,
            EmailMessage.direction == "OUTBOUND",
            EmailMessage.received_at >= since,
            EmailMessage.received_at <= until,
        )
    )
    for row_id, message_id, occurred_at in email_rows:
        key = _message_activity_key("email", row_id, message_id)
        events[key] = max(events.get(key, occurred_at), occurred_at)

    outbox_rows = await session.execute(
        select(Outbox.id, Outbox.message_id, Outbox.sent_at).where(
            Outbox.sent_via == "smtp",
            Outbox.sent_at >= since,
            Outbox.sent_at <= until,
        )
    )
    for row_id, message_id, sent_at in outbox_rows:
        if sent_at is None:
            continue
        key = _message_activity_key("outbox", row_id, message_id)
        events[key] = max(events.get(key, sent_at), sent_at)
    return events


def _send_interval_seconds(settings: Settings, message_id: str) -> int:
    if settings.send_interval_jitter_seconds == 0:
        return settings.min_send_interval_seconds
    digest = hashlib.sha256(message_id.encode("utf-8")).digest()
    jitter = int.from_bytes(digest[:4], "big") % (settings.send_interval_jitter_seconds + 1)
    return settings.min_send_interval_seconds + jitter


def _smtp_rate_limit_cooldown_seconds(exc: smtplib.SMTPResponseException, settings: Settings) -> int | None:
    detail = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
    normalized = detail.lower()
    daily_markers = ("5.4.5", "daily user sending limit", "daily smtp", "daily limit")
    rate_markers = ("4.7.28", "rate limit", "too many", "quota", "temporarily deferred")
    if any(marker in normalized for marker in daily_markers):
        return settings.gmail_daily_cooldown_seconds
    if exc.smtp_code in {550, 554} and ("limit" in normalized or "quota" in normalized):
        return settings.gmail_daily_cooldown_seconds
    if 400 <= exc.smtp_code < 500 or any(marker in normalized for marker in rate_markers):
        return settings.gmail_transient_cooldown_seconds
    return None


async def _set_mailbox_cooldown(
    session: AsyncSession,
    mailbox: str,
    cooldown_until: datetime,
    reason: str,
) -> None:
    throttle = await session.get(MailboxThrottle, mailbox, with_for_update=True)
    if throttle is None:
        session.add(
            MailboxThrottle(
                mailbox=mailbox,
                cooldown_until=cooldown_until,
                reason=reason,
            )
        )
        return
    if throttle.cooldown_until is None or throttle.cooldown_until < cooldown_until:
        throttle.cooldown_until = cooldown_until
        throttle.reason = reason
    throttle.updated_at = datetime.now(UTC)


async def _cancel_and_requeue_stale_quote(
    session: AsyncSession,
    *,
    row: Outbox,
    quote: Quote | None,
    cycle: CommercialDataCycle,
    reason: str,
) -> None:
    """Cancel immutable old quote mail and create one cycle-scoped reprice job."""

    row.status = DeliveryStatus.CANCELLED
    row.last_error = f"commercial data gate cancelled frozen quote: {reason}"[:2000]
    await session.execute(
        delete(EmailMessage).where(
            EmailMessage.direction == "OUTBOUND",
            EmailMessage.message_id == row.message_id,
            EmailMessage.is_history.is_(False),
        )
    )
    await audit(
        session,
        "outbox.cancelled_stale_commercial_data",
        case_id=row.case_id,
        actor="commercial_gate",
        data={
            "outbox_id": row.id,
            "quote_id": quote.id if quote else None,
            "old_cycle_id": quote.commercial_cycle_id if quote else None,
            "new_cycle_id": cycle.id,
            "reason": reason,
        },
    )
    inbound_match = re.fullmatch(r"inbound-reply:(\d+)(?::quote:\d+)?", row.business_key)
    initial_match = re.fullmatch(r"initial-quote:case:(\d+)(?::cycle:\d+)?", row.business_key)
    if inbound_match:
        email_id = int(inbound_match.group(1))
        await enqueue_job(
            session,
            "process_inbound",
            {"email_id": email_id, "reprice": True},
            f"commercial-reprice:inbound:{email_id}:cycle:{cycle.id}",
        )
        return
    if initial_match and row.case_id is not None:
        await enqueue_job(
            session,
            "case_outreach",
            {
                "case_id": row.case_id,
                "quantity": quote.quantity if quote is not None else 1,
                "reprice": True,
            },
            f"commercial-reprice:case:{row.case_id}:cycle:{cycle.id}",
        )
        return
    await session.commit()


async def send_one_outbox(
    session: AsyncSession,
    settings: Settings | None = None,
    *,
    at: datetime | None = None,
) -> bool:
    settings = settings or get_settings()
    now = at or datetime.now(UTC)
    stale_before = now - timedelta(seconds=settings.outbox_lease_seconds)
    row = await session.scalar(
        select(Outbox)
        .where(
            or_(
                Outbox.status.in_([DeliveryStatus.PENDING, DeliveryStatus.FAILED]),
                and_(Outbox.status == DeliveryStatus.CLAIMED, Outbox.locked_at < stale_before),
            ),
            Outbox.available_at <= now,
        )
        # Live replies, quotations, and human-approved mail always stay ahead
        # of bulk reactivation messages, regardless of creation order.
        .order_by(
            sa_case((Outbox.message_kind == "REACTIVATION", 1), else_=0),
            Outbox.id,
        )
        .with_for_update(skip_locked=True)
    )
    if row is None:
        return False
    if row.attempts >= 5:
        row.status = DeliveryStatus.CANCELLED
        row.last_error = "outbox retry limit exhausted"
        await session.commit()
        failed_case = await session.get(SalesCase, row.case_id) if row.case_id else None
        if failed_case:
            await create_handoff(
                session,
                case=failed_case,
                reason=HandoffReason.MAIL_FAILURE,
                summary=f"Outbound delivery exhausted retries for {row.message_id}",
            )
        return True
    reclaimed_claim = row.status == DeliveryStatus.CLAIMED
    if reclaimed_claim and settings.mail_transport == "smtp":
        row.status = DeliveryStatus.UNKNOWN
        row.last_error = "stale SMTP claim requires Sent-folder reconciliation"
        await session.commit()
        return True
    mailbox = (settings.gmail_address or parseaddr(settings.mail_from)[1]).lower()
    if settings.mail_transport == "smtp":
        throttle = await session.get(MailboxThrottle, mailbox)
        if throttle and throttle.cooldown_until and throttle.cooldown_until > now:
            row.status = DeliveryStatus.PENDING
            row.available_at = throttle.cooldown_until
            row.last_error = f"mailbox cooldown active: {throttle.reason or 'Gmail rate limit'}"[:2000]
            await session.commit()
            return True
    case: SalesCase | None = None
    human_approved = _human_approval_from_outbox(row) is not None
    is_forward = row.message_kind == "FORWARD"
    authorized_forward = False
    if is_forward:
        try:
            normalized_forward_recipient = _normalize_forward_recipient(row.recipient)
        except ValueError as exc:
            row.status = DeliveryStatus.CANCELLED
            row.last_error = f"forward authorization failed: {exc}"[:2000]
            await session.commit()
            return True
        if not human_approved or normalized_forward_recipient != row.recipient:
            row.status = DeliveryStatus.CANCELLED
            row.last_error = "forward requires complete human approval metadata"
            await session.commit()
            return True
        authorized_forward = True
    internal_message = human_approved
    if row.message_kind == "REACTIVATION":
        guard = await reactivation_send_guard(session, row, settings=settings, at=now)
        if guard.action == "DEFER":
            row.status = DeliveryStatus.PENDING
            row.available_at = guard.available_at or (now + timedelta(minutes=15))
            row.last_error = guard.reason
            await session.commit()
            return True
        if guard.action == "BLOCK":
            row.status = DeliveryStatus.CANCELLED
            row.last_error = guard.reason
            await session.commit()
            return True
    if (
        settings.commercial_gate_enabled
        and not settings.demo_mode
        and not internal_message
        and not is_business_day(settings, now)
    ):
        row.status = DeliveryStatus.PENDING
        row.available_at = next_business_open(settings, now)
        row.last_error = "commercial gate deferred automated mail until Monday"
        await session.commit()
        return True
    if row.case_id:
        case = await session.scalar(
            select(SalesCase)
            .options(
                selectinload(SalesCase.customer),
                selectinload(SalesCase.contact),
            )
            .where(SalesCase.id == row.case_id)
        )
        customer_ineligible = bool(
            not authorized_forward
            and case is not None
            and (
                case.contact.suppressed
                or case.customer.do_not_contact
                or case.contact.email.lower() != row.recipient.lower()
                or (
                    human_approved
                    and case.status in {CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST}
                )
                or (
                    not human_approved
                    and (
                        case.status != CaseStatus.ACTIVE
                        or not case.customer.auto_send_allowed
                    )
                )
            )
        )
        if case is None or customer_ineligible:
            row.status = DeliveryStatus.CANCELLED
            row.last_error = "case/contact eligibility changed after message was queued"
            await session.commit()
            return True
    is_auto_quote = not human_approved and (
        row.message_kind == "AUTO_QUOTE" or row.quote_id is not None
    )
    if settings.commercial_gate_enabled and not settings.demo_mode and is_auto_quote:
        quote = await session.get(Quote, row.quote_id) if row.quote_id is not None else None
        if case is None:
            row.status = DeliveryStatus.CANCELLED
            row.last_error = "commercial gate could not resolve the quote case"
            await session.commit()
            return True
        if case.product_id is None:
            row.status = DeliveryStatus.CANCELLED
            row.last_error = "commercial gate blocked a quote whose case product is pending"
            await session.commit()
            return True
        await lock_commercial_scope(session, settings.commercial_scope)
        context = await get_commercial_data_provider(settings).get_quote_context(
            session,
            product_id=case.product_id,
            currency=case.currency,
            requested_quantity=quote.quantity if quote is not None else None,
            at=now,
        )
        same_frozen_version = bool(
            quote is not None
            and quote.commercial_cycle_id == context.cycle.id
            and context.policy is not None
            and quote.price_policy_id == context.policy.id
        )
        if context.status is QuoteContextStatus.WAITING and same_frozen_version:
            await ensure_weekly_commercial_refresh(session, settings, at=now)
            row.status = DeliveryStatus.PENDING
            row.available_at = context.next_check_at or (
                now + timedelta(minutes=settings.commercial_retry_minutes)
            )
            row.last_error = f"commercial data gate waiting: {context.reason}"[:2000]
            await session.commit()
            return True
        if context.status is not QuoteContextStatus.AVAILABLE or not same_frozen_version:
            await _cancel_and_requeue_stale_quote(
                session,
                row=row,
                quote=quote,
                cycle=context.cycle,
                reason=(
                    context.reason
                    if context.status is not QuoteContextStatus.AVAILABLE
                    else "frozen quote belongs to an older commercial data version"
                ),
            )
            return True
    if settings.mail_transport == "smtp":
        recipient = row.recipient.lower()
        if settings.safe_mode and recipient not in settings.recipient_allowlist:
            row.status = DeliveryStatus.CANCELLED
            row.last_error = "SAFE_MODE blocked recipient not on allowlist"
            await audit(
                session,
                "outbox.blocked_safe_mode",
                case_id=row.case_id,
                actor="policy",
                data={"recipient": recipient},
            )
            await session.commit()
            return True
        if not settings.auto_send_enabled and not human_approved:
            row.status = DeliveryStatus.CANCELLED
            row.last_error = "AUTO_SEND_ENABLED is false"
            await session.commit()
            return True
        preflight_outcome, preflight_detail, preflight_facts = await _recipient_preflight(
            session,
            recipient,
            settings,
        )
        if preflight_outcome == "DEFER":
            row.status = DeliveryStatus.PENDING
            row.available_at = now + timedelta(minutes=settings.mx_temporary_retry_minutes)
            row.last_error = f"recipient preflight deferred: {preflight_detail}"[:2000]
            await audit(
                session,
                "outbox.preflight_deferred",
                case_id=row.case_id,
                actor="dns",
                data={"outbox_id": row.id, **preflight_facts},
            )
            await session.commit()
            return True
        if preflight_outcome == "BLOCK":
            row.status = DeliveryStatus.CANCELLED
            row.last_error = f"recipient preflight blocked: {preflight_detail}"[:2000]
            auto_suppressed = bool(preflight_facts.get("auto_suppressed"))
            if auto_suppressed and case is not None and case.status not in {
                CaseStatus.CLOSED_WON,
                CaseStatus.CLOSED_LOST,
            }:
                case.status = CaseStatus.PAUSED
            await audit(
                session,
                "outbox.preflight_blocked",
                case_id=row.case_id,
                actor="policy",
                data={"outbox_id": row.id, **preflight_facts},
            )
            await session.commit()
            if case is not None and not auto_suppressed:
                await create_handoff(
                    session,
                    case=case,
                    reason=HandoffReason.EMAIL_DELIVERABILITY,
                    summary=f"Recipient preflight blocked {recipient}: {preflight_detail}",
                    facts={"outbox_id": row.id, **preflight_facts},
                )
            return True
        await audit(
            session,
            "outbox.preflight_passed",
            case_id=row.case_id,
            actor="dns",
            data={"outbox_id": row.id, **preflight_facts},
        )
        since_hour = now - timedelta(hours=1)
        since_day = now - timedelta(days=1)
        sent_events = await _mailbox_sent_events_since(session, mailbox, since_day, now)
        hourly_events = {key: value for key, value in sent_events.items() if value >= since_hour}
        if len(hourly_events) >= settings.max_sends_per_hour:
            row.status = DeliveryStatus.PENDING
            row.available_at = min(hourly_events.values()) + timedelta(hours=1)
            row.last_error = "mailbox-wide hourly send limit deferred message"
            await session.commit()
            return True
        if len(sent_events) >= settings.max_sends_per_day:
            row.status = DeliveryStatus.PENDING
            row.available_at = min(sent_events.values()) + timedelta(days=1)
            row.last_error = "mailbox-wide rolling 24-hour send limit deferred message"
            await session.commit()
            return True
        if sent_events:
            last_sent_at = max(sent_events.values())
            next_send_at = last_sent_at + timedelta(seconds=_send_interval_seconds(settings, row.message_id))
            if next_send_at > now:
                row.status = DeliveryStatus.PENDING
                row.available_at = next_send_at
                row.last_error = "mailbox-wide send spacing deferred message"
                await session.commit()
                return True
    row.status = DeliveryStatus.CLAIMED
    row.locked_at = datetime.now(UTC)
    row.attempts += 1
    await session.commit()
    if not await _final_recipient_delivery_guard(
        session,
        row,
        settings=settings,
        at=now,
    ):
        return True
    try:
        transport_for(settings).send(row.raw_message, row.message_id, row.recipient)
        row.status = DeliveryStatus.SENT
        row.sent_at = datetime.now(UTC)
        row.sent_via = settings.mail_transport
        row.last_error = None
        await audit(
            session,
            "outbox.sent",
            case_id=row.case_id,
            actor=settings.mail_transport,
            data={
                "outbox_id": row.id,
                "message_id": row.message_id,
                "approval_handoff_id": row.approval_handoff_id,
                "human_approved_by": row.human_approved_by,
            },
        )
    except (smtplib.SMTPServerDisconnected, ConnectionResetError, TimeoutError) as exc:
        row.status = DeliveryStatus.UNKNOWN
        row.last_error = f"ambiguous transport outcome: {exc}"
    except smtplib.SMTPResponseException as exc:
        cooldown_seconds = _smtp_rate_limit_cooldown_seconds(exc, settings)
        detail = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        if cooldown_seconds is None:
            failure_type = classify_smtp_failure(exc.smtp_code, detail)
            if failure_type == BounceType.HARD:
                row.status = DeliveryStatus.CANCELLED
                row.last_error = f"permanent SMTP recipient failure {exc.smtp_code}: {detail}"[:2000]
                await _suppress_email_address(
                    session,
                    row.recipient,
                    reason="SMTP_HARD_BOUNCE",
                    bounce_type=failure_type.value,
                    diagnostic=detail,
                )
                if case and case.status == CaseStatus.ACTIVE:
                    case.status = CaseStatus.PAUSED
                await audit(
                    session,
                    "outbox.smtp_hard_bounce_suppressed",
                    case_id=row.case_id,
                    actor="smtp",
                    data={"outbox_id": row.id, "smtp_code": exc.smtp_code, "diagnostic": detail[:2000]},
                )
            else:
                row.status = DeliveryStatus.FAILED
                row.last_error = f"SMTP {exc.smtp_code}: {detail}"[:2000]
                row.available_at = datetime.now(UTC) + timedelta(minutes=min(60, 2**row.attempts))
        else:
            cooldown_until = datetime.now(UTC) + timedelta(seconds=cooldown_seconds)
            reason = f"Gmail SMTP {exc.smtp_code}: {detail}"[:2000]
            await _set_mailbox_cooldown(session, mailbox, cooldown_until, reason)
            row.status = DeliveryStatus.PENDING
            row.attempts = max(0, row.attempts - 1)
            row.available_at = cooldown_until
            row.last_error = reason
            await audit(
                session,
                "outbox.gmail_cooldown",
                case_id=row.case_id,
                actor="smtp",
                data={"outbox_id": row.id, "smtp_code": exc.smtp_code, "cooldown_seconds": cooldown_seconds},
            )
    except Exception as exc:
        row.status = DeliveryStatus.FAILED
        row.last_error = str(exc)[:2000]
        row.available_at = datetime.now(UTC) + timedelta(minutes=min(60, 2**row.attempts))
    await session.commit()
    return True


async def reconcile_unknown_outbox(session: AsyncSession, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    if settings.mail_transport != "smtp" or not (settings.gmail_address and settings.gmail_app_password):
        return False
    row = await session.scalar(
        select(Outbox)
        .where(
            Outbox.status == DeliveryStatus.UNKNOWN,
            Outbox.locked_at < datetime.now(UTC) - timedelta(minutes=10),
        )
        .order_by(Outbox.id)
        .with_for_update(skip_locked=True)
    )
    if row is None:
        return False
    try:
        found = await asyncio.to_thread(GmailIMAPClient(settings).sent_contains_message_id, row.message_id)
    except Exception as exc:
        # Keep an ambiguous delivery in UNKNOWN until Gmail Sent can be checked.
        # Retrying SMTP before reconciliation could deliver a duplicate message.
        row.locked_at = datetime.now(UTC)
        row.last_error = f"Gmail Sent reconciliation deferred: {type(exc).__name__}: {exc}"[:2000]
        await session.commit()
        logger.exception("outbox %s reconciliation failed", row.id)
        return True
    if found:
        row.status = DeliveryStatus.SENT
        row.sent_at = datetime.now(UTC)
        row.sent_via = "smtp"
        row.last_error = None
        await audit(
            session,
            "outbox.reconciled_sent",
            case_id=row.case_id,
            actor="gmail_sent",
            data={"outbox_id": row.id, "message_id": row.message_id},
        )
    else:
        row.status = DeliveryStatus.FAILED
        row.available_at = datetime.now(UTC)
        row.last_error = "Gmail Sent confirmed Message-ID absent; retry permitted"
    await session.commit()
    return True


JOB_HANDLERS = {
    "demo_outreach": lambda session, payload: create_demo_outreach(session, payload),
    "case_outreach": lambda session, payload: create_case_outreach(session, payload),
    "process_inbound": lambda session, payload: process_inbound(session, int(payload["email_id"])),
    "resume_agent_run": lambda session, payload: resume_agent_run(
        session,
        run_id=int(payload["run_id"]),
        expected_version=int(payload["run_version"]),
        assistance_request_id=int(payload["assistance_request_id"]),
    ),
    "notify_handoff": lambda session, payload: notify_handoff(session, int(payload["handoff_id"])),
    "notify_commercial_refresh": lambda session, payload: notify_commercial_refresh(
        session, int(payload["cycle_id"])
    ),
}


async def claim_and_run_job(
    session: AsyncSession,
    worker_id: str,
    settings: Settings | None = None,
) -> bool:
    settings = settings or get_settings()
    stale_before = datetime.now(UTC) - timedelta(seconds=settings.job_lease_seconds)
    job = await session.scalar(
        select(Job)
        .where(
            or_(
                Job.status == JobStatus.PENDING,
                and_(Job.status == JobStatus.RUNNING, Job.locked_at < stale_before),
            ),
            Job.available_at <= datetime.now(UTC),
        )
        .order_by(Job.id)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return False
    job.status = JobStatus.RUNNING
    job.locked_at = datetime.now(UTC)
    job.locked_by = worker_id
    job.attempts += 1
    await session.commit()
    job_id = job.id
    try:
        handler = JOB_HANDLERS[job.kind]
        await handler(session, job.payload)
        job.status = JobStatus.DONE
        job.last_error = None
        job.locked_at = None
        job.locked_by = None
        job.updated_at = datetime.now(UTC)
        await session.commit()
    except JobDeferred as exc:
        await session.rollback()
        job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if job is None:
            raise RuntimeError(f"claimed job {job_id} disappeared") from exc
        job.status = JobStatus.PENDING
        job.attempts = max(0, job.attempts - 1)
        job.available_at = exc.available_at
        job.locked_at = None
        job.locked_by = None
        job.last_error = f"DEFERRED: {exc.reason}"[:2000]
        job.updated_at = datetime.now(UTC)
        await session.commit()
    except asyncio.CancelledError:
        # Cancellation must not leave handler state pending in a reusable
        # session or hold the durable job lease until it expires.
        await session.rollback()
        job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if job is not None:
            job.status = JobStatus.PENDING
            job.attempts = max(0, job.attempts - 1)
            job.available_at = datetime.now(UTC)
            job.locked_at = None
            job.locked_by = None
            job.last_error = "CANCELLED: worker task was interrupted"
            job.updated_at = datetime.now(UTC)
            await session.commit()
        raise
    except Exception as exc:
        logger.exception("job %s failed", job_id)
        error = f"{type(exc).__name__}: {exc}"[:2000]
        # Discard every uncommitted handler mutation before recording retry
        # bookkeeping. Otherwise a failed draft can leave an orphan quote or
        # consume a negotiation round without an outbound message.
        await session.rollback()
        job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if job is None:
            raise RuntimeError(f"claimed job {job_id} disappeared") from exc
        job.last_error = error
        if job.attempts >= job.max_attempts:
            job.status = JobStatus.FAILED
        else:
            job.status = JobStatus.PENDING
            job.available_at = datetime.now(UTC) + timedelta(seconds=min(300, 2**job.attempts))
        job.locked_at = None
        job.locked_by = None
        job.updated_at = datetime.now(UTC)
        await session.commit()
    return True
