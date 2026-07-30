from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage
from pathlib import Path

from app.rag_history import (
    ConversationPair,
    build_conversation_pairs,
    clean_learning_text,
    load_historical_emails,
    split_conversation_pairs,
)

MAILBOX = "sales@company.test"
CUSTOMER = "buyer@customer.test"


def _write_message(
    path: Path,
    *,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    message_id: str,
    occurred_at: datetime,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    auto_submitted: str | None = None,
) -> None:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message["Date"] = occurred_at
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = " ".join(references)
    if auto_submitted:
        message["Auto-Submitted"] = auto_submitted
    message.set_content(body)
    path.write_bytes(message.as_bytes(policy=policy.SMTP))


def _pair(index: int, thread_id: str, occurred_at: datetime) -> ConversationPair:
    return ConversationPair(
        pair_id=f"pair-{index}",
        thread_id=thread_id,
        customer_key=f"customer-{index}",
        intent="QUOTE_REQUEST",
        risk_flags=(),
        subject=f"Subject {index}",
        request_at=occurred_at - timedelta(hours=2),
        response_at=occurred_at,
        response_delay_hours=2,
        request_text="Please quote this product and confirm the lead time.",
        response_text="Thank you. We will prepare the quotation for your review.",
        response_sender=MAILBOX,
        boss_anchor=False,
        request_attachment_names=(),
        response_attachment_names=(),
        request_source_file=f"in-{index}.eml",
        response_source_file=f"out-{index}.eml",
        direct_reply=True,
        quality_score=95,
        quality_reasons=("direct_message_id_reply",),
    )


def test_clean_learning_text_removes_signature_and_disclaimer() -> None:
    value = (
        "Please find our quotation attached.\n\n"
        "Best regards,\nChen Ping\nLanya Chem\n\n"
        "Confidentiality Notice: this message is private."
    )
    assert clean_learning_text(value) == "Please find our quotation attached."


def test_clean_learning_text_removes_ampersand_signature() -> None:
    value = (
        "Please quote 500 kg and confirm the lead time.\n\n"
        "Thanks & Regards,\nRadhika\nMobile: 9999999999"
    )
    assert clean_learning_text(value) == (
        "Please quote 500 kg and confirm the lead time."
    )


def test_clean_learning_text_removes_leading_security_banner() -> None:
    value = (
        "\n\nCAUTION: This email is originated from outside your organization. "
        "Exercise caution when opening attachments. Dear Arvind Sir,\n\n"
        "Good Morning!\n\n"
        "Thanks for your enquiry!"
    )

    assert clean_learning_text(value) == (
        "Dear Arvind Sir,\n\nGood Morning!\n\nThanks for your enquiry!"
    )


def test_clean_learning_text_removes_multiple_security_banner_lines() -> None:
    value = (
        'WARNING: The sender could not be validated and may not match the "From" field.\n'
        "CAUTION: External email. Do not click links or open attachments.\n\n"
        "Dear Customer,\n\n"
        "Please find our response below."
    )

    assert clean_learning_text(value) == (
        "Dear Customer,\n\nPlease find our response below."
    )


def test_load_and_pair_direct_customer_reply(tmp_path: Path) -> None:
    start = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)
    _write_message(
        tmp_path / "inbound.eml",
        sender=CUSTOMER,
        recipient=MAILBOX,
        subject="RFQ - Plasticizer",
        body="Please quote 1,000 kg and confirm your delivery time.",
        message_id="<request@customer.test>",
        occurred_at=start,
    )
    _write_message(
        tmp_path / "outbound.eml",
        sender=MAILBOX,
        recipient=CUSTOMER,
        subject="Re: RFQ - Plasticizer",
        body="Thank you for your inquiry. We will send our quotation today.",
        message_id="<reply@company.test>",
        occurred_at=start + timedelta(hours=3),
        in_reply_to="<request@customer.test>",
        references=["<request@customer.test>"],
    )
    records, errors = load_historical_emails(
        tmp_path,
        mailbox_addresses={MAILBOX},
        company_domains={"company.test", "company-alt.test"},
    )
    result = build_conversation_pairs(records, boss_addresses={MAILBOX})

    assert errors == []
    assert len(result.pairs) == 1
    assert result.pairs[0].direct_reply is True
    assert result.pairs[0].boss_anchor is True
    assert result.pairs[0].response_delay_hours == 3
    assert "PRICE_OR_QUOTE" in result.pairs[0].risk_flags


def test_automated_reply_is_not_paired(tmp_path: Path) -> None:
    start = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)
    _write_message(
        tmp_path / "outbound.eml",
        sender=MAILBOX,
        recipient=CUSTOMER,
        subject="Product introduction",
        body="Please let us know if this product is relevant to you.",
        message_id="<intro@company.test>",
        occurred_at=start,
    )
    _write_message(
        tmp_path / "automatic.eml",
        sender=CUSTOMER,
        recipient=MAILBOX,
        subject="Automatic reply: Product introduction",
        body="I am currently out of the office and will return next week.",
        message_id="<automatic@customer.test>",
        occurred_at=start + timedelta(minutes=2),
        in_reply_to="<intro@company.test>",
        auto_submitted="auto-replied",
    )
    records, errors = load_historical_emails(
        tmp_path,
        mailbox_addresses={MAILBOX},
        company_domains={"company.test"},
    )
    result = build_conversation_pairs(records)

    assert errors == []
    automatic = next(record for record in records if record.direction == "INBOUND")
    assert automatic.is_automated is True
    assert "automated_reply" in automatic.exclusion_reasons
    assert result.pairs == ()


def test_workspace_route_archive_recovers_pair_from_quoted_history(
    tmp_path: Path,
) -> None:
    current_at = datetime(2026, 7, 29, 4, 36, tzinfo=UTC)
    quoted_chain = (
        "Thank you. One drum is required urgently.\n\n"
        "From: Sales <sales@company.test>\n"
        "To: Buyer <buyer@customer.test>\n"
        "Date: Tue, 28 Jul 2026 10:00:00 +0000\n"
        "Subject: Re: Request for Quote\n"
        "Thank you for your inquiry. Our price is USD 4.50 per kg.\n\n"
        "From: Buyer <buyer@customer.test>\n"
        "To: Sales <sales@company.test>\n"
        "Date: Tue, 28 Jul 2026 09:00:00 +0000\n"
        "Subject: Request for Quote\n"
        "Please quote 500 kg and confirm the delivery time."
    )
    _write_message(
        tmp_path / "routed-reply.eml",
        sender=CUSTOMER,
        recipient="sales@company.test",
        subject="Re: Request for Quote",
        body=quoted_chain,
        message_id="<routed-reply@customer.test>",
        occurred_at=current_at,
    )
    records, errors = load_historical_emails(
        tmp_path,
        mailbox_addresses={MAILBOX},
        company_domains={"company.test", "company-alt.test"},
        workspace_route_archive=True,
        include_quoted_history=True,
    )
    result = build_conversation_pairs(records)

    assert errors == []
    assert len([record for record in records if record.source_kind == "QUOTED_TURN"]) == 2
    assert len(result.pairs) == 1
    assert result.pairs[0].request_text.startswith("Please quote 500 kg")
    assert result.pairs[0].response_text.startswith("Thank you for your inquiry")
    assert result.pairs[0].direct_reply is False


def test_workspace_route_archive_infers_missing_quoted_recipient(
    tmp_path: Path,
) -> None:
    current_at = datetime(2026, 7, 29, 4, 36, tzinfo=UTC)
    quoted_chain = (
        "Thank you for your inquiry. Our price is USD 4.50 per kg.\n\n"
        "From: Buyer <buyer@customer.test>\n"
        "Date: Tue, 28 Jul 2026 09:00:00 +0000\n"
        "Subject: Request for Quote\n"
        "Please quote 500 kg and confirm the delivery time."
    )
    _write_message(
        tmp_path / "routed-outbound.eml",
        sender="sales@company.test",
        recipient=CUSTOMER,
        subject="Re: Request for Quote",
        body=quoted_chain,
        message_id="<routed-outbound@company.test>",
        occurred_at=current_at,
    )
    records, errors = load_historical_emails(
        tmp_path,
        mailbox_addresses={MAILBOX},
        company_domains={"company.test", "company-alt.test"},
        workspace_route_archive=True,
        include_quoted_history=True,
    )
    result = build_conversation_pairs(records)

    assert errors == []
    quoted = next(record for record in records if record.source_kind == "QUOTED_TURN")
    assert quoted.recipients == ()
    assert quoted.direction == "INBOUND"
    assert quoted.customer_key is not None
    assert quoted.is_internal is False
    assert quoted.exclusion_reasons == ()
    assert len(result.pairs) == 1


def test_split_is_chronological_and_thread_exclusive() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    pairs = [
        _pair(index, f"thread-{index}", start + timedelta(days=index))
        for index in range(300)
    ]
    result = split_conversation_pairs(pairs)

    assert len(result.knowledge_base) == 200
    assert len(result.development) == 50
    assert len(result.test_holdout) == 50
    assert max(pair.response_at for pair in result.knowledge_base) < min(
        pair.response_at for pair in result.development
    )
    assert max(pair.response_at for pair in result.development) < min(
        pair.response_at for pair in result.test_holdout
    )
    kb_threads = {pair.thread_id for pair in result.knowledge_base}
    dev_threads = {pair.thread_id for pair in result.development}
    test_threads = {pair.thread_id for pair in result.test_holdout}
    assert not kb_threads & dev_threads
    assert not kb_threads & test_threads
    assert not dev_threads & test_threads


def test_split_packs_large_threads_close_to_targets() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    pairs = [
        _pair(index, "large-thread", start + timedelta(minutes=index))
        for index in range(44)
    ]
    pairs.extend(
        _pair(
            1000 + index,
            f"thread-{index}",
            start + timedelta(days=index + 1),
        )
        for index in range(273)
    )

    result = split_conversation_pairs(pairs)

    assert len(result.knowledge_base) == 200
    assert len(result.development) == 50
    assert len(result.test_holdout) == 67
    kb_threads = {pair.thread_id for pair in result.knowledge_base}
    dev_threads = {pair.thread_id for pair in result.development}
    test_threads = {pair.thread_id for pair in result.test_holdout}
    assert not kb_threads & dev_threads
    assert not kb_threads & test_threads
    assert not dev_threads & test_threads
