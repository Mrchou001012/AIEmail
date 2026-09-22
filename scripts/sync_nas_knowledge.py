from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from app.nas_knowledge import NASKnowledgeScanner


def main() -> None:
    parser = argparse.ArgumentParser(description="Build or incrementally update the local NAS knowledge base")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=Path("config/nas_knowledge_policy.yaml"))
    parser.add_argument("--output", type=Path, default=Path("runtime/nas_knowledge"))
    parser.add_argument("--max-file-mb", type=int, default=50)
    parser.add_argument("--file-timeout-seconds", type=int, default=5)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    scanner = NASKnowledgeScanner(
        root=args.root,
        policy_path=args.policy,
        output_dir=args.output,
        max_extract_bytes=args.max_file_mb * 1024 * 1024,
        extraction_timeout_seconds=args.file_timeout_seconds,
    )
    print(json.dumps(scanner.scan(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
