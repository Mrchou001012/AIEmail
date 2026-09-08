from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from app.nas_knowledge import extract_document_bounded

logger = logging.getLogger(__name__)

CJK_PATTERN = re.compile(r"[\u3400-\u9fff]")
CAS_PATTERN = re.compile(r"(?<!\d)(\d{2,7}-\d{2}-\d)(?!\d)")
COA_PREFIX_PATTERN = re.compile(
    r"^\s*(?:coa|certificate\s+of\s+analysis)\s*(?:[-_–—:]\s*)?",
    re.IGNORECASE,
)
RISKY_SUFFIX_PATTERN = re.compile(
    r"(?:"
    r"\b(?:19|20)\d{2}\b|"
    r"\b(?:rev(?:ision)?|ver(?:sion)?|draft|old|new|backup|copy|sample|special|customer)\b|"
    r"\bv\s*\d+\b|"
    r"\bfor\s+[a-z0-9]"
    r")",
    re.IGNORECASE,
)
GENERIC_COA_NAMES = {"coa", "certificateofanalysis"}
COA_CONTAINER_NAMES = {"coa", "certificate", "certificates", "document", "documents"}


class COAFindStatus(StrEnum):
    FOUND = "found"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


def _safe_unicode(value: str) -> str:
    return value.encode("utf-8", errors="backslashreplace").decode("utf-8")


def _normalize_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _coa_product_part(stem: str) -> str:
    return COA_PREFIX_PATTERN.sub("", stem).strip(" -_–—")


def _valid_cas(value: str) -> bool:
    first, second, check = value.split("-")
    body = f"{first}{second}"
    expected = sum(position * int(digit) for position, digit in enumerate(reversed(body), start=1)) % 10
    return expected == int(check)


def _cas_numbers(text: str) -> list[str]:
    return sorted({value for value in CAS_PATTERN.findall(text) if _valid_cas(value)})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
        suffix=".part",
    ) as stream:
        json.dump(payload, stream, ensure_ascii=True, separators=(",", ":"))
        temporary = Path(stream.name)
    temporary.replace(path)


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _load_product_metadata(path: Path | None) -> dict[str, list[dict[str, Any]]]:
    if path is None or not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    index: dict[str, list[dict[str, Any]]] = {}
    for raw in payload.get("products", []):
        row = dict(raw)
        for value in (row.get("code"), row.get("name")):
            key = _normalize_key(str(value or ""))
            if key:
                index.setdefault(key, []).append(row)
    return index


def _enrich_from_product_metadata(
    entry: dict[str, Any],
    metadata: dict[str, list[dict[str, Any]]],
) -> None:
    matches: dict[str, dict[str, Any]] = {}
    for alias in entry.get("aliases", []):
        for row in metadata.get(_normalize_key(str(alias)), []):
            identity = str(row.get("code") or row.get("name") or "")
            matches[identity] = row
    if len(matches) != 1:
        entry["metadata_source"] = None
        return
    product = next(iter(matches.values()))
    aliases = {
        *(str(value) for value in entry.get("aliases", []) if str(value).strip()),
        str(product.get("code") or "").strip(),
        str(product.get("name") or "").strip(),
    }
    entry["aliases"] = sorted((value for value in aliases if value), key=str.casefold)
    cas_number = str(product.get("cas_no") or "").strip()
    if cas_number and _valid_cas(cas_number):
        entry["cas_numbers"] = sorted({*entry.get("cas_numbers", []), cas_number})
    entry["product_code"] = str(product.get("code") or "").strip() or None
    entry["metadata_source"] = "approved product catalog"


def _filesystem_path(path: Path) -> Path:
    text = str(path)
    if os.name == "nt" and text.startswith("\\\\") and not text.startswith("\\\\?\\"):
        return Path(f"\\\\?\\UNC\\{text[2:]}")
    return path


def _product_directory(relative_path: str) -> tuple[str, str]:
    parent = PurePosixPath(relative_path).parent
    if parent.name.casefold() in COA_CONTAINER_NAMES and parent.parent.name:
        parent = parent.parent
    return parent.as_posix(), parent.name


def _candidate_decision(relative_path: str, product_name: str) -> tuple[bool, str]:
    path = PurePosixPath(relative_path)
    if path.suffix.casefold() != ".pdf":
        return False, "not a PDF"
    if "coa" not in path.stem.casefold() and "certificate of analysis" not in path.stem.casefold():
        return False, "filename is not a COA"
    if CJK_PATTERN.search(relative_path):
        return False, "path or filename contains Chinese text"
    product_part = _coa_product_part(path.stem)
    normalized_part = _normalize_key(product_part)
    normalized_product = _normalize_key(product_name)
    if RISKY_SUFFIX_PATTERN.search(product_part):
        return False, "filename contains a date, version, customer, or special-purpose suffix"
    if normalized_part in GENERIC_COA_NAMES or not normalized_part:
        return True, "generic COA filename without a suffix"
    if normalized_part == normalized_product:
        return True, "COA filename exactly matches the product directory"
    return False, "COA filename has an unmatched product or extra suffix"


@dataclass(frozen=True)
class COAFindResult:
    status: COAFindStatus
    query: str
    match_basis: str
    matches: tuple[dict[str, Any], ...] = ()
    auto_send_eligible: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "query": self.query,
            "match_basis": self.match_basis,
            "matches": [dict(row) for row in self.matches],
            "auto_send_eligible": self.auto_send_eligible,
        }


class COACatalogScanner:
    """Build a deny-by-default catalog of one generic English COA per product.

    The scan enumerates only the configured product-document root. It ignores
    every non-COA file and extracts text only from the one selected standard
    COA for each product directory.
    """

    schema_version = "coa-catalog.v1"

    def __init__(
        self,
        *,
        root: Path,
        output_path: Path,
        product_catalog_path: Path | None = Path("config/product_catalog.yaml"),
        extraction_timeout_seconds: int = 15,
        max_file_bytes: int = 50 * 1024 * 1024,
    ) -> None:
        self.root = root
        self.filesystem_root = _filesystem_path(root)
        self.output_path = output_path
        self.product_catalog_path = product_catalog_path
        self.extraction_timeout_seconds = extraction_timeout_seconds
        self.max_file_bytes = max_file_bytes
        self.scan_warnings: list[dict[str, str]] = []

    def _candidates(self) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        stack = [self.filesystem_root]
        while stack:
            directory = stack.pop()
            try:
                with os.scandir(directory) as entries:
                    ordered = sorted(entries, key=lambda item: item.name.casefold(), reverse=True)
            except OSError as exc:
                try:
                    relative = Path(directory).relative_to(self.filesystem_root).as_posix()
                except ValueError:
                    relative = str(directory)
                self.scan_warnings.append(
                    {"path": _safe_unicode(relative), "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
                )
                continue
            for entry in ordered:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    path = Path(entry.path)
                    if path.suffix.casefold() != ".pdf":
                        continue
                    if "coa" not in path.stem.casefold() and "certificate of analysis" not in path.stem.casefold():
                        continue
                    relative = _safe_unicode(path.relative_to(self.filesystem_root).as_posix())
                    stat = entry.stat(follow_symlinks=False)
                    product_path, product_name = _product_directory(relative)
                    accepted, reason = _candidate_decision(relative, product_name)
                    candidates.append(
                        {
                            "path": relative,
                            "absolute_path": path,
                            "product_path": product_path,
                            "product_name": product_name,
                            "accepted_name": accepted,
                            "name_reason": reason,
                            "size": stat.st_size,
                            "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                            "fingerprint": f"{stat.st_size}:{stat.st_mtime_ns}",
                        }
                    )
                except OSError as exc:
                    logger.warning("cannot inspect COA candidate %s: %s", entry.path, exc)
        return candidates

    def scan(self) -> dict[str, Any]:
        started = datetime.now(UTC)
        self.scan_warnings = []
        previous_payload = _load_json(self.output_path, {})
        previous = {str(row.get("path")): row for row in previous_payload.get("entries", [])}
        grouped: dict[str, list[dict[str, Any]]] = {}
        product_metadata = _load_product_metadata(self.product_catalog_path)
        for candidate in self._candidates():
            grouped.setdefault(str(candidate["product_path"]), []).append(candidate)

        entries: list[dict[str, Any]] = []
        review: list[dict[str, Any]] = []
        changed = 0
        extraction_errors = 0
        for product_path, candidates in sorted(grouped.items(), key=lambda item: item[0].casefold()):
            accepted = [candidate for candidate in candidates if candidate["accepted_name"]]
            rejected = [candidate for candidate in candidates if not candidate["accepted_name"]]
            if len(accepted) != 1:
                review.append(
                    {
                        "product_path": product_path,
                        "product_name": candidates[0]["product_name"],
                        "reason": (
                            "multiple standard English COAs match"
                            if len(accepted) > 1
                            else "no suffix-free standard English COA matches"
                        ),
                        "candidates": [
                            {
                                "path": row["path"],
                                "accepted_name": row["accepted_name"],
                                "reason": row["name_reason"],
                            }
                            for row in sorted(candidates, key=lambda item: str(item["path"]).casefold())
                        ],
                    }
                )
                continue

            selected = accepted[0]
            old = previous.get(str(selected["path"]))
            if old and old.get("fingerprint") == selected["fingerprint"]:
                entry = dict(old)
            else:
                changed += 1
                path = Path(selected["absolute_path"])
                entry = {
                    "product_path": product_path,
                    "product_name": selected["product_name"],
                    "aliases": sorted(
                        {
                            str(selected["product_name"]),
                            _coa_product_part(PurePosixPath(str(selected["path"])).stem),
                        },
                        key=str.casefold,
                    ),
                    "cas_numbers": [],
                    "path": selected["path"],
                    "size": selected["size"],
                    "modified_at": selected["modified_at"],
                    "fingerprint": selected["fingerprint"],
                    "sha256": None,
                    "selection_basis": selected["name_reason"],
                    "extract_status": "pending",
                    "extract_error": None,
                }
                if int(selected["size"]) > self.max_file_bytes:
                    entry["extract_status"] = "too_large"
                    entry["extract_error"] = "selected COA exceeds the safe file-size limit"
                else:
                    try:
                        text = extract_document_bounded(
                            path,
                            timeout_seconds=self.extraction_timeout_seconds,
                        )
                        entry["sha256"] = _sha256(path)
                        entry["cas_numbers"] = _cas_numbers(text)
                        entry["extract_status"] = "indexed" if text else "empty"
                    except Exception as exc:
                        extraction_errors += 1
                        entry["extract_status"] = "error"
                        entry["extract_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            _enrich_from_product_metadata(entry, product_metadata)
            entries.append(entry)
            if rejected:
                review.append(
                    {
                        "product_path": product_path,
                        "product_name": selected["product_name"],
                        "reason": "standard COA selected; alternate files were excluded",
                        "selected_path": selected["path"],
                        "candidates": [
                            {
                                "path": row["path"],
                                "accepted_name": False,
                                "reason": row["name_reason"],
                            }
                            for row in sorted(rejected, key=lambda item: str(item["path"]).casefold())
                        ],
                    }
                )

        completed = datetime.now(UTC)
        payload = {
            "schema_version": self.schema_version,
            "root": str(self.root),
            "started_at": started.isoformat(),
            "completed_at": completed.isoformat(),
            "duration_seconds": round((completed - started).total_seconds(), 3),
            "complete": not self.scan_warnings,
            "candidate_file_count": sum(len(rows) for rows in grouped.values()),
            "product_directory_count": len(grouped),
            "selected_count": len(entries),
            "review_count": len(review),
            "changed_count": changed,
            "extraction_error_count": extraction_errors,
            "enumeration_warnings": self.scan_warnings,
            "entries": sorted(entries, key=lambda row: str(row["product_path"]).casefold()),
            "review": review,
        }
        _atomic_json(self.output_path, payload)
        return payload


class COACatalog:
    schema_version = COACatalogScanner.schema_version

    def __init__(self, catalog_path: Path) -> None:
        payload = _load_json(catalog_path, {})
        if payload.get("schema_version") != self.schema_version:
            raise ValueError("unsupported or missing COA catalog")
        self.root = Path(str(payload.get("root") or ""))
        self.filesystem_root = _filesystem_path(self.root)
        self.entries = tuple(dict(row) for row in payload.get("entries", []))
        self.review = tuple(dict(row) for row in payload.get("review", []))

    def find(self, query: str, *, cas_number: str | None = None) -> COAFindResult:
        clean_query = query.strip()
        clean_cas = (cas_number or "").strip()
        if clean_cas:
            matches = tuple(row for row in self.entries if clean_cas in row.get("cas_numbers", []))
            if matches:
                return self._result(clean_query or clean_cas, "exact_cas", matches, exact=True)

        normalized = _normalize_key(clean_query)
        if not normalized:
            return COAFindResult(COAFindStatus.NOT_FOUND, clean_query, "empty_query")
        exact = tuple(
            row
            for row in self.entries
            if normalized in {_normalize_key(str(alias)) for alias in row.get("aliases", [])}
        )
        if exact:
            return self._result(clean_query, "exact_alias", exact, exact=True)

        review_matches = tuple(
            row for row in self.review if _normalize_key(str(row.get("product_name") or "")) == normalized
        )
        if review_matches:
            return COAFindResult(
                COAFindStatus.AMBIGUOUS,
                clean_query,
                "product_requires_coa_review",
                review_matches,
                False,
            )

        ranked: list[tuple[float, dict[str, Any]]] = []
        for row in self.entries:
            score = max(
                (SequenceMatcher(None, normalized, _normalize_key(str(alias))).ratio() for alias in row.get("aliases", [])),
                default=0.0,
            )
            ranked.append((score, row))
        ranked.sort(key=lambda item: item[0], reverse=True)
        if ranked and ranked[0][0] >= 0.92:
            best_score = ranked[0][0]
            best = tuple(row for score, row in ranked if abs(score - best_score) < 1e-9)
            runner_up = next((score for score, row in ranked if row not in best), 0.0)
            if len(best) == 1 and best_score - runner_up >= 0.08:
                return COAFindResult(COAFindStatus.FOUND, clean_query, "fuzzy_alias", best, False)
            return COAFindResult(COAFindStatus.AMBIGUOUS, clean_query, "fuzzy_alias_tie", best, False)
        return COAFindResult(COAFindStatus.NOT_FOUND, clean_query, "no_alias_or_cas_match")

    def _result(
        self,
        query: str,
        basis: str,
        matches: tuple[dict[str, Any], ...],
        *,
        exact: bool,
    ) -> COAFindResult:
        if len(matches) != 1:
            return COAFindResult(COAFindStatus.AMBIGUOUS, query, f"{basis}_multiple", matches, False)
        entry = matches[0]
        eligible = bool(
            exact
            and entry.get("sha256")
            and entry.get("extract_status") in {"indexed", "empty"}
        )
        return COAFindResult(COAFindStatus.FOUND, query, basis, matches, eligible)

    def attachment_path(self, entry: dict[str, Any]) -> Path:
        relative = str(entry.get("path") or "").replace("\\", "/").lstrip("/")
        parts = PurePosixPath(relative).parts
        if not relative or ".." in parts:
            raise ValueError("invalid COA catalog path")
        return self.filesystem_root.joinpath(*parts)

    def entry_for_path(self, relative_path: str) -> dict[str, Any]:
        """Return one currently approved entry by its catalog-relative path."""

        clean_path = relative_path.replace("\\", "/").lstrip("/")
        matches = [row for row in self.entries if str(row.get("path")) == clean_path]
        if len(matches) != 1:
            raise ValueError("COA is no longer uniquely present in the approved catalog")
        return dict(matches[0])

    def read_verified_attachment(self, entry: dict[str, Any]) -> bytes:
        path = self.attachment_path(entry)
        payload = path.read_bytes()
        expected = str(entry.get("sha256") or "")
        if not expected or hashlib.sha256(payload).hexdigest() != expected:
            raise ValueError("COA file changed after catalog selection; rescan before use")
        return payload
