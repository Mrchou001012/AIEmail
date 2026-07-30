from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from scripts.select_imap_rag_candidates import _select_candidates


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge read-only IMAP candidate-index shards locally."
    )
    parser.add_argument("--input", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target", type=int, default=450)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    indexes = [
        json.loads(path.read_text(encoding="utf-8")) for path in args.input
    ]
    folders = {str(index.get("selected_folder")) for index in indexes}
    if len(folders) != 1:
        raise ValueError("candidate shards use different IMAP folders")
    headers = [
        candidate
        for index in indexes
        for candidate in index.get("candidates", [])
    ]
    selected = _select_candidates(headers, target=args.target)
    report = {
        "schema_version": "rag-imap-candidate-index.v1",
        "read_only": True,
        "selected_folder": folders.pop(),
        "source_shards": len(indexes),
        "source_candidates": len(headers),
        "selected_candidates": len(selected),
        "boss_anchors": sum(1 for item in selected if item["boss_anchor"]),
        "intent_buckets": dict(
            sorted(Counter(item["intent_bucket"] for item in selected).items())
        ),
        "sender_counts": dict(
            Counter(item["sender"] for item in selected).most_common()
        ),
        "candidates": selected,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "candidates"},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
