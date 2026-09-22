"""Inbound processing workflow."""

import logging

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import AIClient
from app.catalogs.category_product_list import _maybe_send_product_list
from app.catalogs.general_product_list import _maybe_send_general_product_list
from app.catalogs.products import product_codes_match
from app.coa.coa_service import _maybe_handle_coa_request
from app.common.service_core import (
    _atomic_business_operation,
    audit,
    create_handoff,
)
from app.db import (
    AIInvocation,
    CaseStatus,
    DeliveryStatus,
    EmailMessage,
    Handoff,
    Outbox,
    SalesCase,
)
from app.dispositions.disposition_service import apply_email_disposition
from app.domain import HandoffReason, Intent, SendContext, evaluate_send_policy
from app.inbound.automated_reply_service import _handle_automated_reply
from app.inbound.bounce_service import _handle_bounce
from app.jobs import enqueue_job
from app.mail import (
    attachments_require_review,
)
from app.quotations.multi_product_quote import _maybe_send_multi_product_quote
from app.quotations.quote_clarification import _maybe_send_quote_clarification
from app.quotations.quote_context import _augment_pending_quote_context
from app.quotations.single_product_quote import prepare_single_product_quote
from app.reactivation import record_reactivation_reply
from app.research.company_research_service import _maybe_research_and_send_product_list
from app.settings import get_settings

logger = logging.getLogger(__name__)


@_atomic_business_operation
async def process_inbound(session: AsyncSession, email_id: int) -> None:
    email_row = await session.get(EmailMessage, email_id)
    if email_row is None:
        return
    if email_row.is_bounce:
        await _handle_bounce(session, email_row)
        return
    if await apply_email_disposition(session, email_row):
        await session.commit()
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
    secondary_coa_request = analysis.intent != Intent.COA_REQUEST and analysis.coa_requested
    if await _maybe_handle_coa_request(
        session,
        case=case,
        email_row=email_row,
        analysis=analysis,
        analysis_facts=analysis_facts,
    ):
        return
    if secondary_coa_request:
        # The verified COA has its own durable outbox row. Keep the remaining
        # quote/product-list workflow independent and avoid attaching it twice.
        analysis = analysis.model_copy(update={"coa_requested": False})
    multi_product_request = len([line for line in analysis.product_requests if line.product_code]) >= 2
    if case.product_id is None and analysis.intent == Intent.QUOTE_REQUEST and not multi_product_request:
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
    if analysis.intent == Intent.PRODUCT_LIST_REQUEST and await _maybe_send_general_product_list(
        session,
        case=case,
        email_row=email_row,
        analysis=analysis,
        analysis_facts=analysis_facts,
    ):
        return
    if case.product_id is None or case.product is None:
        if case.category_id is not None and analysis.intent == Intent.PRODUCT_LIST_REQUEST:
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
                HandoffReason.PRODUCT_CATEGORY_REVIEW if analysis.intent == Intent.PRODUCT_LIST_REQUEST else HandoffReason.HUMAN_CONTROL
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
    await prepare_single_product_quote(
        session,
        case=case,
        email_row=email_row,
        analysis=analysis,
        analysis_facts=analysis_facts,
        settings=settings,
        ai=ai,
        metadata=metadata,
    )
