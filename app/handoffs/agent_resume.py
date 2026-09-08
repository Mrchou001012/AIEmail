"""Agent resume workflow."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import app.coa.coa_requests as coa_requests
from app.ai import AIClient, InboundAnalysis
from app.catalogs.category_product_list import _maybe_send_product_list
from app.coa.coa_service import _maybe_handle_coa_request
from app.common.service_core import _atomic_business_operation, audit
from app.db import (
    AgentRun,
    AgentRunStatus,
    AgentStep,
    AgentStepStatus,
    AIInvocation,
    AssistanceRequest,
    AssistanceStatus,
    CaseStage,
    CaseStatus,
    Contact,
    DeliveryStatus,
    EmailMessage,
    Handoff,
    Outbox,
    ProductCategory,
    SalesCase,
)
from app.domain import Intent
from app.handoffs.agent_runtime import COA_LOOKUP_REQUEST_TYPE, ensure_handoff_agent_run
from app.mail import normalized_subject


@_atomic_business_operation
async def resume_agent_run(
    session: AsyncSession,
    *,
    run_id: int,
    expected_version: int,
    assistance_request_id: int,
) -> None:
    """Continue a paused product-catalog task after typed human assistance."""

    run = await session.scalar(select(AgentRun).where(AgentRun.id == run_id).with_for_update())
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
        sequence = int(await session.scalar(select(func.max(AgentStep.sequence)).where(AgentStep.run_id == run.id)) or 0) + 1
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

    if request is None or request.status != AssistanceStatus.ANSWERED or not request.answer_json:
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
                "product_code": None,
                "requested_product_name": corrected_query,
                "requested_cas_number": corrected_cas,
                "product_requests": [],
                "missing_fields": [field for field in analysis.missing_fields if field != "requested_product_name"],
            }
        )
        run.status = AgentRunStatus.RUNNING
        run.current_step = "recheck-coa-catalog"
        run.last_error = None
        case.status = CaseStatus.ACTIVE
        pending_before_retry = [
            str(item).strip() for item in ((handoff.extracted_facts or {}).get("missing_coa_queries") or []) if str(item).strip()
        ]
        retry_original_query = pending_before_retry[0] if pending_before_retry else corrected_query
        resume_facts = {
            **(handoff.extracted_facts or {}),
            **corrected_analysis.model_dump(mode="json"),
            "human_corrected_coa_lookup": {
                "product_query": corrected_query,
                "cas_number": corrected_cas,
                "assistance_request_id": request.id,
                "answered_by": request.answered_by,
            },
            "coa_retry_original_query": retry_original_query,
        }
        handoff.extracted_facts = resume_facts
        expected_outbox_key = coa_requests.coa_outbox_business_key(
            email_id=email_row.id,
            product_queries=[corrected_query],
            followup=bool(resume_facts.get("partial_coa_outbox_id")),
        )
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
                Outbox.business_key == expected_outbox_key,
                Outbox.status != DeliveryStatus.CANCELLED,
            )
        )
        remaining_missing = [
            str(item).strip() for item in ((handoff.extracted_facts or {}).get("missing_coa_queries") or []) if str(item).strip()
        ]
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
        if outbox is not None and remaining_missing:
            request.status = AssistanceStatus.APPLIED
            request.applied_at = completed_at
            step.status = AgentStepStatus.COMPLETED
            step.output_json = {
                "outbox_id": outbox.id,
                "remaining_missing_coa_queries": remaining_missing,
                "next_step": "human-coa-assistance",
            }
            step.completed_at = completed_at
            run.status = AgentRunStatus.WAITING_HUMAN
            run.current_step = "human_review"
            await ensure_handoff_agent_run(session, handoff=handoff)
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
        contact = await session.scalar(select(Contact).options(selectinload(Contact.customer)).where(Contact.id == contact_id))
        if contact is None or email_row.from_address.casefold() != contact.email.casefold():
            await block("The source sender cannot be safely linked to one customer contact")
            return
        currency_rows = await session.execute(
            select(SalesCase.currency).where(
                SalesCase.customer_id == contact.customer_id,
                SalesCase.status.not_in([CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST]),
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
    prepared_product_list = (handoff.extracted_facts or {}).get("prepared_product_list")
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
        await block("The category was applied, but current safety policy still requires human review")
        return

    completed_at = datetime.now(UTC)
    request.status = AssistanceStatus.APPLIED
    request.applied_at = completed_at
    handoff.status = "RESOLVED"
    handoff.resolution_note = f"Agent resumed after {request.answered_by or 'human'} selected {category.name}; outbox_id={outbox.id}"
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
