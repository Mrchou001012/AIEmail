from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.rag_history import build_history_dataset


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build an offline RAG dataset from Foxmail-exported EML files."
    )
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--mailbox", action="append", required=True)
    parser.add_argument("--boss-address", action="append", default=[])
    parser.add_argument("--company-domain", action="append", default=[])
    parser.add_argument("--raw-limit", type=int, default=1200)
    parser.add_argument("--knowledge-base-target", type=int, default=200)
    parser.add_argument("--development-target", type=int, default=50)
    parser.add_argument("--test-minimum", type=int, default=50)
    parser.add_argument("--test-maximum", type=int, default=100)
    parser.add_argument("--minimum-quality", type=int, default=60)
    parser.add_argument(
        "--workspace-route-archive",
        action="store_true",
        help="Treat mail routed for any company address as belonging to this archive.",
    )
    parser.add_argument(
        "--include-quoted-history",
        action="store_true",
        help="Recover earlier customer/company turns embedded in reply chains.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = build_history_dataset(
        raw_dir=args.raw_dir,
        output_dir=args.output_dir,
        mailbox_addresses=set(args.mailbox),
        company_domains=set(args.company_domain),
        raw_limit=args.raw_limit,
        knowledge_base_target=args.knowledge_base_target,
        development_target=args.development_target,
        test_minimum=args.test_minimum,
        test_maximum=args.test_maximum,
        minimum_quality=args.minimum_quality,
        workspace_route_archive=args.workspace_route_archive,
        include_quoted_history=args.include_quoted_history,
        boss_addresses=set(args.boss_address),
    )
    print(json.dumps(result.report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
