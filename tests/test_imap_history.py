from __future__ import annotations

from email import policy
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from app.imap_history import (
    HistoryIMAPSettings,
    ReadOnlyHistoryDownloader,
    has_body_payload,
)


def _raw_message(message_id: str, body: str | None) -> bytes:
    message = EmailMessage()
    message["From"] = "buyer@example.com"
    message["To"] = "sales@company.test"
    message["Subject"] = "RFQ"
    message["Message-ID"] = message_id
    if body is not None:
        message.set_content(body)
    return message.as_bytes(policy=policy.SMTP)


class FakeIMAP:
    def __init__(self, *args: Any, **kwargs: Any):
        self.commands: list[tuple[str, tuple[Any, ...]]] = []
        self.readonly: bool | None = None
        self.logged_out = False
        self.full_message = _raw_message("<missing@example.com>", "Complete body")

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        assert user == "sales@company.test"
        assert password == "test-app-password"
        return "OK", [b"authenticated"]

    def select(
        self, mailbox: str, readonly: bool = False
    ) -> tuple[str, list[bytes]]:
        self.readonly = readonly
        assert mailbox == '"[Gmail]/All Mail"'
        return "OK", [b"2"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self.commands.append((command, args))
        if command.casefold() == "search":
            return "OK", [b"41 42"]
        if "HEADER.FIELDS" in str(args[-1]):
            return (
                "OK",
                [
                    (
                        b"1 (UID 41 BODY[HEADER.FIELDS (MESSAGE-ID)] {37}",
                        b"Message-ID: <other@example.com>\r\n\r\n",
                    ),
                    b")",
                    (
                        b"2 (UID 42 BODY[HEADER.FIELDS (MESSAGE-ID)] {39}",
                        b"Message-ID: <missing@example.com>\r\n\r\n",
                    ),
                    b")",
                ],
            )
        assert args[-1] == "(UID BODY.PEEK[])"
        return "OK", [(b"2 (UID 42 BODY[] {100}", self.full_message), b")"]

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return "BYE", [b"logout"]


def test_has_body_payload_distinguishes_header_only_export() -> None:
    header_only = _raw_message("<empty@example.com>", None)
    complete = _raw_message("<complete@example.com>", "Hello")

    assert has_body_payload(header_only) is False
    assert has_body_payload(complete) is True


def test_supplement_is_read_only_and_uses_body_peek(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    output_dir = tmp_path / "complete"
    raw_dir.mkdir()
    (raw_dir / "missing.eml").write_bytes(
        _raw_message("<missing@example.com>", None)
    )
    (raw_dir / "complete.eml").write_bytes(
        _raw_message("<complete@example.com>", "Already present")
    )

    created_clients: list[FakeIMAP] = []

    def factory(*args: Any, **kwargs: Any) -> FakeIMAP:
        client = FakeIMAP(*args, **kwargs)
        created_clients.append(client)
        return client

    settings = HistoryIMAPSettings(
        address="sales@company.test",
        app_password="test-app-password",
    )
    report = ReadOnlyHistoryDownloader(
        settings,
        client_factory=factory,
    ).supplement(
        raw_dir=raw_dir,
        output_dir=output_dir,
        remote_index_path=tmp_path / "remote-index.json",
        max_messages=1,
    )

    client = created_clients[0]
    assert client.readonly is True
    assert client.logged_out is True
    assert all(command.casefold() in {"search", "fetch"} for command, _ in client.commands)
    assert any(
        args[-1] == "(UID BODY.PEEK[])" for command, args in client.commands
        if command.casefold() == "fetch"
    )
    assert report.missing_body_local == 1
    assert report.downloaded_messages == 1
    assert report.pending_after_run == 0
    assert report.output_complete_files == 2
    output_messages = list(output_dir.rglob("*.eml"))
    assert len(output_messages) == 2
    assert any(
        path.read_bytes() == (raw_dir / "complete.eml").read_bytes()
        for path in output_messages
    )
    assert all(has_body_payload(path.read_bytes()) for path in output_messages)
    assert (output_dir / "_source_map.jsonl").is_file()
