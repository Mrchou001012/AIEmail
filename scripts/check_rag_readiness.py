from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from app.embeddings import BailianSettings
from app.rag_retrieval import LocalRAGRetriever
from app.settings import Settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the production RAG configuration and local index without "
            "printing secrets. Network access is disabled unless --online is set."
        )
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Optional dotenv file, for example /etc/aiemail/aiemail.env.",
    )
    parser.add_argument(
        "--index",
        type=Path,
        help="Override RAG_INDEX_PATH for this check.",
    )
    parser.add_argument(
        "--online",
        action="store_true",
        help="Make one Bailian query-embedding request and test retrieval.",
    )
    return parser.parse_args()


def load_env_file(path: Path) -> None:
    if not path.is_file():
        raise ValueError(f"environment file does not exist: {path}")
    for key, value in dotenv_values(path).items():
        if key and value is not None:
            os.environ.setdefault(key, value)


def inspect_index(
    *,
    index_path: Path,
    expected_dimension: int,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    report: dict[str, Any] = {
        "path": str(index_path.resolve()),
        "exists": index_path.is_file(),
    }
    if not index_path.is_file():
        errors.append("RAG index file does not exist")
        return report, errors

    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"RAG index cannot be read: {type(exc).__name__}")
        return report, errors

    examples = payload.get("examples")
    report.update(
        {
            "bytes": index_path.stat().st_size,
            "schema_version": payload.get("schema_version"),
            "embedding_model": payload.get("embedding_model"),
            "embedding_dimension": payload.get("embedding_dimension"),
            "example_count": len(examples) if isinstance(examples, list) else 0,
            "boss_anchor_count": (
                sum(bool(item.get("boss_anchor")) for item in examples)
                if isinstance(examples, list)
                else 0
            ),
            "source_sha256_present": bool(payload.get("source_sha256")),
        }
    )
    if payload.get("schema_version") != "email-rag-index.v1":
        errors.append("unsupported RAG index schema")
    if not isinstance(examples, list) or not examples:
        errors.append("RAG index contains no examples")
    if payload.get("embedding_dimension") != expected_dimension:
        errors.append(
            "RAG index dimension does not match BAILIAN_EMBEDDING_DIMENSION"
        )
    return report, errors


def build_readiness_report(
    *,
    settings: Settings,
    bailian: BailianSettings,
    index_path: Path,
) -> tuple[dict[str, Any], list[str], list[str]]:
    index_report, errors = inspect_index(
        index_path=index_path,
        expected_dimension=bailian.embedding_dimension,
    )
    warnings: list[str] = []
    if not settings.rag_enabled:
        warnings.append("RAG_ENABLED is false; retrieval will remain inactive")
    if not bailian.api_key:
        errors.append("BAILIAN_API_KEY is not configured")
    if not bailian.api_host:
        errors.append("BAILIAN_API_HOST is not configured")

    report = {
        "ready": not errors,
        "network_checked": False,
        "rag_enabled": settings.rag_enabled,
        "rag_top_k": settings.rag_top_k,
        "rag_min_similarity": settings.rag_min_similarity,
        "bailian": {
            "api_key_configured": bool(bailian.api_key),
            "api_host_configured": bool(bailian.api_host),
            "embedding_model": bailian.embedding_model,
            "embedding_dimension": bailian.embedding_dimension,
        },
        "index": index_report,
        "warnings": warnings,
        "errors": errors,
        "secrets_printed": False,
    }
    return report, warnings, errors


def verify_online(
    report: dict[str, Any],
    *,
    index_path: Path,
    settings: Settings,
) -> None:
    retriever = LocalRAGRetriever(index_path)
    matches = retriever.retrieve(
        (
            "Intent: quote_request\n"
            "Subject: Request for quotation\n"
            "Customer request: Please quote 500 kg of the requested product."
        ),
        intent="quote_request",
        top_k=settings.rag_top_k,
        min_similarity=settings.rag_min_similarity,
    )
    report["network_checked"] = True
    report["online_retrieval"] = {
        "match_count": len(matches),
        "top_example_id": matches[0].example_id if matches else None,
        "top_intent": matches[0].intent if matches else None,
        "top_similarity": round(matches[0].similarity, 4) if matches else None,
    }


def main() -> None:
    args = parse_args()
    if args.env_file:
        load_env_file(args.env_file)
    settings = Settings()
    bailian = BailianSettings()
    index_path = args.index or settings.rag_index_path
    report, _, errors = build_readiness_report(
        settings=settings,
        bailian=bailian,
        index_path=index_path,
    )

    if args.online and not errors:
        try:
            verify_online(
                report,
                index_path=index_path,
                settings=settings,
            )
        except Exception as exc:
            errors.append(f"online retrieval failed: {type(exc).__name__}: {exc}")
            report["errors"] = errors
            report["ready"] = False

    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not errors else 1)


if __name__ == "__main__":
    main()
