from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from app.coa_catalog import COACatalogScanner


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build or incrementally update the standard English COA catalog"
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("runtime/coa_catalog/catalog.json"),
    )
    parser.add_argument(
        "--product-catalog",
        type=Path,
        default=Path("config/product_catalog.yaml"),
    )
    parser.add_argument("--max-file-mb", type=int, default=50)
    parser.add_argument("--file-timeout-seconds", type=int, default=15)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    scanner = COACatalogScanner(
        root=args.root,
        output_path=args.output,
        product_catalog_path=args.product_catalog,
        max_file_bytes=args.max_file_mb * 1024 * 1024,
        extraction_timeout_seconds=args.file_timeout_seconds,
    )
    result = scanner.scan()
    summary = {
        key: value
        for key, value in result.items()
        if key not in {"entries", "review", "enumeration_warnings"}
    }
    summary["enumeration_warning_count"] = len(result["enumeration_warnings"])
    print(json.dumps(summary, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
