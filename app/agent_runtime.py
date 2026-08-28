"""Durable Agent execution state and typed human-assistance lifecycle.

The runtime deliberately does not send mail or calculate commercial facts. It
records why execution paused, validates a human answer, and queues a versioned
resume job. Business execution remains in :mod:`app.services`.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import (
    AgentRun,
    AgentRunStatus,
    AgentStep,
    AgentStepStatus,
    AssistanceRequest,
    AssistanceStatus,
    AuditEvent,
    Handoff,
    Job,
    ProductCategory,
)

PRODUCT_CATEGORY_REQUEST_TYPE = "PRODUCT_CATEGORY_SELECTION"
PRODUCT_CATEGORY_REQUEST_KEY = "select-product-category"
COA_LOOKUP_REQUEST_TYPE = "COA_LOOKUP_CORRECTION"
COA_LOOKUP_REQUEST_KEY = "correct-coa-lookup"
CAS_NUMBER_PATTERN = re.compile(r"^\d{2,7}-\d{2}-\d$")


@dataclass(frozen=True)
class AssistanceAnswerResult:
    request: AssistanceRequest
    run: AgentRun
    job: Job | None
    newly_answered: bool


async def _next_step_sequence(session: AsyncSession, run_id: int) -> int:
    current = await session.scalar(
        select(func.max(AgentStep.sequence)).where(AgentStep.run_id == run_id)
    )
    return int(current or 0) + 1


async def ensure_handoff_agent_run(
    session: AsyncSession,
    *,
    handoff: Handoff,
) -> AgentRun:
    """Create the durable wait state for a newly created inbound handoff.

    Typed assistance is created only for workflows with a validated resume
    contract. Draft approval handoffs keep the same Agent run but do not ask a
    second question before the reviewer approves the prepared draft.
    """

    if handoff.source_email_id is None:
        raise ValueError("an Agent run requires an inbound source email")
    run = await session.scalar(
        select(AgentRun).where(AgentRun.handoff_id == handoff.id)
    )
    if run is None:
        run = AgentRun(
            case_id=handoff.case_id,
            source_email_id=handoff.source_email_id,
            handoff_id=handoff.id,
            goal="Resolve the inbound customer request and prepare a policy-compliant reply",
            status=AgentRunStatus.WAITING_HUMAN,
            context_json={
                "handoff_reason": handoff.reason_code,
                "source_email_id": handoff.source_email_id,
            },
            current_step="human_review",
            version=1,
        )
        session.add(run)
        await session.flush()
        session.add(
            AgentStep(
                run_id=run.id,
                sequence=1,
                kind="HUMAN_HANDOFF",
                idempotency_key="initial-handoff",
                status=AgentStepStatus.WAITING,
                input_json={
                    "handoff_id": handoff.id,
                    "reason": handoff.reason_code,
                    "summary": handoff.summary,
                },
            )
        )

    facts = dict(handoff.extracted_facts or {})
    facts["agent_run_id"] = run.id

    if handoff.reason_code == "PRODUCT_CATEGORY_REVIEW":
        request = await session.scalar(
            select(AssistanceRequest).where(
                AssistanceRequest.run_id == run.id,
                AssistanceRequest.request_key == PRODUCT_CATEGORY_REQUEST_KEY,
            )
        )
        if request is None:
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
            research = facts.get("company_research") or {}
            decision = research.get("decision") if isinstance(research, dict) else {}
            recommended_key = (
                str(decision.get("recommended_category_key") or "")
                if isinstance(decision, dict)
                else ""
            )
            options = [
                {
                    "category_id": category.id,
                    "key": category.key,
                    "name": category.name,
                    "name_zh": category.name_zh,
                    "recommended": category.key == recommended_key,
                }
                for category in categories
            ]
            request = AssistanceRequest(
                run_id=run.id,
                handoff_id=handoff.id,
                request_key=PRODUCT_CATEGORY_REQUEST_KEY,
                request_type=PRODUCT_CATEGORY_REQUEST_TYPE,
                question=(
                    "请选择应发送给该客户的产品系列。确认后 Agent 会从当前断点继续，"
                    "重新执行安全校验并生成产品目录回复。"
                ),
                response_schema={
                    "type": "object",
                    "required": ["category_id"],
                    "properties": {
                        "category_id": {"type": "integer", "minimum": 1},
                        "note": {"type": "string", "maxLength": 2000},
                    },
                    "additionalProperties": False,
                },
                options_json=options,
                status=AssistanceStatus.OPEN,
            )
            session.add(request)
            await session.flush()
        run.current_step = PRODUCT_CATEGORY_REQUEST_KEY
        run.status = AgentRunStatus.WAITING_HUMAN
        facts["assistance_request_id"] = request.id

    if (
        handoff.reason_code == "COA_REVIEW"
        and facts.get("intent") == "coa_request"
        and not isinstance(facts.get("prepared_coa"), dict)
    ):
        request = await session.scalar(
            select(AssistanceRequest).where(
                AssistanceRequest.run_id == run.id,
                AssistanceRequest.request_key == COA_LOOKUP_REQUEST_KEY,
            )
        )
        if request is None:
            lookup = facts.get("coa_lookup")
            matches = lookup.get("matches") if isinstance(lookup, dict) else []
            options: list[dict[str, Any]] = []
            for match in matches if isinstance(matches, list) else []:
                if not isinstance(match, dict):
                    continue
                for candidate in match.get("candidates") or []:
                    if isinstance(candidate, dict):
                        options.append(
                            {
                                "path": str(candidate.get("path") or ""),
                                "accepted_name": bool(candidate.get("accepted_name")),
                                "reason": str(candidate.get("reason") or ""),
                            }
                        )
            request = AssistanceRequest(
                run_id=run.id,
                handoff_id=handoff.id,
                request_key=COA_LOOKUP_REQUEST_KEY,
                request_type=COA_LOOKUP_REQUEST_TYPE,
                question=(
                    "请先在 NAS 中补充或重命名为无中文、无日期/客户/专用后缀的标准英文 COA，"
                    "等待目录同步后，再填写可唯一匹配的产品名、产品代码或 CAS。Agent 会从断点"
                    "重新检索；仍不唯一时不会发送。"
                ),
                response_schema={
                    "type": "object",
                    "required": ["product_query"],
                    "properties": {
                        "product_query": {"type": "string", "minLength": 1, "maxLength": 255},
                        "cas_number": {"type": "string", "maxLength": 32},
                        "note": {"type": "string", "maxLength": 2000},
                    },
                    "additionalProperties": False,
                },
                options_json=options[:100],
                status=AssistanceStatus.OPEN,
            )
            session.add(request)
            await session.flush()
        run.current_step = COA_LOOKUP_REQUEST_KEY
        run.status = AgentRunStatus.WAITING_HUMAN
        facts["assistance_request_id"] = request.id

    handoff.extracted_facts = facts
    return run


async def answer_product_category_assistance(
    session: AsyncSession,
    *,
    request_id: int,
    category_id: int,
    actor: str,
    note: str = "",
) -> AssistanceAnswerResult:
    """Validate a category answer and atomically queue one versioned resume."""

    request = await session.scalar(
        select(AssistanceRequest)
        .where(AssistanceRequest.id == request_id)
        .with_for_update()
    )
    if request is None:
        raise ValueError("assistance request not found")
    if request.request_type != PRODUCT_CATEGORY_REQUEST_TYPE:
        raise ValueError("assistance request is not a product-category selection")

    run = await session.scalar(
        select(AgentRun).where(AgentRun.id == request.run_id).with_for_update()
    )
    handoff = await session.get(Handoff, request.handoff_id)
    if run is None or handoff is None:
        raise ValueError("assistance request has no active Agent run or handoff")

    if request.status != AssistanceStatus.OPEN:
        existing_category_id = int((request.answer_json or {}).get("category_id") or 0)
        if existing_category_id != category_id:
            raise ValueError("assistance request was already answered with another category")
        if (
            request.status == AssistanceStatus.ANSWERED
            and run.status == AgentRunStatus.BLOCKED
        ):
            category = await session.get(ProductCategory, category_id)
            if category is None or not category.active:
                raise ValueError("selected product category is missing or inactive")
            run.version += 1
            run.status = AgentRunStatus.RESUME_QUEUED
            run.current_step = "resume-product-category"
            run.last_error = None
            job = Job(
                kind="resume_agent_run",
                payload={
                    "run_id": run.id,
                    "run_version": run.version,
                    "assistance_request_id": request.id,
                },
                idempotency_key=f"agent-resume:{run.id}:{run.version}",
            )
            session.add(job)
            session.add(
                AuditEvent(
                    case_id=run.case_id,
                    actor=actor[:128],
                    event_type="agent.resume_requeued",
                    data={
                        "agent_run_id": run.id,
                        "run_version": run.version,
                        "assistance_request_id": request.id,
                        "category_id": category.id,
                        "category_key": category.key,
                    },
                )
            )
            await session.commit()
            return AssistanceAnswerResult(request, run, job, False)
        existing_job = await session.scalar(
            select(Job).where(
                Job.idempotency_key
                == f"agent-resume:{run.id}:{run.version}"
            )
        )
        return AssistanceAnswerResult(request, run, existing_job, False)

    if handoff.status != "OPEN":
        raise ValueError("handoff is no longer open")
    if handoff.reason_code != "PRODUCT_CATEGORY_REVIEW":
        raise ValueError("handoff no longer requires product-category selection")
    if run.status in {AgentRunStatus.COMPLETED, AgentRunStatus.CANCELLED}:
        raise ValueError("Agent run can no longer be resumed")

    category = await session.get(ProductCategory, category_id)
    if category is None or not category.active:
        raise ValueError("selected product category is missing or inactive")

    now = datetime.now(UTC)
    clean_note = note.strip()
    request.answer_json = {
        "category_id": category.id,
        "category_key": category.key,
        "category_name": category.name,
        "note": clean_note,
    }
    request.status = AssistanceStatus.ANSWERED
    request.answered_by = actor[:128]
    request.answered_at = now
    run.version += 1
    run.status = AgentRunStatus.RESUME_QUEUED
    run.current_step = "resume-product-category"
    run.last_error = None
    run.context_json = {
        **(run.context_json or {}),
        "selected_category_id": category.id,
        "selected_category_key": category.key,
        "answered_by": actor[:128],
    }

    sequence = await _next_step_sequence(session, run.id)
    session.add(
        AgentStep(
            run_id=run.id,
            sequence=sequence,
            kind="HUMAN_INPUT",
            idempotency_key=f"assistance-answer:{request.id}",
            status=AgentStepStatus.COMPLETED,
            input_json={
                "assistance_request_id": request.id,
                "request_type": request.request_type,
            },
            output_json=dict(request.answer_json),
            started_at=now,
            completed_at=now,
        )
    )
    job = Job(
        kind="resume_agent_run",
        payload={
            "run_id": run.id,
            "run_version": run.version,
            "assistance_request_id": request.id,
        },
        idempotency_key=f"agent-resume:{run.id}:{run.version}",
    )
    session.add(job)
    session.add(
        AuditEvent(
            case_id=run.case_id,
            actor=actor[:128],
            event_type="agent.assistance_answered",
            data={
                "agent_run_id": run.id,
                "run_version": run.version,
                "assistance_request_id": request.id,
                "category_id": category.id,
                "category_key": category.key,
            },
        )
    )
    await session.commit()
    return AssistanceAnswerResult(request, run, job, True)


async def answer_coa_lookup_assistance(
    session: AsyncSession,
    *,
    request_id: int,
    product_query: str,
    cas_number: str | None,
    actor: str,
    note: str = "",
) -> AssistanceAnswerResult:
    """Record a corrected COA lookup key and queue one versioned continuation."""

    clean_query = product_query.strip()
    clean_cas = (cas_number or "").strip()
    if not clean_query or len(clean_query) > 255:
        raise ValueError("product_query must contain 1 to 255 characters")
    if clean_cas and not CAS_NUMBER_PATTERN.fullmatch(clean_cas):
        raise ValueError("cas_number must use the standard digits-digits-digit format")
    request = await session.scalar(
        select(AssistanceRequest)
        .where(AssistanceRequest.id == request_id)
        .with_for_update()
    )
    if request is None:
        raise ValueError("assistance request not found")
    if request.request_type != COA_LOOKUP_REQUEST_TYPE:
        raise ValueError("assistance request is not a COA lookup correction")
    run = await session.scalar(
        select(AgentRun).where(AgentRun.id == request.run_id).with_for_update()
    )
    handoff = await session.get(Handoff, request.handoff_id)
    if run is None or handoff is None:
        raise ValueError("assistance request has no active Agent run or handoff")
    answer = {
        "product_query": clean_query,
        "cas_number": clean_cas or None,
        "note": note.strip(),
    }
    if request.status != AssistanceStatus.OPEN:
        if request.answer_json != answer:
            raise ValueError("assistance request was already answered differently")
        if request.status == AssistanceStatus.ANSWERED and run.status == AgentRunStatus.BLOCKED:
            run.version += 1
            run.status = AgentRunStatus.RESUME_QUEUED
            run.current_step = "resume-coa-lookup"
            run.last_error = None
            job = Job(
                kind="resume_agent_run",
                payload={
                    "run_id": run.id,
                    "run_version": run.version,
                    "assistance_request_id": request.id,
                },
                idempotency_key=f"agent-resume:{run.id}:{run.version}",
            )
            session.add(job)
            await session.commit()
            return AssistanceAnswerResult(request, run, job, False)
        existing_job = await session.scalar(
            select(Job).where(
                Job.idempotency_key == f"agent-resume:{run.id}:{run.version}"
            )
        )
        return AssistanceAnswerResult(request, run, existing_job, False)
    if handoff.status != "OPEN" or handoff.reason_code != "COA_REVIEW":
        raise ValueError("COA handoff is no longer open")
    if run.status in {AgentRunStatus.COMPLETED, AgentRunStatus.CANCELLED}:
        raise ValueError("Agent run can no longer be resumed")

    now = datetime.now(UTC)
    request.answer_json = answer
    request.status = AssistanceStatus.ANSWERED
    request.answered_by = actor[:128]
    request.answered_at = now
    run.version += 1
    run.status = AgentRunStatus.RESUME_QUEUED
    run.current_step = "resume-coa-lookup"
    run.last_error = None
    run.context_json = {
        **(run.context_json or {}),
        "corrected_coa_query": clean_query,
        "corrected_coa_cas": clean_cas or None,
        "answered_by": actor[:128],
    }
    sequence = await _next_step_sequence(session, run.id)
    session.add(
        AgentStep(
            run_id=run.id,
            sequence=sequence,
            kind="HUMAN_INPUT",
            idempotency_key=f"assistance-answer:{request.id}",
            status=AgentStepStatus.COMPLETED,
            input_json={
                "assistance_request_id": request.id,
                "request_type": request.request_type,
            },
            output_json=answer,
            started_at=now,
            completed_at=now,
        )
    )
    job = Job(
        kind="resume_agent_run",
        payload={
            "run_id": run.id,
            "run_version": run.version,
            "assistance_request_id": request.id,
        },
        idempotency_key=f"agent-resume:{run.id}:{run.version}",
    )
    session.add(job)
    session.add(
        AuditEvent(
            case_id=run.case_id,
            actor=actor[:128],
            event_type="agent.assistance_answered",
            data={
                "agent_run_id": run.id,
                "run_version": run.version,
                "assistance_request_id": request.id,
                "product_query": clean_query,
                "cas_number": clean_cas or None,
            },
        )
    )
    await session.commit()
    return AssistanceAnswerResult(request, run, job, True)


def assistance_request_payload(request: AssistanceRequest) -> dict[str, Any]:
    return {
        "id": request.id,
        "run_id": request.run_id,
        "handoff_id": request.handoff_id,
        "request_key": request.request_key,
        "request_type": request.request_type,
        "question": request.question,
        "response_schema": request.response_schema,
        "options": request.options_json,
        "status": request.status.value,
        "answer": request.answer_json,
        "answered_by": request.answered_by,
        "answered_at": request.answered_at.isoformat() if request.answered_at else None,
        "applied_at": request.applied_at.isoformat() if request.applied_at else None,
        "created_at": request.created_at.isoformat(),
        "updated_at": request.updated_at.isoformat(),
    }


async def finalize_handoff_agent_run(
    session: AsyncSession,
    *,
    handoff_id: int,
    actor: str,
    outcome: str,
    cancelled: bool = False,
) -> AgentRun | None:
    """Keep Agent state truthful when a reviewer completes a handoff another way."""

    run = await session.scalar(
        select(AgentRun).where(AgentRun.handoff_id == handoff_id).with_for_update()
    )
    if run is None or run.status in {
        AgentRunStatus.COMPLETED,
        AgentRunStatus.CANCELLED,
    }:
        return run
    now = datetime.now(UTC)
    run.status = AgentRunStatus.CANCELLED if cancelled else AgentRunStatus.COMPLETED
    run.current_step = outcome[:128]
    run.last_error = None
    run.completed_at = now
    requests = (
        (
            await session.execute(
                select(AssistanceRequest).where(
                    AssistanceRequest.run_id == run.id,
                    AssistanceRequest.status.in_(
                        [AssistanceStatus.OPEN, AssistanceStatus.ANSWERED]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    for request in requests:
        request.status = AssistanceStatus.CANCELLED
    steps = (
        (
            await session.execute(
                select(AgentStep).where(
                    AgentStep.run_id == run.id,
                    AgentStep.status.in_(
                        [
                            AgentStepStatus.WAITING,
                            AgentStepStatus.QUEUED,
                            AgentStepStatus.RUNNING,
                            AgentStepStatus.BLOCKED,
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    for step in steps:
        step.status = AgentStepStatus.CANCELLED
        step.completed_at = step.completed_at or now
    session.add(
        AuditEvent(
            case_id=run.case_id,
            actor=actor[:128],
            event_type="agent.run_finalized_externally",
            data={
                "agent_run_id": run.id,
                "handoff_id": handoff_id,
                "outcome": outcome,
                "cancelled": cancelled,
            },
        )
    )
    return run
