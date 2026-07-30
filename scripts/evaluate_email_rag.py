from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
from typing import Any

from app.rag_retrieval import LocalRAGRetriever


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate historical-email RAG retrieval on a held-out split."
    )
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--examples", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument("--min-similarity", type=float, default=0.25)
    return parser.parse_args()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def main() -> None:
    args = parse_args()
    evaluation_rows = _read_jsonl(args.examples)
    if not evaluation_rows:
        raise ValueError("evaluation split is empty")
    retriever = LocalRAGRetriever(args.index)
    details: list[dict[str, Any]] = []
    for row in evaluation_rows:
        matches = retriever.retrieve(
            str(row["retrieval_text"]),
            intent=str(row.get("intent") or "other"),
            top_k=args.top_k,
            min_similarity=args.min_similarity,
        )
        details.append(
            {
                "query_id": row["id"],
                "query_thread_id": row.get("thread_id"),
                "intent": row.get("intent"),
                "match_count": len(matches),
                "top_similarity": matches[0].similarity if matches else None,
                "same_intent_at_k": any(
                    match.intent == row.get("intent")
                    for match in matches
                ),
                "boss_anchor_at_k": any(
                    match.boss_anchor for match in matches
                ),
                "thread_leakage": any(
                    match.thread_id
                    and match.thread_id == row.get("thread_id")
                    for match in matches
                ),
                "match_ids": [match.example_id for match in matches],
            }
        )
    similarities = [
        float(row["top_similarity"])
        for row in details
        if row["top_similarity"] is not None
    ]
    count = len(details)
    report = {
        "schema_version": "email-rag-evaluation.v1",
        "evaluation_examples": count,
        "top_k": args.top_k,
        "min_similarity": args.min_similarity,
        "retrieval_coverage": sum(row["match_count"] > 0 for row in details) / count,
        "same_intent_at_k": sum(row["same_intent_at_k"] for row in details) / count,
        "boss_anchor_exposure": sum(row["boss_anchor_at_k"] for row in details) / count,
        "thread_leakage_count": sum(row["thread_leakage"] for row in details),
        "mean_top_similarity": mean(similarities) if similarities else None,
        "details": details,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                key: value
                for key, value in report.items()
                if key != "details"
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
