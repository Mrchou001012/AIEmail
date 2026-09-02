import asyncio
import logging
import socket
from collections.abc import Callable

from app.coa_catalog import COACatalogScanner
from app.db import SessionLocal
from app.jobs import claim_and_run_job
from app.nas_knowledge import NASKnowledgeScanner
from app.reactivation import ensure_reactivation_dispatch
from app.services import (
    ensure_weekly_commercial_refresh,
    reconcile_permanent_bounce_handoffs,
    reconcile_unknown_outbox,
    send_one_outbox,
)
from app.settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


async def _run_step(name: str, operation, *args) -> bool:
    try:
        async with SessionLocal() as session:
            return bool(await operation(session, *args))
    except Exception:
        # A transient database, IMAP, SMTP, or integration failure must not
        # terminate the worker process and strand the remaining durable jobs.
        logger.exception("worker step %s failed", name)
        return False


async def _run_local_step(name: str, operation: Callable[[], object]) -> bool:
    try:
        await asyncio.to_thread(operation)
        return True
    except Exception:
        logger.exception("worker local step %s failed", name)
        return False


def _nas_scanner(settings):
    return NASKnowledgeScanner(
        root=settings.nas_knowledge_root,
        policy_path=settings.nas_knowledge_policy_path,
        output_dir=settings.nas_knowledge_output_dir,
        max_extract_bytes=settings.nas_knowledge_max_file_mb * 1024 * 1024,
        extraction_timeout_seconds=settings.nas_knowledge_file_timeout_seconds,
    )


def _coa_scanner(settings):
    return COACatalogScanner(
        root=settings.coa_catalog_root,
        output_path=settings.coa_catalog_path,
        product_catalog_path=settings.coa_product_catalog_path,
        max_file_bytes=settings.coa_catalog_max_file_mb * 1024 * 1024,
        extraction_timeout_seconds=settings.coa_catalog_file_timeout_seconds,
    )


async def main() -> None:
    worker_id = f"{socket.gethostname()}-worker"
    settings = get_settings()
    next_commercial_check = 0.0
    next_reactivation_check = 0.0
    next_bounce_reconcile = 0.0
    next_nas_knowledge_scan = 0.0
    next_coa_catalog_scan = 0.0
    logger.info("worker started as %s", worker_id)
    while True:
        did_work = False
        loop_time = asyncio.get_running_loop().time()
        if loop_time >= next_commercial_check:
            did_work = await _run_step("commercial-refresh", ensure_weekly_commercial_refresh)
            next_commercial_check = loop_time + settings.commercial_refresh_check_seconds
        did_work = await _run_step("job", claim_and_run_job, worker_id) or did_work
        if loop_time >= next_reactivation_check:
            did_work = await _run_step("reactivation", ensure_reactivation_dispatch) or did_work
            next_reactivation_check = loop_time + settings.reactivation_check_seconds
        if loop_time >= next_bounce_reconcile:
            did_work = (
                await _run_step("bounce-review-reconcile", reconcile_permanent_bounce_handoffs)
                or did_work
            )
            next_bounce_reconcile = loop_time + 60
        if settings.nas_knowledge_enabled and loop_time >= next_nas_knowledge_scan:
            did_work = await _run_local_step("nas-knowledge", _nas_scanner(settings).scan) or did_work
            next_nas_knowledge_scan = loop_time + settings.nas_knowledge_poll_seconds
        if settings.coa_catalog_enabled and loop_time >= next_coa_catalog_scan:
            did_work = await _run_local_step("coa-catalog", _coa_scanner(settings).scan) or did_work
            next_coa_catalog_scan = loop_time + settings.coa_catalog_poll_seconds
        did_work = await _run_step("reconcile", reconcile_unknown_outbox) or did_work
        did_work = await _run_step("outbox", send_one_outbox) or did_work
        if not did_work:
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
