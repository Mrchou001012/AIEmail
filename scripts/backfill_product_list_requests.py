from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from dotenv import dotenv_values


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Preview or safely queue replies for recent explicit product-list "
            "requests that are still represented by open handoffs."
        )
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path("/etc/aiemail/aiemail.env"),
    )
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument(
        "--handoff-id",
        type=int,
        action="append",
        default=[],
        help="Restrict processing to one handoff. May be repeated.",
    )
    parser.add_argument(
        "--include-history",
        action="store_true",
        help=(
            "Allow explicitly selected Gmail-history handoffs. Requires at least "
            "one --handoff-id and remains subject to --max-age-days."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Queue eligible replies. Without this flag the command is read-only.",
    )
    args = parser.parse_args()
    if not args.env_file.is_file():
        parser.error(f"environment file does not exist: {args.env_file}")
    if args.limit <= 0:
        parser.error("--limit must be positive")
    if args.max_age_days <= 0:
        parser.error("--max-age-days must be positive")
    if any(value <= 0 for value in args.handoff_id):
        parser.error("--handoff-id values must be positive")
    if args.include_history and not args.handoff_id:
        parser.error("--include-history requires at least one --handoff-id")
    if args.apply and not args.handoff_id:
        parser.error("--apply requires at least one reviewed --handoff-id")
    return args


async def run(args: argparse.Namespace) -> dict[str, object]:
    os.environ.update(
        {
            key: value
            for key, value in dotenv_values(args.env_file).items()
            if value is not None
        }
    )

    # Application settings and the async database engine are initialized at
    # import time, so load them only after the explicit environment file.
    from app.db import SessionLocal
    from app.services import backfill_product_list_requests

    async with SessionLocal() as session:
        return await backfill_product_list_requests(
            session,
            apply=args.apply,
            limit=args.limit,
            max_age_days=args.max_age_days,
            handoff_ids=tuple(args.handoff_id),
            include_history=args.include_history,
        )


def main() -> None:
    args = parse_args()
    print(json.dumps(asyncio.run(run(args)), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
