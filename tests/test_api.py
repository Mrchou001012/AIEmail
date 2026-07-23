import json
from unittest.mock import AsyncMock

import pytest

from app.api import (
    COMMERCIAL_UPDATE_PATH,
    HANDOFF_REVIEW_PATH,
    REACTIVATION_PATH,
    commercial_update_page,
    dashboard,
    health,
    reactivation_page,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("database_ok", "expected_status", "expected_body"),
    [
        (True, 200, {"status": "ok", "database": True}),
        (False, 503, {"status": "degraded", "database": False}),
    ],
)
async def test_health_uses_one_database_probe(
    monkeypatch: pytest.MonkeyPatch,
    database_ok: bool,
    expected_status: int,
    expected_body: dict[str, object],
) -> None:
    probe = AsyncMock(return_value=database_ok)
    monkeypatch.setattr("app.api.db_health", probe)

    response = await health()

    assert response.status_code == expected_status
    assert json.loads(response.body) == expected_body
    probe.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_dashboard_is_a_protected_no_store_html_surface() -> None:
    response = await dashboard("admin")

    assert response.status_code == 200
    assert "AI 发信运行台" in response.body.decode("utf-8")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_handoff_review_page_exposes_complete_human_workflow() -> None:
    html = HANDOFF_REVIEW_PATH.read_text(encoding="utf-8")

    assert "人工处理" in html
    assert "/assign" in html
    assert "/cases" in html
    assert "/send" in html
    assert "确认并加入发件队列" in html
    assert "resume_automation" in html


@pytest.mark.asyncio
async def test_commercial_update_page_is_protected_no_store_html() -> None:
    response = await commercial_update_page("admin")

    assert response.status_code == 200
    assert "本周价格与库存" in response.body.decode("utf-8")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"


def test_commercial_update_page_exposes_atomic_editor_workflow() -> None:
    html = COMMERCIAL_UPDATE_PATH.read_text(encoding="utf-8")

    assert "/admin/commercial/current/editor" in html
    assert "/admin/commercial/current/confirm" in html
    assert "本周基础价" in html
    assert "库存数量" in html
    assert "确认并启用本周自动报价" in html


@pytest.mark.asyncio
async def test_reactivation_page_is_protected_no_store_html() -> None:
    response = await reactivation_page("admin")

    assert response.status_code == 200
    assert "历史客户唤醒" in response.body.decode("utf-8")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"


def test_reactivation_page_exposes_selection_and_campaign_controls() -> None:
    html = REACTIVATION_PATH.read_text(encoding="utf-8")

    assert "/admin/reactivation/campaigns" in html
    assert "选择当前可选项" in html
    assert "启动批次" in html
    assert "暂停" in html
    assert "pendingSelection=new Map()" in html
    assert "setTimeout(()=>flushSelectionQueue()" in html
    assert "await flushSelectionQueue();" in html
