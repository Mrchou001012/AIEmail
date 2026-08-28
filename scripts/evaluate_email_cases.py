from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.ai import generic_product_list_requested, stub_analyze
from app.coa_catalog import COACatalog, COAFindStatus
from app.domain import Intent
from app.mail import parse_mime


def _predicted_route(
    *,
    analysis,
    request_text: str,
    catalog: COACatalog | None,
) -> tuple[str, list[str]]:
    blockers: list[str] = []
    if analysis.intent == Intent.PRODUCT_LIST_REQUEST:
        if generic_product_list_requested(request_text):
            return "full_product_catalog_draft", blockers
        blockers.append("product category or approved scoped catalog is required")
        return "product_category_assistance", blockers
    if analysis.intent == Intent.QUOTE_REQUEST:
        if analysis.packaging_requested:
            blockers.append("approved packing details are required")
        codes = [
            line.product_code
            for line in analysis.product_requests
            if line.product_code
        ]
        if not codes and analysis.product_code:
            codes = [analysis.product_code]
        if not codes:
            blockers.append("approved product code is required")
        if analysis.quantity is None and not all(
            line.quantity for line in analysis.product_requests
        ):
            blockers.append("quantity is required")
        if analysis.coa_requested:
            if catalog is None:
                blockers.append("COA catalog is unavailable")
            else:
                for code in dict.fromkeys(codes):
                    result = catalog.find(code)
                    if (
                        result.status is not COAFindStatus.FOUND
                        or not result.auto_send_eligible
                    ):
                        blockers.append(f"unique standard English COA is required for {code}")
        if blockers:
            return "quote_assistance_or_clarification", blockers
        return (
            "multi_quote_with_coa_draft"
            if len(set(codes)) > 1 and analysis.coa_requested
            else "standard_quote_draft"
        ), blockers
    if analysis.intent == Intent.COA_REQUEST:
        query = analysis.requested_product_name or analysis.product_code or ""
        if catalog is None:
            return "coa_assistance", ["COA catalog is unavailable"]
        result = catalog.find(query, cas_number=analysis.requested_cas_number)
        if result.status is COAFindStatus.FOUND and result.auto_send_eligible:
            return "coa_draft", blockers
        return "coa_assistance", ["unique standard English COA is required"]
    return "general_human_review", ["request is outside a proven automatic workflow"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate private .eml acceptance cases without storing customer content."
    )
    parser.add_argument("--input", type=Path, default=Path("email-case"))
    parser.add_argument(
        "--coa-catalog",
        type=Path,
        default=Path("runtime/coa_catalog/catalog.json"),
    )
    args = parser.parse_args()
    catalog = COACatalog(args.coa_catalog) if args.coa_catalog.exists() else None
    rows: list[dict[str, object]] = []
    for path in sorted(args.input.glob("*.eml"), key=lambda item: item.name.casefold()):
        parsed = parse_mime(path.read_bytes())
        analysis = stub_analyze(parsed.subject, parsed.body_text, parsed.attachments)
        route, blockers = _predicted_route(
            analysis=analysis,
            request_text=f"{parsed.subject}\n{parsed.body_text}",
            catalog=catalog,
        )
        rows.append(
            {
                "case": path.name,
                "intent": analysis.intent.value,
                "quote_requested": analysis.quote_requested,
                "coa_requested": analysis.coa_requested,
                "product_list_requested": analysis.product_list_requested,
                "product_codes": [
                    line.product_code
                    for line in analysis.product_requests
                    if line.product_code
                ],
                "quantities_kg": [
                    line.quantity
                    for line in analysis.product_requests
                    if line.product_code
                ],
                "predicted_route": route,
                "blockers": blockers,
            }
        )
    print(
        json.dumps(
            {
                "input_directory": str(args.input),
                "case_count": len(rows),
                "draft_or_clarification_count": sum(
                    not row["blockers"] for row in rows
                ),
                "cases": rows,
            },
            ensure_ascii=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
