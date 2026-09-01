from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.db import CaseStatus, Contact, Customer, Outbox, SalesCase
from app.services import _case_outbound_gate


def _case(*, lifecycle_status: str = "ACTIVE", qualification_status: str = "UNKNOWN") -> SalesCase:
    customer = Customer(
        company_name="Lifecycle Gate Test",
        auto_send_allowed=True,
        qualification_status=qualification_status,
        metadata_json={},
    )
    contact = Contact(
        customer_id=1,
        name="Buyer",
        email="buyer@example.com",
        lifecycle_status=lifecycle_status,
        metadata_json={},
    )
    return SalesCase(
        id=1,
        customer_id=1,
        contact_id=1,
        status=CaseStatus.ACTIVE,
        customer=customer,
        contact=contact,
    )


def _outbox() -> Outbox:
    return Outbox(
        case_id=1,
        message_kind="AUTO_QUOTE",
        recipient="buyer@example.com",
        business_key="gate-test",
        message_id="<gate-test@example.com>",
        raw_message="test",
    )


@pytest.mark.asyncio
async def test_autonomous_case_mail_is_deferred_during_temporary_absence() -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    case = _case(lifecycle_status="TEMPORARILY_UNAVAILABLE")
    case.contact.unavailable_until = now + timedelta(days=3)
    session = AsyncMock()
    session.scalar.return_value = case

    resolved, action, reason, available_at = await _case_outbound_gate(
        session,
        _outbox(),
        at=now,
        human_approved=False,
    )

    assert resolved is case
    assert action == "DEFER"
    assert reason == "contact is temporarily unavailable"
    assert available_at == case.contact.unavailable_until


@pytest.mark.asyncio
async def test_non_target_customer_blocks_autonomous_case_mail() -> None:
    case = _case(qualification_status="NON_TARGET")
    session = AsyncMock()
    session.scalar.return_value = case

    _, action, reason, _ = await _case_outbound_gate(
        session,
        _outbox(),
        at=datetime(2026, 9, 1, tzinfo=UTC),
        human_approved=False,
    )

    assert action == "BLOCK"
    assert reason == "customer or contact is no longer a sales target"


@pytest.mark.asyncio
async def test_expired_temporary_absence_reactivates_contact() -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    case = _case(lifecycle_status="TEMPORARILY_UNAVAILABLE")
    case.contact.unavailable_until = now - timedelta(minutes=1)
    session = AsyncMock()
    session.scalar.return_value = case

    _, action, _, _ = await _case_outbound_gate(
        session,
        _outbox(),
        at=now,
        human_approved=False,
    )

    assert action == "PASS"
    assert case.contact.lifecycle_status == "ACTIVE"
    assert case.contact.unavailable_until is None
