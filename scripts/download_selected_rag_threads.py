from __future__ import annotations

import argparse
import hashlib
import imaplib
import json
import re
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from app.imap_history import HistoryIMAPSettings, ReadOnlyHistoryDownloader

UID_PATTERN = re.compile(rb"\bUID\s+(\d+)\b", re.I)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download every original message in selected Gmail threads with "
            "read-only IMAP PEEK."
        )
    )
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-threads", type=int, default=10)
    parser.add_argument(
        "--max-new-messages",
        type=int,
        default=0,
        help="Optional per-run message cap; zero means unlimited.",
    )
    parser.add_argument(
        "--max-messages-per-thread",
        type=int,
        default=30,
        help="Keep messages nearest the selected seed; zero downloads the full thread.",
    )
    parser.add_argument("--max-bytes-per-message", type=int, default=262144)
    parser.add_argument("--fetch-batch-size", type=int, default=5)
    parser.add_argument("--connect-attempts", type=int, default=3)
    parser.add_argument(
        "--existing-seeds-only",
        action="store_true",
        help="Only expand candidate threads whose selected outbound seed is downloaded.",
    )
    parser.add_argument(
        "--boss-anchors-first",
        action="store_true",
        help="Expand Boss-authored candidate threads before the remaining team threads.",
    )
    parser.add_argument(
        "--balanced-intents",
        action="store_true",
        help="Round-robin candidate intent buckets for a more diverse corpus.",
    )
    return parser.parse_args()


def _fetch_batch(
    client: Any,
    uids: list[int],
    *,
    max_bytes: int,
) -> dict[int, bytes]:
    if not uids:
        return {}
    status, response = client.uid(
        "fetch",
        ",".join(str(uid) for uid in uids),
        f"(UID BODY.PEEK[]<0.{max_bytes}>)",
    )
    if status != "OK" or not isinstance(response, list):
        raise RuntimeError("IMAP thread body fetch failed")
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


def _thread_uids(client: Any, gmail_thread_id: int) -> list[int]:
    status, response = client.uid(
        "search",
        None,
        "X-GM-THRID",
        str(gmail_thread_id),
    )
    if status != "OK" or not response or not isinstance(response[0], bytes):
        raise RuntimeError(f"IMAP thread search failed: {gmail_thread_id}")
    return sorted(
        {
            int(value)
            for value in response[0].split()
            if value.isdigit()
        }
    )


def _connect(
    downloader: ReadOnlyHistoryDownloader,
    *,
    attempts: int,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return downloader._connect()
        except (OSError, TimeoutError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(2 ** (attempt - 1))
    raise RuntimeError(f"unable to connect after {attempts} attempts") from last_error


def _save_state(path: Path, completed_thread_ids: set[int]) -> None:
    temporary = path.with_suffix(".json.part")
    temporary.write_text(
        json.dumps(
            {
                "schema_version": "rag-imap-thread-state.v1",
                "completed_thread_ids": sorted(completed_thread_ids),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def _ordered_candidates(
    candidates: list[dict[str, Any]],
    *,
    boss_anchors_first: bool,
    balanced_intents: bool,
) -> list[dict[str, Any]]:
    def quality_key(item: dict[str, Any]) -> tuple[int, int]:
        return (-int(item.get("candidate_score") or 0), -int(item["uid"]))

    bosses = (
        sorted(
            (item for item in candidates if item.get("boss_anchor")),
            key=quality_key,
        )
        if boss_anchors_first
        else []
    )
    boss_thread_ids = {int(item["gmail_thread_id"]) for item in bosses}
    remaining = [
        item
        for item in candidates
        if int(item["gmail_thread_id"]) not in boss_thread_ids
    ]
    if balanced_intents:
        buckets: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
        for item in sorted(remaining, key=quality_key):
            buckets[str(item.get("intent_bucket") or "FOLLOWUP_OTHER")].append(
                item
            )
        bucket_order = (
            "QUOTE_AVAILABILITY",
            "ORDER_PO",
            "LOGISTICS",
            "SAMPLE_TECHNICAL",
            "PAYMENT_TERMS",
            "FOLLOWUP_OTHER",
        )
        balanced: list[dict[str, Any]] = []
        while any(buckets.values()):
            for bucket in bucket_order:
                if buckets[bucket]:
                    balanced.append(buckets[bucket].popleft())
            extra_buckets = sorted(set(buckets) - set(bucket_order))
            for bucket in extra_buckets:
                if buckets[bucket]:
                    balanced.append(buckets[bucket].popleft())
        remaining = balanced
    return [*bosses, *remaining]


def main() -> None:
    args = parse_args()
    if (
        args.max_threads <= 0
        or args.max_new_messages < 0
        or args.max_messages_per_thread < 0
        or args.max_bytes_per_message <= 0
        or not 1 <= args.fetch_batch_size <= 10
        or args.connect_attempts <= 0
    ):
        raise ValueError("download limits must be positive")

    index = json.loads(args.index.read_text(encoding="utf-8"))
    candidates = index.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("candidate index does not contain candidates")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    state_path = args.output_dir / "_thread_download_state.json"
    if state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        completed_thread_ids = {
            int(value) for value in state.get("completed_thread_ids", [])
        }
    else:
        completed_thread_ids = set()

    pending: list[dict[str, Any]] = []
    seen_thread_ids: set[int] = set()
    candidate_order = _ordered_candidates(
        candidates,
        boss_anchors_first=args.boss_anchors_first,
        balanced_intents=args.balanced_intents,
    )
    for item in candidate_order:
        thread_id = int(item["gmail_thread_id"])
        if thread_id in completed_thread_ids or thread_id in seen_thread_ids:
            continue
        if args.existing_seeds_only:
            seed_path = args.output_dir / f"uid-{int(item['uid'])}.eml"
            if not seed_path.is_file():
                continue
        pending.append(item)
        seen_thread_ids.add(thread_id)
        if len(pending) >= args.max_threads:
            break

    settings = HistoryIMAPSettings()
    downloader = ReadOnlyHistoryDownloader(settings)
    client = None
    downloaded: list[dict[str, Any]] = []
    expanded: list[dict[str, Any]] = []
    try:
        if pending:
            client = _connect(downloader, attempts=args.connect_attempts)
            if index.get("selected_folder") != downloader._active_folder:
                raise RuntimeError("IMAP folder changed since candidate selection")
            for candidate in pending:
                thread_id = int(candidate["gmail_thread_id"])
                remote_uids = _thread_uids(client, thread_id)
                if not remote_uids:
                    continue
                seed_uid = int(candidate["uid"])
                if (
                    args.max_messages_per_thread
                    and len(remote_uids) > args.max_messages_per_thread
                ):
                    uids = sorted(
                        sorted(
                            remote_uids,
                            key=lambda uid: (abs(uid - seed_uid), uid),
                        )[: args.max_messages_per_thread]
                    )
                else:
                    uids = remote_uids
                missing = [
                    uid
                    for uid in uids
                    if not (args.output_dir / f"uid-{uid}.eml").is_file()
                ]
                if args.max_new_messages:
                    remaining_budget = args.max_new_messages - len(downloaded)
                    missing = missing[: max(0, remaining_budget)]
                fetched_uids: set[int] = set()
                for start in range(0, len(missing), args.fetch_batch_size):
                    batch_uids = missing[start : start + args.fetch_batch_size]
                    try:
                        payloads = _fetch_batch(
                            client,
                            batch_uids,
                            max_bytes=args.max_bytes_per_message,
                        )
                    except (imaplib.IMAP4.abort, OSError):
                        downloader._logout_quietly(client)
                        client = _connect(
                            downloader,
                            attempts=args.connect_attempts,
                        )
                        payloads = _fetch_batch(
                            client,
                            batch_uids,
                            max_bytes=args.max_bytes_per_message,
                        )
                    for uid, raw in payloads.items():
                        target = args.output_dir / f"uid-{uid}.eml"
                        temporary = target.with_suffix(".eml.part")
                        temporary.write_bytes(raw)
                        temporary.replace(target)
                        fetched_uids.add(uid)
                        downloaded.append(
                            {
                                "uid": uid,
                                "gmail_thread_id": thread_id,
                                "bytes": len(raw),
                                "sha256": hashlib.sha256(raw).hexdigest(),
                            }
                        )
                complete = all(
                    (args.output_dir / f"uid-{uid}.eml").is_file()
                    for uid in uids
                )
                expanded.append(
                    {
                        "gmail_thread_id": thread_id,
                        "remote_message_count": len(remote_uids),
                        "target_message_count": len(uids),
                        "new_message_count": len(fetched_uids),
                        "remaining_target_count": sum(
                            not (
                                args.output_dir / f"uid-{uid}.eml"
                            ).is_file()
                            for uid in uids
                        ),
                        "complete": complete,
                    }
                )
                if complete:
                    completed_thread_ids.add(thread_id)
                    _save_state(state_path, completed_thread_ids)
                if (
                    args.max_new_messages
                    and len(downloaded) >= args.max_new_messages
                ):
                    break
    finally:
        downloader._logout_quietly(client)

    all_downloaded = sorted(args.output_dir.glob("uid-*.eml"))
    report = {
        "schema_version": "rag-imap-thread-download.v1",
        "read_only": True,
        "fetch_mode": f"BODY.PEEK[]<0.{args.max_bytes_per_message}>",
        "fetch_batch_size": args.fetch_batch_size,
        "selected_candidate_threads": len(candidates),
        "expanded_this_run": len(expanded),
        "completed_threads_total": len(completed_thread_ids),
        "downloaded_this_run": len(downloaded),
        "downloaded_messages_total": len(all_downloaded),
        "downloaded_bytes_this_run": sum(item["bytes"] for item in downloaded),
        "threads": expanded,
        "items": downloaded,
    }
    (args.output_dir / "_thread_download_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                key: value
                for key, value in report.items()
                if key not in {"threads", "items"}
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
