"""Company research context workflow."""

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from app.ai import CompanyCategoryDecision, CompanyResearchSource
from app.db import Customer
from app.settings import Settings


def _nonfree_email_domain(email_address: str) -> str | None:
    _, separator, domain = email_address.strip().casefold().rpartition("@")
    if not separator or not domain or domain in FREE_EMAIL_DOMAINS:
        return None
    return domain[:255]


def _source_hostname(url: str) -> str | None:
    try:
        host = (urlparse(url).hostname or "").casefold().rstrip(".")
    except ValueError:
        return None
    return host or None


def _company_research_gate(
    decision: CompanyCategoryDecision,
    sources: list[CompanyResearchSource],
    *,
    company_domain: str | None,
    active_category_keys: set[str],
    settings: Settings,
) -> dict[str, Any]:
    source_domains = sorted({hostname for source in sources if (hostname := _source_hostname(source.url)) is not None})
    exact_domain_source = bool(
        company_domain and any(hostname == company_domain or hostname.endswith(f".{company_domain}") for hostname in source_domains)
    )
    score_gap = max(
        0.0,
        float(decision.category_confidence) - float(decision.runner_up_confidence),
    )
    reasons: list[str] = []
    if decision.recommended_category_key not in active_category_keys:
        reasons.append("NO_ACTIVE_CATEGORY_RECOMMENDATION")
    if decision.identity_confidence < settings.company_research_min_identity_confidence:
        reasons.append("LOW_IDENTITY_CONFIDENCE")
    if decision.category_confidence < settings.company_research_min_category_confidence:
        reasons.append("LOW_CATEGORY_CONFIDENCE")
    if score_gap < settings.company_research_min_score_gap:
        reasons.append("CATEGORY_SCORE_GAP_TOO_SMALL")
    if decision.conflicting_evidence:
        reasons.append("CONFLICTING_EVIDENCE")
    if len(source_domains) < settings.company_research_min_sources and not exact_domain_source:
        reasons.append("INSUFFICIENT_INDEPENDENT_SOURCES")
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "score_gap": round(score_gap, 4),
        "source_domains": source_domains,
        "exact_domain_source": exact_domain_source,
    }


def _cached_company_research(
    customer: Customer,
    *,
    company_domain: str | None,
    catalog_signature: str,
    now: datetime,
) -> tuple[CompanyCategoryDecision, list[CompanyResearchSource], dict[str, Any]] | None:
    cache = (customer.metadata_json or {}).get(COMPANY_RESEARCH_CACHE_KEY)
    if not isinstance(cache, dict):
        return None
    if (
        cache.get("schema_version") != COMPANY_RESEARCH_CACHE_SCHEMA
        or cache.get("catalog_signature") != catalog_signature
        or cache.get("company_domain") != company_domain
    ):
        return None
    try:
        expires_at = datetime.fromisoformat(str(cache["expires_at"]))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= now:
            return None
        decision = CompanyCategoryDecision.model_validate(cache["decision"])
        sources = [CompanyResearchSource.model_validate(item) for item in cache.get("sources") or []]
    except (KeyError, TypeError, ValueError):
        return None
    metadata = cache.get("metadata") if isinstance(cache.get("metadata"), dict) else {}
    return decision, sources, {**metadata, "cache_hit": True}


def _store_company_research_cache(
    customer: Customer,
    *,
    company_domain: str | None,
    catalog_signature: str,
    decision: CompanyCategoryDecision,
    sources: list[CompanyResearchSource],
    metadata: dict[str, Any],
    settings: Settings,
    now: datetime,
) -> None:
    safe_metadata = {
        key: metadata.get(key)
        for key in ("provider", "model", "request_hash", "input_tokens", "output_tokens")
        if metadata.get(key) is not None
    }
    cache = {
        "schema_version": COMPANY_RESEARCH_CACHE_SCHEMA,
        "researched_at": now.isoformat(),
        "expires_at": (now + timedelta(days=settings.company_research_cache_days)).isoformat(),
        "company_domain": company_domain,
        "catalog_signature": catalog_signature,
        "decision": decision.model_dump(mode="json"),
        "sources": [source.model_dump(mode="json") for source in sources],
        "metadata": safe_metadata,
    }
    customer.metadata_json = {
        **(customer.metadata_json or {}),
        COMPANY_RESEARCH_CACHE_KEY: cache,
    }


FREE_EMAIL_DOMAINS = frozenset(
    {
        "aol.com",
        "gmail.com",
        "googlemail.com",
        "hotmail.com",
        "icloud.com",
        "live.com",
        "outlook.com",
        "proton.me",
        "protonmail.com",
        "qq.com",
        "yahoo.com",
        "yahoo.co.in",
        "ymail.com",
    }
)


COMPANY_RESEARCH_CACHE_KEY = "company_category_research"


COMPANY_RESEARCH_CACHE_SCHEMA = "company-category-research.v1"
