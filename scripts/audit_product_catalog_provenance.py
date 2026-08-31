from __future__ import annotations

import argparse
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import yaml

AUTHORITATIVE_PREFIX = "!PRODUCT DATA/!PRODUCT DOCS/"
PRODUCT_LIST_PREFIX = "!PRODUCT DATA/PRODUCT LIST/"
INTERNAL_PRODUCT_PREFIXES = (
    "TRAINING/PRODUCT/",
    "LANYA-INDIA/TRAINING DATA/",
)
TRUSTED_DOCUMENT_MARKERS = (
    "/COA-",
    "/SPEC-",
    "/SDS-",
    "/MOA-",
    "/ROS-",
    "产品资料检索表.xlsx",
    "PRODUCT LIST",
    "宣传册",
)


def normalized(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def code_without_grade(value: object) -> str:
    return re.sub(r"\s*\(\s*\d+(?:\.\d+)?%\s*\)\s*$", "", str(value or "").strip())


def name_without_abbreviation(value: object) -> str:
    return re.sub(r"\s*\([A-Z][A-Z0-9-]{1,12}\)\s*$", "", str(value or "").strip())


def text_contains(value: object, text: str) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    return raw.casefold() in text.casefold()


def source_rank(row: dict[str, Any]) -> tuple[int, int, str]:
    path = str(row.get("path") or "")
    classification = str(row.get("classification") or "")
    if row.get("_source_kind") == "approved_source_doc":
        tier = 1
    elif path.startswith(AUTHORITATIVE_PREFIX) and any(marker.casefold() in path.casefold() for marker in TRUSTED_DOCUMENT_MARKERS):
        tier = 0
    elif path.startswith(PRODUCT_LIST_PREFIX):
        tier = 1
    elif path.startswith(AUTHORITATIVE_PREFIX):
        tier = 2
    elif path.startswith(INTERNAL_PRODUCT_PREFIXES):
        tier = 3
    else:
        tier = 4
    class_rank = 0 if classification == "customer_ready" else 1
    return tier, class_rank, path


@dataclass(frozen=True)
class FieldAudit:
    status: str
    source_path: str | None
    evidence: str | None
    note: str


def matching_rows(rows: list[dict[str, Any]], value: object) -> list[dict[str, Any]]:
    raw = str(value or "").strip()
    if not raw:
        return []
    raw_folded = raw.casefold()
    raw_normalized = normalized(raw)
    exact: list[dict[str, Any]] = []
    normalized_matches: list[dict[str, Any]] = []
    for row in rows:
        combined_folded = str(row.get("_combined_folded") or "")
        if raw_folded in combined_folded:
            exact.append(row)
        elif raw_normalized and raw_normalized in str(row.get("_combined_normalized") or ""):
            normalized_matches.append(row)
    return sorted((*exact, *normalized_matches), key=source_rank)


def identity_rows(rows: list[dict[str, Any]], product: dict[str, Any]) -> list[dict[str, Any]]:
    identities = [
        product.get("code"),
        code_without_grade(product.get("code")),
        product.get("catalog_code"),
        product.get("cas_no"),
        product.get("name"),
        name_without_abbreviation(product.get("name")),
    ]
    candidates: dict[tuple[str, int], dict[str, Any]] = {}
    for value in identities:
        for row in matching_rows(rows, value):
            key = (str(row.get("path") or ""), int(row.get("chunk") or row.get("_source_row") or 0))
            candidates[key] = row
    return sorted(candidates.values(), key=source_rank)


def evidence_excerpt(row: dict[str, Any], value: object, limit: int = 500) -> str:
    text = str(row.get("text") or "")
    raw = str(value or "").strip()
    position = text.casefold().find(raw.casefold()) if raw else -1
    start = max(0, position - 120) if position >= 0 else 0
    return text[start : start + limit].strip()


def audit_field(rows: list[dict[str, Any]], field: str, value: object) -> FieldAudit:
    raw = str(value or "").strip()
    if not raw:
        return FieldAudit("blank", None, None, "Source catalog leaves this field blank.")
    matches = matching_rows(rows, raw)
    if not matches:
        return FieldAudit("unsupported", None, None, "No matching value found in the indexed knowledge sources.")
    best = matches[0]
    combined = f"{best.get('path', '')}\n{best.get('text', '')}"
    status = "verified" if text_contains(raw, combined) else "normalized"
    note = "Exact value appears in source." if status == "verified" else "Formatting-normalized value appears in source."
    if (
        field == "code"
        and best.get("_source_kind") != "approved_source_doc"
        and not str(best.get("path") or "").startswith((AUTHORITATIVE_PREFIX, PRODUCT_LIST_PREFIX, *INTERNAL_PRODUCT_PREFIXES))
    ):
        status = "weak_evidence"
        note = "Value appears only outside the preferred product-document sources."
    return FieldAudit(
        status,
        str(best.get("path") or ""),
        evidence_excerpt(best, raw),
        note,
    )


CATEGORY_LABELS = {
    "industrial_silanes": ("Industrial Silanes", "Industrial silane", "工业硅烷"),
    "pharmaceutical": ("Pharmaceutical", "Pharmaceutical silane", "医药", "医药硅烷"),
    "rubber_plastics": ("Rubber & Plastics", "Rubber and Plastics", "橡塑"),
    "acetylacetone_salts": (
        "Acetylacetone & Its Salts",
        "Acetylacetone & its salts",
        "乙酰丙酮及其盐类",
    ),
    "silicone_oil": ("Silicone Oil", "硅油"),
}


def audit_product_field(identity: list[dict[str, Any]], field: str, value: object) -> FieldAudit:
    raw = str(value or "").strip()
    if not raw:
        return FieldAudit("blank", None, None, "Field is intentionally blank; no value was inferred.")
    if raw.casefold() in {"-", "n/a", "na", "unknown"}:
        return FieldAudit("placeholder", None, None, "Placeholder is not a customer-facing factual value.")

    values = CATEGORY_LABELS.get(raw, (raw,)) if field == "category" else (raw,)
    for candidate in values:
        exact = [row for row in identity if text_contains(candidate, str(row.get("_combined") or ""))]
        if exact:
            best = sorted(exact, key=source_rank)[0]
            return FieldAudit(
                "verified",
                str(best.get("path") or ""),
                evidence_excerpt(best, candidate),
                "Exact value appears in the same product evidence.",
            )

    normalized_values = [normalized(candidate) for candidate in values if normalized(candidate)]
    normalized_matches = [
        row
        for row in identity
        if any(candidate in str(row.get("_combined_normalized") or "") for candidate in normalized_values)
    ]
    if normalized_matches:
        best = sorted(normalized_matches, key=source_rank)[0]
        return FieldAudit(
            "normalized",
            str(best.get("path") or ""),
            evidence_excerpt(best, raw),
            "Formatting-normalized value appears in the same product evidence.",
        )

    if field == "code":
        base = code_without_grade(raw)
        if base != raw:
            base_matches = [row for row in identity if text_contains(base, str(row.get("_combined") or ""))]
            if base_matches:
                best = sorted(base_matches, key=source_rank)[0]
                return FieldAudit(
                    "conflict",
                    str(best.get("path") or ""),
                    evidence_excerpt(best, base),
                    f"Source identifies the code as {base}; purity is a specification, not part of the evidenced code.",
                )
    if field == "name":
        core = normalized(name_without_abbreviation(raw))
        core_matches = [
            row for row in identity if core and core in str(row.get("_combined_normalized") or "")
        ]
        if core_matches:
            best = sorted(core_matches, key=source_rank)[0]
            return FieldAudit(
                "normalized",
                str(best.get("path") or ""),
                evidence_excerpt(best, name_without_abbreviation(raw)),
                "Source confirms the product name; trailing abbreviation is display-only.",
            )

    return FieldAudit("unsupported", None, None, "No matching value found in evidence tied to this product identity.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit curated product-catalog fields against the local NAS knowledge index.")
    parser.add_argument("--catalog", type=Path, default=Path("config/product_catalog.yaml"))
    parser.add_argument("--knowledge-index", type=Path, default=Path("runtime/nas_knowledge/knowledge_index.json"))
    parser.add_argument("--source-doc", type=Path)
    parser.add_argument("--output", type=Path, default=Path("runtime/product_catalog_provenance_audit.json"))
    return parser.parse_args()


WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _word_text(element: ElementTree.Element) -> str:
    return " ".join(
        text.strip()
        for text in element.itertext()
        if text and text.strip()
    )


def docx_knowledge_rows(path: Path) -> list[dict[str, Any]]:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    body = root.find(f"{{{WORD_NS}}}body")
    if body is None:
        return []

    category: str | None = None
    series: str | None = None
    rubber_headers: list[str] | None = None
    result: list[dict[str, Any]] = []
    source_row = 0

    def add_row(text: str) -> None:
        nonlocal source_row
        source_row += 1
        combined = "\n".join(
            part
            for part in (
                f"Category: {category}" if category else "",
                f"Series: {series}" if series else "",
                text,
            )
            if part
        )
        result.append(
            {
                "path": str(path),
                "classification": "internal",
                "text": combined,
                "_source_kind": "approved_source_doc",
                "_source_row": source_row,
                "_combined": f"{path}\n{combined}",
                "_combined_folded": f"{path}\n{combined}".casefold(),
                "_combined_normalized": normalized(f"{path}\n{combined}"),
            }
        )

    for child in body:
        local = child.tag.rsplit("}", 1)[-1]
        if local == "p":
            text = _word_text(child).strip().rstrip(":：").strip()
            heading = re.sub(r"\s+", "", text)
            if heading in {"工业硅烷", "医药", "橡塑"}:
                category = {
                    "工业硅烷": "Industrial Silanes 工业硅烷",
                    "医药": "Pharmaceutical 医药",
                    "橡塑": "Rubber & Plastics 橡塑",
                }[heading]
                series = None
                rubber_headers = None
            continue
        if local != "tbl":
            continue

        table_rows: list[list[str]] = []
        for table_row in child.findall(f"{{{WORD_NS}}}tr"):
            cells = [
                _word_text(cell).strip()
                for cell in table_row.findall(f"{{{WORD_NS}}}tc")
            ]
            table_rows.append(cells)

        for cells in table_rows:
            nonempty = [cell for cell in cells if cell]
            if not nonempty:
                continue
            if category and category.startswith("Rubber & Plastics"):
                if rubber_headers is None:
                    rubber_headers = cells
                    continue
                for column, value in enumerate(cells):
                    if not value:
                        continue
                    series = rubber_headers[column] if column < len(rubber_headers) else None
                    add_row(f"Product Name: {value}")
                continue
            if len(set(nonempty)) == 1:
                candidate = nonempty[0]
                if "series" in candidate.casefold() or candidate in {"Other Products"}:
                    series = candidate
                continue
            headers = {
                "sno.",
                "brand",
                "product name",
                "product",
                "cas no.",
                "cas number",
                "content",
            }
            if any(header.casefold() in headers for header in nonempty):
                continue
            if len(cells) >= 5 and cells[0].strip().isdigit():
                add_row(
                    "\n".join(
                        (
                            f"Code: {cells[1]}",
                            f"Product Name: {cells[2]}",
                            f"CAS No.: {cells[3]}",
                            f"Content: {cells[4]}",
                        )
                    )
                )
            elif len(cells) == 2:
                if cells[0] and cells[1] and cells[0] == cells[1]:
                    series = cells[0]
                elif category and category.startswith("Industrial Silanes"):
                    add_row(f"Code: {cells[0]}\nProduct Name: {cells[1]}")
                else:
                    add_row(f"Product Name: {cells[0]}\nCAS No.: {cells[1]}")
    return result


def main() -> None:
    args = parse_args()
    catalog = yaml.safe_load(args.catalog.read_text(encoding="utf-8")) or {}
    index = json.loads(args.knowledge_index.read_text(encoding="utf-8"))
    rows = [
        {
            **row,
            "_combined": f"{row.get('path', '')}\n{row.get('text', '')}",
            "_combined_folded": f"{row.get('path', '')}\n{row.get('text', '')}".casefold(),
            "_combined_normalized": normalized(f"{row.get('path', '')}\n{row.get('text', '')}"),
        }
        for row in index.get("chunks", [])
        if row.get("classification") in {"customer_ready", "internal"}
    ]
    if args.source_doc is not None:
        rows.extend(docx_knowledge_rows(args.source_doc))
    results = []
    for number, product in enumerate(catalog.get("products") or [], start=1):
        evidence = identity_rows(rows, product)
        catalog_code = (
            product.get("catalog_code")
            if "catalog_code" in product
            else product.get("code")
        )
        fields = {
            field: audit_product_field(evidence, field, product.get(field))
            for field in ("name", "cas_no", "content", "series")
        }
        fields["catalog_code"] = audit_product_field(
            evidence, "code", catalog_code
        )
        category = audit_product_field(evidence, "category", product.get("category"))
        statuses = [audit.status for audit in (*fields.values(), category)]
        visible = bool(product.get("catalog_visible", True))
        result_status = (
            "excluded"
            if not visible
            else "review_required"
            if any(
                status in {"unsupported", "weak_evidence", "conflict", "placeholder"}
                for status in statuses
            )
            else "verified"
        )
        results.append(
            {
                "number": number,
                "category": product.get("category"),
                "code": product.get("code"),
                "catalog_code": catalog_code,
                "catalog_visible": visible,
                "name": product.get("name"),
                "cas_no": product.get("cas_no"),
                "content": product.get("content"),
                "series": product.get("series"),
                "status": result_status,
                "evidence_row_count": len(evidence),
                "fields": {
                    field: {
                        "status": audit.status,
                        "source_path": audit.source_path,
                        "evidence": audit.evidence,
                        "note": audit.note,
                    }
                    for field, audit in {**fields, "category": category}.items()
                },
            }
        )
    summary: dict[str, int] = {}
    for result in results:
        summary[result["status"]] = summary.get(result["status"], 0) + 1
    payload = {
        "schema_version": "product-catalog-provenance-audit.v1",
        "catalog": str(args.catalog),
        "knowledge_index": str(args.knowledge_index),
        "source_doc": str(args.source_doc) if args.source_doc is not None else None,
        "source_document_count": len({str(row.get("path") or "") for row in rows}),
        "product_count": len(results),
        "summary": summary,
        "products": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "product_count": len(results), "summary": summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
