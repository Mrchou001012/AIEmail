from __future__ import annotations

import json
from pathlib import Path

from app.embeddings import EmbeddingBatch
from app.rag_retrieval import (
    LocalRAGRetriever,
    build_rag_index,
    sanitize_embedding_text,
)


class FakeEmbeddingClient:
    def embed(
        self,
        texts: list[str],
        *,
        input_type: str,
        instruct: str | None = None,
    ) -> EmbeddingBatch:
        vectors = []
        for text in texts:
            lowered = text.casefold()
            if "shipment" in lowered or "tracking" in lowered:
                vectors.append((0.0, 1.0, 0.0))
            elif "quote" in lowered or "price" in lowered:
                vectors.append((1.0, 0.0, 0.0))
            else:
                vectors.append((0.0, 0.0, 1.0))
        return EmbeddingBatch(
            vectors=tuple(vectors),
            total_tokens=len(texts) * 5,
            model="fake-embedding",
            request_id="request-1",
        )


class IntentBoundaryEmbeddingClient:
    def embed(
        self,
        texts: list[str],
        *,
        input_type: str,
        instruct: str | None = None,
    ) -> EmbeddingBatch:
        vectors = []
        for text in texts:
            if "cross-intent boss" in text.casefold():
                vectors.append((0.8, 0.6))
            else:
                vectors.append((1.0, 0.0))
        return EmbeddingBatch(
            vectors=tuple(vectors),
            total_tokens=len(texts),
            model="intent-boundary-embedding",
            request_id="request-intent-boundary",
        )


def _write_examples(path: Path) -> None:
    rows = [
        {
            "id": "quote-1",
            "intent": "quote_request",
            "subject": "Price request",
            "request_text": "Please quote 500 kg.",
            "retrieval_text": "Intent: quote_request\nPlease quote 500 kg.",
            "reference_response": "Thank you for your inquiry.",
            "quality_score": 95,
            "boss_anchor": True,
        },
        {
            "id": "shipping-1",
            "intent": "shipping",
            "subject": "Tracking",
            "request_text": "Please share shipment tracking.",
            "retrieval_text": "Intent: shipping\nPlease share shipment tracking.",
            "reference_response": "Please find the tracking details.",
            "quality_score": 90,
            "boss_anchor": False,
        },
    ]
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def test_sanitize_embedding_text_removes_contact_details() -> None:
    result = sanitize_embedding_text(
        "Email buyer@example.com or call +91 99999 88888 about the quote."
    )
    assert "buyer@example.com" not in result
    assert "99999" not in result
    assert "[email]" in result
    assert "[phone]" in result


def test_build_and_retrieve_local_rag_index(tmp_path: Path) -> None:
    examples_path = tmp_path / "examples.jsonl"
    index_path = tmp_path / "index.json"
    _write_examples(examples_path)
    report = build_rag_index(
        examples_path=examples_path,
        index_path=index_path,
        embedding_client=FakeEmbeddingClient(),
    )

    assert report["example_count"] == 2
    assert report["embedding_input_tokens"] == 10
    retriever = LocalRAGRetriever(
        index_path,
        embedding_client=FakeEmbeddingClient(),
    )
    matches = retriever.retrieve(
        "Could you quote your best price?",
        intent="quote_request",
        top_k=1,
        min_similarity=0.2,
    )

    assert len(matches) == 1
    assert matches[0].example_id == "quote-1"
    assert matches[0].boss_anchor is True
    assert matches[0].similarity == 1.0


def test_cross_intent_boss_anchor_does_not_replace_same_intent_match(
    tmp_path: Path,
) -> None:
    examples_path = tmp_path / "examples.jsonl"
    index_path = tmp_path / "index.json"
    rows = [
        {
            "id": "quote-1",
            "intent": "quote_request",
            "subject": "Quote one",
            "request_text": "Please quote product one.",
            "retrieval_text": "Please quote product one.",
            "reference_response": "Quote response one.",
            "quality_score": 90,
            "boss_anchor": False,
        },
        {
            "id": "quote-2",
            "intent": "quote_request",
            "subject": "Quote two",
            "request_text": "Please quote product two.",
            "retrieval_text": "Please quote product two.",
            "reference_response": "Quote response two.",
            "quality_score": 80,
            "boss_anchor": False,
        },
        {
            "id": "shipping-boss",
            "intent": "shipping",
            "subject": "Cross-intent boss",
            "request_text": "Cross-intent boss shipping request.",
            "retrieval_text": "Cross-intent boss shipping request.",
            "reference_response": "Cross-intent boss shipping response.",
            "quality_score": 100,
            "boss_anchor": True,
        },
    ]
    examples_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    build_rag_index(
        examples_path=examples_path,
        index_path=index_path,
        embedding_client=IntentBoundaryEmbeddingClient(),
    )
    retriever = LocalRAGRetriever(
        index_path,
        embedding_client=IntentBoundaryEmbeddingClient(),
    )

    matches = retriever.retrieve(
        "Please quote this product.",
        intent="quote_request",
        top_k=2,
        min_similarity=0.5,
    )

    assert [match.example_id for match in matches] == ["quote-1", "quote-2"]
