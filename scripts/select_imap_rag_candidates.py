from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from app.imap_history import HistoryIMAPSettings, ReadOnlyHistoryDownloader
from scripts.audit_imap_rag_candidates import _classify_header, _fetch_headers, _search


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a read-only, evenly sampled index of RAG email candidates."
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--header-sample", type=int, default=3000)
    parser.add_argument("--target", type=int, default=600)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument(
        "--company-domain",
        action="append",
        required=True,
        help="Company email domain; repeat for aliases.",
    )
    parser.add_argument(
        "--boss-address",
        action="append",
        default=[],
        help="High-quality sender address; repeat for aliases.",
    )
    return parser.parse_args()


def build_sender_query(addresses: set[str]) -> str:
    if not addresses:
        return ""
    return "{" + " ".join(
        f"from:{address}" for address in sorted(addresses)
    ) + "}"


def build_company_query(company_domains: set[str]) -> str:
    company_from = "{" + " ".join(
        f"from:(@{domain})" for domain in sorted(company_domains)
    ) + "}"
    return (
        f"{company_from} "
        "{enquiry inquiry quote quotation price offer sample order coa shipment "
        "dispatch delivery payment availability stock lead-time leadtime}"
    )


def _even_sample(values: list[bytes], limit: int) -> list[bytes]:
    if limit <= 0 or len(values) <= limit:
        return values
    if limit == 1:
        return [values[-1]]
    indexes = {
        round(index * (len(values) - 1) / (limit - 1))
        for index in range(limit)
    }
    return [values[index] for index in sorted(indexes)]


def _intent_bucket(subject: str) -> str:
    value = subject.casefold()
    patterns = (
        (
            "SAMPLE_TECHNICAL",
            r"\b(?:sample|coa|sds|msds|tds|specification|technical)\b",
        ),
        (
            "LOGISTICS",
            r"\b(?:shipment|shipping|dispatch|delivery|freight|transport|lead\s*time)\b",
        ),
        ("ORDER_PO", r"\b(?:purchase order|confirmed order|order|p\.?o\.?)\b"),
        ("PAYMENT_TERMS", r"\b(?:payment|credit|advance|terms?)\b"),
        (
            "QUOTE_AVAILABILITY",
            r"\b(?:rfq|enquiry|inquiry|quote|quotation|price|offer|availability|stock)\b",
        ),
    )
    for bucket, pattern in patterns:
        if re.search(pattern, value, flags=re.I):
            return bucket
    return "FOLLOWUP_OTHER"


def _normalized_subject(subject: str) -> str:
    return re.sub(
        r"^(?:(?:re|fw|fwd)\s*:\s*)+",
        "",
        subject.strip(),
        flags=re.I,
    ).casefold()


def _select_candidates(
    headers: list[dict[str, Any]],
    *,
    target: int,
    company_domains: set[str],
    boss_addresses: set[str],
) -> list[dict[str, Any]]:
    eligible: list[dict[str, Any]] = []
    for row in headers:
        score, reasons = _classify_header(
            row,
            company_domains=company_domains,
            boss_addresses=boss_addresses,
        )
        reason_set = set(reasons)
        if not ({"boss_sender", "company_sender"} & reason_set):
            continue
        if "external_customer_recipient" not in reason_set:
            continue
        if "automated_or_bulk" in reason_set or "internal_only" in reason_set:
            continue
        if "thread_headers" not in reason_set and "reply_subject" not in reason_set:
            continue
        if score < 8:
            continue
        candidate = dict(row)
        candidate["candidate_score"] = score
        candidate["candidate_reasons"] = reasons
        candidate["intent_bucket"] = _intent_bucket(str(row["subject"]))
        candidate["boss_anchor"] = str(row["sender"]) in boss_addresses
        eligible.append(candidate)

    eligible.sort(
        key=lambda item: (
            bool(item["boss_anchor"]),
            int(item["candidate_score"]),
            int(item["uid"]),
        ),
        reverse=True,
    )
    bucket_queues: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_threads: set[str] = set()
    for item in eligible:
        thread_key = (
            f"gmail:{item['gmail_thread_id']}"
            if item.get("gmail_thread_id")
            else (
                f"subject:{_normalized_subject(str(item['subject']))}:"
                f"{','.join(sorted(item['recipients']))}"
            )
        )
        if thread_key in seen_threads:
            continue
        seen_threads.add(thread_key)
        item["selection_thread_key"] = thread_key
        bucket_queues[str(item["intent_bucket"])].append(item)

    selected: list[dict[str, Any]] = []
    sender_counts: Counter[str] = Counter()
    bucket_order = [
        "QUOTE_AVAILABILITY",
        "ORDER_PO",
        "LOGISTICS",
        "SAMPLE_TECHNICAL",
        "PAYMENT_TERMS",
        "FOLLOWUP_OTHER",
    ]
    while len(selected) < target:
        made_progress = False
        for bucket in bucket_order:
            queue = bucket_queues[bucket]
            while queue:
                item = queue.pop(0)
                sender = str(item["sender"])
                sender_cap = max(40, target // 4)
                if sender_counts[sender] >= sender_cap and not item["boss_anchor"]:
                    continue
                selected.append(item)
                sender_counts[sender] += 1
                made_progress = True
                break
            if len(selected) >= target:
                break
        if not made_progress:
            break
    selected.sort(key=lambda item: int(item["uid"]))
    return selected


def main() -> None:
    args = parse_args()
    if args.shard_count <= 0 or not 0 <= args.shard_index < args.shard_count:
        raise ValueError("shard-index must be within shard-count")
    company_domains = {
        domain.strip().casefold().lstrip("@")
        for domain in args.company_domain
        if domain.strip().lstrip("@")
    }
    boss_addresses = {
        address.strip().casefold()
        for address in args.boss_address
        if address.strip()
    }
    if not company_domains:
        raise ValueError("at least one company domain is required")
    company_query = build_company_query(company_domains)
    boss_query = build_sender_query(boss_addresses)
    settings = HistoryIMAPSettings()
    downloader = ReadOnlyHistoryDownloader(settings)
    client = None
    try:
        client = downloader._connect()
        company_uids = _search(client, company_query)
        boss_uids = _search(client, boss_query) if boss_query else []
        sampled_all = _even_sample(company_uids, args.header_sample)
        sampled = sampled_all[args.shard_index :: args.shard_count]
        combined_uids = sorted(
            set(sampled)
            | (set(boss_uids) if args.shard_index == 0 else set()),
            key=int,
        )
        headers = _fetch_headers(client, combined_uids)
        selected = _select_candidates(
            headers,
            target=args.target,
            company_domains=company_domains,
            boss_addresses=boss_addresses,
        )
        report = {
            "schema_version": "rag-imap-candidate-index.v1",
            "read_only": True,
            "selected_folder": downloader._active_folder,
            "company_query_matches": len(company_uids),
            "boss_query_matches": len(boss_uids),
            "shard_index": args.shard_index,
            "shard_count": args.shard_count,
            "headers_sampled": len(combined_uids),
            "headers_received": len(headers),
            "selected_candidates": len(selected),
            "boss_anchors": sum(1 for item in selected if item["boss_anchor"]),
            "intent_buckets": dict(
                sorted(Counter(item["intent_bucket"] for item in selected).items())
            ),
            "sender_counts": dict(
                Counter(item["sender"] for item in selected).most_common()
            ),
            "candidates": selected,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {key: value for key, value in report.items() if key != "candidates"},
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        downloader._logout_quietly(client)


if __name__ == "__main__":
    main()
