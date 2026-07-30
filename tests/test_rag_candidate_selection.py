from __future__ import annotations

from scripts.audit_imap_rag_candidates import _classify_header, build_queries
from scripts.select_imap_rag_candidates import (
    build_company_query,
    build_sender_query,
)


def test_candidate_queries_use_supplied_domains_and_addresses() -> None:
    queries = build_queries(
        company_domains={"example.com", "example.org"},
        boss_addresses={"sales-director@example.com"},
    )

    assert "from:(@example.com)" in queries["company_business_outbound"]
    assert "to:(@example.org)" in queries["external_business_inbound"]
    assert (
        "from:sales-director@example.com"
        in queries["boss_all_outbound"]
    )
    assert "lanyachem" not in str(queries)


def test_header_classification_marks_configured_boss_sender() -> None:
    score, reasons = _classify_header(
        {
            "sender": "sales-director@example.com",
            "recipients": ["buyer@customer.test"],
            "subject": "Re: Quotation request",
            "in_reply_to": "<previous@example.test>",
            "references": "",
            "auto_submitted": "",
            "precedence": "",
            "list_unsubscribe": False,
            "message_id": "<reply@example.com>",
        },
        company_domains={"example.com"},
        boss_addresses={"sales-director@example.com"},
    )

    assert score >= 8
    assert "boss_sender" in reasons
    assert "external_customer_recipient" in reasons


def test_selection_queries_are_generic() -> None:
    assert build_company_query({"example.com"}).startswith(
        "{from:(@example.com)}"
    )
    assert build_sender_query(
        {"director@example.com", "sales@example.com"}
    ) == "{from:director@example.com from:sales@example.com}"
    assert build_sender_query(set()) == ""
