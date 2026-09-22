from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.rag_retrieval import build_rag_index


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a local historical-email RAG index with Bailian embeddings."
    )
    parser.add_argument("--examples", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = build_rag_index(
        examples_path=args.examples,
        index_path=args.output,
        batch_size=args.batch_size,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
