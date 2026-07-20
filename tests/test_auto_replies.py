from app.auto_replies import AutomatedReplyType, classify_automated_reply


def test_out_of_office_records_return_hint_and_backup_contact() -> None:
    result = classify_automated_reply(
        subject="Automatic reply: quotation",
        body=(
            "I am currently out of the office and will return on 22 July 2026. "
            "For urgent matters please contact backup@example.com."
        ),
        headers={"auto-submitted": "auto-replied"},
        sender="buyer@example.com",
    )

    assert result.reply_type == AutomatedReplyType.OUT_OF_OFFICE
    assert result.return_hint == "22 July 2026"
    assert result.replacement_emails == ("backup@example.com",)
    assert "header:auto-submitted" in result.detected_by


def test_departed_contact_takes_priority_over_generic_auto_reply() -> None:
    result = classify_automated_reply(
        subject="Automatic reply",
        body="I no longer work with Example Ltd. Please contact sales@example.com.",
        headers={"auto-submitted": "auto-replied"},
        sender="former@example.com",
    )

    assert result.reply_type == AutomatedReplyType.DEPARTED
    assert result.replacement_emails == ("sales@example.com",)


def test_contact_change_without_departure_requires_review() -> None:
    result = classify_automated_reply(
        subject="New point of contact",
        body="Going forward, please contact newbuyer@example.com for all quotations.",
        sender="buyer@example.com",
    )

    assert result.reply_type == AutomatedReplyType.CONTACT_CHANGE


def test_generic_auto_submitted_message_is_recorded() -> None:
    result = classify_automated_reply(
        subject="Message received",
        body="Thank you. Your request has been received.",
        headers={"auto-submitted": "auto-generated"},
    )

    assert result.reply_type == AutomatedReplyType.GENERIC_AUTOREPLY


def test_google_account_security_alert_is_a_system_notification() -> None:
    result = classify_automated_reply(
        subject="Security alert",
        body="We noticed a new sign-in to your Google Account on an Apple iPhone 17 device.",
        sender="no-reply@accounts.google.com",
    )

    assert result.reply_type == AutomatedReplyType.SYSTEM_NOTIFICATION
    assert result.confidence == 1.0
    assert result.detected_by == (
        "sender:system-notification:no-reply@accounts.google.com",
    )


def test_google_account_rule_does_not_blanket_filter_other_senders() -> None:
    for sender in (
        "buyer@google.com",
        "no-reply@accounts.google.com.evil.example",
        "buyer@example.com",
    ):
        result = classify_automated_reply(
            subject="Security alert",
            body="Please quote YAC-TES 600 kg. This order is security critical.",
            sender=sender,
        )

        assert result.reply_type is None


def test_normal_customer_message_is_not_treated_as_automatic() -> None:
    result = classify_automated_reply(
        subject="Re: quotation",
        body="I will be away next month, but please send the revised quotation today.",
    )

    assert result.reply_type is None
    assert not result.is_automated
