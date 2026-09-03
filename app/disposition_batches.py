from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import (
    AIClient,
    inbound_disposition_message_params,
    parse_inbound_disposition_message,
)
from app.db import (
    EmailMessage,
    InboundDispositionBatch,
    InboundDispositionBatchItem,
)
from app.disposition_service import (
    build_disposition_plan,
    decision_to_disposition,
    disposition_from_payload,
    disposition_to_payload,
    rule_classify_email_disposition,
)
from app.inbound_disposition import InboundDisposition, InboundDispositionType
from app.settings import Settings, get_settings

BATCH_TERMINAL_STATUSES = frozenset({"SUCCEEDED", "PARTIAL_FAILED", "FAILED"})
ITEM_TERMINAL_STATUSES = frozenset({"AI_SUCCEEDED", "RULE_ONLY", "FALLBACK"})
ATTENTION_DISPOSITION_TYPES = frozenset(
    {
        InboundDispositionType.CONTACT_IDENTITY_MISMATCH,
        InboundDispositionType.UNCERTAIN,
    }
)


def _batch_delay(settings: Settings, retry_count: int = 0) -> timedelta:
    seconds = settings.inbound_disposition_ai_batch_poll_seconds
    if retry_count:
        seconds = min(300, seconds * (2 ** min(retry_count, 4)))
    return timedelta(seconds=seconds)


def _request_for_row(
    row: EmailMessage,
    settings: Settings,
) -> tuple[dict[str, Any], str]:
    headers = (row.automated_reply_metadata or {}).get("headers") or {}
    return inbound_disposition_message_params(
        settings=settings,
        subject=row.subject,
        body=row.body_text,
        sender=row.from_address,
        headers={str(key): str(value) for key, value in headers.items()},
    )


def _fallback_disposition(
    row: EmailMessage,
    *,
    error_type: str,
    settings: Settings,
) -> InboundDisposition:
    return replace(
        rule_classify_email_disposition(row, settings=settings),
        classifier_source="deterministic_fallback",
        classification_error=error_type[:128],
    )


def _append_attempt(
    item: InboundDispositionBatchItem,
    *,
    result_type: str,
    error_type: str | None = None,
    error_message: str | None = None,
) -> None:
    item.attempt_history_json = [
        *(item.attempt_history_json or []),
        {
            "attempt": item.attempt_count,
            "at": datetime.now(UTC).isoformat(),
            "result_type": result_type,
            "error_type": error_type,
            "error_message": (error_message or "")[:500] or None,
        },
    ]


def _error_details(result: dict[str, Any]) -> tuple[str, str]:
    error: Any = result.get("error") or {}
    if isinstance(error, dict) and isinstance(error.get("error"), dict):
        error = error["error"]
    if not isinstance(error, dict):
        return "UNKNOWN_PROVIDER_ERROR", str(error)[:500]
    return (
        str(error.get("type") or "UNKNOWN_PROVIDER_ERROR")[:128],
        str(error.get("message") or "")[:500],
    )


def _refresh_counts(
    batch: InboundDispositionBatch,
    items: list[InboundDispositionBatchItem],
) -> None:
    batch.pending_count = sum(item.status == "PENDING" for item in items)
    batch.rule_count = sum(item.status == "RULE_ONLY" for item in items)
    batch.succeeded_count = sum(item.status == "AI_SUCCEEDED" for item in items)
    batch.failed_count = sum(item.status == "FALLBACK" for item in items)


def _finish_batch(
    batch: InboundDispositionBatch,
    items: list[InboundDispositionBatchItem],
) -> None:
    _refresh_counts(batch, items)
    batch.provider_batch_id = None
    batch.ended_at = datetime.now(UTC)
    if batch.failed_count == 0:
        batch.status = "SUCCEEDED"
    elif batch.succeeded_count > 0:
        batch.status = "PARTIAL_FAILED"
    else:
        batch.status = "FAILED"


async def create_disposition_batch(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
    created_by: str,
    limit: int,
    include_business: bool,
    include_synced_history: bool,
) -> InboundDispositionBatch:
    settings = settings or get_settings()
    filters = [
        EmailMessage.direction == "INBOUND",
        EmailMessage.is_bounce.is_(False),
    ]
    if not include_synced_history:
        filters.append(EmailMessage.is_history.is_(False))
    rows = (
        (
            await session.execute(
                select(EmailMessage)
                .where(*filters)
                .order_by(EmailMessage.received_at.desc(), EmailMessage.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if len(rows) > settings.inbound_disposition_ai_max_batch:
        raise ValueError(
            "AI disposition batch exceeds INBOUND_DISPOSITION_AI_MAX_BATCH; "
            "reduce the scan limit"
        )
    batch = InboundDispositionBatch(
        status="PENDING",
        created_by=created_by,
        options_json={
            "limit": limit,
            "include_business": include_business,
            "include_synced_history": include_synced_history,
        },
        provider_batch_ids_json=[],
        total_count=len(rows),
        ai_requested_count=0,
        rule_count=0,
        pending_count=0,
        succeeded_count=0,
        failed_count=0,
        retry_count=0,
    )
    session.add(batch)
    await session.flush()
    items: list[InboundDispositionBatchItem] = []
    for row in rows:
        _, input_hash = _request_for_row(row, settings)
        rule = rule_classify_email_disposition(row, settings=settings)
        rule_only = (
            rule.disposition_type is InboundDispositionType.SYSTEM_NOTIFICATION
        )
        item = InboundDispositionBatchItem(
            batch_id=batch.id,
            email_id=row.id,
            custom_id=f"email_{row.id}",
            input_hash=input_hash,
            status="RULE_ONLY" if rule_only else "PENDING",
            attempt_count=0,
            classification_json=(
                disposition_to_payload(rule) if rule_only else {}
            ),
            attempt_history_json=[],
            needs_attention=False,
        )
        session.add(item)
        items.append(item)
    batch.ai_requested_count = sum(item.status == "PENDING" for item in items)
    _refresh_counts(batch, items)
    if batch.pending_count == 0:
        _finish_batch(batch, items)
    await session.commit()
    return batch


async def _load_batch_items(
    session: AsyncSession,
    batch_id: int,
    *,
    lock: bool = False,
) -> list[InboundDispositionBatchItem]:
    statement = (
        select(InboundDispositionBatchItem)
        .where(InboundDispositionBatchItem.batch_id == batch_id)
        .order_by(InboundDispositionBatchItem.id)
    )
    if lock:
        statement = statement.with_for_update()
    return list((await session.execute(statement)).scalars().all())


async def _mark_submission_failure(
    session: AsyncSession,
    *,
    batch: InboundDispositionBatch,
    items: list[InboundDispositionBatchItem],
    error_type: str,
    error_message: str,
    settings: Settings,
) -> datetime | None:
    pending = [item for item in items if item.status == "PENDING"]
    retryable = False
    for item in pending:
        item.attempt_count += 1
        item.last_attempt_at = datetime.now(UTC)
        item.error_type = error_type[:128]
        item.error_message = error_message[:2000]
        _append_attempt(
            item,
            result_type="submit_error",
            error_type=error_type,
            error_message=error_message,
        )
        if item.attempt_count < settings.inbound_disposition_ai_batch_max_attempts:
            retryable = True
            continue
        row = await session.get(EmailMessage, item.email_id)
        if row is not None:
            item.classification_json = disposition_to_payload(
                _fallback_disposition(
                    row,
                    error_type=error_type,
                    settings=settings,
                )
            )
        item.status = "FALLBACK"
        item.needs_attention = True
    batch.retry_count += 1
    batch.last_error = f"{error_type}: {error_message}"[:2000]
    _refresh_counts(batch, items)
    if retryable:
        batch.status = "RETRYING"
        await session.commit()
        return datetime.now(UTC) + _batch_delay(settings, batch.retry_count)
    _finish_batch(batch, items)
    await session.commit()
    return None


async def _mark_provider_attempt_failure(
    session: AsyncSession,
    *,
    batch: InboundDispositionBatch,
    items: list[InboundDispositionBatchItem],
    error_type: str,
    error_message: str,
    settings: Settings,
) -> datetime | None:
    """Close an already-submitted provider attempt and retry only failed items."""

    retryable = False
    for item in items:
        if item.status != "PENDING":
            continue
        item.error_type = error_type[:128]
        item.error_message = error_message[:2000]
        _append_attempt(
            item,
            result_type="provider_read_error",
            error_type=error_type,
            error_message=error_message,
        )
        if item.attempt_count < settings.inbound_disposition_ai_batch_max_attempts:
            retryable = True
            continue
        row = await session.get(EmailMessage, item.email_id)
        if row is not None:
            item.classification_json = disposition_to_payload(
                _fallback_disposition(
                    row,
                    error_type=error_type,
                    settings=settings,
                )
            )
        item.status = "FALLBACK"
        item.needs_attention = True
    batch.provider_batch_id = None
    batch.retry_count += 1
    batch.last_error = f"{error_type}: {error_message}"[:2000]
    options = dict(batch.options_json or {})
    options["provider_read_failures"] = 0
    batch.options_json = options
    _refresh_counts(batch, items)
    if retryable:
        batch.status = "RETRYING"
        await session.commit()
        return datetime.now(UTC) + _batch_delay(settings, batch.retry_count)
    _finish_batch(batch, items)
    await session.commit()
    return None


async def _defer_provider_read_failure(
    session: AsyncSession,
    *,
    batch: InboundDispositionBatch,
    items: list[InboundDispositionBatchItem],
    error: Exception,
    settings: Settings,
) -> datetime | None:
    """Retry transient status/result reads before abandoning that provider attempt."""

    error_type = type(error).__name__
    error_message = str(error)
    options = dict(batch.options_json or {})
    failures = int(options.get("provider_read_failures") or 0) + 1
    options["provider_read_failures"] = failures
    batch.options_json = options
    batch.last_error = f"{error_type}: {error_message}"[:2000]
    if failures < settings.inbound_disposition_ai_batch_max_attempts:
        await session.commit()
        return datetime.now(UTC) + _batch_delay(settings, failures)
    return await _mark_provider_attempt_failure(
        session,
        batch=batch,
        items=items,
        error_type=error_type,
        error_message=error_message,
        settings=settings,
    )


async def process_disposition_batch(
    session: AsyncSession,
    batch_id: int,
    *,
    settings: Settings | None = None,
) -> datetime | None:
    """Advance one durable batch; return the next poll time when unfinished."""

    settings = settings or get_settings()
    batch = await session.scalar(
        select(InboundDispositionBatch)
        .where(InboundDispositionBatch.id == batch_id)
        .with_for_update()
    )
    if batch is None or batch.status in BATCH_TERMINAL_STATUSES:
        return None
    items = await _load_batch_items(session, batch.id, lock=True)
    client = AIClient(settings)
    if not batch.provider_batch_id:
        requests: list[dict[str, Any]] = []
        request_items: list[InboundDispositionBatchItem] = []
        for item in items:
            if item.status != "PENDING":
                continue
            row = await session.get(EmailMessage, item.email_id)
            if row is None:
                item.status = "FALLBACK"
                item.needs_attention = True
                item.error_type = "EMAIL_NOT_FOUND"
                item.error_message = "Source email was removed before AI review"
                continue
            params, input_hash = _request_for_row(row, settings)
            if input_hash != item.input_hash:
                item.status = "FALLBACK"
                item.needs_attention = True
                item.error_type = "EMAIL_CONTENT_CHANGED"
                item.error_message = "Source content changed after batch creation"
                item.classification_json = disposition_to_payload(
                    _fallback_disposition(
                        row,
                        error_type="EMAIL_CONTENT_CHANGED",
                        settings=settings,
                    )
                )
                continue
            requests.append({"custom_id": item.custom_id, "params": params})
            request_items.append(item)
        if not requests:
            _finish_batch(batch, items)
            await session.commit()
            return None
        try:
            provider = await client.create_inbound_disposition_batch(requests)
        except Exception as exc:
            return await _mark_submission_failure(
                session,
                batch=batch,
                items=items,
                error_type=type(exc).__name__,
                error_message=str(exc),
                settings=settings,
            )
        provider_id = str(provider.get("id") or "")
        if not provider_id:
            return await _mark_submission_failure(
                session,
                batch=batch,
                items=items,
                error_type="MISSING_PROVIDER_BATCH_ID",
                error_message="Anthropic returned no batch id",
                settings=settings,
            )
        now = datetime.now(UTC)
        for item in request_items:
            item.attempt_count += 1
            item.last_attempt_at = now
            item.error_type = None
            item.error_message = None
        batch.provider_batch_id = provider_id
        batch.provider_batch_ids_json = [
            *(batch.provider_batch_ids_json or []),
            provider_id,
        ]
        batch.status = "IN_PROGRESS"
        batch.submitted_at = batch.submitted_at or now
        batch.last_error = None
        await session.commit()
        return now + _batch_delay(settings)

    try:
        provider = await client.retrieve_inbound_disposition_batch(
            batch.provider_batch_id
        )
    except Exception as exc:
        return await _defer_provider_read_failure(
            session,
            batch=batch,
            items=items,
            error=exc,
            settings=settings,
        )
    if (batch.options_json or {}).get("provider_read_failures"):
        options = dict(batch.options_json or {})
        options["provider_read_failures"] = 0
        batch.options_json = options
    if provider.get("processing_status") != "ended":
        batch.status = "IN_PROGRESS"
        await session.commit()
        return datetime.now(UTC) + _batch_delay(settings)

    try:
        results = await client.retrieve_inbound_disposition_batch_results(
            batch.provider_batch_id
        )
    except Exception as exc:
        return await _defer_provider_read_failure(
            session,
            batch=batch,
            items=items,
            error=exc,
            settings=settings,
        )

    result_by_custom_id = {
        str(result.get("custom_id") or ""): result for result in results
    }
    retry_pending = False
    for item in items:
        if item.status != "PENDING":
            continue
        row = await session.get(EmailMessage, item.email_id)
        result_wrapper = result_by_custom_id.get(item.custom_id)
        result = (
            result_wrapper.get("result")
            if isinstance(result_wrapper, dict)
            else None
        )
        if not isinstance(result, dict):
            result = {
                "type": "errored",
                "error": {
                    "type": "MISSING_BATCH_RESULT",
                    "message": "Provider results did not contain this custom_id",
                },
            }
        result_type = str(result.get("type") or "errored")
        item.provider_result_type = result_type[:32]
        if result_type == "succeeded" and row is not None:
            try:
                message = result.get("message") or {}
                decision = parse_inbound_disposition_message(message)
                usage = message.get("usage") or {}
                disposition = decision_to_disposition(
                    row,
                    rule=rule_classify_email_disposition(row, settings=settings),
                    decision=decision,
                    metadata={
                        "model": message.get("model"),
                        "request_hash": item.input_hash,
                        "request_id": message.get("id"),
                    },
                )
                item.classification_json = disposition_to_payload(disposition)
                item.status = "AI_SUCCEEDED"
                item.needs_attention = (
                    disposition.disposition_type in ATTENTION_DISPOSITION_TYPES
                )
                item.error_type = None
                item.error_message = None
                item.input_tokens = int(usage.get("input_tokens") or 0)
                item.output_tokens = int(usage.get("output_tokens") or 0)
                _append_attempt(item, result_type="succeeded")
                continue
            except Exception as exc:
                result_type = "parse_error"
                error_type, error_message = type(exc).__name__, str(exc)
        else:
            error_type, error_message = _error_details(result)
            if row is None:
                error_type = "EMAIL_NOT_FOUND"
                error_message = "Source email was removed before result processing"
        item.error_type = error_type[:128]
        item.error_message = error_message[:2000]
        _append_attempt(
            item,
            result_type=result_type,
            error_type=error_type,
            error_message=error_message,
        )
        if (
            row is not None
            and item.attempt_count
            < settings.inbound_disposition_ai_batch_max_attempts
        ):
            item.status = "PENDING"
            retry_pending = True
        else:
            item.status = "FALLBACK"
            item.needs_attention = True
            if row is not None:
                item.classification_json = disposition_to_payload(
                    _fallback_disposition(
                        row,
                        error_type=error_type,
                        settings=settings,
                    )
                )
    batch.provider_batch_id = None
    _refresh_counts(batch, items)
    if retry_pending:
        batch.status = "RETRYING"
        batch.retry_count += 1
        await session.commit()
        return datetime.now(UTC) + _batch_delay(settings, batch.retry_count)
    _finish_batch(batch, items)
    await session.commit()
    return None


async def disposition_batch_result(
    session: AsyncSession,
    batch_id: int,
    *,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    settings = settings or get_settings()
    batch = await session.get(InboundDispositionBatch, batch_id)
    if batch is None:
        return None
    items = await _load_batch_items(session, batch.id)
    plans: list[dict[str, Any]] = []
    include_business = bool((batch.options_json or {}).get("include_business"))
    for item in items:
        if item.status not in ITEM_TERMINAL_STATUSES:
            continue
        row = await session.get(EmailMessage, item.email_id)
        if row is None:
            continue
        disposition = disposition_from_payload(row, item.classification_json or {})
        plan = await build_disposition_plan(
            session,
            row,
            settings=settings,
            disposition=disposition,
        )
        if (
            include_business
            or plan["disposition_type"] != InboundDispositionType.BUSINESS.value
        ):
            plan["batch_id"] = batch.id
            plan["ai_batch_item_status"] = item.status
            plan["ai_attempt_count"] = item.attempt_count
            plan["ai_failed"] = item.status == "FALLBACK"
            plan["ai_error_type"] = item.error_type
            plan["ai_error_message"] = item.error_message
            plan["needs_attention"] = item.needs_attention
            plans.append(plan)
    counts: dict[str, int] = {}
    for plan in plans:
        key = str(plan["disposition_type"])
        counts[key] = counts.get(key, 0) + 1
    return {
        "mode": "batch-dry-run",
        "batch_id": batch.id,
        "batch_status": batch.status,
        "batch_created_at": batch.created_at.isoformat(),
        "batch_ended_at": batch.ended_at.isoformat() if batch.ended_at else None,
        "batch_options": dict(batch.options_json or {}),
        "complete": batch.status in BATCH_TERMINAL_STATUSES,
        "scanned_count": batch.total_count,
        "candidate_count": len(plans),
        "applied_count": 0,
        "counts": dict(sorted(counts.items())),
        "plans": plans,
        "ai_summary": {
            "requested": batch.ai_requested_count,
            "pending": batch.pending_count,
            "succeeded": batch.succeeded_count,
            "failed": batch.failed_count,
            "rule_only": batch.rule_count,
            "retry_count": batch.retry_count,
            "last_error": batch.last_error,
            "retry_available": batch.failed_count > 0,
        },
    }


async def list_disposition_batches(
    session: AsyncSession,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    batches = list(
        (
            await session.execute(
                select(InboundDispositionBatch)
                .order_by(InboundDispositionBatch.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": batch.id,
            "status": batch.status,
            "total_count": batch.total_count,
            "ai_requested_count": batch.ai_requested_count,
            "rule_count": batch.rule_count,
            "pending_count": batch.pending_count,
            "succeeded_count": batch.succeeded_count,
            "failed_count": batch.failed_count,
            "retry_count": batch.retry_count,
            "created_by": batch.created_by,
            "created_at": batch.created_at.isoformat(),
            "ended_at": batch.ended_at.isoformat() if batch.ended_at else None,
            "options": dict(batch.options_json or {}),
        }
        for batch in batches
    ]


async def retry_failed_disposition_batch(
    session: AsyncSession,
    batch_id: int,
) -> InboundDispositionBatch | None:
    batch = await session.scalar(
        select(InboundDispositionBatch)
        .where(InboundDispositionBatch.id == batch_id)
        .with_for_update()
    )
    if batch is None:
        return None
    if batch.status not in {"PARTIAL_FAILED", "FAILED"}:
        raise ValueError("Only a completed failed batch can be retried")
    items = await _load_batch_items(session, batch.id, lock=True)
    retried = 0
    for item in items:
        if item.status != "FALLBACK":
            continue
        item.attempt_history_json = [
            *(item.attempt_history_json or []),
            {
                "attempt": item.attempt_count,
                "at": datetime.now(UTC).isoformat(),
                "result_type": "manual_retry_requested",
                "error_type": item.error_type,
                "error_message": None,
            },
        ]
        item.status = "PENDING"
        item.attempt_count = 0
        item.classification_json = {}
        item.provider_result_type = None
        item.needs_attention = False
        retried += 1
    if not retried:
        raise ValueError("Batch has no failed AI items to retry")
    batch.status = "RETRYING"
    batch.provider_batch_id = None
    batch.ended_at = None
    batch.last_error = None
    batch.retry_count += 1
    _refresh_counts(batch, items)
    await session.commit()
    return batch


async def batch_item_disposition(
    session: AsyncSession,
    *,
    batch_id: int,
    email_id: int,
) -> tuple[InboundDisposition, InboundDispositionBatchItem] | None:
    latest_batch_id = await session.scalar(
        select(InboundDispositionBatchItem.batch_id)
        .where(InboundDispositionBatchItem.email_id == email_id)
        .order_by(InboundDispositionBatchItem.batch_id.desc())
        .limit(1)
    )
    if latest_batch_id is not None and latest_batch_id != batch_id:
        raise ValueError(
            "Historical disposition batches are read-only; review the latest batch"
        )
    item = await session.scalar(
        select(InboundDispositionBatchItem).where(
            InboundDispositionBatchItem.batch_id == batch_id,
            InboundDispositionBatchItem.email_id == email_id,
            InboundDispositionBatchItem.status.in_(ITEM_TERMINAL_STATUSES),
        )
    )
    row = await session.get(EmailMessage, email_id)
    if item is None or row is None:
        return None
    return disposition_from_payload(row, item.classification_json or {}), item
