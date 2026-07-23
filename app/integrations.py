import logging

import httpx

from app.commercial import commercial_update_link, review_link
from app.db import CommercialDataCycle, Handoff, SalesCase
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)
# httpx logs complete request URLs at INFO level. DingTalk webhook tokens live in
# the URL, so production logs must never retain those credentials.
logging.getLogger("httpx").setLevel(logging.WARNING)

HANDOFF_REASON_LABELS = {
    "SAMPLE_REQUEST": "寄样申请",
    "ORDER_COMMITMENT": "订单确认",
    "SHIPPING_REQUEST": "交期或发货申请",
    "TECHNICAL_REQUEST": "技术问题",
    "COMPLAINT": "客户投诉",
    "BELOW_FLOOR": "还价低于底价",
    "NONSTANDARD": "非标准业务条件",
    "LOW_CONFIDENCE": "关键信息识别不确定",
    "ATTACHMENT_REVIEW": "附件需要检查",
    "SUPPRESSED": "客户已停止自动联系",
    "HUMAN_CONTROL": "案例已由人工接管",
    "AI_FAILURE": "自动分析失败",
    "MAIL_FAILURE": "邮件发送失败",
    "THREAD_AMBIGUOUS": "邮件会话关联不明确",
    "PRICE_NEGOTIATION": "客户还价",
    "PREBOOK_REQUEST": "预订需求",
    "PACKAGING_REVIEW": "包装信息待确认",
    "PERSONNEL_CHANGE": "客户人员变动",
    "AUTOMATED_REPLY_REVIEW": "自动回复待核对",
    "EMAIL_DELIVERABILITY": "收件地址投递检查",
    "BOUNCE_REVIEW": "退信待核对",
    "NEW_INQUIRY_REVIEW": "新询盘待确认",
    "INVENTORY_UNAVAILABLE": "库存不可用",
}

HANDOFF_REASON_SUMMARIES = {
    "SAMPLE_REQUEST": "客户提出寄样需求，需人工确认样品、地址及后续安排。",
    "ORDER_COMMITMENT": "客户涉及下单或订单承诺，需人工确认后继续。",
    "SHIPPING_REQUEST": "客户询问交期、发货或物流安排，需人工确认。",
    "TECHNICAL_REQUEST": "客户提出技术问题，需由业务或技术人员处理。",
    "COMPLAINT": "邮件涉及客户投诉，已停止自动回复并转人工处理。",
    "BELOW_FLOOR": "客户目标价格低于自动报价底线，需人工决定。",
    "NONSTANDARD": "邮件包含标准自动流程以外的业务条件，需人工确认。",
    "LOW_CONFIDENCE": "系统无法可靠识别邮件中的关键信息，需人工核对。",
    "ATTACHMENT_REVIEW": "邮件包含需要人工检查的附件或内嵌内容。",
    "SUPPRESSED": "该客户或联系人已停止自动联系，系统未继续发送。",
    "HUMAN_CONTROL": "该案例当前由人工负责，自动处理已暂停。",
    "AI_FAILURE": "自动分析或邮件起草失败，需人工检查。",
    "MAIL_FAILURE": "邮件多次发送失败，需检查地址或发送状态。",
    "THREAD_AMBIGUOUS": "系统无法将该邮件唯一关联到一个业务案例。",
    "PRICE_NEGOTIATION": "客户提出还价，按当前规则转人工处理。",
    "PREBOOK_REQUEST": "客户提出预订需求，当前规则要求人工确认。",
    "PACKAGING_REVIEW": "包装信息无法从现有资料中可靠确认。",
    "PERSONNEL_CHANGE": "邮件显示客户人员离职、休假或联系方式发生变化。",
    "AUTOMATED_REPLY_REVIEW": "该自动回复无法安全地由系统直接处理。",
    "EMAIL_DELIVERABILITY": "收件地址检查未通过，本次自动发送已停止。",
    "BOUNCE_REVIEW": "退信暂时无法安全确定处理方式，需要核对。",
    "NEW_INQUIRY_REVIEW": "新询盘无法安全自动建立或关联业务案例。",
    "INVENTORY_UNAVAILABLE": "当前库存不足或库存信息尚未确认。",
}


def _handoff_reason_label(reason_code: str) -> str:
    return HANDOFF_REASON_LABELS.get(reason_code, "需要人工处理")


def _handoff_summary(reason_code: str) -> str:
    return HANDOFF_REASON_SUMMARIES.get(
        reason_code,
        "系统无法安全完成自动处理，请打开处理页面查看详细记录。",
    )


def _single_line(value: object, *, limit: int = 300) -> str:
    return " ".join(str(value).split())[:limit]


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
        reason_label = _handoff_reason_label(handoff.reason_code)
        title = f"AIEmail · 人工处理 #{handoff.id}：{reason_label}"
        lines = [
            f"### {title}",
            "",
            f"- 关联案例：{'#' + str(case.id) if case else '未关联'}",
            f"- 处理原因：{reason_label}",
            f"- 情况说明：{_handoff_summary(handoff.reason_code)}",
        ]
        facts = handoff.extracted_facts or {}
        for label, key in (
            ("客户发件人", "sender"),
            ("收件地址", "recipient"),
            ("产品", "product_code"),
            ("数量", "quantity"),
            ("原邮件主题", "subject"),
        ):
            value = facts.get(key)
            if value is not None and value != "":
                lines.append(f"- {label}：{_single_line(value)}")
        lines.append(
            f"- 处理入口：[打开人工处理页面]({review_link(self.settings, handoff.id, case.id if case else None)})"
        )
        text = "\n".join(lines) + "\n"
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
            f"- 状态及后续系统入口：{commercial_update_link(self.settings, cycle)}\n"
        )
        return await self.notify_markdown(title, text)
