import logging

import httpx

from app.db import Handoff, SalesCase
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class DingTalkNotifier:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    async def notify(self, handoff: Handoff, case: SalesCase | None) -> str:
        case_id = case.id if case else "unmatched"
        title = f"Sales handoff #{handoff.id}: {handoff.reason_code}"
        text = (
            f"### {title}\n\n"
            f"- Case: {case_id}\n"
            f"- Reason: {handoff.reason_code}\n"
            f"- Summary: {handoff.summary}\n"
            f"- Review: {self.settings.public_base_url}/admin/handoffs/{handoff.id}/review\n"
        )
        if self.settings.dingtalk_transport != "webhook":
            logger.warning("DingTalk(log): %s", text.replace("\n", " | "))
            return "LOGGED"
        if not self.settings.dingtalk_webhook_url:
            raise RuntimeError("DingTalk webhook is not configured")
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                self.settings.dingtalk_webhook_url,
                json={"msgtype": "markdown", "markdown": {"title": title, "text": text}},
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("errcode") not in {0, None}:
                raise RuntimeError(f"DingTalk rejected notification: {payload.get('errmsg')}")
        return "SENT"
