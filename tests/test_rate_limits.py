import smtplib

from app.services import _send_interval_seconds, _smtp_rate_limit_cooldown_seconds
from app.settings import Settings


def test_send_interval_is_stable_and_bounded() -> None:
    settings = Settings(
        _env_file=None,
        min_send_interval_seconds=120,
        send_interval_jitter_seconds=180,
    )

    first = _send_interval_seconds(settings, "<stable@example.com>")
    second = _send_interval_seconds(settings, "<stable@example.com>")

    assert first == second
    assert 120 <= first <= 300


def test_gmail_smtp_errors_choose_mailbox_cooldown() -> None:
    settings = Settings(
        _env_file=None,
        gmail_transient_cooldown_seconds=600,
        gmail_daily_cooldown_seconds=86400,
    )

    assert (
        _smtp_rate_limit_cooldown_seconds(
            smtplib.SMTPDataError(421, b"4.7.28 Rate limit exceeded"),
            settings,
        )
        == 600
    )
    assert (
        _smtp_rate_limit_cooldown_seconds(
            smtplib.SMTPDataError(550, b"5.4.5 Daily user sending limit exceeded"),
            settings,
        )
        == 86400
    )
    assert _smtp_rate_limit_cooldown_seconds(smtplib.SMTPDataError(550, b"Quota exceeded"), settings) == 86400
    assert _smtp_rate_limit_cooldown_seconds(smtplib.SMTPDataError(550, b"Mailbox unavailable"), settings) is None
