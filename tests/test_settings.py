from app.settings import Settings


def test_recipient_allowlist_accepts_comma_separated_environment_value(monkeypatch) -> None:
    monkeypatch.setenv("RECIPIENT_ALLOWLIST", "First@Example.com, second@example.com")

    settings = Settings(_env_file=None)

    assert settings.recipient_allowlist == ["first@example.com", "second@example.com"]


def test_imap_batch_size_is_configurable(monkeypatch) -> None:
    monkeypatch.setenv("IMAP_BATCH_SIZE", "250")

    settings = Settings(_env_file=None)

    assert settings.imap_batch_size == 250


def test_company_research_defaults_to_disabled_observation_mode() -> None:
    settings = Settings(_env_file=None)

    assert settings.company_research_enabled is False
    assert settings.company_research_auto_send_enabled is False
    assert settings.company_research_cache_days == 90
    assert settings.company_research_max_searches == 2


def test_customer_reply_workflows_default_to_review_only() -> None:
    settings = Settings(_env_file=None)

    assert settings.coa_auto_send_enabled is False
    assert settings.product_list_auto_send_enabled is False
    assert settings.quote_auto_send_enabled is False


def test_inbound_internal_domains_have_lanya_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.inbound_disposition_internal_domains == [
        "lanyachem.com",
        "lanyachemindia.com",
        "lanyachem.de",
    ]


def test_inbound_internal_domains_accept_comma_separated_environment_value(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "INBOUND_DISPOSITION_INTERNAL_DOMAINS",
        " LANYACHEM.COM, lanyachem.de, lanyachem.com ",
    )

    settings = Settings(_env_file=None)

    assert settings.inbound_disposition_internal_domains == [
        "lanyachem.com",
        "lanyachem.de",
    ]
