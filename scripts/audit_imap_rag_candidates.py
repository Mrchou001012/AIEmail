from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from email import policy
from email.parser import BytesHeaderParser, BytesParser
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from app.imap_history import HistoryIMAPSettings, ReadOnlyHistoryDownloader

BUSINESS_TERMS = (
    "enquiry",
    "inquiry",
    "quote",
    "quotation",
    "price",
    "sample",
    "order",
    "coa",
    "shipment",
    "delivery",
    "payment",
    "availability",
)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only Gmail header audit for selecting RAG candidates."
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--header-limit",
        type=int,
        default=500,
        help="Inspect at most this many latest headers per query.",
    )
    parser.add_argument(
        "--body-sample-limit",
        type=int,
        default=12,
        help="Read partial bodies for this many highest-scoring external reply candidates.",
    )
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


def build_queries(
    *,
    company_domains: set[str],
    boss_addresses: set[str],
) -> dict[str, str]:
    company_from = "{" + " ".join(
        f"from:(@{domain})" for domain in sorted(company_domains)
    ) + "}"
    company_to = "{" + " ".join(
        f"to:(@{domain})" for domain in sorted(company_domains)
    ) + "}"
    business = (
        "{enquiry inquiry quote quotation price sample order coa shipment "
        "delivery payment availability}"
    )
    queries = {
        "company_business_outbound": f"{company_from} {business}",
        "external_business_inbound": (
            f"-{company_from} {company_to} {business}"
        ),
        "obvious_noise": (
            "{from:(noreply) from:(no-reply) from:(mailer-daemon) "
            "category:promotions category:social}"
        ),
    }
    if boss_addresses:
        boss_from = "{" + " ".join(
            f"from:{address}" for address in sorted(boss_addresses)
        ) + "}"
        queries.update(
            {
                "boss_all_outbound": boss_from,
                "boss_business_outbound": f"{boss_from} {business}",
                "boss_reply_like": (
                    f"{boss_from} {{subject:re subject:fw subject:fwd}}"
                ),
            }
        )
    return queries


def _search(client: Any, query: str) -> list[bytes]:
    escaped = query.replace("\\", "\\\\").replace('"', '\\"')
    status, response = client.uid("search", None, "X-GM-RAW", f'"{escaped}"')
    if status != "OK":
        raise RuntimeError(f"Gmail X-GM-RAW search failed for query: {query}")
    if not response or not isinstance(response[0], bytes):
        return []
    return response[0].split()


def _fetch_headers(client: Any, uids: list[bytes]) -> list[dict[str, Any]]:
    if not uids:
        return []
    parser = BytesHeaderParser(policy=policy.default)
    uid_pattern = re.compile(rb"\bUID\s+(\d+)\b", re.I)
    thread_pattern = re.compile(rb"\bX-GM-THRID\s+(\d+)\b", re.I)
    result: list[dict[str, Any]] = []
    for start in range(0, len(uids), 250):
        uid_set = b",".join(uids[start : start + 250]).decode("ascii")
        status, response = client.uid(
            "fetch",
            uid_set,
            (
                "(UID X-GM-THRID BODY.PEEK[HEADER.FIELDS "
                "(MESSAGE-ID IN-REPLY-TO REFERENCES FROM TO CC SUBJECT DATE "
                "AUTO-SUBMITTED PRECEDENCE LIST-UNSUBSCRIBE)])"
            ),
        )
        if status != "OK" or not isinstance(response, list):
            raise RuntimeError("Gmail header fetch failed")
        for item in response:
            if (
                not isinstance(item, tuple)
                or len(item) < 2
                or not isinstance(item[0], bytes)
                or not isinstance(item[1], bytes)
            ):
                continue
            uid_match = uid_pattern.search(item[0])
            if uid_match is None:
                continue
            thread_match = thread_pattern.search(item[0])
            message = parser.parsebytes(item[1], headersonly=True)
            sender = parseaddr(str(message.get("From") or ""))[1].casefold()
            recipients = [
                address.casefold()
                for _, address in getaddresses(
                    [
                        *message.get_all("To", []),
                        *message.get_all("Cc", []),
                    ]
                )
                if address
            ]
            result.append(
                {
                    "uid": int(uid_match.group(1)),
                    "gmail_thread_id": (
                        int(thread_match.group(1)) if thread_match else None
                    ),
                    "message_id": str(message.get("Message-ID") or ""),
                    "in_reply_to": str(message.get("In-Reply-To") or ""),
                    "references": str(message.get("References") or ""),
                    "sender": sender,
                    "recipients": recipients,
                    "subject": str(message.get("Subject") or "")[:500],
                    "date": str(message.get("Date") or "")[:200],
                    "auto_submitted": str(
                        message.get("Auto-Submitted") or ""
                    ).casefold(),
                    "precedence": str(message.get("Precedence") or "").casefold(),
                    "list_unsubscribe": bool(message.get("List-Unsubscribe")),
                }
            )
    return result


def _domain(address: str) -> str:
    return address.rpartition("@")[2].casefold()


def _classify_header(
    row: dict[str, Any],
    *,
    company_domains: set[str],
    boss_addresses: set[str],
) -> tuple[int, tuple[str, ...]]:
    sender = str(row["sender"])
    subject = str(row["subject"])
    lowered = subject.casefold()
    reasons: list[str] = []
    score = 0
    sender_is_company = _domain(sender) in company_domains
    external_recipients = [
        recipient
        for recipient in row["recipients"]
        if _domain(str(recipient)) not in company_domains
    ]
    if sender in boss_addresses:
        score += 4
        reasons.append("boss_sender")
    elif sender_is_company:
        score += 2
        reasons.append("company_sender")
    if sender_is_company and external_recipients:
        score += 3
        reasons.append("external_customer_recipient")
    elif sender_is_company:
        score -= 8
        reasons.append("internal_only")
    if row["in_reply_to"] or row["references"]:
        score += 4
        reasons.append("thread_headers")
    if any(term in lowered for term in BUSINESS_TERMS):
        score += 2
        reasons.append("business_subject")
    if re.match(r"^\s*re\s*:", subject, flags=re.I):
        score += 1
        reasons.append("reply_subject")
    if re.match(r"^\s*(?:fw|fwd)\s*:", subject, flags=re.I):
        score -= 3
        reasons.append("forward_subject")
    local_part = sender.partition("@")[0]
    if (
        local_part in {"noreply", "no-reply", "mailer-daemon"}
        or row["auto_submitted"] not in {"", "no"}
        or row["precedence"] in {"bulk", "junk", "list"}
        or row["list_unsubscribe"]
    ):
        score -= 8
        reasons.append("automated_or_bulk")
    if not row["message_id"]:
        score -= 3
        reasons.append("missing_message_id")
    return score, tuple(reasons)


def _fetch_body_sample(client: Any, uid: int) -> str:
    status, response = client.uid(
        "fetch",
        str(uid),
        "(UID BODY.PEEK[]<0.100000>)",
    )
    if status != "OK" or not isinstance(response, list):
        return ""
    payloads = [
        item[1]
        for item in response
        if isinstance(item, tuple)
        and len(item) >= 2
        and isinstance(item[1], bytes)
    ]
    if not payloads:
        return ""
    message = BytesParser(policy=policy.default).parsebytes(max(payloads, key=len))
    plain: list[str] = []
    html: list[str] = []
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except (KeyError, LookupError, TypeError, UnicodeError, ValueError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode(
                part.get_content_charset() or "utf-8",
                errors="replace",
            )
        if not isinstance(content, str):
            continue
        if part.get_content_type() == "text/plain":
            plain.append(content)
        else:
            html.append(content)
    text = "\n".join(plain).strip()
    if not text and html:
        text = BeautifulSoup("\n".join(html), "html.parser").get_text("\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:8000]


def main() -> None:
    args = parse_args()
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
    queries = build_queries(
        company_domains=company_domains,
        boss_addresses=boss_addresses,
    )
    settings = HistoryIMAPSettings()
    downloader = ReadOnlyHistoryDownloader(settings)
    client = None
    try:
        client = downloader._connect()
        query_results: dict[str, Any] = {}
        candidate_by_message_id: dict[str, dict[str, Any]] = {}
        for name, query in queries.items():
            uids = _search(client, query)
            inspected_uids = uids[-args.header_limit :]
            headers = _fetch_headers(client, inspected_uids)
            score_counts: Counter[str] = Counter()
            for row in headers:
                score, reasons = _classify_header(
                    row,
                    company_domains=company_domains,
                    boss_addresses=boss_addresses,
                )
                row["candidate_score"] = score
                row["candidate_reasons"] = reasons
                score_counts[
                    "high" if score >= 7 else "medium" if score >= 4 else "low"
                ] += 1
                message_key = row["message_id"] or f"uid:{row['uid']}"
                existing = candidate_by_message_id.get(message_key)
                if existing is None or score > existing["candidate_score"]:
                    candidate_by_message_id[message_key] = row
            query_results[name] = {
                "query": query,
                "matched_messages": len(uids),
                "headers_inspected": len(headers),
                "sample_quality_bands": dict(score_counts),
            }
        candidates = sorted(
            candidate_by_message_id.values(),
            key=lambda item: (item["candidate_score"], item["uid"]),
            reverse=True,
        )
        body_samples: list[dict[str, Any]] = []
        sampled_subjects: set[str] = set()
        for candidate in candidates:
            reasons = set(candidate["candidate_reasons"])
            if not {
                "company_sender",
                "boss_sender",
            } & reasons or "external_customer_recipient" not in reasons:
                continue
            if "forward_subject" in reasons or candidate["candidate_score"] < 8:
                continue
            subject_key = re.sub(
                r"^(?:(?:re|fw|fwd)\s*:\s*)+",
                "",
                str(candidate["subject"]),
                flags=re.I,
            ).casefold()
            if subject_key in sampled_subjects:
                continue
            sampled_subjects.add(subject_key)
            body_samples.append(
                {
                    "uid": candidate["uid"],
                    "sender": candidate["sender"],
                    "recipients": candidate["recipients"],
                    "subject": candidate["subject"],
                    "candidate_score": candidate["candidate_score"],
                    "body_sample": _fetch_body_sample(client, candidate["uid"]),
                }
            )
            if len(body_samples) >= args.body_sample_limit:
                break
        report = {
            "schema_version": "rag-mailbox-header-audit.v1",
            "read_only": True,
            "selected_folder": downloader._active_folder,
            "query_results": query_results,
            "unique_headers_inspected": len(candidate_by_message_id),
            "high_value_header_candidates": sum(
                1 for item in candidates if item["candidate_score"] >= 7
            ),
            "medium_value_header_candidates": sum(
                1 for item in candidates if 4 <= item["candidate_score"] < 7
            ),
            "top_candidates": candidates[:100],
            "body_samples": body_samples,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "output": str(args.output.resolve()),
                    "query_results": query_results,
                    "unique_headers_inspected": len(candidate_by_message_id),
                    "high_value_header_candidates": report[
                        "high_value_header_candidates"
                    ],
                    "medium_value_header_candidates": report[
                        "medium_value_header_candidates"
                    ],
                    "body_samples": len(body_samples),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        downloader._logout_quietly(client)


if __name__ == "__main__":
    main()
