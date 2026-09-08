from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import app.coa.coa_preview as coa_preview
from app.ai import InboundAnalysis
from app.coa.coa_delivery import PreparedCOAResponse
from app.db import CaseStatus
from app.domain import Intent


@pytest.mark.asyncio
async def test_historical_coa_preview_never_creates_delivery(monkeypatch, tmp_path) -> None:
    handoff = SimpleNamespace(
        id=12,
        status="OPEN",
        source_email_id=2853,
        case_id=1915,
        reason_code="INVENTORY_UNAVAILABLE",
        summary="Old quote blocker",
        extracted_facts={},
    )
    email = SimpleNamespace(
        id=2853,
        direction="INBOUND",
        subject="Re: Checking in from Lanya Chem",
        body_text="Please quote HMDS with current COA.",
        attachment_metadata=[],
    )
    sales_case = SimpleNamespace(
        id=1915,
        status=CaseStatus.WAITING_HUMAN,
        customer=SimpleNamespace(),
        contact=SimpleNamespace(name="Buyer"),
        product=SimpleNamespace(code="YAC-HMDS", name="HMDS"),
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[handoff, sales_case]),
        get=AsyncMock(return_value=email),
        add=Mock(),
        commit=AsyncMock(),
    )
    analysis = InboundAnalysis(
        intent=Intent.QUOTE_REQUEST,
        intent_confidence=0.99,
        quote_requested=True,
        coa_requested=True,
        requested_product_name="YAC-HMDS",
        product_confidence=0.99,
        numeric_confidence=1.0,
    )
    ai = SimpleNamespace(analyze=AsyncMock(return_value=(analysis, {"model": "test"})))
    monkeypatch.setattr(coa_preview, "AIClient", lambda _settings: ai)
    monkeypatch.setattr(coa_preview, "ensure_handoff_agent_run", AsyncMock())
    monkeypatch.setattr(
        coa_preview,
        "prepare_coa_response",
        lambda **_kwargs: PreparedCOAResponse(
            subject="Re: Checking in from Lanya Chem",
            body_text="Dear Buyer,\n\nPlease find attached the COA for YAC-HMDS.",
            product_names=("YAC-HMDS",),
            prepared_coas=(
                {
                    "path": "SILANES/YAC-HMDS/COA-YAC-HMDS.pdf",
                    "filename": "COA-YAC-HMDS.pdf",
                    "sha256": "a" * 64,
                    "size": 4,
                    "product_name": "YAC-HMDS",
                    "match_basis": "exact_alias",
                    "catalog_schema": "coa-catalog.v1",
                },
            ),
            attachments=(),
            lookups=({"query": "YAC-HMDS", "result": {"status": "found"}},),
            catalog_schema="coa-catalog.v1",
        ),
    )
    settings = SimpleNamespace(
        coa_catalog_enabled=True,
        coa_catalog_path=tmp_path / "catalog.json",
    )

    result = await coa_preview.prepare_handoff_coa_preview(
        session,
        handoff_id=handoff.id,
        actor="reviewer",
        settings=settings,
    )

    assert result["handoff_id"] == 12
    assert result["coa_requested"] is True
    assert result["prepared_count"] == 1
    assert result["missing_coa_queries"] == []
    assert result["delivery_created"] is False
    assert result["preview"]["subject"] == "Re: Checking in from Lanya Chem"
    assert handoff.reason_code == "COA_REVIEW"
    assert handoff.extracted_facts["prepared_coa"]["filename"] == "COA-YAC-HMDS.pdf"
    assert sales_case.status == CaseStatus.WAITING_HUMAN
    assert session.commit.await_count == 1
