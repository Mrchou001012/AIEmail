from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    database_url: str = "postgresql+asyncpg://sales_agent:sales_agent_demo_password@db:5432/sales_agent"
    demo_mode: bool = True
    ai_provider: Literal["stub", "anthropic"] = "stub"
    anthropic_model: str = "claude-opus-4-8"
    anthropic_api_key: str | None = None

    mail_transport: Literal["file", "smtp"] = "file"
    mail_from: str = "sales-agent@example.com"
    gmail_address: str | None = None
    gmail_app_password: str | None = None
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_sync_enabled: bool = False
    imap_folder: str = "INBOX"
    imap_sent_folder: str = "[Gmail]/Sent Mail"
    imap_poll_seconds: int = 60
    imap_batch_size: int = 50
    imap_daily_download_limit_mb: int = 1500
    imap_max_backoff_seconds: int = 1800
    job_lease_seconds: int = 900
    outbox_lease_seconds: int = 600
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    smtp_starttls: bool = False

    dingtalk_transport: Literal["log", "webhook"] = "log"
    dingtalk_webhook_url: str | None = None

    safe_mode: bool = True
    auto_send_enabled: bool = False
    recipient_allowlist: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["internal@example.com"])
    max_sends_per_hour: int = 5
    max_sends_per_day: int = 20
    min_send_interval_seconds: int = 120
    send_interval_jitter_seconds: int = 180
    gmail_transient_cooldown_seconds: int = 600
    gmail_daily_cooldown_seconds: int = 86400

    admin_username: str = "admin"
    admin_password: str = "change-me-locally"
    public_base_url: str = "http://localhost:8000"
    runtime_dir: Path = Path("runtime")
    content_dir: Path = Path("config/content")

    intent_confidence_threshold: float = 0.80
    product_confidence_threshold: float = 0.85
    numeric_confidence_threshold: float = 0.90

    @field_validator("ai_provider", "mail_transport", "dingtalk_transport", mode="before")
    @classmethod
    def normalize_mode(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("recipient_allowlist", mode="before")
    @classmethod
    def split_allowlist(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip().lower() for part in value.split(",") if part.strip()]
        return value

    @field_validator("gmail_app_password", mode="before")
    @classmethod
    def normalize_google_app_password(cls, value: object) -> object:
        if isinstance(value, str):
            return value.replace(" ", "") or None
        return value

    @field_validator("anthropic_model")
    @classmethod
    def exact_default_model(cls, value: str) -> str:
        if not value.startswith("claude-"):
            raise ValueError("ANTHROPIC_MODEL must be an exact Claude model identifier")
        return value

    @field_validator("imap_batch_size")
    @classmethod
    def valid_imap_batch_size(cls, value: int) -> int:
        if not 1 <= value <= 1000:
            raise ValueError("IMAP_BATCH_SIZE must be between 1 and 1000")
        return value

    @field_validator(
        "imap_poll_seconds",
        "imap_daily_download_limit_mb",
        "imap_max_backoff_seconds",
        "max_sends_per_hour",
        "max_sends_per_day",
        "gmail_transient_cooldown_seconds",
        "gmail_daily_cooldown_seconds",
    )
    @classmethod
    def positive_limit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("rate and bandwidth limits must be positive")
        return value

    @field_validator("min_send_interval_seconds", "send_interval_jitter_seconds")
    @classmethod
    def nonnegative_interval(cls, value: int) -> int:
        if value < 0:
            raise ValueError("send intervals cannot be negative")
        return value

    def ensure_runtime(self) -> None:
        (self.runtime_dir / "demo_outbox").mkdir(parents=True, exist_ok=True)
        (self.runtime_dir / "inbound_archive").mkdir(parents=True, exist_ok=True)
        (self.runtime_dir / "mail_archive").mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_runtime()
    return settings
