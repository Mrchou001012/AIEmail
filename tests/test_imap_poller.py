from app.imap_poller import _imap_backoff_seconds
from app.settings import Settings


def test_imap_backoff_grows_and_honors_gmail_throttling_floor() -> None:
    settings = Settings(
        _env_file=None,
        imap_poll_seconds=60,
        imap_max_backoff_seconds=1800,
    )

    assert _imap_backoff_seconds(settings, 1, RuntimeError("socket EOF")) == 120
    assert _imap_backoff_seconds(settings, 2, RuntimeError("too many simultaneous connections")) == 600
    assert _imap_backoff_seconds(settings, 8, RuntimeError("rate limit")) == 1800
