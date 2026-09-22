from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from app.imap_history import HistoryIMAPSettings, ReadOnlyHistoryDownloader

UID_PATTERN = re.compile(rb"\bUID\s+(\d+)\b", re.I)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download selected RAG candidate messages with read-only IMAP PEEK."
    )
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-messages", type=int, default=40)
    parser.add_argument("--max-bytes-per-message", type=int, default=524288)
    parser.add_argument("--connect-attempts", type=int, default=3)
    return parser.parse_args()


def _fetch_batch(
    client: Any,
    uids: list[int],
    *,
    max_bytes: int,
) -> dict[int, bytes]:
    if not uids:
        return {}
    uid_set = ",".join(str(uid) for uid in uids)
    status, response = client.uid(
        "fetch",
        uid_set,
        f"(UID BODY.PEEK[]<0.{max_bytes}>)",
    )
    if status != "OK" or not isinstance(response, list):
        raise RuntimeError("IMAP candidate body fetch failed")
    result: dict[int, bytes] = {}
    for item in response:
        if (
            not isinstance(item, tuple)
            or len(item) < 2
            or not isinstance(item[0], bytes)
            or not isinstance(item[1], bytes)
        ):
            continue
        match = UID_PATTERN.search(item[0])
        if match:
            result[int(match.group(1))] = item[1]
    return result


def main() -> None:
    args = parse_args()
    if (
        args.max_messages <= 0
        or args.max_bytes_per_message <= 0
        or args.connect_attempts <= 0
    ):
        raise ValueError("download limits must be positive")
    index = json.loads(args.index.read_text(encoding="utf-8"))
    candidates = index.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("candidate index does not contain candidates")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    pending = [
        item
        for item in candidates
        if not (args.output_dir / f"uid-{int(item['uid'])}.eml").is_file()
    ][: args.max_messages]
    settings = HistoryIMAPSettings()
    downloader = ReadOnlyHistoryDownloader(settings)
    client = None
    downloaded: list[dict[str, Any]] = []
    try:
        if pending:
            last_connect_error: Exception | None = None
            for attempt in range(1, args.connect_attempts + 1):
                try:
                    client = downloader._connect()
                    break
                except (OSError, TimeoutError) as exc:
                    last_connect_error = exc
                    if attempt < args.connect_attempts:
                        time.sleep(2 ** (attempt - 1))
            if client is None:
                raise RuntimeError(
                    f"unable to connect after {args.connect_attempts} attempts"
                ) from last_connect_error
            if index.get("selected_folder") != downloader._active_folder:
                raise RuntimeError("IMAP folder changed since candidate selection")
            for start in range(0, len(pending), 10):
                batch = pending[start : start + 10]
                payloads = _fetch_batch(
                    client,
                    [int(item["uid"]) for item in batch],
                    max_bytes=args.max_bytes_per_message,
                )
                for item in batch:
                    uid = int(item["uid"])
                    raw = payloads.get(uid)
                    if not raw:
                        continue
                    target = args.output_dir / f"uid-{uid}.eml"
                    temporary = target.with_suffix(".eml.part")
                    temporary.write_bytes(raw)
                    temporary.replace(target)
                    downloaded.append(
                        {
                            "uid": uid,
                            "bytes": len(raw),
                            "sha256": hashlib.sha256(raw).hexdigest(),
                            "subject": item.get("subject"),
                            "sender": item.get("sender"),
                            "intent_bucket": item.get("intent_bucket"),
                            "boss_anchor": bool(item.get("boss_anchor")),
                        }
                    )
    finally:
        downloader._logout_quietly(client)

    all_downloaded = sorted(args.output_dir.glob("uid-*.eml"))
    remaining = len(candidates) - len(all_downloaded)
    report = {
        "schema_version": "rag-imap-download-batch.v1",
        "read_only": True,
        "fetch_mode": f"BODY.PEEK[]<0.{args.max_bytes_per_message}>",
        "selected_candidates": len(candidates),
        "downloaded_this_run": len(downloaded),
        "downloaded_total": len(all_downloaded),
        "remaining": max(0, remaining),
        "downloaded_bytes_this_run": sum(item["bytes"] for item in downloaded),
        "items": downloaded,
    }
    (args.output_dir / "_download_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "items"},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
