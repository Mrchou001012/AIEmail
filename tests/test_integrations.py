from types import SimpleNamespace

import pytest

from app.domain import HandoffReason
from app.integrations import HANDOFF_REASON_LABELS, DingTalkNotifier
from app.settings import Settings


class CapturingNotifier(DingTalkNotifier):
    def __init__(self) -> None:
        super().__init__(Settings(_env_file=None))
        self.title = ""
        self.text = ""

    async def notify_markdown(self, title: str, text: str) -> str:
        self.title = title
        self.text = text
        return "CAPTURED"


def test_every_handoff_reason_has_a_chinese_notification_label() -> None:
    assert {reason.value for reason in HandoffReason} <= set(HANDOFF_REASON_LABELS)


@pytest.mark.asyncio
async def test_handoff_notification_uses_chinese_display_text_without_changing_code() -> None:
    notifier = CapturingNotifier()
    handoff = SimpleNamespace(
        id=42,
        reason_code=HandoffReason.PRICE_NEGOTIATION.value,
        summary="Inbound counteroffer requires human review",
        extracted_facts={
            "sender": "buyer@example.com",
            "product_code": "YAC-TES",
            "quantity": 600,
            "subject": "Re: quotation",
        },
    )

    result = await notifier.notify(handoff, SimpleNamespace(id=7))

    assert result == "CAPTURED"
    assert notifier.title == "AIEmail · 人工处理 #42：客户还价"
    assert "关联案例：#7" in notifier.text
    assert "处理原因：客户还价" in notifier.text
    assert "情况说明：客户提出还价，按当前规则转人工处理。" in notifier.text
    assert "客户发件人：buyer@example.com" in notifier.text
    assert "产品：YAC-TES" in notifier.text
    assert "数量：600" in notifier.text
    assert "处理入口：[打开人工处理页面]" in notifier.text
    assert "PRICE_NEGOTIATION" not in notifier.text
    assert "Inbound counteroffer requires human review" not in notifier.text
    assert "Sales handoff" not in notifier.text
    assert "Case:" not in notifier.text
