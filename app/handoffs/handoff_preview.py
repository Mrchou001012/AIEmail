"""Handoff preview workflow."""

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.ai import AIClient, render_draft_preview
from app.common.email_identity import reply_contact_name as _reply_contact_name
from app.common.service_core import (
    _retrieve_historical_style_examples,
    audit,
)
from app.db import (
    AIInvocation,
    EmailMessage,
    Handoff,
    Outbox,
    SalesCase,
)
from app.domain import HandoffReason
from app.handoffs.handoff_prepared_preview import _prepared_handoff_draft_preview
from app.settings import get_settings

logger = logging.getLogger(__name__)


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
    if await session.scalar(select(Outbox.id).where(Outbox.approval_handoff_id == handoff.id)):
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
            "message": (
                "正在检索并校验 COA 目录…"
                if handoff.reason_code == HandoffReason.COA_REVIEW.value
                else "正在使用已确认的产品目录生成可核对草稿…"
            ),
        }
        yield {"type": "subject", "value": prepared_preview["subject"]}
        yield {"type": "body_reset"}
        for index, block in enumerate(str(prepared_preview["body_text"]).split("\n\n")):
            if block.strip():
                yield {
                    "type": "body_block",
                    "kind": "greeting" if index == 0 else "paragraph",
                    "value": block.strip(),
                }
        missing_business_facts = list(prepared_preview.get("missing_business_facts") or [])
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
            "contact_name": _reply_contact_name(
                sales_case.contact.name,
                source_email.body_text,
            ),
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
