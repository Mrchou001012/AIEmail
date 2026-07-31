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
            "Upsert the curated product catalog (categories and products) into the database. "
            "Dry-run by default; pass --apply to persist."
        )
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("config/product_catalog.yaml"),
        help="Path to the catalog YAML (default: config/product_catalog.yaml)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path("/etc/aiemail/aiemail.env"),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist the import. Without this flag the command is read-only.",
    )
    args = parser.parse_args()
    if not args.catalog.is_file():
        parser.error(f"catalog does not exist: {args.catalog}")
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
    from app.product_catalog import import_product_catalog

    async with SessionLocal() as session:
        result = await import_product_catalog(
            session,
            path=args.catalog,
            apply=args.apply,
        )
    return result


def main() -> None:
    args = parse_args()
    print(json.dumps(asyncio.run(run(args)), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
