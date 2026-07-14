import imaplib
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime

from app.mail import GmailIMAPClient, _imap_mailbox_arg, build_message, normalized_subject, parse_mime
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


def test_subject_normalization() -> None:
    assert normalized_subject("Re: FWD:  Product Quote ") == "product quote"


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
