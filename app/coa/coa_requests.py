"""Deterministic planning helpers for inbound COA delivery workflows."""

from __future__ import annotations

import hashlib
import html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.ai import InboundAnalysis
from app.coa.coa_delivery import PreparedCOAResponse
from app.domain import Intent


@dataclass(frozen=True)
class COARequestPlan:
    primary_request: bool
    product_queries: tuple[str, ...]
    cas_number: str | None
    prior_missing_queries: tuple[str, ...]
    retry_original_query: str
    followup: bool

    def lookup_facts(
        self,
        *,
        analysis_facts: dict[str, Any],
        catalog_path: Path,
    ) -> dict[str, Any]:
        return {
            **analysis_facts,
            "coa_queries": list(self.product_queries),
            "coa_cas_number": self.cas_number,
            "coa_catalog_path": str(catalog_path),
        }


def plan_coa_request(
    *,
    analysis: InboundAnalysis,
    analysis_facts: dict[str, Any],
    case_product_code: str = "",
    case_product_name: str = "",
) -> COARequestPlan:
    requested_codes = [
        str(line.product_code).strip()
        for line in analysis.product_requests
        if line.product_code and str(line.product_code).strip()
    ]
    explicit_query = (
        analysis.requested_product_name or analysis.product_code or ""
    ).strip()
    if len(requested_codes) >= 2:
        product_queries = requested_codes
    elif explicit_query:
        product_queries = [explicit_query]
    elif requested_codes:
        product_queries = requested_codes
    else:
        fallback = (case_product_code or case_product_name).strip()
        product_queries = [fallback] if fallback else []
    prior_missing = tuple(
        str(item).strip()
        for item in (analysis_facts.get("missing_coa_queries") or [])
        if str(item).strip()
    )
    return COARequestPlan(
        primary_request=analysis.intent == Intent.COA_REQUEST,
        product_queries=tuple(product_queries),
        cas_number=(analysis.requested_cas_number or "").strip() or None,
        prior_missing_queries=prior_missing,
        retry_original_query=str(
            analysis_facts.get("coa_retry_original_query") or ""
        ).strip(),
        followup=bool(analysis_facts.get("partial_coa_outbox_id")),
    )


def coa_outbox_business_key(
    *,
    email_id: int,
    product_queries: tuple[str, ...] | list[str],
    followup: bool,
) -> str:
    if not followup:
        return f"inbound-coa:{email_id}"
    token = hashlib.sha256(
        json.dumps(list(product_queries), ensure_ascii=False, sort_keys=True).encode(
            "utf-8"
        )
    ).hexdigest()[:16]
    return f"inbound-coa-followup:{email_id}:{token}"


def outstanding_coa_queries(
    *,
    response_missing: tuple[str, ...],
    plan: COARequestPlan,
) -> list[str]:
    outstanding = list(response_missing)
    if not plan.followup or not plan.prior_missing_queries:
        return outstanding
    retried = (
        plan.retry_original_query
        or (plan.product_queries[0] if plan.product_queries else "")
    ).casefold()
    for query in plan.prior_missing_queries:
        if query.casefold() != retried and query not in outstanding:
            outstanding.append(query)
    return outstanding


def partial_coa_handoff_facts(
    *,
    prepared_facts: dict[str, Any],
    outbox_id: int,
    prepared_coas: tuple[dict[str, Any], ...],
    outstanding_queries: list[str],
) -> dict[str, Any]:
    facts = {
        **prepared_facts,
        "partial_coa_outbox_id": outbox_id,
        "delivered_coas": [dict(row) for row in prepared_coas],
        "missing_coa_queries": list(outstanding_queries),
        "coa_partial": True,
    }
    facts.pop("ai_draft_preview", None)
    return facts


def prepared_coa_facts(
    *,
    lookup_facts: dict[str, Any],
    response: PreparedCOAResponse,
    generated_at: str,
) -> dict[str, Any]:
    return {
        **lookup_facts,
        **response.as_facts(),
        "ai_draft_preview": {
            "subject": response.subject,
            "body_text": response.body_text,
            "generated_at": generated_at,
            "provider": "deterministic-coa",
            "model": response.catalog_schema,
            "rag_matches": [],
        },
    }


def signed_coa_body(
    *,
    response_body: str,
    signature_text: str,
    signature_html: str,
) -> tuple[str, str]:
    text_body = "\n".join([response_body, "", signature_text.strip()])
    html_body = "".join(
        f"<p>{html.escape(line) if line else '&nbsp;'}</p>"
        for line in response_body.splitlines()
    ) + signature_html
    return text_body, html_body


def coa_delivery_audit_data(
    *,
    email_id: int,
    outbox_id: int,
    business_key: str,
    response: PreparedCOAResponse,
    secondary_request: bool,
    outstanding_queries: list[str],
) -> dict[str, Any]:
    return {
        "email_id": email_id,
        "outbox_id": outbox_id,
        "business_key": business_key,
        "coa_paths": [row["path"] for row in response.prepared_coas],
        "coa_sha256": [row["sha256"] for row in response.prepared_coas],
        "match_bases": [row["match_basis"] for row in response.prepared_coas],
        "secondary_request": secondary_request,
        "missing_coa_queries": outstanding_queries,
    }
