"""Durable job queue and worker dispatch.

Business workflows remain in their domain modules. This module owns only job
lifecycle, retry bookkeeping, and late-bound dispatch so adding a job no longer
grows the legacy services facade.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Job, JobStatus
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)

JobHandler = Callable[[AsyncSession, dict[str, Any]], Awaitable[None]]


class JobDeferred(RuntimeError):
    """A durable business wait that must not consume the job retry budget."""

    def __init__(self, reason: str, available_at: datetime):
        super().__init__(reason)
        self.reason = reason
        self.available_at = available_at


async def enqueue_job(
    session: AsyncSession,
    kind: str,
    payload: dict[str, Any],
    idempotency_key: str,
    available_at: datetime | None = None,
) -> Job | None:
    """Persist an idempotent job without invalidating the caller's ORM state."""

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
        await session.commit()
        return None


async def _service_handler(name: str, session: AsyncSession, *args: Any) -> None:
    # Late import keeps the queue independent from the legacy services facade
    # and preserves existing test monkeypatches during the gradual extraction.
    from app import services

    await getattr(services, name)(session, *args)


async def _demo_outreach(session: AsyncSession, payload: dict[str, Any]) -> None:
    await _service_handler("create_demo_outreach", session, payload)


async def _case_outreach(session: AsyncSession, payload: dict[str, Any]) -> None:
    await _service_handler("create_case_outreach", session, payload)


async def _process_inbound(session: AsyncSession, payload: dict[str, Any]) -> None:
    await _service_handler("process_inbound", session, int(payload["email_id"]))


async def _resume_agent_run(session: AsyncSession, payload: dict[str, Any]) -> None:
    from app.services import resume_agent_run

    await resume_agent_run(
        session,
        run_id=int(payload["run_id"]),
        expected_version=int(payload["run_version"]),
        assistance_request_id=int(payload["assistance_request_id"]),
    )


async def _notify_handoff(session: AsyncSession, payload: dict[str, Any]) -> None:
    await _service_handler("notify_handoff", session, int(payload["handoff_id"]))


async def _notify_commercial_refresh(
    session: AsyncSession, payload: dict[str, Any]
) -> None:
    await _service_handler(
        "notify_commercial_refresh", session, int(payload["cycle_id"])
    )


async def _inbound_disposition_batch(
    session: AsyncSession, payload: dict[str, Any]
) -> None:
    from app.disposition_batches import process_disposition_batch

    next_poll = await process_disposition_batch(
        session,
        int(payload["batch_id"]),
        settings=get_settings(),
    )
    if next_poll is not None:
        raise JobDeferred("Anthropic disposition batch is still processing", next_poll)


JOB_HANDLERS: dict[str, JobHandler] = {
    "demo_outreach": _demo_outreach,
    "case_outreach": _case_outreach,
    "process_inbound": _process_inbound,
    "resume_agent_run": _resume_agent_run,
    "notify_handoff": _notify_handoff,
    "notify_commercial_refresh": _notify_commercial_refresh,
    "inbound_disposition_batch": _inbound_disposition_batch,
}


async def claim_and_run_job(
    session: AsyncSession,
    worker_id: str,
    settings: Settings | None = None,
) -> bool:
    """Claim one durable job and apply retry/defer semantics."""

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
        await session.rollback()
        job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if job is None:
            raise RuntimeError(f"claimed job {job_id} disappeared") from exc
        job.last_error = error
        if job.attempts >= job.max_attempts:
            job.status = JobStatus.FAILED
        else:
            job.status = JobStatus.PENDING
            job.available_at = datetime.now(UTC) + timedelta(
                seconds=min(300, 2**job.attempts)
            )
        job.locked_at = None
        job.locked_by = None
        job.updated_at = datetime.now(UTC)
        await session.commit()
    return True
