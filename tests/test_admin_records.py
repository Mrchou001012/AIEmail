import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import list_handoff_records, list_handoffs, list_jobs, list_outbox
from app.db import DeliveryStatus, Handoff, Job, JobStatus, Outbox
from app.domain import HandoffReason

pytestmark = pytest.mark.integration


async def test_admin_records_pagination_and_status_filters(
    db_session: AsyncSession,
) -> None:
    for index in range(4):
        db_session.add(
            Handoff(
                reason_code=HandoffReason.HUMAN_CONTROL.value,
                summary=f"handoff-{index}",
                status="OPEN" if index < 3 else "RESOLVED",
            )
        )
    for index in range(3):
        db_session.add(
            Outbox(
                recipient=f"a{index}@example.com",
                message_id=f"<outbox-{index}@example.com>",
                business_key=f"outbox-{index}",
                raw_message="raw",
                status=(
                    DeliveryStatus.FAILED
                    if index == 2
                    else (
                        DeliveryStatus.CLAIMED
                        if index == 1
                        else DeliveryStatus.SENT
                    )
                ),
            )
        )
    for index in range(3):
        db_session.add(
            Job(
                kind="process_inbound",
                payload={"email_id": index},
                idempotency_key=f"job-{index}",
                status=(
                    JobStatus.FAILED
                    if index == 2
                    else JobStatus.DONE
                ),
            )
        )
    await db_session.commit()

    open_page = await list_handoff_records(
        "admin",
        db_session,
        status="OPEN",
        offset=0,
        limit=2,
    )
    assert open_page["total"] == 3
    assert len(open_page["items"]) == 2

    second_page = await list_handoff_records("admin", db_session, offset=1, limit=2)
    assert second_page["total"] == 4
    assert [item["id"] for item in second_page["items"]] == [3, 2]

    failed_outbox = await list_outbox("admin", db_session, status="FAILED")
    assert failed_outbox["total"] == 1
    assert failed_outbox["items"][0]["status"] == "FAILED"

    claimed_outbox = await list_outbox("admin", db_session, status="CLAIMED")
    assert claimed_outbox["total"] == 1
    assert claimed_outbox["items"][0]["status"] == "CLAIMED"

    failed_jobs = await list_jobs("admin", db_session, status="FAILED")
    assert failed_jobs["total"] == 1
    assert failed_jobs["items"][0]["status"] == "FAILED"

    legacy_handoffs = await list_handoffs("admin", db_session)
    assert isinstance(legacy_handoffs, list)
    assert len(legacy_handoffs) == 4
    assert set(legacy_handoffs[0]) == {
        "id",
        "case_id",
        "reason",
        "summary",
        "status",
        "dingtalk_status",
    }
