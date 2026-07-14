from app.settings import Settings


def test_recipient_allowlist_accepts_comma_separated_environment_value(monkeypatch) -> None:
    monkeypatch.setenv("RECIPIENT_ALLOWLIST", "First@Example.com, second@example.com")

    settings = Settings(_env_file=None)

    assert settings.recipient_allowlist == ["first@example.com", "second@example.com"]


def test_imap_batch_size_is_configurable(monkeypatch) -> None:
    monkeypatch.setenv("IMAP_BATCH_SIZE", "250")

    settings = Settings(_env_file=None)

    assert settings.imap_batch_size == 250
