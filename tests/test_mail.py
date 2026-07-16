import imaplib
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime
from pathlib import Path

from app.mail import (
    GmailIMAPClient,
    _imap_mailbox_arg,
    build_message,
    has_thread_subject_prefix,
    normalized_subject,
    parse_mime,
)
from app.settings import Settings


def test_imap_mailbox_argument_quotes_spaces_and_escapes_special_characters() -> None:
    assert _imap_mailbox_arg("INBOX") == '"INBOX"'
    assert _imap_mailbox_arg("[Gmail]/Sent Mail") == '"[Gmail]/Sent Mail"'
    assert _imap_mailbox_arg('Folder "Q"') == '"Folder \\"Q\\""'


def test_imap_fetch_reconnects_after_remote_eof(monkeypatch) -> None:
    clients = []

    class FakeIMAP:
        def __init__(self, host, port, timeout):
            self.number = len(clients)
            clients.append(self)

        def login(self, address, password):
            return "OK", []

        def select(self, folder, readonly):
            assert folder == '"[Gmail]/Sent Mail"'
            return "OK", [b"2"]

        def response(self, name):
            return name, [b"77"]

        def uid(self, command, *args):
            if command == "search":
                return "OK", [b"1 2"]
            uid = args[0]
            if self.number == 0 and uid == b"1":
                raise imaplib.IMAP4.abort("socket error: EOF")
            return "OK", [(b"RFC822", b"message-" + uid)]

        def logout(self):
            return "BYE", []

    monkeypatch.setattr("app.mail.imaplib.IMAP4_SSL", FakeIMAP)
    settings = Settings(
        _env_file=None,
        gmail_address="sales@example.com",
        gmail_app_password="app-password",
    )

    uid_validity, highest_uid, messages = GmailIMAPClient(settings).fetch_after(
        0,
        folder="[Gmail]/Sent Mail",
        limit=100,
    )

    assert uid_validity == 77
    assert highest_uid == 2
    assert messages == [(1, b"message-1"), (2, b"message-2")]
    assert len(clients) == 2


def test_imap_fetch_returns_sequential_partial_batch_after_retries(monkeypatch) -> None:
    clients = []

    class FakeIMAP:
        def __init__(self, host, port, timeout):
            clients.append(self)

        def login(self, address, password):
            return "OK", []

        def select(self, folder, readonly):
            return "OK", [b"2"]

        def response(self, name):
            return name, [b"77"]

        def uid(self, command, *args):
            if command == "search":
                return "OK", [b"1 2"]
            uid = args[0]
            if uid == b"2":
                raise imaplib.IMAP4.abort("socket error: EOF")
            return "OK", [(b"RFC822", b"message-1")]

        def logout(self):
            raise imaplib.IMAP4.abort("socket already closed")

    monkeypatch.setattr("app.mail.imaplib.IMAP4_SSL", FakeIMAP)
    settings = Settings(
        _env_file=None,
        gmail_address="sales@example.com",
        gmail_app_password="app-password",
    )

    uid_validity, highest_uid, messages = GmailIMAPClient(settings).fetch_after(0, limit=100)

    assert uid_validity == 77
    assert highest_uid == 2
    assert messages == [(1, b"message-1")]
    assert len(clients) == 3


def test_imap_fetch_stops_after_download_budget(monkeypatch) -> None:
    class FakeIMAP:
        def __init__(self, host, port, timeout):
            pass

        def login(self, address, password):
            return "OK", []

        def select(self, folder, readonly):
            return "OK", [b"3"]

        def response(self, name):
            return name, [b"77"]

        def uid(self, command, *args):
            if command == "search":
                return "OK", [b"1 2 3"]
            uid = args[0]
            return "OK", [(b"RFC822", b"12345" + uid)]

        def logout(self):
            return "BYE", []

    monkeypatch.setattr("app.mail.imaplib.IMAP4_SSL", FakeIMAP)
    settings = Settings(
        _env_file=None,
        gmail_address="sales@example.com",
        gmail_app_password="app-password",
    )

    _, highest_uid, messages = GmailIMAPClient(settings).fetch_after(0, limit=100, max_bytes=10)

    assert highest_uid == 3
    assert messages == [(1, b"123451"), (2, b"123452")]


def test_imap_fetch_excludes_cursor_uid_from_reversed_star_range(monkeypatch) -> None:
    fetched_uids = []

    class FakeIMAP:
        def __init__(self, host, port, timeout):
            pass

        def login(self, address, password):
            return "OK", []

        def select(self, folder, readonly):
            return "OK", [b"1"]

        def response(self, name):
            return name, [b"77"]

        def uid(self, command, *args):
            if command == "search":
                assert args[-1] == "UID 2612:*"
                # Gmail may return the current maximum UID because IMAP ranges
                # can run in either direction when "*" is lower than 2612.
                return "OK", [b"2611"]
            fetched_uids.append(args[0])
            return "OK", [(b"RFC822", b"must-not-be-fetched")]

        def logout(self):
            return "BYE", []

    monkeypatch.setattr("app.mail.imaplib.IMAP4_SSL", FakeIMAP)
    settings = Settings(
        _env_file=None,
        gmail_address="sales@example.com",
        gmail_app_password="app-password",
    )

    uid_validity, highest_uid, messages = GmailIMAPClient(settings).fetch_after(
        2611,
        expected_uid_validity=77,
        limit=100,
    )

    assert uid_validity == 77
    assert highest_uid == 2611
    assert messages == []
    assert fetched_uids == []


def test_mime_prefers_plain_and_records_attachment() -> None:
    message = EmailMessage()
    message["From"] = "Buyer <buyer@example.com>"
    message["To"] = "sales@example.com"
    message["Subject"] = "Re: Quote"
    message["Message-ID"] = "<incoming@example.com>"
    message["In-Reply-To"] = "<original@example.com>"
    message["References"] = "<older@example.com> <original@example.com>"
    message.set_content("Please quote.\n\nOn Monday Someone wrote:\n> old content")
    message.add_alternative("<p>Please quote.</p>", subtype="html")
    message.add_attachment(b"demo", maintype="application", subtype="pdf", filename="po.pdf")
    parsed = parse_mime(message.as_bytes())
    assert parsed.body_text == "Please quote."
    assert parsed.in_reply_to == "<original@example.com>"
    assert parsed.references == ["<older@example.com>", "<original@example.com>"]
    assert parsed.attachments[0]["filename"] == "po.pdf"


def test_stable_message_id_and_thread_headers() -> None:
    first_id, first_raw = build_message(
        from_address="sales@example.com",
        recipient="buyer@example.com",
        subject="Re: Quote",
        text_body="Hello",
        html_body="<p>Hello</p>",
        stable_key="case:1:quote:2",
        in_reply_to="<incoming@example.com>",
        references=["<older@example.com>", "<incoming@example.com>"],
    )
    second_id, second_raw = build_message(
        from_address="sales@example.com",
        recipient="buyer@example.com",
        subject="Re: Quote",
        text_body="Hello",
        html_body="<p>Hello</p>",
        stable_key="case:1:quote:2",
        in_reply_to="<incoming@example.com>",
        references=["<older@example.com>", "<incoming@example.com>"],
    )
    assert first_id == second_id
    assert first_raw == second_raw
    assert "In-Reply-To: <incoming@example.com>" in first_raw


def test_build_message_embeds_signature_logo() -> None:
    root = Path(__file__).resolve().parents[1]
    signature_html = (root / "config" / "content" / "email_signature.html").read_text(encoding="utf-8")

    _, raw = build_message(
        from_address="sales@example.com",
        recipient="buyer@example.com",
        subject="Signature test",
        text_body="Signature test",
        html_body=signature_html,
        stable_key="signature-logo-test",
    )
    message = BytesParser(policy=policy.default).parsebytes(raw.encode("utf-8"))
    logo = next(part for part in message.walk() if part.get("Content-ID") == "<lanyachem-logo>")

    assert logo.get_content_type() == "image/png"
    assert logo.get_content_disposition() == "inline"
    assert (logo.get_payload(decode=True) or b"").startswith(b"\x89PNG\r\n\x1a\n")


def test_subject_normalization() -> None:
    assert normalized_subject("Re: FWD:  Product Quote ") == "product quote"
    assert normalized_subject("回复：Re: YAC-TES，600 kg") == "yac-tes，600 kg"
    assert has_thread_subject_prefix("回复：Re: YAC-TES，600 kg") is True
    assert has_thread_subject_prefix("YAC-TES，600 kg") is False


def test_mime_records_original_message_time_and_all_recipients() -> None:
    message = EmailMessage()
    message["From"] = "sales@example.com"
    message["To"] = "buyer@example.com"
    message["Cc"] = "Other <other@example.com>"
    message["Date"] = format_datetime(datetime(2025, 5, 6, 7, 8, tzinfo=UTC))
    message.set_content("Historical message")

    parsed = parse_mime(message.as_bytes())

    assert parsed.to_addresses == ["buyer@example.com", "other@example.com"]
    assert parsed.occurred_at == datetime(2025, 5, 6, 7, 8, tzinfo=UTC)


def test_mime_records_automatic_reply_headers() -> None:
    message = EmailMessage()
    message["From"] = "buyer@example.com"
    message["To"] = "sales@example.com"
    message["Subject"] = "Automatic reply"
    message["Auto-Submitted"] = "auto-replied"
    message["X-Auto-Response-Suppress"] = "All"
    message.set_content("I am currently out of the office.")

    parsed = parse_mime(message.as_bytes())

    assert parsed.header_metadata == {
        "auto-submitted": "auto-replied",
        "x-auto-response-suppress": "All",
    }
