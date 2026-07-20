from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.db import (
    CaseStage,
    CaseStatus,
    Contact,
    Customer,
    EmailMessage,
    Outbox,
    Product,
    ReactivationCampaign,
    ReactivationRecipient,
    SalesCase,
)
from app.reactivation import (
    _schedule_slots,
    ensure_reactivation_dispatch,
    next_campaign_window,
    scan_campaign_candidates,
    start_campaign,
    validate_template,
)


def _campaign(**overrides) -> ReactivationCampaign:
    values = {
        "name": "India dormant customers",
        "status": "DRAFT",
        "subject_template": "Checking in about {product_code}",
        "body_template": "Dear {contact_name},\n\nDo you need {product_name}?",
        "min_inactive_days": 365,
        "reply_filter": "ANY",
        "daily_limit": 2,
        "timezone": "Asia/Kolkata",
        "send_window_start_hour": 9,
        "send_window_end_hour": 17,
        "start_date": date(2026, 7, 20),
        "max_reactivations": 2,
        "second_reactivation_days": 90,
        "created_by": "admin",
        "metadata_json": {"require_consent_basis": True},
    }
    values.update(overrides)
    return ReactivationCampaign(**values)


def test_template_fields_are_allowlisted() -> None:
    validate_template("Hello {contact_name}, {product_code}")
    with pytest.raises(ValueError, match="unsupported template field"):
        validate_template("Send to {recipient.__class__}")
    with pytest.raises(ValueError, match="format specifications"):
        validate_template("Hello {contact_name!r}")


def test_daily_schedule_uses_business_window_and_skips_weekends() -> None:
    campaign = _campaign()
    india = ZoneInfo("Asia/Kolkata")
    observed_at = datetime(2026, 7, 20, 8, 0, tzinfo=india).astimezone(UTC)
    slots = _schedule_slots(campaign, 5, at=observed_at)
    local_slots = [slot.astimezone(india) for slot in slots]
    assert [(slot.date(), slot.hour) for slot in local_slots] == [
        (date(2026, 7, 20), 9),
        (date(2026, 7, 20), 13),
        (date(2026, 7, 21), 9),
        (date(2026, 7, 21), 13),
        (date(2026, 7, 22), 9),
    ]

    friday_after_close = datetime(2026, 7, 24, 18, 0, tzinfo=india).astimezone(UTC)
    next_window = next_campaign_window(campaign, friday_after_close)
    assert next_window is not None
    assert next_window.astimezone(india) == datetime(2026, 7, 27, 9, 0, tzinfo=india)


@pytest.mark.integration
async def test_scan_and_dispatch_eligible_dormant_customer(db_session) -> None:
    india = ZoneInfo("Asia/Kolkata")
    observed_at = datetime(2026, 7, 20, 8, 0, tzinfo=india).astimezone(UTC)
    product = Product(
        code="YAC-TEOS40",
        name="YAC-TEOS40",
        unit="kg",
        approved_text_key="yac_teos40",
        active=True,
    )
    customer = Customer(
        company_name="Dormant Buyer Pvt Ltd",
        language="en",
        auto_send_allowed=True,
        consent_basis="existing business relationship",
        do_not_contact=False,
    )
    db_session.add_all([product, customer])
    await db_session.flush()
    contact = Contact(customer_id=customer.id, name="Asha Buyer", email="asha@example.com", language="en")
    db_session.add(contact)
    await db_session.flush()
    source_case = SalesCase(
        customer_id=customer.id,
        contact_id=contact.id,
        product_id=product.id,
        currency="INR",
        stage=CaseStage.FOLLOW_UP,
        status=CaseStatus.PAUSED,
        subject_key="old conversation",
        last_activity_at=observed_at - timedelta(days=500),
    )
    db_session.add(source_case)
    await db_session.flush()
    db_session.add(
        EmailMessage(
            case_id=source_case.id,
            customer_id=customer.id,
            contact_id=contact.id,
            direction="OUTBOUND",
            message_id="<old-outbound@example.com>",
            from_address="sales@example.com",
            to_addresses=[contact.email],
            subject="Old conversation",
            body_text="Old message",
            raw_sha256="a" * 64,
            is_history=True,
            received_at=observed_at - timedelta(days=500),
        )
    )
    campaign = _campaign()
    campaign.created_at = observed_at - timedelta(minutes=1)
    db_session.add(campaign)
    await db_session.flush()
    await db_session.commit()

    result = await scan_campaign_candidates(db_session, campaign, at=observed_at)
    assert result == {"eligible": 1, "excluded": 0, "total": 1}
    recipient = await db_session.scalar(select(ReactivationRecipient))
    assert recipient is not None
    assert recipient.eligible is True
    assert recipient.selected is False
    recipient.selected = True
    recipient.status = "SELECTED"
    await db_session.commit()

    assert await start_campaign(db_session, campaign, at=observed_at) == 1
    assert recipient.scheduled_for is not None
    assert await ensure_reactivation_dispatch(
        db_session,
        at=recipient.scheduled_for + timedelta(seconds=1),
    )
    await db_session.refresh(recipient)
    outbox = await db_session.get(Outbox, recipient.outbox_id)
    assert outbox is not None
    assert outbox.message_kind == "REACTIVATION"
    assert outbox.recipient == contact.email
    assert recipient.status == "QUEUED"
    assert recipient.case_id != source_case.id


@pytest.mark.integration
async def test_scan_excludes_suppressed_and_active_contacts(db_session) -> None:
    observed_at = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)
    product = Product(code="YAC-TES", name="YAC-TES", unit="kg", approved_text_key="yac_tes", active=True)
    customer = Customer(
        company_name="Busy Buyer",
        auto_send_allowed=True,
        consent_basis="existing business relationship",
    )
    db_session.add_all([product, customer])
    await db_session.flush()
    contact = Contact(customer_id=customer.id, name="Buyer", email="busy@example.com", suppressed=True)
    db_session.add(contact)
    await db_session.flush()
    sales_case = SalesCase(
        customer_id=customer.id,
        contact_id=contact.id,
        product_id=product.id,
        currency="INR",
        status=CaseStatus.ACTIVE,
    )
    db_session.add(sales_case)
    await db_session.flush()
    db_session.add(
        EmailMessage(
            case_id=sales_case.id,
            customer_id=customer.id,
            contact_id=contact.id,
            direction="OUTBOUND",
            message_id="<busy-old@example.com>",
            from_address="sales@example.com",
            to_addresses=[contact.email],
            subject="Old",
            body_text="Old",
            raw_sha256="b" * 64,
            is_history=True,
            received_at=observed_at - timedelta(days=600),
        )
    )
    campaign = _campaign()
    db_session.add(campaign)
    await db_session.flush()
    await db_session.commit()

    result = await scan_campaign_candidates(db_session, campaign, at=observed_at)
    assert result["eligible"] == 0
    recipient = await db_session.scalar(select(ReactivationRecipient))
    assert recipient is not None
    assert recipient.exclusion_reason == "CONTACT_SUPPRESSED"
