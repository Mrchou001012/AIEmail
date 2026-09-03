import json
from unittest.mock import AsyncMock

import pytest
from bs4 import BeautifulSoup

from app.api import (
    COMMERCIAL_UPDATE_PATH,
    CONTACTS_PATH,
    FAVICON_PATH,
    HANDOFF_REVIEW_PATH,
    INBOUND_DISPOSITIONS_PATH,
    REACTIVATION_PATH,
    RECORDS_PATH,
    HandoffCaseRequest,
    _dashboard_headers,
    _suggested_handoff_reply,
    _validate_inbound_disposition_confirmation,
    commercial_update_page,
    contacts_page,
    dashboard,
    download_prepared_product_list,
    favicon,
    health,
    inbound_dispositions_page,
    reactivation_page,
    record_statuses,
    stream_handoff_preview,
)
from app.db import Handoff
from app.mail import OutboundAttachment
from app.services import _reply_contact_name, _strip_duplicate_signature_lead


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
async def test_favicon_is_public_and_served_as_an_icon() -> None:
    response = await favicon()

    assert FAVICON_PATH.exists()
    assert response.media_type == "image/x-icon"
    assert response.headers["cache-control"] == "public, max-age=86400"


@pytest.mark.asyncio
async def test_record_status_metadata_includes_claimed_outbox_state() -> None:
    statuses = await record_statuses("admin")

    assert "CLAIMED" in statuses["outbox"]
    assert statuses["handoffs"] == ["OPEN", "RESOLVED"]


def test_records_page_exposes_accessible_tab_semantics() -> None:
    page = BeautifulSoup(RECORDS_PATH.read_text(encoding="utf-8"), "html.parser")
    tablist = page.select_one('[role="tablist"]')
    tabs = page.select('[role="tab"]')
    panel = page.select_one('[role="tabpanel"]')

    assert tablist is not None
    assert [tab.get("data-tab") for tab in tabs] == ["handoffs", "outbox", "jobs"]
    assert panel is not None and panel.get("id") == "records-panel"
    assert all(tab.get("aria-controls") == "records-panel" for tab in tabs)


def test_handoff_page_accepts_runtime_forward_recipient_and_loads_history() -> None:
    source = HANDOFF_REVIEW_PATH.read_text(encoding="utf-8")
    page = BeautifulSoup(source, "html.parser")

    assert page.select_one("#forward-recipient") is not None
    assert page.select_one("#forward-send") is not None
    assert page.select_one("#forward-state") is not None
    assert 'const closed = data.status !== "OPEN"' in source
    assert "await loadForwardRecipients()" in source


def test_handoff_suggestion_does_not_duplicate_the_automatic_signature() -> None:
    suggestion = _suggested_handoff_reply(
        Handoff(reason_code="THREAD_AMBIGUOUS"),
        None,
        None,
    )

    assert suggestion["body_text"].startswith("Dear Customer,")
    assert "Best regards" not in suggestion["body_text"]


def test_reply_contact_name_uses_high_confidence_signature_name_for_placeholder() -> None:
    body = """Dear Mam,

Thanks for your email.

Thanks and Regards,

Nikita Karande
Marketing & Sales
SEEMA BIOTECH
"""

    assert _reply_contact_name("Customer", body) == "Nikita Karande"


def test_reply_contact_name_preserves_verified_contact_name() -> None:
    body = "Best regards,\nSomeone Else\nSales Manager"

    assert _reply_contact_name("Alice Buyer", body) == "Alice Buyer"


def test_reply_contact_name_rejects_job_title_or_company_as_name() -> None:
    assert _reply_contact_name("Customer", "Best regards,\nMarketing & Sales\nSEEMA BIOTECH") == "Customer"
    assert _reply_contact_name("Customer", "Best regards,\nSEEMA BIOTECH\nMumbai") == "Customer"


def test_handoff_suggestion_prefers_saved_ai_preview() -> None:
    suggestion = _suggested_handoff_reply(
        Handoff(
            reason_code="NEW_INQUIRY_REVIEW",
            extracted_facts={
                "ai_draft_preview": {
                    "subject": "Re: AI preview",
                    "body_text": "Dear Zhou Lei,\n\nSaved preview.",
                }
            },
        ),
        None,
        None,
    )

    assert suggestion == {
        "subject": "Re: AI preview",
        "body_text": "Dear Zhou Lei,\n\nSaved preview.",
    }


def test_human_reply_removes_a_trailing_automatic_signature_lead() -> None:
    body = _strip_duplicate_signature_lead(
        "Dear Customer,\n\nPlease see the attached quotation.\n\nBEST REGARDS,",
        "Best regards,\n\nShreya Saxena",
    )

    assert body == "Dear Customer,\n\nPlease see the attached quotation."


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
    assert "/case-product" in html
    assert "产品待确认 / 产品目录咨询" in html
    assert "/send" in html
    assert "/send-with-attachments" in html
    assert 'id="reply-attachments"' in html
    assert 'id="source-attachments"' in html
    assert "FormData" in html
    assert "/display" in html
    assert "body.innerHTML = display.body_html" in html
    assert "内嵌图片会显示在正文中" in html
    assert 'id="load-remote-images"' in html
    assert "远程图片可能用于追踪邮件是否被打开" in html
    assert "确认并加入发件队列" in html
    assert "resume_automation" in html
    assert "/replace-recipient" in html
    assert 'id="replacement-email"' in html
    assert "同公司其他邮箱和其他案例不会被修改" in html
    assert 'id="sender-contact-review"' in html
    assert 'id="contact-search"' in html
    assert 'id="customer-match-select"' in html
    assert 'id="add-sender-contact"' in html
    assert "/admin/contact-directory?query=" in html
    assert "/admin/customers/${customerId}/contacts" in html
    assert "旧邮箱和历史记录会保留" in html
    assert "formatApiError" in html
    assert "payload.detail ?? payload" in html
    assert "当前发件地址尚未登记。请先确认客户归属并新增联系人。" in html
    assert "/draft-preview/stream" in html
    assert 'id="prepared-product-list-review"' in html
    assert "/prepared-product-list/download" in html
    assert "下载 Excel 预览" in html
    assert "查看 ${esc(codes.length)} 个对外产品代码" in html
    assert "preparedProductList.catalog_codes" in html
    assert 'id="send-attachment-summary"' in html
    assert "发送时将自动附加" in html
    assert "确认当前正文并附加" not in html
    assert "付款条件待重新生成，暂不可发送" in html
    assert "response.body.getReader()" in html
    assert 'new TextDecoder("utf-8")' in html


@pytest.mark.asyncio
async def test_handoff_draft_stream_uses_ndjson_and_disables_proxy_buffering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_stream(*args, **kwargs):
        yield {"type": "status", "message": "正在生成"}
        yield {
            "type": "complete",
            "preview": {"subject": "Re: Inquiry", "body_text": "Dear Customer,"},
        }

    monkeypatch.setattr("app.api.stream_handoff_draft_preview", fake_stream)

    response = await stream_handoff_preview(123, "admin", AsyncMock())
    body = "".join([chunk async for chunk in response.body_iterator])
    events = [json.loads(line) for line in body.splitlines()]

    assert response.media_type == "application/x-ndjson"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert [event["type"] for event in events] == ["status", "complete"]


@pytest.mark.asyncio
async def test_prepared_product_list_download_is_review_only_and_no_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = AsyncMock(
        return_value=OutboundAttachment(
            filename="Lanya_Chem_all_products_product_list.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            payload=b"xlsx-preview",
        )
    )
    monkeypatch.setattr("app.api.prepared_product_list_attachment", build)
    session = AsyncMock()

    response = await download_prepared_product_list(2013, "admin", session)

    assert response.body == b"xlsx-preview"
    assert response.headers["cache-control"] == "no-store"
    assert "attachment" in response.headers["content-disposition"]
    build.assert_awaited_once_with(session, handoff_id=2013)


def test_handoff_case_request_allows_product_to_remain_pending() -> None:
    request = HandoffCaseRequest(contact_id=1, product_id=None, currency="INR")

    assert request.product_id is None


@pytest.mark.asyncio
async def test_contacts_page_is_protected_no_store_html() -> None:
    response = await contacts_page("admin")

    assert response.status_code == 200
    assert "客户邮箱管理" in response.body.decode("utf-8")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"


def test_contacts_page_exposes_endpoint_level_management() -> None:
    html = CONTACTS_PATH.read_text(encoding="utf-8")

    assert "/admin/contact-directory" in html
    assert "/admin/customers/" in html
    assert "/admin/contacts/" in html
    assert "只停用此邮箱" in html
    assert "旧地址不会被覆盖" in html


@pytest.mark.asyncio
async def test_inbound_dispositions_page_is_protected_dry_run_html() -> None:
    response = await inbound_dispositions_page("admin")

    assert response.status_code == 200
    assert "来信处置审计" in response.body.decode("utf-8")
    assert response.headers["cache-control"] == "no-store"


def test_inbound_dispositions_page_never_exposes_bulk_apply() -> None:
    html = INBOUND_DISPOSITIONS_PATH.read_text(encoding="utf-8")

    assert "/admin/inbound-dispositions/backfill" in html
    assert 'apply:"true"' not in html
    assert "确认应用" in html
    assert "/apply`" in html
    assert "/rollback`" in html


def test_inbound_dispositions_page_hides_apply_for_unresolved_data() -> None:
    html = INBOUND_DISPOSITIONS_PATH.read_text(encoding="utf-8")

    assert "row.can_apply === false" in html
    assert "当前无法应用" in html
    assert "application_blockers" in html


def test_inbound_dispositions_page_recovers_batch_after_refresh() -> None:
    html = INBOUND_DISPOSITIONS_PATH.read_text(encoding="utf-8")

    assert 'const batchStorageKey = "aiemail.inboundDispositionBatchId"' in html
    assert 'url.searchParams.set("batch_id", String(normalized))' in html
    assert "window.localStorage.setItem(batchStorageKey" in html
    assert "const recoveredBatchId = recoverBatchId()" in html
    assert "pollBatch(recoveredBatchId)" in html


def test_inbound_dispositions_page_lists_and_switches_historical_batches() -> None:
    html = INBOUND_DISPOSITIONS_PATH.read_text(encoding="utf-8")

    assert 'id="batch-history"' in html
    assert 'id="view-batch"' in html
    assert 'fetch("/admin/inbound-dispositions/batches?limit=50")' in html
    assert 'const button = $("#refresh-batches");' in html
    assert '$("#batch-history").addEventListener("change", viewSelectedBatch)' in html
    assert "历史批次只读" in html


def test_disposition_confirmation_rejects_stale_classification() -> None:
    error = _validate_inbound_disposition_confirmation(
        {
            "disposition_type": "DEPARTED",
            "blockers": [],
            "latest_action": None,
            "plan_token": "a" * 64,
        },
        expected_disposition_type="TEMPORARY_ABSENCE",
        expected_plan_token="a" * 64,
        acknowledged_blockers=[],
    )

    assert error is not None and "changed since review" in error


def test_disposition_confirmation_rejects_unresolvable_action() -> None:
    error = _validate_inbound_disposition_confirmation(
        {
            "disposition_type": "NON_TARGET",
            "blockers": ["CUSTOMER_NOT_RESOLVED"],
            "application_blockers": ["CUSTOMER_NOT_RESOLVED"],
            "latest_action": None,
            "plan_token": "e" * 64,
        },
        expected_disposition_type="NON_TARGET",
        expected_plan_token="e" * 64,
        acknowledged_blockers=["CUSTOMER_NOT_RESOLVED"],
    )

    assert error == (
        "Disposition cannot be applied until required data is resolved: "
        "CUSTOMER_NOT_RESOLVED"
    )


def test_disposition_confirmation_requires_exact_blocker_acknowledgement() -> None:
    plan = {
        "disposition_type": "DEPARTED",
        "blockers": ["NO_REPLACEMENT_CONTACT"],
        "latest_action": None,
        "plan_token": "b" * 64,
    }

    assert _validate_inbound_disposition_confirmation(
        plan,
        expected_disposition_type="DEPARTED",
        expected_plan_token="b" * 64,
        acknowledged_blockers=[],
    ) == "All current blockers require explicit acknowledgement: NO_REPLACEMENT_CONTACT"
    assert (
        _validate_inbound_disposition_confirmation(
            plan,
            expected_disposition_type="DEPARTED",
            expected_plan_token="b" * 64,
            acknowledged_blockers=["NO_REPLACEMENT_CONTACT"],
        )
        is None
    )


def test_disposition_confirmation_rejects_stale_plan_token() -> None:
    error = _validate_inbound_disposition_confirmation(
        {
            "disposition_type": "NON_TARGET",
            "blockers": [],
            "latest_action": None,
            "plan_token": "c" * 64,
        },
        expected_disposition_type="NON_TARGET",
        expected_plan_token="d" * 64,
        acknowledged_blockers=[],
    )

    assert error == "Disposition plan changed since review; reload before applying"


def test_remote_images_are_only_permitted_by_the_handoff_specific_csp() -> None:
    assert "img-src 'self' data:;" in _dashboard_headers()["Content-Security-Policy"]
    assert (
        "img-src 'self' data: https:;"
        in _dashboard_headers(allow_remote_images=True)["Content-Security-Policy"]
    )


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
    assert "计划 / 当前发送时间" in html
    assert "outbox_available_at" in html
    assert "outbox_last_error" in html
    assert "最早发送" in html
    assert "邮箱滚动 24 小时发送限额" in html
    assert "white-space:nowrap" in html
    assert "选择当前可选项" in html
    assert "启动批次" in html
    assert "暂停" in html
    assert "pendingSelection=new Map()" in html
    assert "setTimeout(()=>flushSelectionQueue()" in html
    assert "await flushSelectionQueue();" in html
