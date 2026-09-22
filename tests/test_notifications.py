from unittest.mock import AsyncMock

import pytest

from app.common.notifications import notify_handoff
from app.db import Handoff
from app.domain import HandoffReason


@pytest.mark.asyncio
async def test_bounce_review_does_not_notify_dingtalk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff = Handoff(
        id=42,
        reason_code=HandoffReason.BOUNCE_REVIEW.value,
        summary="Delivery failure needs review",
        extracted_facts={"recipient": "customer@example.com"},
        status="OPEN",
        dingtalk_status="PENDING",
    )
    session = AsyncMock()
    session.get.return_value = handoff

    class UnexpectedNotifier:
        def __init__(self) -> None:
            pytest.fail("bounce reviews must not initialize the DingTalk notifier")

    monkeypatch.setattr("app.common.notifications.DingTalkNotifier", UnexpectedNotifier)
    await notify_handoff(session, handoff.id)

    assert handoff.status == "OPEN"
    assert handoff.dingtalk_status == "CANCELLED"
    session.commit.assert_awaited_once_with()
