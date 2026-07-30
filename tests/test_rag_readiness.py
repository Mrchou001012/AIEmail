from __future__ import annotations

import json
from pathlib import Path

from app.embeddings import BailianSettings
from app.settings import Settings
from scripts.check_rag_readiness import build_readiness_report


def write_index(path: Path, *, dimension: int = 1024) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "email-rag-index.v1",
                "embedding_model": "qwen3.7-text-embedding",
                "embedding_dimension": dimension,
                "source_sha256": "abc123",
                "examples": [
                    {
                        "id": "example-1",
                        "boss_anchor": True,
                        "vector": [0.0] * dimension,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_readiness_report_is_secret_safe(tmp_path: Path) -> None:
    index_path = tmp_path / "index.json"
    write_index(index_path)

    report, warnings, errors = build_readiness_report(
        settings=Settings(
            rag_enabled=False,
            rag_index_path=index_path,
        ),
        bailian=BailianSettings(
            api_key="secret-value",
            api_host="https://example.aliyuncs.com",
            embedding_dimension=1024,
        ),
        index_path=index_path,
    )

    serialized = json.dumps(report)
    assert errors == []
    assert warnings == ["RAG_ENABLED is false; retrieval will remain inactive"]
    assert report["ready"] is True
    assert report["index"]["example_count"] == 1
    assert report["index"]["boss_anchor_count"] == 1
    assert report["bailian"]["api_key_configured"] is True
    assert "secret-value" not in serialized


def test_readiness_report_rejects_dimension_mismatch(tmp_path: Path) -> None:
    index_path = tmp_path / "index.json"
    write_index(index_path, dimension=512)

    report, _, errors = build_readiness_report(
        settings=Settings(
            rag_enabled=True,
            rag_index_path=index_path,
        ),
        bailian=BailianSettings(
            api_key="secret-value",
            api_host="https://example.aliyuncs.com",
            embedding_dimension=1024,
        ),
        index_path=index_path,
    )

    assert report["ready"] is False
    assert "RAG index dimension does not match" in errors[0]
