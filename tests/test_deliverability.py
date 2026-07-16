import dns.exception
import dns.resolver

from app.deliverability import MXStatus, lookup_mx, validate_address_format


def test_address_format_normalizes_domain_and_rejects_bad_syntax() -> None:
    valid = validate_address_format(" Buyer@Example.COM ")
    invalid = validate_address_format("not-an-address")

    assert valid.valid is True
    assert valid.normalized == "buyer@example.com"
    assert valid.domain == "example.com"
    assert invalid.valid is False
    assert invalid.domain is None


def test_mx_lookup_returns_sorted_records(monkeypatch) -> None:
    class Exchange:
        def __init__(self, value: str) -> None:
            self.value = value

        def __str__(self) -> str:
            return self.value

    class Answer:
        def __init__(self, preference: int, exchange: str) -> None:
            self.preference = preference
            self.exchange = Exchange(exchange)

    monkeypatch.setattr(
        dns.resolver.Resolver,
        "resolve",
        lambda *args, **kwargs: [Answer(20, "mx2.example.com."), Answer(10, "mx1.example.com.")],
    )

    result = lookup_mx("Example.COM")

    assert result.status == MXStatus.VALID
    assert result.records == ("10 mx1.example.com", "20 mx2.example.com")


def test_mx_lookup_distinguishes_permanent_and_temporary_dns_failures(monkeypatch) -> None:
    monkeypatch.setattr(
        dns.resolver.Resolver,
        "resolve",
        lambda *args, **kwargs: (_ for _ in ()).throw(dns.resolver.NXDOMAIN()),
    )
    assert lookup_mx("missing.example").status == MXStatus.NO_DOMAIN

    monkeypatch.setattr(
        dns.resolver.Resolver,
        "resolve",
        lambda *args, **kwargs: (_ for _ in ()).throw(dns.exception.Timeout()),
    )
    assert lookup_mx("slow.example").status == MXStatus.TEMPORARY_ERROR


def test_null_mx_is_not_deliverable(monkeypatch) -> None:
    class NullExchange:
        def __str__(self) -> str:
            return "."

    class Answer:
        preference = 0
        exchange = NullExchange()

    monkeypatch.setattr(dns.resolver.Resolver, "resolve", lambda *args, **kwargs: [Answer()])

    result = lookup_mx("no-mail.example")

    assert result.status == MXStatus.NULL_MX
    assert result.deliverable is False
