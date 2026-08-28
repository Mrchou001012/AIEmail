from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services as services
from app.ai import InboundAnalysis
from app.coa_catalog import COAFindResult, COAFindStatus
from app.domain import Intent


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
    fake_catalog = SimpleNamespace(schema_version="coa-catalog.v1", find=lambda *_args, **_kwargs: result)
    create_handoff = AsyncMock()
    monkeypatch.setattr(services, "COACatalog", lambda _path: fake_catalog)
    monkeypatch.setattr(services, "create_handoff", create_handoff)
    monkeypatch.setattr(
        services,
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
    assert facts["prepared_coa"] == {
        "path": entry["path"],
        "filename": "COA-LANOPAP-DF 4234.pdf",
        "sha256": "a" * 64,
        "size": 1234,
        "product_name": "LANOPAP-DF 4234",
        "match_basis": "exact_alias",
        "catalog_schema": "coa-catalog.v1",
    }
    assert facts["ai_draft_preview"]["provider"] == "deterministic-coa"
    assert "Please find attached" in facts["ai_draft_preview"]["body_text"]
    assert create_handoff.await_args.kwargs["source_email_id"] == 8


@pytest.mark.asyncio
async def test_coa_request_with_no_unique_match_asks_for_specific_human_help(monkeypatch, tmp_path):
    result = COAFindResult(
        status=COAFindStatus.AMBIGUOUS,
        query="YAC-A110",
        match_basis="product_requires_coa_review",
        matches=({"product_name": "YAC-A110", "candidates": []},),
        auto_send_eligible=False,
    )
    fake_catalog = SimpleNamespace(find=lambda *_args, **_kwargs: result)
    create_handoff = AsyncMock()
    monkeypatch.setattr(services, "COACatalog", lambda _path: fake_catalog)
    monkeypatch.setattr(services, "create_handoff", create_handoff)
    monkeypatch.setattr(
        services,
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
