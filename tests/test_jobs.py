from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db import Base, JobStatus
from app.jobs import JOB_HANDLERS, JobDeferred, claim_and_run_job, enqueue_job
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


@pytest.mark.asyncio
async def test_deferred_job_does_not_consume_retry_budget(
    db_session: AsyncSession,
) -> None:
    available_at = datetime.now(UTC) + timedelta(minutes=5)

    async def deferred(_: AsyncSession, __: dict[str, object]) -> None:
        raise JobDeferred("waiting for external result", available_at)

    JOB_HANDLERS["test_deferred"] = deferred
    try:
        job = await enqueue_job(
            db_session,
            "test_deferred",
            {},
            "test-deferred-job",
        )
        assert job is not None

        assert await claim_and_run_job(
            db_session,
            "test-worker",
            Settings(_env_file=None),
        )
        await db_session.refresh(job)

        assert job.status is JobStatus.PENDING
        assert job.attempts == 0
        assert job.available_at == available_at.replace(tzinfo=None)
        assert job.last_error == "DEFERRED: waiting for external result"
    finally:
        JOB_HANDLERS.pop("test_deferred", None)


@pytest.mark.asyncio
async def test_enqueue_job_remains_idempotent(db_session: AsyncSession) -> None:
    first = await enqueue_job(db_session, "unknown", {}, "same-key")
    second = await enqueue_job(db_session, "unknown", {}, "same-key")

    assert first is not None
    assert second is None
