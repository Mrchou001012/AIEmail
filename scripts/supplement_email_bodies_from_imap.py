from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from app.imap_history import HistoryIMAPSettings, ReadOnlyHistoryDownloader


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only Gmail IMAP supplement for Foxmail EML exports whose body "
            "was not cached during bulk export."
        )
    )
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--remote-index",
        type=Path,
        help="Cache Message-ID to IMAP UID matches so short resumable batches avoid rescanning.",
    )
    parser.add_argument(
        "--max-messages",
        type=int,
        help="Download at most this many unique messages in the current run.",
    )
    parser.add_argument(
        "--folder",
        help="Override RAG_IMAP_FOLDER (default: [Gmail]/All Mail).",
    )
    parser.add_argument(
        "--max-download-mb",
        type=int,
        help="Override RAG_IMAP_MAX_DOWNLOAD_MB for this run.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    overrides: dict[str, object] = {}
    if args.folder:
        overrides["folder"] = args.folder
    if args.max_download_mb is not None:
        overrides["max_download_mb"] = args.max_download_mb
    settings = HistoryIMAPSettings(**overrides)
    downloader = ReadOnlyHistoryDownloader(settings)
    report = downloader.supplement(
        raw_dir=args.raw_dir,
        output_dir=args.output_dir,
        report_path=args.report,
        remote_index_path=args.remote_index,
        max_messages=args.max_messages,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
