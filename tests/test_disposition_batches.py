from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.ai import inbound_disposition_message_params
from app.db import (
    Base,
    EmailMessage,
    InboundDispositionBatchItem,
)
from app.disposition_batches import (
    batch_item_disposition,
    create_disposition_batch,
    disposition_batch_result,
    list_disposition_batches,
    process_disposition_batch,
    retry_failed_disposition_batch,
)
from app.settings import Settings


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


def _settings(*, attempts: int = 2) -> Settings:
    return Settings(
        _env_file=None,
        ai_provider="anthropic",
        anthropic_api_key="test-only",
        inbound_disposition_ai_enabled=True,
        inbound_disposition_ai_batch_enabled=True,
        inbound_disposition_ai_batch_max_attempts=attempts,
        inbound_disposition_ai_batch_poll_seconds=5,
    )


def test_disposition_requests_use_low_variance_sampling() -> None:
    params, _ = inbound_disposition_message_params(
        settings=_settings(),
        subject="Re: Checking in",
        body="Please contact buyer@example.com.",
        sender="sender@example.com",
    )

    assert params["temperature"] == 0


def _email(token: str, body: str, *, subject: str = "Re: Checking in") -> EmailMessage:
    return EmailMessage(
        direction="INBOUND",
        from_address=f"buyer-{token}@example.com",
        to_addresses=["sales@lanyachem.com"],
        subject=subject,
        body_text=body,
        attachment_metadata=[],
        raw_sha256=token * 64,
        is_history=False,
        is_automated_reply=False,
        automated_reply_metadata={},
        is_bounce=False,
        received_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


def _success(custom_id: str, disposition_type: str = "BUSINESS") -> dict[str, Any]:
    payload = {
        "disposition_type": disposition_type,
        "confidence": 0.93,
        "reason": "The message is a direct business reply.",
        "evidence": ["Please send your product list"],
        "replacement_emails": [],
        "return_hint": None,
        "forwarded_to_replacement": False,
        "non_target_reason": None,
        "product_list_requested": True,
    }
    return {
        "custom_id": custom_id,
        "result": {
            "type": "succeeded",
            "message": {
                "id": f"msg_{custom_id}",
                "model": "claude-test",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "usage": {"input_tokens": 100, "output_tokens": 40},
            },
        },
    }


def _error(custom_id: str, error_type: str = "overloaded_error") -> dict[str, Any]:
    return {
        "custom_id": custom_id,
        "result": {
            "type": "errored",
            "error": {"type": error_type, "message": "temporary provider failure"},
        },
    }


class _ScriptedBatchAI:
    scripts: list[list[dict[str, Any]]] = []
    submitted_custom_ids: list[list[str]] = []
    batches: dict[str, list[dict[str, Any]]] = {}

    def __init__(self, _: Settings) -> None:
        pass

    @classmethod
    def reset(cls, scripts: list[list[dict[str, Any]]]) -> None:
        cls.scripts = list(scripts)
        cls.submitted_custom_ids = []
        cls.batches = {}

    async def create_inbound_disposition_batch(
        self, requests: list[dict[str, Any]]
    ) -> dict[str, Any]:
        custom_ids = [str(request["custom_id"]) for request in requests]
        type(self).submitted_custom_ids.append(custom_ids)
        batch_id = f"batch_{len(type(self).submitted_custom_ids)}"
        type(self).batches[batch_id] = type(self).scripts.pop(0)
        return {"id": batch_id}

    async def retrieve_inbound_disposition_batch(
        self, provider_batch_id: str
    ) -> dict[str, Any]:
        return {"id": provider_batch_id, "processing_status": "ended"}

    async def retrieve_inbound_disposition_batch_results(
        self, provider_batch_id: str
    ) -> list[dict[str, Any]]:
        return type(self).batches[provider_batch_id]


async def _new_batch(
    session: AsyncSession,
    settings: Settings,
    *rows: EmailMessage,
):
    session.add_all(rows)
    await session.commit()
    return await create_disposition_batch(
        session,
        settings=settings,
        created_by="test",
        limit=50,
        include_business=True,
        include_synced_history=False,
    )


async def _advance_to_terminal(
    session: AsyncSession,
    batch_id: int,
    settings: Settings,
    *,
    limit: int = 10,
) -> None:
    for _ in range(limit):
        next_run = await process_disposition_batch(
            session, batch_id, settings=settings
        )
        if next_run is None:
            return
    raise AssertionError("batch did not reach a terminal state")


@pytest.mark.asyncio
async def test_batch_success_maps_reversed_results_by_custom_id(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    batch = await _new_batch(
        db_session,
        settings,
        _email("a", "Please send your product list"),
        _email("b", "Please send your product catalogue"),
    )
    items = list(
        (
            await db_session.execute(
                select(InboundDispositionBatchItem)
                .where(InboundDispositionBatchItem.batch_id == batch.id)
                .order_by(InboundDispositionBatchItem.id)
            )
        )
        .scalars()
        .all()
    )
    _ScriptedBatchAI.reset(
        [[_success(items[1].custom_id), _success(items[0].custom_id)]]
    )
    monkeypatch.setattr("app.disposition_batches.AIClient", _ScriptedBatchAI)

    await _advance_to_terminal(db_session, batch.id, settings)
    result = await disposition_batch_result(db_session, batch.id, settings=settings)

    assert result is not None
    assert result["batch_status"] == "SUCCEEDED"
    assert result["ai_summary"] == {
        "requested": 2,
        "pending": 0,
        "succeeded": 2,
        "failed": 0,
        "rule_only": 0,
        "retry_count": 0,
        "last_error": None,
        "retry_available": False,
    }
    assert _ScriptedBatchAI.submitted_custom_ids == [
        [items[0].custom_id, items[1].custom_id]
    ]


@pytest.mark.asyncio
async def test_batch_retries_only_failed_item_then_succeeds(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(attempts=2)
    batch = await _new_batch(
        db_session,
        settings,
        _email("c", "Please send your product list"),
        _email("d", "Please send your catalogue"),
    )
    items = list(
        (
            await db_session.execute(
                select(InboundDispositionBatchItem)
                .where(InboundDispositionBatchItem.batch_id == batch.id)
                .order_by(InboundDispositionBatchItem.id)
            )
        )
        .scalars()
        .all()
    )
    _ScriptedBatchAI.reset(
        [
            [_success(items[0].custom_id), _error(items[1].custom_id)],
            [_success(items[1].custom_id)],
        ]
    )
    monkeypatch.setattr("app.disposition_batches.AIClient", _ScriptedBatchAI)

    await _advance_to_terminal(db_session, batch.id, settings)
    result = await disposition_batch_result(db_session, batch.id, settings=settings)

    assert result is not None
    assert result["batch_status"] == "SUCCEEDED"
    assert result["ai_summary"]["succeeded"] == 2
    assert _ScriptedBatchAI.submitted_custom_ids == [
        [items[0].custom_id, items[1].custom_id],
        [items[1].custom_id],
    ]


@pytest.mark.asyncio
async def test_batch_partial_failure_falls_back_and_marks_attention(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(attempts=1)
    batch = await _new_batch(
        db_session,
        settings,
        _email("e", "Please send your product list"),
        _email("f", "Please send your catalogue"),
    )
    items = list(
        (
            await db_session.execute(
                select(InboundDispositionBatchItem)
                .where(InboundDispositionBatchItem.batch_id == batch.id)
                .order_by(InboundDispositionBatchItem.id)
            )
        )
        .scalars()
        .all()
    )
    _ScriptedBatchAI.reset(
        [[_success(items[0].custom_id), _error(items[1].custom_id)]]
    )
    monkeypatch.setattr("app.disposition_batches.AIClient", _ScriptedBatchAI)

    await _advance_to_terminal(db_session, batch.id, settings)
    result = await disposition_batch_result(db_session, batch.id, settings=settings)

    assert result is not None
    assert result["batch_status"] == "PARTIAL_FAILED"
    assert result["ai_summary"]["succeeded"] == 1
    assert result["ai_summary"]["failed"] == 1
    failed = next(plan for plan in result["plans"] if plan["ai_failed"])
    assert failed["needs_attention"] is True
    assert failed["classifier_source"] == "deterministic_fallback"
    assert failed["ai_error_type"] == "overloaded_error"


@pytest.mark.asyncio
async def test_batch_full_submission_failure_exhausts_retries(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _SubmissionFailureAI(_ScriptedBatchAI):
        calls = 0

        async def create_inbound_disposition_batch(
            self, requests: list[dict[str, Any]]
        ) -> dict[str, Any]:
            type(self).calls += 1
            raise ConnectionError("provider unavailable")

    settings = _settings(attempts=2)
    batch = await _new_batch(
        db_session,
        settings,
        _email("g", "Please send your product list"),
        _email("h", "Please send your catalogue"),
    )
    _SubmissionFailureAI.calls = 0
    monkeypatch.setattr("app.disposition_batches.AIClient", _SubmissionFailureAI)

    await _advance_to_terminal(db_session, batch.id, settings)
    result = await disposition_batch_result(db_session, batch.id, settings=settings)

    assert result is not None
    assert result["batch_status"] == "FAILED"
    assert result["ai_summary"]["failed"] == 2
    assert _SubmissionFailureAI.calls == 2
    assert all(plan["ai_failed"] for plan in result["plans"])
    assert all(plan["needs_attention"] for plan in result["plans"])


@pytest.mark.asyncio
async def test_manual_retry_resets_only_fallback_items(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(attempts=1)
    batch = await _new_batch(
        db_session,
        settings,
        _email("i", "Please send your product list"),
        _email("j", "Please send your catalogue"),
    )
    items = list(
        (
            await db_session.execute(
                select(InboundDispositionBatchItem)
                .where(InboundDispositionBatchItem.batch_id == batch.id)
                .order_by(InboundDispositionBatchItem.id)
            )
        )
        .scalars()
        .all()
    )
    _ScriptedBatchAI.reset(
        [[_success(items[0].custom_id), _error(items[1].custom_id)]]
    )
    monkeypatch.setattr("app.disposition_batches.AIClient", _ScriptedBatchAI)
    await _advance_to_terminal(db_session, batch.id, settings)

    await retry_failed_disposition_batch(db_session, batch.id)
    refreshed = list(
        (
            await db_session.execute(
                select(InboundDispositionBatchItem)
                .where(InboundDispositionBatchItem.batch_id == batch.id)
                .order_by(InboundDispositionBatchItem.id)
            )
        )
        .scalars()
        .all()
    )

    assert refreshed[0].status == "AI_SUCCEEDED"
    assert refreshed[0].attempt_count == 1
    assert refreshed[1].status == "PENDING"
    assert refreshed[1].attempt_count == 0
    assert refreshed[1].needs_attention is False


@pytest.mark.asyncio
async def test_provider_result_download_failure_is_bounded_and_falls_back(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _ResultDownloadFailureAI(_ScriptedBatchAI):
        async def retrieve_inbound_disposition_batch_results(
            self, provider_batch_id: str
        ) -> list[dict[str, Any]]:
            raise TimeoutError(f"could not download {provider_batch_id}")

    settings = _settings(attempts=1)
    batch = await _new_batch(
        db_session,
        settings,
        _email("k", "Please send your product list"),
    )
    _ResultDownloadFailureAI.reset([[]])
    monkeypatch.setattr(
        "app.disposition_batches.AIClient", _ResultDownloadFailureAI
    )

    await _advance_to_terminal(db_session, batch.id, settings)
    result = await disposition_batch_result(db_session, batch.id, settings=settings)

    assert result is not None
    assert result["batch_status"] == "FAILED"
    assert result["ai_summary"]["failed"] == 1
    assert result["plans"][0]["ai_error_type"] == "TimeoutError"
    assert result["plans"][0]["needs_attention"] is True


@pytest.mark.asyncio
async def test_trusted_system_notification_uses_rules_without_ai(
    db_session: AsyncSession,
) -> None:
    settings = _settings()
    batch = await _new_batch(
        db_session,
        settings,
        _email(
            "l",
            "DO NOT REPLY TO THIS EMAIL - THIS IS AN AUTOMATED SERVER NOTICE",
            subject="Failure Notification",
        ),
    )
    result = await disposition_batch_result(db_session, batch.id, settings=settings)

    assert result is not None
    assert result["batch_status"] == "SUCCEEDED"
    assert result["ai_summary"]["requested"] == 0
    assert result["ai_summary"]["rule_only"] == 1
    assert result["plans"][0]["disposition_type"] == "SYSTEM_NOTIFICATION"


@pytest.mark.asyncio
async def test_batch_history_lists_newest_first(db_session: AsyncSession) -> None:
    settings = _settings()
    first = await _new_batch(
        db_session,
        settings,
        _email("m", "Please send your product list"),
    )
    second = await create_disposition_batch(
        db_session,
        settings=settings,
        created_by="test",
        limit=50,
        include_business=False,
        include_synced_history=False,
    )

    history = await list_disposition_batches(db_session, limit=20)

    assert [row["id"] for row in history[:2]] == [second.id, first.id]
    assert history[0]["total_count"] == 1
    assert history[0]["options"]["include_business"] is False

    first_item = await db_session.scalar(
        select(InboundDispositionBatchItem).where(
            InboundDispositionBatchItem.batch_id == first.id
        )
    )
    assert first_item is not None
    with pytest.raises(ValueError, match="Historical disposition batches are read-only"):
        await batch_item_disposition(
            db_session,
            batch_id=first.id,
            email_id=first_item.email_id,
        )
