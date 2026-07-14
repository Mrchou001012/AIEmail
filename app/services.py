import asyncio
import hashlib
import html
import logging
import smtplib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from email.utils import parseaddr
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.ai import AIClient, validate_rendered_email
from app.db import (
    AIInvocation,
    AuditEvent,
    CaseStage,
    CaseStatus,
    Contact,
    Customer,
    DeliveryStatus,
    EmailMessage,
    Handoff,
    Job,
    JobStatus,
    MailboxThrottle,
    Outbox,
    PricePolicy,
    Product,
    Quote,
    SalesCase,
)
from app.domain import (
    HandoffReason,
    Intent,
    PricingPolicy,
    SendContext,
    counteroffer,
    evaluate_send_policy,
    initial_quote,
    transition,
)
from app.imports import ContentBundle, load_content
from app.integrations import DingTalkNotifier
from app.mail import GmailIMAPClient, build_message, match_case, parse_mime, transport_for
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


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


async def create_handoff(
    session: AsyncSession,
    *,
    case: SalesCase | None,
    reason: HandoffReason,
    summary: str,
    facts: dict[str, Any] | None = None,
    source_email_id: int | None = None,
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
    await enqueue_job(
        session,
        "notify_handoff",
        {"handoff_id": handoff.id},
        f"handoff-notify:{handoff.id}",
    )
    return handoff


async def active_policy(session: AsyncSession, product_id: int, currency: str) -> PricePolicy | None:
    today = date.today()
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
) -> tuple[str, str]:
    snippet = bundle.product_snippets[product_key]
    # Free-form model prose is deliberately not inserted into a commercial email.
    # The structured plan selects tone/snippet IDs; factual language remains local and reviewed.
    safe_greeting = plan.greeting.lower().startswith("dear ") and not any(ch.isdigit() for ch in plan.greeting)
    greeting = plan.greeting if safe_greeting else "Dear Customer,"
    opening = "Thank you for your inquiry."
    price_lead_in = "Please find our standard quotation details below."
    closing = "Please let us know if you have questions about this non-binding standard quotation."
    lines = [
        greeting,
        "",
        opening,
        snippet,
        "",
        price_lead_in,
        f"Product: {product_name}",
        f"Quantity: {quantity} {unit}",
        f"Unit price: {currency} {price:.4f}",
        f"Incoterm: {incoterm}",
        f"Payment term: {payment_term}",
        f"Quote valid until: {valid_until.isoformat()}",
        "",
        closing,
        "",
        bundle.signature_text.strip(),
    ]
    text = "\n".join(lines)
    html_body = "<p>" + "</p><p>".join(html.escape(line) if line else "&nbsp;" for line in lines[:-1]) + "</p>" + bundle.signature_html
    validate_rendered_email(text, exact_price=price, currency=currency, approved_fragments=[snippet])
    return text, html_body


async def freeze_outbox(
    session: AsyncSession,
    *,
    case: SalesCase,
    quote: Quote,
    subject: str,
    text_body: str,
    html_body: str,
    business_key: str,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
) -> Outbox | None:
    message_id, raw = build_message(
        from_address=get_settings().mail_from,
        recipient=case.contact.email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        stable_key=business_key,
        in_reply_to=in_reply_to,
        references=references,
    )
    parsed_outbound = parse_mime(raw.encode("utf-8"))
    try:
        async with session.begin_nested():
            row = Outbox(
                case_id=case.id,
                business_key=business_key,
                message_id=message_id,
                recipient=case.contact.email,
                raw_message=raw,
            )
            session.add(row)
            await session.flush()
            session.add(
                EmailMessage(
                    case_id=case.id,
                    direction="OUTBOUND",
                    message_id=message_id,
                    in_reply_to=in_reply_to,
                    references_json=references or [],
                    from_address=parseaddr(get_settings().mail_from)[1],
                    to_addresses=[case.contact.email],
                    subject=subject,
                    body_text=text_body,
                    body_html=html_body,
                    attachment_metadata=[],
                    raw_sha256=parsed_outbound.raw_sha256,
                )
            )
        await audit(
            session,
            "outbox.frozen",
            case_id=case.id,
            actor="system",
            data={"outbox_id": row.id, "message_id": message_id, "quote_id": quote.id},
        )
        await session.commit()
        return row
    except IntegrityError:
        await session.rollback()
        return None


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
    valid_until = date.today() + timedelta(days=policy_row.quote_valid_days)
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
    )
    await freeze_outbox(
        session,
        case=case,
        quote=quote,
        subject="Industrial Widget 100 quotation",
        text_body=text,
        html_body=html_body,
        business_key=business_key,
    )


async def create_case_outreach(session: AsyncSession, payload: dict[str, Any]) -> None:
    case_id = int(payload["case_id"])
    quantity = int(payload.get("quantity") or 1)
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
    historical_outbound = await session.scalar(
        select(EmailMessage)
        .where(
            EmailMessage.case_id == case.id,
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
    business_key = f"initial-quote:case:{case.id}"
    if await session.scalar(select(Outbox.id).where(Outbox.business_key == business_key)) is not None:
        return
    existing_quote = await session.scalar(select(Quote.id).where(Quote.case_id == case.id).limit(1))
    if existing_quote is not None:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.NONSTANDARD,
            summary="Case already has a quotation but no matching initial-outreach outbox record",
        )
        return
    policy_row = await active_policy(session, case.product_id, case.currency)
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
    valid_until = date.today() + timedelta(days=policy_row.quote_valid_days)
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
        )
    except Exception as exc:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.AI_FAILURE,
            summary=f"Initial outreach drafting failed: {type(exc).__name__}",
        )
        return
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
        },
    )
    session.add(quote)
    await session.flush()
    subject = f"{case.product.name} quotation"
    case.subject_key = subject.lower()
    await freeze_outbox(
        session,
        case=case,
        quote=quote,
        subject=subject,
        text_body=text,
        html_body=html_body,
        business_key=business_key,
    )


async def _ensure_inbound_follow_up(
    session: AsyncSession,
    row: EmailMessage,
    *,
    ambiguous: bool = False,
) -> None:
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
        reason=HandoffReason.THREAD_AMBIGUOUS,
        summary=f"{summary_prefix} from {row.from_address}: {row.subject}",
        source_email_id=row.id,
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
    duplicate_query = select(EmailMessage).where(
        (EmailMessage.raw_sha256 == parsed.raw_sha256)
        | ((EmailMessage.message_id == parsed.message_id) & EmailMessage.message_id.is_not(None))
    )
    duplicate = await session.scalar(duplicate_query)
    if duplicate:
        if direction == "INBOUND" and duplicate.direction == "INBOUND" and not is_history:
            await _ensure_inbound_follow_up(session, duplicate)
        return duplicate
    case, ambiguous = await match_case(session, parsed, direction=direction)
    try:
        async with session.begin_nested():
            row = EmailMessage(
                case_id=case.id if case else None,
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
                received_at=parsed.occurred_at or datetime.now(UTC),
            )
            session.add(row)
            await session.flush()
    except IntegrityError:
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
        },
    )
    await session.commit()
    if direction == "INBOUND" and not is_history:
        await _ensure_inbound_follow_up(session, row, ambiguous=ambiguous)
    return row


async def process_inbound(session: AsyncSession, email_id: int) -> None:
    email_row = await session.get(EmailMessage, email_id)
    if email_row is None or email_row.case_id is None:
        return
    case = await session.get(SalesCase, email_row.case_id)
    if case is None:
        return
    reply_key = f"inbound-reply:{email_row.id}"
    existing_reply = await session.scalar(
        select(Outbox.id).where(
            or_(
                Outbox.business_key == reply_key,
                Outbox.business_key.like(f"{reply_key}:quote:%"),
            )
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
    if analysis.intent not in {Intent.QUOTE_REQUEST, Intent.COUNTEROFFER}:
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
                product_known=analysis.product_code in {case.product.code, None},
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
    latest_quote = await session.scalar(select(Quote).where(Quote.case_id == case.id).order_by(Quote.round_number.desc()))
    policy_row = await active_policy(session, case.product_id, case.currency)
    if policy_row is None or latest_quote is None or latest_quote.currency != case.currency:
        await create_handoff(
            session,
            case=case,
            reason=HandoffReason.NONSTANDARD,
            summary="No standard policy or same-currency prior quote matched the inbound request",
            facts=analysis_facts,
            source_email_id=email_row.id,
        )
        return
    quantity = analysis.quantity or latest_quote.quantity
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
            product_known=analysis.product_code in {case.product.code, None},
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
    if analysis.intent == Intent.COUNTEROFFER:
        if analysis.requested_unit_price is None:
            await create_handoff(
                session,
                case=case,
                reason=HandoffReason.LOW_CONFIDENCE,
                summary="Counteroffer did not contain a reliable requested unit price",
                facts=analysis_facts,
                source_email_id=email_row.id,
            )
            return
        price_decision = counteroffer(
            _pricing_policy(policy_row),
            Decimal(latest_quote.unit_price),
            analysis.requested_unit_price,
            case.negotiation_round,
            quantity,
        )
    else:
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
    valid_until = date.today() + timedelta(days=policy_row.quote_valid_days)
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
    try:
        plan = await ai.draft_plan(
            {
                "subject": email_row.subject,
                "contact_name": case.contact.name,
                "approved_product_key": case.product.approved_text_key,
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
    round_number = latest_quote.round_number + 1
    next_stage = transition(case.stage, CaseStage.NEGOTIATING)
    case.negotiation_round = round_number
    case.stage = next_stage
    quote = Quote(
        case_id=case.id,
        price_policy_id=policy_row.id,
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
            "requested_price": str(analysis.requested_unit_price),
        },
    )
    session.add(quote)
    await session.flush()
    await freeze_outbox(
        session,
        case=case,
        quote=quote,
        subject=f"Re: {email_row.subject}",
        text_body=text,
        html_body=html_body,
        business_key=f"inbound-reply:{email_row.id}",
        in_reply_to=email_row.message_id,
        references=[*email_row.references_json, email_row.message_id] if email_row.message_id else email_row.references_json,
    )


async def notify_handoff(session: AsyncSession, handoff_id: int) -> None:
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None or handoff.dingtalk_status == "SENT":
        return
    case = await session.get(SalesCase, handoff.case_id) if handoff.case_id else None
    try:
        await DingTalkNotifier().notify(handoff, case)
        handoff.dingtalk_status = "SENT"
    except Exception as exc:
        handoff.dingtalk_status = "FAILED"
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


async def send_one_outbox(session: AsyncSession, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    now = datetime.now(UTC)
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
        .order_by(Outbox.id)
        .with_for_update(skip_locked=True)
    )
    if row is None:
        return False
    if row.attempts >= 5:
        row.status = DeliveryStatus.CANCELLED
        row.last_error = "outbox retry limit exhausted"
        await session.commit()
        case = await session.get(SalesCase, row.case_id) if row.case_id else None
        if case:
            await create_handoff(
                session,
                case=case,
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
    if row.case_id:
        case = await session.scalar(
            select(SalesCase)
            .options(
                selectinload(SalesCase.customer),
                selectinload(SalesCase.contact),
            )
            .where(SalesCase.id == row.case_id)
        )
        if (
            case is None
            or case.status != CaseStatus.ACTIVE
            or case.contact.suppressed
            or case.customer.do_not_contact
            or not case.customer.auto_send_allowed
            or case.contact.email.lower() != row.recipient.lower()
        ):
            row.status = DeliveryStatus.CANCELLED
            row.last_error = "case/contact eligibility changed after message was queued"
            await session.commit()
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
        if not settings.auto_send_enabled:
            row.status = DeliveryStatus.CANCELLED
            row.last_error = "AUTO_SEND_ENABLED is false"
            await session.commit()
            return True
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
            data={"outbox_id": row.id, "message_id": row.message_id},
        )
    except (smtplib.SMTPServerDisconnected, ConnectionResetError, TimeoutError) as exc:
        row.status = DeliveryStatus.UNKNOWN
        row.last_error = f"ambiguous transport outcome: {exc}"
    except smtplib.SMTPResponseException as exc:
        cooldown_seconds = _smtp_rate_limit_cooldown_seconds(exc, settings)
        detail = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        if cooldown_seconds is None:
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
    "notify_handoff": lambda session, payload: notify_handoff(session, int(payload["handoff_id"])),
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
        job.updated_at = datetime.now(UTC)
        await session.commit()
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
        job.updated_at = datetime.now(UTC)
        await session.commit()
    return True
