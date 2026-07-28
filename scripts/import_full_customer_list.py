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
            "Preview or import every primary/secondary email endpoint from the "
            "original Chinese CRM customer workbook."
        )
    )
    parser.add_argument("workbook", type=Path)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path("/etc/aiemail/aiemail.env"),
    )
    parser.add_argument("--timezone", default="Asia/Kolkata")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist the import. Without this flag the command is read-only.",
    )
    parser.add_argument(
        "--enable-auto-send",
        action="store_true",
        help="Authorize imported CRM relationships for autonomous outreach.",
    )
    parser.add_argument(
        "--skip-cases",
        action="store_true",
        help="Create contacts only, even when a product maps uniquely.",
    )
    parser.add_argument(
        "--allow-unparsed-email-cells",
        action="store_true",
        help="Apply even when a cell still contains an unparsed @ fragment.",
    )
    args = parser.parse_args()
    if not args.workbook.is_file():
        parser.error(f"workbook does not exist: {args.workbook}")
    if not args.env_file.is_file():
        parser.error(f"environment file does not exist: {args.env_file}")
    return args


async def run(args: argparse.Namespace) -> dict[str, object]:
    os.environ.update(
        {
            key: value
            for key, value in dotenv_values(args.env_file).items()
            if value is not None
        }
    )
    from app.db import SessionLocal
    from app.full_customer_import import import_full_customer_workbook

    async with SessionLocal() as session:
        result = await import_full_customer_workbook(
            args.workbook,
            session,
            apply=args.apply,
            timezone=args.timezone,
            enable_auto_send=args.enable_auto_send,
            create_cases=not args.skip_cases,
            allow_unparsed_email_cells=args.allow_unparsed_email_cells,
        )
    return result.to_dict()


def main() -> None:
    args = parse_args()
    print(json.dumps(asyncio.run(run(args)), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
