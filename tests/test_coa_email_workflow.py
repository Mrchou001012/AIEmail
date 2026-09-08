from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.coa.coa_service as coa_service
import app.handoffs.handoff_prepared_preview as handoff_service
import app.services as services
from app.ai import InboundAnalysis
from app.coa.coa_catalog import COAFindResult, COAFindStatus
from app.coa.coa_delivery import COAResponseError, PreparedCOAResponse
from app.db import CaseStage, CaseStatus
from app.domain import Intent
from app.mail import OutboundAttachment


def _analysis(product_name: str | None = "LANOPAP-DF 4234") -> InboundAnalysis:
    return InboundAnalysis(
        intent=Intent.COA_REQUEST,
        intent_confidence=0.97,
        requested_product_name=product_name,
        product_confidence=0.93,
        numeric_confidence=0.5,
    )


@pytest.mark.asyncio
async def test_coa_request_creates_review_only_draft_with_pinned_attachment(monkeypatch, tmp_path):
    entry = {
        "path": "PAPER CHEMICALS/LANOPAP-DF 4234/COA-LANOPAP-DF 4234.pdf",
        "product_name": "LANOPAP-DF 4234",
        "product_code": "LANOPAP-DF 4234",
        "sha256": "a" * 64,
        "size": 1234,
    }
    result = COAFindResult(
        status=COAFindStatus.FOUND,
        query="LANOPAP-DF 4234",
        match_basis="exact_alias",
        matches=(entry,),
        auto_send_eligible=True,
    )
    prepared = {
        "path": entry["path"],
        "filename": "COA-LANOPAP-DF 4234.pdf",
        "sha256": "a" * 64,
        "size": 1234,
        "product_name": "LANOPAP-DF 4234",
        "match_basis": "exact_alias",
        "catalog_schema": "coa-catalog.v1",
    }
    response = PreparedCOAResponse(
        subject="COA for LANOPAP-DF 4234",
        body_text="Please find attached the requested COA.",
        product_names=("LANOPAP-DF 4234",),
        prepared_coas=(prepared,),
        attachments=(),
        lookups=({"query": "LANOPAP-DF 4234", "result": result.as_dict()},),
        catalog_schema="coa-catalog.v1",
    )
    create_handoff = AsyncMock()
    monkeypatch.setattr(coa_service, "_prepare_coa_response", lambda **_kwargs: response)
    monkeypatch.setattr(coa_service, "create_handoff", create_handoff)
    monkeypatch.setattr(
        coa_service,
        "get_settings",
        lambda: SimpleNamespace(
            coa_catalog_enabled=True,
            coa_catalog_path=tmp_path / "catalog.json",
            coa_auto_send_enabled=False,
        ),
    )
    case = SimpleNamespace(
        contact=SimpleNamespace(name="Alice Buyer"),
        product=None,
    )
    email = SimpleNamespace(id=8, subject="COA for LANOPAP-DF 4234")

    handled = await services._maybe_handle_coa_request(
        object(),
        case=case,
        email_row=email,
        analysis=_analysis(),
        analysis_facts=_analysis().model_dump(mode="json"),
    )

    assert handled is True
    facts = create_handoff.await_args.kwargs["facts"]
    assert facts["prepared_coa"] == prepared
    assert facts["ai_draft_preview"]["provider"] == "deterministic-coa"
    assert "Please find attached" in facts["ai_draft_preview"]["body_text"]
    assert create_handoff.await_args.kwargs["source_email_id"] == 8


@pytest.mark.asyncio
async def test_coa_request_with_no_unique_match_asks_for_specific_human_help(monkeypatch, tmp_path):
    create_handoff = AsyncMock()
    def ambiguous_response(**_kwargs):
        raise COAResponseError(
            "COA match is ambiguous and requires human selection",
            help_needed="confirm the correct suffix-free standard English COA",
        )

    monkeypatch.setattr(coa_service, "_prepare_coa_response", ambiguous_response)
    monkeypatch.setattr(coa_service, "create_handoff", create_handoff)
    monkeypatch.setattr(
        coa_service,
        "get_settings",
        lambda: SimpleNamespace(
            coa_catalog_enabled=True,
            coa_catalog_path=tmp_path / "catalog.json",
            coa_auto_send_enabled=False,
        ),
    )
    case = SimpleNamespace(contact=SimpleNamespace(name="Buyer"), product=None)
    email = SimpleNamespace(id=9, subject="Please send COA")

    await services._maybe_handle_coa_request(
        object(),
        case=case,
        email_row=email,
        analysis=_analysis("YAC-A110"),
        analysis_facts=_analysis("YAC-A110").model_dump(mode="json"),
    )

    facts = create_handoff.await_args.kwargs["facts"]
    assert "confirm the correct suffix-free standard English COA" == facts["coa_help_needed"]
    assert "prepared_coa" not in facts


@pytest.mark.asyncio
async def test_secondary_coa_request_queues_verified_reply_and_allows_quote_to_continue(
    monkeypatch,
    tmp_path,
):
    prepared = {
        "path": "SILANES/YAC-HMDS/COA-YAC-HMDS.pdf",
        "filename": "COA-YAC-HMDS.pdf",
        "sha256": "b" * 64,
        "size": 8,
        "product_name": "YAC-HMDS",
        "match_basis": "exact_alias",
        "catalog_schema": "coa-catalog.v1",
    }
    attachment = OutboundAttachment(
        filename="COA-YAC-HMDS.pdf",
        content_type="application/pdf",
        payload=b"verified",
    )
    response = PreparedCOAResponse(
        subject="Re: HMDS quotation",
        body_text="Dear Buyer,\n\nPlease find attached the requested COA.",
        product_names=("YAC-HMDS",),
        prepared_coas=(prepared,),
        attachments=(attachment,),
        lookups=({"query": "YAC-HMDS", "result": {"status": "found"}},),
        catalog_schema="coa-catalog.v1",
    )
    settings = SimpleNamespace(
        coa_catalog_enabled=True,
        coa_catalog_path=tmp_path / "catalog.json",
        coa_auto_send_enabled=True,
        intent_confidence_threshold=0.80,
        product_confidence_threshold=0.85,
        numeric_confidence_threshold=0.90,
        content_dir=tmp_path,
    )
    stage_outbox = AsyncMock(return_value=SimpleNamespace(id=42))
    monkeypatch.setattr(coa_service, "get_settings", lambda: settings)
    monkeypatch.setattr(coa_service, "_prepare_coa_response", lambda **_kwargs: response)
    monkeypatch.setattr(
        coa_service,
        "evaluate_send_policy",
        lambda *_args, **_kwargs: SimpleNamespace(allow_send=True, reason=None),
    )
    monkeypatch.setattr(
        coa_service,
        "load_content",
        lambda _path: SimpleNamespace(signature_text="Regards", signature_html=""),
    )
    monkeypatch.setattr(
        coa_service,
        "_reply_source",
        lambda _email: SimpleNamespace(body_text="Original", body_html=None, inline_images=()),
    )
    monkeypatch.setattr(coa_service, "stage_outbox", stage_outbox)
    monkeypatch.setattr(coa_service, "audit", AsyncMock())

    analysis = _analysis("YAC-HMDS").model_copy(
        update={"intent": Intent.QUOTE_REQUEST, "coa_requested": True}
    )
    case = SimpleNamespace(
        id=7,
        stage=CaseStage.QUOTING,
        status=CaseStatus.ACTIVE,
        customer=SimpleNamespace(auto_send_allowed=True, do_not_contact=False),
        contact=SimpleNamespace(name="Buyer", suppressed=False),
        product=None,
    )
    email = SimpleNamespace(
        id=18,
        subject="HMDS quotation",
        body_text="Please quote HMDS and send the COA.",
        from_address="buyer@example.com",
        received_at=datetime.now(UTC),
        message_id="<inbound@example.com>",
        in_reply_to=None,
        references_json=[],
    )

    handled = await services._maybe_handle_coa_request(
        object(),
        case=case,
        email_row=email,
        analysis=analysis,
        analysis_facts=analysis.model_dump(mode="json"),
    )

    assert handled is False
    assert stage_outbox.await_args.kwargs["message_kind"] == "COA"
    assert stage_outbox.await_args.kwargs["attachments"] == (attachment,)


@pytest.mark.asyncio
async def test_partial_coa_reply_sends_available_file_and_creates_missing_item_handoff(
    monkeypatch,
    tmp_path,
):
    prepared = {
        "path": "SILANES/YAC-HMDS/COA-YAC-HMDS.pdf",
        "filename": "COA-YAC-HMDS.pdf",
        "sha256": "c" * 64,
        "size": 8,
        "product_name": "YAC-HMDS",
        "match_basis": "exact_alias",
        "catalog_schema": "coa-catalog.v1",
    }
    attachment = OutboundAttachment(
        filename="COA-YAC-HMDS.pdf",
        content_type="application/pdf",
        payload=b"verified",
    )
    response = PreparedCOAResponse(
        subject="Re: COAs",
        body_text=(
            "Dear Buyer,\n\nPlease find attached the COA for YAC-HMDS.\n\n"
            "The COA for YAC-TMCS is not included in this email."
        ),
        product_names=("YAC-HMDS",),
        prepared_coas=(prepared,),
        attachments=(attachment,),
        lookups=(),
        catalog_schema="coa-catalog.v1",
        missing_queries=("YAC-TMCS",),
    )
    settings = SimpleNamespace(
        coa_catalog_enabled=True,
        coa_catalog_path=tmp_path / "catalog.json",
        coa_auto_send_enabled=True,
        intent_confidence_threshold=0.80,
        product_confidence_threshold=0.85,
        numeric_confidence_threshold=0.90,
        content_dir=tmp_path,
    )
    stage_outbox = AsyncMock(return_value=SimpleNamespace(id=71))
    create_handoff = AsyncMock()
    monkeypatch.setattr(coa_service, "get_settings", lambda: settings)
    monkeypatch.setattr(coa_service, "_prepare_coa_response", lambda **_kwargs: response)
    monkeypatch.setattr(
        coa_service,
        "evaluate_send_policy",
        lambda *_args, **_kwargs: SimpleNamespace(allow_send=True, reason=None),
    )
    monkeypatch.setattr(
        coa_service,
        "load_content",
        lambda _path: SimpleNamespace(signature_text="Regards", signature_html=""),
    )
    monkeypatch.setattr(
        coa_service,
        "_reply_source",
        lambda _email: SimpleNamespace(body_text="Original", body_html=None, inline_images=()),
    )
    monkeypatch.setattr(coa_service, "stage_outbox", stage_outbox)
    monkeypatch.setattr(coa_service, "create_handoff", create_handoff)
    monkeypatch.setattr(coa_service, "audit", AsyncMock())

    analysis = _analysis("YAC-HMDS").model_copy(
        update={"intent": Intent.QUOTE_REQUEST, "coa_requested": True}
    )
    case = SimpleNamespace(
        id=7,
        stage=CaseStage.QUOTING,
        status=CaseStatus.ACTIVE,
        customer=SimpleNamespace(auto_send_allowed=True, do_not_contact=False),
        contact=SimpleNamespace(name="Buyer", suppressed=False),
        product=None,
    )
    email = SimpleNamespace(
        id=19,
        subject="COAs",
        body_text="Please quote and send HMDS and TMCS COAs.",
        from_address="buyer@example.com",
        received_at=datetime.now(UTC),
        message_id="<partial@example.com>",
        in_reply_to=None,
        references_json=[],
    )

    handled = await services._maybe_handle_coa_request(
        object(),
        case=case,
        email_row=email,
        analysis=analysis,
        analysis_facts=analysis.model_dump(mode="json"),
    )

    assert handled is False
    assert stage_outbox.await_args.kwargs["attachments"] == (attachment,)
    handoff_call = create_handoff.await_args.kwargs
    assert handoff_call["reason"].value == "COA_REVIEW"
    assert handoff_call["facts"]["missing_coa_queries"] == ["YAC-TMCS"]
    assert handoff_call["facts"]["partial_coa_outbox_id"] == 71
    assert "ai_draft_preview" not in handoff_call["facts"]


@pytest.mark.asyncio
async def test_generic_handoff_preview_automatically_routes_explicit_coa_request(
    monkeypatch,
):
    preview = {
        "subject": "Re: HMDS",
        "body_text": "Please find attached the requested COA.",
        "delivery_created": False,
    }
    prepare = AsyncMock(return_value={"preview": preview})
    monkeypatch.setattr(handoff_service, "prepare_detected_coa_preview", prepare)
    analysis = _analysis("YAC-HMDS").model_copy(
        update={"intent": Intent.QUOTE_REQUEST, "coa_requested": True}
    )
    handoff = SimpleNamespace(extracted_facts={})
    source_email = SimpleNamespace()
    sales_case = SimpleNamespace()
    settings = SimpleNamespace()

    result = await services._prepared_handoff_draft_preview(
        object(),
        handoff=handoff,
        source_email=source_email,
        sales_case=sales_case,
        analysis=analysis,
        actor="reviewer",
        settings=settings,
    )

    assert result == preview
    assert prepare.await_args.kwargs["persist"] is False
