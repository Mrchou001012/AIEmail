"""Side-effect-free preparation and verified reading of COA attachments."""

from __future__ import annotations

from typing import Any

from app.coa_catalog import COACatalog, COAFindStatus
from app.mail import OutboundAttachment
from app.settings import Settings


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
) -> list[dict[str, Any]]:
    if not settings.coa_catalog_enabled:
        raise ValueError("approved COA catalog is disabled")
    catalog = COACatalog(settings.coa_catalog_path)
    prepared: list[dict[str, Any]] = []
    for code in product_codes:
        result = catalog.find(code)
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
                "path": relative_path,
                "filename": relative_path.replace("\\", "/").rsplit("/", 1)[-1],
                "sha256": str(entry["sha256"]),
                "size": int(entry["size"]),
                "match_basis": result.match_basis,
            }
        )
    return prepared


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
