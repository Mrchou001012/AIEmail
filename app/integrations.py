import logging

import httpx

from app.commercial import commercial_update_link, review_link
from app.db import CommercialDataCycle, Handoff, SalesCase
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)
# httpx logs complete request URLs at INFO level. DingTalk webhook tokens live in
# the URL, so production logs must never retain those credentials.
logging.getLogger("httpx").setLevel(logging.WARNING)


class DingTalkNotifier:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    async def notify_markdown(self, title: str, text: str) -> str:
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

    async def notify(self, handoff: Handoff, case: SalesCase | None) -> str:
        case_id = case.id if case else "unmatched"
        title = f"AIEmail · Sales handoff #{handoff.id}: {handoff.reason_code}"
        text = (
            f"### {title}\n\n"
            f"- Case: {case_id}\n"
            f"- Reason: {handoff.reason_code}\n"
            f"- Summary: {handoff.summary}\n"
            f"- Review: {review_link(self.settings, handoff.id, case.id if case else None)}\n"
        )
        return await self.notify_markdown(title, text)

    async def notify_commercial_refresh(self, cycle: CommercialDataCycle) -> str:
        title = f"AIEmail 本周价格和库存待更新（{cycle.week_start.isoformat()}）"
        missing = []
        if cycle.price_status != "CONFIRMED":
            missing.append("本周价格表")
        if cycle.inventory_status != "CONFIRMED":
            missing.append("现货库存")
        text = (
            f"### {title}\n\n"
            f"- 业务周：{cycle.week_start.isoformat()} 至 {cycle.week_end.isoformat()}（周五）\n"
            f"- 待确认：{'、'.join(missing) or '无'}\n"
            "- 在价格和库存都确认前，AI 自动报价回复已暂停，不会沿用上周价格。\n"
            "- 当前系统请先上传价格表，再按本周价格版本确认各产品库存。\n"
            f"- 状态/后续 CRM 入口：{commercial_update_link(self.settings, cycle)}\n"
        )
        return await self.notify_markdown(title, text)
