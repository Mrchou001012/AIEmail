from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the resumable read-only Gmail RAG thread downloader."
    )
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--target-completed-threads", type=int, default=100)
    parser.add_argument("--retry-delay-seconds", type=int, default=60)
    parser.add_argument("--max-hours", type=float, default=4)
    parser.add_argument("--detach", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def _state_count(output_dir: Path) -> int:
    path = output_dir / "_thread_download_state.json"
    if not path.is_file():
        return 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    return len(payload.get("completed_thread_ids", []))


def _write_status(output_dir: Path, payload: dict[str, Any]) -> None:
    path = output_dir / "_thread_worker_status.json"
    temporary = path.with_suffix(".json.part")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _worker_command(args: argparse.Namespace) -> list[str]:
    script = Path(__file__).with_name("download_selected_rag_threads.py")
    return [
        sys.executable,
        str(script),
        "--index",
        str(args.index),
        "--output-dir",
        str(args.output_dir),
        "--max-threads",
        str(args.target_completed_threads),
        "--max-messages-per-thread",
        "20",
        "--max-bytes-per-message",
        "131072",
        "--fetch-batch-size",
        "5",
        "--connect-attempts",
        "3",
        "--boss-anchors-first",
        "--balanced-intents",
    ]


def run_worker(args: argparse.Namespace) -> int:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(UTC)
    deadline = started_at + timedelta(hours=args.max_hours)
    attempts = 0
    previous_count = _state_count(args.output_dir)
    while (
        previous_count < args.target_completed_threads
        and datetime.now(UTC) < deadline
    ):
        attempts += 1
        result = subprocess.run(
            _worker_command(args),
            cwd=Path(__file__).resolve().parents[1],
            check=False,
        )
        current_count = _state_count(args.output_dir)
        _write_status(
            args.output_dir,
            {
                "schema_version": "rag-mailbox-worker-status.v1",
                "pid": os.getpid(),
                "started_at": started_at.isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
                "target_completed_threads": args.target_completed_threads,
                "completed_threads": current_count,
                "attempts": attempts,
                "last_exit_code": result.returncode,
                "state": (
                    "complete"
                    if current_count >= args.target_completed_threads
                    else "retrying"
                ),
            },
        )
        if current_count >= args.target_completed_threads:
            return 0
        if result.returncode == 0 and current_count == previous_count:
            return 2
        previous_count = current_count
        time.sleep(args.retry_delay_seconds)
    return 0 if previous_count >= args.target_completed_threads else 3


def detach_worker(args: argparse.Namespace) -> int:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.output_dir / "_thread_worker.log"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--index",
        str(args.index),
        "--output-dir",
        str(args.output_dir),
        "--target-completed-threads",
        str(args.target_completed_threads),
        "--retry-delay-seconds",
        str(args.retry_delay_seconds),
        "--max-hours",
        str(args.max_hours),
        "--worker",
    ]
    creation_flags = 0
    if os.name == "nt":
        creation_flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
            close_fds=True,
        )
    _write_status(
        args.output_dir,
        {
            "schema_version": "rag-mailbox-worker-status.v1",
            "pid": process.pid,
            "started_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
            "target_completed_threads": args.target_completed_threads,
            "completed_threads": _state_count(args.output_dir),
            "attempts": 0,
            "last_exit_code": None,
            "state": "starting",
        },
    )
    time.sleep(1)
    initial_exit_code = process.poll()
    print(
        json.dumps(
            {
                "started": True,
                "pid": process.pid,
                "log_path": str(log_path.resolve()),
                "target_completed_threads": args.target_completed_threads,
                "read_only": True,
                "initial_exit_code": initial_exit_code,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main() -> None:
    args = parse_args()
    if args.target_completed_threads <= 0:
        raise ValueError("target completed threads must be positive")
    if args.retry_delay_seconds < 10:
        raise ValueError("retry delay must be at least 10 seconds")
    if args.max_hours <= 0:
        raise ValueError("max hours must be positive")
    if args.detach and args.worker:
        raise ValueError("--detach and --worker are mutually exclusive")
    raise SystemExit(detach_worker(args) if args.detach else run_worker(args))


if __name__ == "__main__":
    main()
