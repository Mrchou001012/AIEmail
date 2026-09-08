from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.coa.coa_catalog import COACatalog, COACatalogScanner, COAFindStatus
from app.coa.coa_delivery import prepare_coa_response


def _pdf(path: Path, payload: bytes = b"fake pdf") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def test_scanner_selects_only_suffix_free_english_coa_and_builds_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "PRODUCT DOCS"
    standard = root / "OTHER PRODUCT" / "AcAc" / "COA-AcAc.pdf"
    _pdf(standard, b"standard-acac")
    _pdf(root / "OTHER PRODUCT" / "AcAc" / "COA-AcAc Korea SK.pdf")
    _pdf(root / "OTHER PRODUCT" / "AcAc" / "COA-AcAc 2026-08-01.pdf")
    _pdf(root / "OTHER PRODUCT" / "AcAc" / "COA-AcAc 中文版.pdf")
    _pdf(root / "OTHER PRODUCT" / "AcAc" / "TDS-AcAc.pdf")
    _pdf(root / "OTHER PRODUCT" / "仅内部" / "COA-Internal.pdf")
    output = tmp_path / "runtime" / "coa.json"

    monkeypatch.setattr(
        "app.coa.coa_catalog.extract_document_bounded",
        lambda path, timeout_seconds: "Product: Acetylacetone\nCAS No. 123-54-6",
    )
    payload = COACatalogScanner(root=root, output_path=output).scan()

    assert payload["candidate_file_count"] == 5
    assert payload["selected_count"] == 1
    assert payload["entries"][0]["path"] == "OTHER PRODUCT/AcAc/COA-AcAc.pdf"
    assert payload["entries"][0]["cas_numbers"] == ["123-54-6"]
    assert payload["entries"][0]["selection_basis"] == (
        "COA filename exactly matches the product directory"
    )
    excluded = {
        candidate["path"]: candidate["reason"]
        for decision in payload["review"]
        for candidate in decision["candidates"]
    }
    assert "extra suffix" in excluded["OTHER PRODUCT/AcAc/COA-AcAc Korea SK.pdf"]
    assert "date" in excluded["OTHER PRODUCT/AcAc/COA-AcAc 2026-08-01.pdf"]
    assert "Chinese" in excluded["OTHER PRODUCT/AcAc/COA-AcAc 中文版.pdf"]
    assert "Chinese" in excluded["OTHER PRODUCT/仅内部/COA-Internal.pdf"]

    catalog = COACatalog(output)
    by_name = catalog.find("AcAc")
    assert by_name.status is COAFindStatus.FOUND
    assert by_name.match_basis == "exact_alias"
    assert by_name.auto_send_eligible is True
    assert catalog.find("anything", cas_number="123-54-6").status is COAFindStatus.FOUND
    assert catalog.read_verified_attachment(by_name.matches[0]) == b"standard-acac"


def test_scanner_holds_multiple_or_suffixed_coas_for_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "PRODUCT DOCS"
    _pdf(root / "SILANE" / "YAC-A110" / "COA.pdf")
    _pdf(root / "SILANE" / "YAC-A110" / "COA-YAC-A110.pdf")
    _pdf(root / "SILANE" / "YAC-A111" / "COA-YAC-A111 customer.pdf")
    output = tmp_path / "coa.json"
    monkeypatch.setattr("app.coa.coa_catalog.extract_document_bounded", lambda path, timeout_seconds: "")

    payload = COACatalogScanner(root=root, output_path=output).scan()

    assert payload["selected_count"] == 0
    assert {row["product_name"] for row in payload["review"]} == {"YAC-A110", "YAC-A111"}
    catalog = COACatalog(output)
    result = catalog.find("YAC-A110")
    assert result.status is COAFindStatus.AMBIGUOUS
    assert result.match_basis == "product_requires_coa_review"


def test_incremental_scan_reuses_unchanged_selected_file_and_removes_deleted_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "PRODUCT DOCS"
    selected = root / "OTHER" / "ACETONE" / "COA-ACETONE.pdf"
    _pdf(selected)
    output = tmp_path / "coa.json"
    calls: list[Path] = []

    def extract(path: Path, *, timeout_seconds: int) -> str:
        calls.append(path)
        return "CAS 67-64-1"

    monkeypatch.setattr("app.coa.coa_catalog.extract_document_bounded", extract)
    scanner = COACatalogScanner(root=root, output_path=output)
    first = scanner.scan()
    second = scanner.scan()

    assert first["changed_count"] == 1
    assert second["changed_count"] == 0
    assert len(calls) == 1
    selected.unlink()
    third = scanner.scan()
    assert third["selected_count"] == 0
    assert json.loads(output.read_text(encoding="utf-8"))["entries"] == []


def test_verified_attachment_rejects_file_changed_after_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "PRODUCT DOCS"
    selected = root / "OTHER" / "ACETONE" / "COA-ACETONE.pdf"
    _pdf(selected, b"approved")
    output = tmp_path / "coa.json"
    monkeypatch.setattr("app.coa.coa_catalog.extract_document_bounded", lambda path, timeout_seconds: "CAS 67-64-1")
    COACatalogScanner(root=root, output_path=output).scan()
    catalog = COACatalog(output)
    result = catalog.find("ACETONE")
    selected.write_bytes(b"changed")

    with pytest.raises(ValueError, match="changed after catalog selection"):
        catalog.read_verified_attachment(result.matches[0])


def test_prepare_coa_response_verifies_every_product_in_mixed_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "PRODUCT DOCS"
    _pdf(root / "SILANES" / "YAC-HMDS" / "COA-YAC-HMDS.pdf", b"hmds")
    _pdf(root / "SILANES" / "YAC-TMCS" / "COA-YAC-TMCS.pdf", b"tmcs")
    output = tmp_path / "coa.json"
    monkeypatch.setattr(
        "app.coa.coa_catalog.extract_document_bounded",
        lambda path, timeout_seconds: "",
    )
    COACatalogScanner(root=root, output_path=output).scan()
    settings = SimpleNamespace(coa_catalog_enabled=True, coa_catalog_path=output)

    response = prepare_coa_response(
        settings=settings,
        contact_name="Buyer",
        original_subject="Please quote HMDS and TMCS",
        product_queries=["YAC-HMDS", "YAC-TMCS"],
    )

    assert response.product_names == ("YAC-HMDS", "YAC-TMCS")
    assert [item.filename for item in response.attachments] == [
        "COA-YAC-HMDS.pdf",
        "COA-YAC-TMCS.pdf",
    ]
    assert [item.payload for item in response.attachments] == [b"hmds", b"tmcs"]


def test_prepare_coa_response_returns_verified_available_files_and_missing_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "PRODUCT DOCS"
    _pdf(root / "SILANES" / "YAC-HMDS" / "COA-YAC-HMDS.pdf", b"hmds")
    output = tmp_path / "coa.json"
    monkeypatch.setattr(
        "app.coa.coa_catalog.extract_document_bounded",
        lambda path, timeout_seconds: "",
    )
    COACatalogScanner(root=root, output_path=output).scan()

    response = prepare_coa_response(
        settings=SimpleNamespace(coa_catalog_enabled=True, coa_catalog_path=output),
        contact_name="Buyer",
        original_subject="Please send HMDS and TMCS COAs",
        product_queries=["YAC-HMDS", "YAC-TMCS"],
    )

    assert response.product_names == ("YAC-HMDS",)
    assert response.missing_queries == ("YAC-TMCS",)
    assert [item.payload for item in response.attachments] == [b"hmds"]
    assert "YAC-TMCS is not included" in response.body_text
    facts = response.as_facts()
    assert facts["coa_partial"] is True
    assert facts["missing_coa_queries"] == ["YAC-TMCS"]
    assert "prepared_coa" not in facts
