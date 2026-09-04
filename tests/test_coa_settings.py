from app.settings import Settings


def test_coa_catalog_background_scan_defaults_to_enabled(monkeypatch) -> None:
    monkeypatch.delenv("COA_CATALOG_SCAN_ENABLED", raising=False)

    settings = Settings(_env_file=None)

    assert settings.coa_catalog_scan_enabled is True


def test_coa_catalog_background_scan_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("COA_CATALOG_SCAN_ENABLED", "false")

    settings = Settings(_env_file=None)

    assert settings.coa_catalog_scan_enabled is False
