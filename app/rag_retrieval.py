from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from app.embeddings import BailianEmbeddingClient, EmbeddingBatch

EMAIL_PATTERN = re.compile(
    r"(?i)(?<![\w.+-])[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[a-z0-9-]+(?:\.[a-z0-9-]+)+(?![\w-])"
)
PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?\d[\s().-]*){8,15}(?!\d)")
MULTISPACE_PATTERN = re.compile(r"[ \t]+")
BLANK_LINES_PATTERN = re.compile(r"\n{3,}")


class EmbeddingClient(Protocol):
    def embed(
        self,
        texts: list[str],
        *,
        input_type: str,
        instruct: str | None = None,
    ) -> EmbeddingBatch: ...


@dataclass(frozen=True)
class RetrievedExample:
    example_id: str
    thread_id: str
    similarity: float
    intent: str
    subject: str
    request_text: str
    reference_response: str
    quality_score: int
    boss_anchor: bool

    def prompt_document(self) -> dict[str, Any]:
        return {
            "example_id": self.example_id,
            "similarity": round(self.similarity, 4),
            "intent": self.intent,
            "subject": self.subject,
            "customer_request": self.request_text[:3000],
            "historical_response": self.reference_response[:3000],
            "quality_score": self.quality_score,
            "boss_anchor": self.boss_anchor,
        }


def sanitize_embedding_text(value: str) -> str:
    """Minimize personal data sent to the external embedding endpoint."""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    normalized = EMAIL_PATTERN.sub("[email]", normalized)
    normalized = PHONE_PATTERN.sub("[phone]", normalized)
    lines = [MULTISPACE_PATTERN.sub(" ", line).rstrip() for line in normalized.splitlines()]
    return BLANK_LINES_PATTERN.sub("\n\n", "\n".join(lines)).strip()[:20_000]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"invalid JSONL object at line {line_number}")
            rows.append(row)
    return rows


def _cosine(first: list[float], second: tuple[float, ...]) -> float:
    if len(first) != len(second):
        raise ValueError("query and document embedding dimensions do not match")
    dot = sum(left * right for left, right in zip(first, second, strict=True))
    first_norm = math.sqrt(sum(value * value for value in first))
    second_norm = math.sqrt(sum(value * value for value in second))
    if first_norm == 0 or second_norm == 0:
        return 0.0
    return dot / (first_norm * second_norm)


def build_rag_index(
    *,
    examples_path: Path,
    index_path: Path,
    embedding_client: EmbeddingClient | None = None,
    batch_size: int = 20,
) -> dict[str, Any]:
    if not 1 <= batch_size <= 20:
        raise ValueError("embedding batch size must be between 1 and 20")
    examples = _read_jsonl(examples_path)
    if not examples:
        raise ValueError("knowledge-base examples file is empty")
    client = embedding_client or BailianEmbeddingClient()
    indexed: list[dict[str, Any]] = []
    total_tokens = 0
    model = ""
    dimension = 0
    for start in range(0, len(examples), batch_size):
        batch_rows = examples[start : start + batch_size]
        texts = [
            sanitize_embedding_text(str(row.get("retrieval_text") or ""))
            for row in batch_rows
        ]
        if any(not text for text in texts):
            raise ValueError("knowledge-base example has empty retrieval_text")
        result = client.embed(texts, input_type="document")
        total_tokens += result.total_tokens
        model = result.model
        for row, vector in zip(batch_rows, result.vectors, strict=True):
            dimension = len(vector)
            indexed.append(
                {
                    "id": str(row["id"]),
                    "thread_id": str(row.get("thread_id") or ""),
                    "intent": str(row.get("intent") or "other"),
                    "subject": str(row.get("subject") or ""),
                    "request_text": str(row.get("request_text") or ""),
                    "reference_response": str(row.get("reference_response") or ""),
                    "quality_score": int(row.get("quality_score") or 0),
                    "boss_anchor": bool(row.get("boss_anchor")),
                    "vector": list(vector),
                }
            )

    source_digest = hashlib.sha256(examples_path.read_bytes()).hexdigest()
    payload = {
        "schema_version": "email-rag-index.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "embedding_model": model,
        "embedding_dimension": dimension,
        "source_path": str(examples_path.resolve()),
        "source_sha256": source_digest,
        "example_count": len(indexed),
        "embedding_input_tokens": total_tokens,
        "examples": indexed,
    }
    index_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = index_path.with_suffix(f"{index_path.suffix}.part")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(index_path)
    return {
        key: value
        for key, value in payload.items()
        if key != "examples"
    }


class LocalRAGRetriever:
    def __init__(
        self,
        index_path: Path,
        *,
        embedding_client: EmbeddingClient | None = None,
    ):
        self.index_path = index_path
        self.embedding_client = embedding_client or BailianEmbeddingClient()
        payload = json.loads(index_path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != "email-rag-index.v1":
            raise ValueError("unsupported RAG index schema")
        examples = payload.get("examples")
        if not isinstance(examples, list) or not examples:
            raise ValueError("RAG index contains no examples")
        self.embedding_model = str(payload.get("embedding_model") or "")
        self.embedding_dimension = int(payload.get("embedding_dimension") or 0)
        self.examples = examples

    def retrieve(
        self,
        query_text: str,
        *,
        intent: str | None = None,
        top_k: int = 4,
        min_similarity: float = 0.25,
    ) -> tuple[RetrievedExample, ...]:
        if not 1 <= top_k <= 10:
            raise ValueError("top_k must be between 1 and 10")
        sanitized = sanitize_embedding_text(query_text)
        if not sanitized:
            return ()
        result = self.embedding_client.embed(
            [sanitized],
            input_type="query",
            instruct=(
                "Retrieve historical B2B sales email conversations with a "
                "similar customer intent, product need, and sales stage."
            ),
        )
        query_vector = list(result.vectors[0])
        scored: list[RetrievedExample] = []
        for row in self.examples:
            raw_vector = row.get("vector")
            if not isinstance(raw_vector, list):
                continue
            similarity = _cosine(
                query_vector,
                tuple(float(value) for value in raw_vector),
            )
            if intent and row.get("intent") == intent:
                similarity = min(1.0, similarity + 0.03)
            if similarity < min_similarity:
                continue
            scored.append(
                RetrievedExample(
                    example_id=str(row["id"]),
                    thread_id=str(row.get("thread_id") or ""),
                    similarity=similarity,
                    intent=str(row.get("intent") or "other"),
                    subject=str(row.get("subject") or ""),
                    request_text=str(row.get("request_text") or ""),
                    reference_response=str(row.get("reference_response") or ""),
                    quality_score=int(row.get("quality_score") or 0),
                    boss_anchor=bool(row.get("boss_anchor")),
                )
            )
        scored.sort(
            key=lambda item: (
                item.similarity,
                item.boss_anchor,
                item.quality_score,
                item.example_id,
            ),
            reverse=True,
        )
        selected = scored[:top_k]
        best_boss_anchor = next(
            (
                item
                for item in scored
                if item.boss_anchor
                and (intent is None or item.intent == intent)
            ),
            None,
        )
        if (
            best_boss_anchor is not None
            and best_boss_anchor not in selected
            and len(selected) >= 2
        ):
            selected[-1] = best_boss_anchor
        selected.sort(
            key=lambda item: (
                item.similarity,
                item.boss_anchor,
                item.quality_score,
            ),
            reverse=True,
        )
        return tuple(selected)
