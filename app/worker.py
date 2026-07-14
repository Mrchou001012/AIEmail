import asyncio
import logging
import socket

from app.db import SessionLocal
from app.services import claim_and_run_job, reconcile_unknown_outbox, send_one_outbox

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


async def main() -> None:
    worker_id = f"{socket.gethostname()}-worker"
    logger.info("worker started as %s", worker_id)
    while True:
        did_work = await _run_step("job", claim_and_run_job, worker_id)
        did_work = await _run_step("reconcile", reconcile_unknown_outbox) or did_work
        did_work = await _run_step("outbox", send_one_outbox) or did_work
        if not did_work:
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
