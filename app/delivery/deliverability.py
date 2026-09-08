from dataclasses import dataclass
from enum import StrEnum

import dns.exception
import dns.resolver
from email_validator import EmailNotValidError, validate_email


class MXStatus(StrEnum):
    VALID = "VALID"
    NO_DOMAIN = "NO_DOMAIN"
    NO_MX = "NO_MX"
    NULL_MX = "NULL_MX"
    TEMPORARY_ERROR = "TEMPORARY_ERROR"
    UNCHECKED = "UNCHECKED"


@dataclass(frozen=True)
class AddressFormatResult:
    valid: bool
    normalized: str
    domain: str | None
    error: str | None = None


@dataclass(frozen=True)
class MXResult:
    status: MXStatus
    domain: str
    records: tuple[str, ...] = ()
    error: str | None = None

    @property
    def deliverable(self) -> bool:
        return self.status in {MXStatus.VALID, MXStatus.UNCHECKED}

    @property
    def temporary(self) -> bool:
        return self.status == MXStatus.TEMPORARY_ERROR


def validate_address_format(address: str) -> AddressFormatResult:
    candidate = address.strip().casefold()
    try:
        result = validate_email(
            candidate,
            check_deliverability=False,
            allow_smtputf8=False,
            test_environment=True,
        )
    except EmailNotValidError as exc:
        return AddressFormatResult(False, candidate[:320], None, str(exc)[:1000])
    normalized = result.normalized.casefold()
    return AddressFormatResult(True, normalized, result.domain.casefold())


def lookup_mx(domain: str, *, timeout_seconds: int = 5) -> MXResult:
    normalized_domain = domain.strip().rstrip(".").casefold()
    resolver = dns.resolver.Resolver()
    resolver.timeout = timeout_seconds
    resolver.lifetime = timeout_seconds
    try:
        answers = resolver.resolve(normalized_domain, "MX", search=False, lifetime=timeout_seconds)
    except dns.resolver.NXDOMAIN:
        return MXResult(MXStatus.NO_DOMAIN, normalized_domain, error="domain does not exist")
    except dns.resolver.NoAnswer:
        return MXResult(MXStatus.NO_MX, normalized_domain, error="domain has no MX record")
    except (dns.exception.Timeout, dns.resolver.LifetimeTimeout) as exc:
        return MXResult(MXStatus.TEMPORARY_ERROR, normalized_domain, error=f"DNS timeout: {exc}"[:1000])
    except dns.resolver.NoNameservers as exc:
        return MXResult(MXStatus.TEMPORARY_ERROR, normalized_domain, error=f"DNS nameserver failure: {exc}"[:1000])
    except dns.exception.DNSException as exc:
        return MXResult(MXStatus.TEMPORARY_ERROR, normalized_domain, error=f"DNS failure: {exc}"[:1000])

    records = tuple(
        sorted(
            f"{int(answer.preference)} {str(answer.exchange).rstrip('.').casefold()}"
            for answer in answers
        )
    )
    if not records:
        return MXResult(MXStatus.NO_MX, normalized_domain, error="domain has no MX record")
    if all(record.split(" ", 1)[1] == "" for record in records):
        return MXResult(MXStatus.NULL_MX, normalized_domain, records, "domain explicitly accepts no email")
    return MXResult(MXStatus.VALID, normalized_domain, records)
