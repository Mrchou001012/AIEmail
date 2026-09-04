"""Side-effect-free preparation and verified reading of COA attachments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.coa_catalog import COACatalog, COAFindStatus
from app.mail import OutboundAttachment
from app.settings import Settings


class COAResponseError(ValueError):
    """A safe, user-reviewable reason why a requested COA cannot be sent."""

    def __init__(
        self,
        summary: str,
        *,
        help_needed: str,
        facts: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(summary)
        self.summary = summary
        self.help_needed = help_needed
        self.facts = facts or {}


@dataclass(frozen=True)
class PreparedCOAResponse:
    subject: str
    body_text: str
    product_names: tuple[str, ...]
    prepared_coas: tuple[dict[str, Any], ...]
    attachments: tuple[OutboundAttachment, ...]
    lookups: tuple[dict[str, Any], ...]
    catalog_schema: str
    missing_queries: tuple[str, ...] = ()

    def as_facts(self) -> dict[str, Any]:
        facts: dict[str, Any] = {
            "coa_queries": [str(row.get("query") or "") for row in self.lookups],
            "coa_lookups": [dict(row) for row in self.lookups],
            "prepared_coas": [dict(row) for row in self.prepared_coas],
            "missing_coa_queries": list(self.missing_queries),
            "coa_partial": bool(self.prepared_coas and self.missing_queries),
        }
        if len(self.prepared_coas) == 1 and not self.missing_queries:
            facts.update(
                {
                    "coa_query": str(self.lookups[0].get("query") or ""),
                    "coa_lookup": dict(self.lookups[0].get("result") or {}),
                    "prepared_coa": dict(self.prepared_coas[0]),
                }
            )
        return facts


def coa_reply_draft(
    *,
    contact_name: str,
    original_subject: str,
    product_name: str,
) -> tuple[str, str]:
    subject = (
        f"Re: {original_subject.strip()}"
        if original_subject.strip()
        and not original_subject.strip().casefold().startswith("re:")
        else (original_subject.strip() or f"COA for {product_name}")
    )
    greeting_name = contact_name.strip() or "Customer"
    body = (
        f"Dear {greeting_name},\n\n"
        f"Please find attached the Certificate of Analysis (COA) for {product_name}."
    )
    return subject[:998], body


def prepare_coa_attachments(
    *,
    settings: Settings,
    product_codes: list[str],
    cas_number: str | None = None,
) -> list[dict[str, Any]]:
    if not settings.coa_catalog_enabled:
        raise ValueError("approved COA catalog is disabled")
    catalog = COACatalog(settings.coa_catalog_path)
    prepared: list[dict[str, Any]] = []
    for code in product_codes:
        result = catalog.find(
            code,
            cas_number=cas_number if len(product_codes) == 1 else None,
        )
        if (
            result.status is not COAFindStatus.FOUND
            or len(result.matches) != 1
            or not result.auto_send_eligible
        ):
            raise ValueError(f"no unique approved standard English COA for {code}")
        entry = dict(result.matches[0])
        relative_path = str(entry["path"])
        prepared.append(
            {
                "product_code": code,
                "product_name": str(
                    entry.get("product_code") or entry.get("product_name") or code
                ),
                "path": relative_path,
                "filename": relative_path.replace("\\", "/").rsplit("/", 1)[-1],
                "sha256": str(entry["sha256"]),
                "size": int(entry["size"]),
                "match_basis": result.match_basis,
                "catalog_schema": catalog.schema_version,
            }
        )
    return prepared


def prepare_coa_response(
    *,
    settings: Settings,
    contact_name: str,
    original_subject: str,
    product_queries: list[str],
    cas_number: str | None = None,
) -> PreparedCOAResponse:
    """Resolve requested COAs, returning verified matches plus unresolved items."""

    clean_queries: list[str] = []
    seen: set[str] = set()
    for query in product_queries:
        clean = query.strip()
        key = clean.casefold()
        if clean and key not in seen:
            clean_queries.append(clean)
            seen.add(key)
    clean_cas = (cas_number or "").strip() or None
    if not clean_queries and not clean_cas:
        raise COAResponseError(
            "Customer requested a COA but the product or CAS number is missing",
            help_needed="product name, product code, or CAS number",
        )

    lookup_queries = clean_queries or [""]
    catalog = COACatalog(settings.coa_catalog_path)
    prepared: list[dict[str, Any]] = []
    lookups: list[dict[str, Any]] = []
    missing_queries: list[str] = []
    seen_paths: set[str] = set()
    for query in lookup_queries:
        result = catalog.find(
            query,
            cas_number=clean_cas if len(lookup_queries) == 1 else None,
        )
        lookup = {"query": query or clean_cas or "", "result": result.as_dict()}
        lookups.append(lookup)
        if (
            result.status is not COAFindStatus.FOUND
            or len(result.matches) != 1
            or not result.auto_send_eligible
        ):
            missing_queries.append(query or clean_cas or "unknown product")
            continue
        entry = dict(result.matches[0])
        relative_path = str(entry["path"])
        if relative_path in seen_paths:
            continue
        seen_paths.add(relative_path)
        prepared.append(
            {
                "product_code": query,
                "product_name": str(
                    entry.get("product_code")
                    or entry.get("product_name")
                    or query
                    or clean_cas
                ),
                "path": relative_path,
                "filename": relative_path.replace("\\", "/").rsplit("/", 1)[-1],
                "sha256": str(entry["sha256"]),
                "size": int(entry["size"]),
                "match_basis": result.match_basis,
                "catalog_schema": catalog.schema_version,
            }
        )

    if not prepared:
        ambiguous = any(
            row.get("result", {}).get("status") == COAFindStatus.AMBIGUOUS.value
            for row in lookups
        )
        raise COAResponseError(
            (
                "COA match is ambiguous and requires human selection"
                if ambiguous
                else "No unique approved standard English COA was found"
            ),
            help_needed=(
                "confirm the correct suffix-free standard English COA"
                if ambiguous
                else "provide or identify a suffix-free standard English COA"
            ),
            facts={
                "coa_queries": lookup_queries,
                "coa_lookups": lookups,
                "missing_coa_queries": missing_queries,
            },
        )

    product_names = tuple(str(row["product_name"]) for row in prepared)
    display_names = " and ".join(product_names)
    subject, body_text = coa_reply_draft(
        contact_name=contact_name,
        original_subject=original_subject,
        product_name=display_names,
    )
    if len(product_names) > 1:
        greeting_name = contact_name.strip() or "Customer"
        body_text = (
            f"Dear {greeting_name},\n\n"
            "Please find attached the Certificates of Analysis (COAs) for "
            f"{display_names}."
        )
    if missing_queries:
        missing_names = ", ".join(missing_queries)
        body_text = (
            f"{body_text}\n\n"
            "The COA"
            f"{'s' if len(missing_queries) > 1 else ''} for {missing_names} "
            f"{'are' if len(missing_queries) > 1 else 'is'} not included in this "
            "email. Our team will follow up separately."
        )
    return PreparedCOAResponse(
        subject=subject,
        body_text=body_text,
        product_names=product_names,
        prepared_coas=tuple(prepared),
        attachments=read_prepared_coa_attachments(
            settings=settings,
            prepared_coas=prepared,
        ),
        lookups=tuple(lookups),
        catalog_schema=catalog.schema_version,
        missing_queries=tuple(missing_queries),
    )


def read_prepared_coa_attachments(
    *,
    settings: Settings,
    prepared_coas: list[dict[str, Any]],
) -> tuple[OutboundAttachment, ...]:
    if not prepared_coas:
        return ()
    catalog = COACatalog(settings.coa_catalog_path)
    attachments: list[OutboundAttachment] = []
    for prepared in prepared_coas:
        entry = catalog.entry_for_path(str(prepared.get("path") or ""))
        if str(entry.get("sha256") or "") != str(prepared.get("sha256") or ""):
            raise ValueError("prepared COA no longer matches the approved catalog")
        attachments.append(
            OutboundAttachment(
                filename=str(prepared.get("filename") or "COA.pdf"),
                content_type="application/pdf",
                payload=catalog.read_verified_attachment(entry),
            )
        )
    return tuple(attachments)
