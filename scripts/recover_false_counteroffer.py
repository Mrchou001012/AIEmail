from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from dotenv import dotenv_values


def duplicate_source(value: str) -> tuple[int, int]:
    try:
        email_id_text, handoff_id_text = value.split(":", maxsplit=1)
        email_id = int(email_id_text)
        handoff_id = int(handoff_id_text)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "duplicate source must use EMAIL_ID:HANDOFF_ID"
        ) from exc
    if email_id <= 0 or handoff_id <= 0:
        raise argparse.ArgumentTypeError("duplicate source IDs must be positive integers")
    return email_id, handoff_id


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Safely reparse and reprocess a proven false PRICE_NEGOTIATION handoff, "
            "optionally suppressing explicit duplicate requests."
        )
    )
    parser.add_argument("--env-file", type=Path, default=Path("/etc/aiemail/aiemail.env"))
    parser.add_argument("--email-id", type=int, required=True)
    parser.add_argument("--case-id", type=int, required=True)
    parser.add_argument("--handoff-id", type=int, required=True)
    parser.add_argument("--expected-body", required=True)
    parser.add_argument("--expected-existing-quantity", type=int, required=True)
    parser.add_argument("--expected-new-quantity", type=int, required=True)
    parser.add_argument("--expected-recipient", required=True)
    parser.add_argument("--recovery-commit", required=True)
    parser.add_argument("--expected-dingtalk-status", default="SENT")
    parser.add_argument(
        "--duplicate-source",
        action="append",
        default=[],
        type=duplicate_source,
        metavar="EMAIL_ID:HANDOFF_ID",
        help=(
            "A proven earlier semantic duplicate to suppress without replying. "
            "May be repeated; the canonical --email-id must be the latest request."
        ),
    )
    parser.add_argument("--max-duplicate-gap-seconds", type=int, default=300)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Required acknowledgement that the guarded recovery may update production data.",
    )
    args = parser.parse_args()
    if not args.apply:
        parser.error("--apply is required")
    if args.max_duplicate_gap_seconds <= 0:
        parser.error("--max-duplicate-gap-seconds must be positive")
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

    # Import after the explicit environment file has been loaded because the
    # application constructs its async database engine during module import.
    from app.recovery import (
        FalseCounterofferDuplicateSource,
        FalseCounterofferRecoveryRequest,
        recover_false_counteroffer,
    )

    result = await recover_false_counteroffer(
        FalseCounterofferRecoveryRequest(
            email_id=args.email_id,
            case_id=args.case_id,
            handoff_id=args.handoff_id,
            expected_body=args.expected_body,
            expected_existing_quantity=args.expected_existing_quantity,
            expected_new_quantity=args.expected_new_quantity,
            expected_recipient=args.expected_recipient,
            recovery_commit=args.recovery_commit,
            expected_dingtalk_status=args.expected_dingtalk_status,
            duplicate_sources=tuple(
                FalseCounterofferDuplicateSource(email_id=email_id, handoff_id=handoff_id)
                for email_id, handoff_id in args.duplicate_source
            ),
            max_duplicate_gap_seconds=args.max_duplicate_gap_seconds,
        )
    )
    return result.as_dict()


def main() -> None:
    args = parse_args()
    print(json.dumps(asyncio.run(run(args)), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
