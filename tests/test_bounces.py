from app.bounces import BounceType, classify_bounce, classify_smtp_failure


def dsn_raw(*, status: str, diagnostic: str, recipient: str = "buyer@example.com") -> bytes:
    return f"""From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>
To: sales@example.com
Subject: Delivery Status Notification (Failure)
Message-ID: <bounce-1@googlemail.com>
MIME-Version: 1.0
Content-Type: multipart/report; report-type=delivery-status; boundary=dsn

--dsn
Content-Type: text/plain; charset=utf-8

Your message wasn't delivered to {recipient}. {diagnostic}
--dsn
Content-Type: message/delivery-status

Final-Recipient: rfc822; {recipient}
Action: failed
Status: {status}
Diagnostic-Code: smtp; {diagnostic}

--dsn
Content-Type: message/rfc822

Message-ID: <original-1@example.com>
From: sales@example.com
To: {recipient}
Subject: Quote

Hello
--dsn--
""".replace("\n", "\r\n").encode()


def test_structured_invalid_recipient_dsn_is_hard_bounce() -> None:
    raw = dsn_raw(status="5.1.1", diagnostic="550 5.1.1 The email account does not exist")
    result = classify_bounce(
        raw,
        subject="Delivery Status Notification (Failure)",
        body="Your message wasn't delivered.",
        sender="mailer-daemon@googlemail.com",
    )

    assert result.is_bounce is True
    assert result.bounce_type == BounceType.HARD
    assert result.permanent is True
    assert result.recipient == "buyer@example.com"
    assert result.status_code == "5.1.1"
    assert result.original_message_id == "<original-1@example.com>"


def test_mailbox_full_and_policy_failures_are_not_permanent() -> None:
    soft = classify_bounce(
        dsn_raw(status="5.2.2", diagnostic="Mailbox is full"),
        subject="Undeliverable",
        body="Mailbox is full",
        sender="postmaster@example.net",
    )
    policy = classify_bounce(
        dsn_raw(status="5.7.1", diagnostic="Message blocked by policy"),
        subject="Undeliverable",
        body="Message blocked by policy",
        sender="postmaster@example.net",
    )

    assert soft.bounce_type == BounceType.SOFT
    assert soft.permanent is False
    assert policy.bounce_type == BounceType.POLICY
    assert policy.permanent is False


def test_nxdomain_evidence_overrides_temporary_status_and_try_again_text() -> None:
    diagnostic = (
        "DNS Error: DNS type 'mx' lookup of aptuitlaurus.com responded with code NXDOMAIN. "
        "Domain name not found: aptuitlaurus.com. Check the address and try again."
    )
    result = classify_bounce(
        dsn_raw(status="4.4.1", diagnostic=diagnostic),
        subject="Delivery Status Notification (Failure)",
        body=f"Address not found. {diagnostic}",
        sender="mailer-daemon@googlemail.com",
    )

    assert result.is_bounce is True
    assert result.bounce_type == BounceType.HARD
    assert result.permanent is True
    assert classify_smtp_failure(450, f"450 4.4.1 {diagnostic}") == BounceType.HARD


def test_customer_text_about_delivery_failure_is_not_trusted_as_a_bounce() -> None:
    raw = b"From: buyer@example.com\r\nTo: sales@example.com\r\nSubject: Question\r\n\r\nOur own delivery failed yesterday."
    result = classify_bounce(
        raw,
        subject="Question",
        body="Our own delivery failed yesterday.",
        sender="buyer@example.com",
    )

    assert result.is_bounce is False


def test_smtp_recipient_failure_classification_is_conservative() -> None:
    assert classify_smtp_failure(550, "550 5.1.1 User unknown") == BounceType.HARD
    assert classify_smtp_failure(550, "550 5.2.2 Mailbox full") == BounceType.SOFT
    assert classify_smtp_failure(550, "550 5.7.1 Blocked by policy") == BounceType.POLICY
    assert classify_smtp_failure(550, "550 Requested action not taken") == BounceType.UNKNOWN
