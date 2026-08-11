import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import app.services as services
from app.db import (
    AuditEvent,
    CaseStage,
    CaseStatus,
    DeliveryStatus,
    EmailAddressStatus,
    EmailMessage,
    ForwardRecipient,
    Handoff,
    MailboxThrottle,
    Outbox,
    SalesCase,
    SessionLocal,
)
from app.domain import HandoffReason
from app.services import (
    forward_handoff_email,
    list_forward_recipients,
    process_inbound,
    save_forward_recipient,
    seed_demo_data,
    send_one_outbox,
)
from app.settings import Settings, get_settings

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _authorize_forward_test_recipients(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        get_settings(),
        "forward_recipient_allowlist",
        ["sales@lanyachem.com"],
    )


async def _forward_case(db_session: AsyncSession) -> tuple[SalesCase, EmailMessage, Handoff]:
    ids = await seed_demo_data(db_session)
    case = SalesCase(
        customer_id=ids["customer_id"],
        contact_id=ids["contact_id"],
        product_id=ids["product_id"],
        currency="INR",
        stage=CaseStage.QUOTING,
        status=CaseStatus.ACTIVE,
        subject_key="forward fixture",
    )
    db_session.add(case)
    await db_session.flush()
    email_row = EmailMessage(
        case_id=case.id,
        direction="INBOUND",
        mailbox="integration-test",
        message_id="<forward-source@example.com>",
        from_address="customer@example.com",
        to_addresses=["sales-agent@example.com"],
        subject="Please quote",
        body_text="Please quote 100 kg.",
        body_html="<p>Please quote <b>100 kg</b>.</p>",
        attachment_metadata=[],
        raw_sha256=hashlib.sha256(b"forward-source").hexdigest(),
    )
    db_session.add(email_row)
    await db_session.flush()
    handoff = Handoff(
        case_id=case.id,
        source_email_id=email_row.id,
        reason_code=HandoffReason.NEW_INQUIRY_REVIEW.value,
        summary="forward fixture",
        extracted_facts={},
        status="OPEN",
    )
    db_session.add(handoff)
    await db_session.commit()
    return case, email_row, handoff


async def test_forward_handoff_email_preserves_content_and_takes_over(
    db_session: AsyncSession,
) -> None:
    case, _email_row, handoff = await _forward_case(db_session)

    outbox = await forward_handoff_email(
        db_session,
        handoff_id=handoff.id,
        recipient="sales@lanyachem.com",
        actor="admin",
        note="请跟进",
    )
    assert outbox.message_kind == "FORWARD"
    assert outbox.recipient == "sales@lanyachem.com"
    assert outbox.approval_handoff_id == handoff.id
    assert outbox.human_approved_by == "admin"
    assert outbox.human_approved_at is not None

    mime = BytesParser(policy=policy.default).parsebytes(
        outbox.raw_message.encode("utf-8")
    )
    assert str(mime["Subject"] or "").startswith("Fwd:")
    assert "multipart/alternative" in mime.get_content_type()
    text_body = mime.get_body(preferencelist=("plain",)).get_content()
    html_body = mime.get_body(preferencelist=("html",)).get_content()
    assert "---------- Forwarded message ---------" in text_body
    assert "Please quote 100 kg." in text_body
    assert "请跟进" in text_body
    assert "Please quote <b>100 kg</b>" in html_body or "Please quote" in html_body

    await db_session.refresh(case)
    await db_session.refresh(handoff)
    assert case.status == CaseStatus.HUMAN_TAKEOVER
    assert handoff.status == "RESOLVED"
    recipient = await db_session.scalar(
        select(ForwardRecipient).where(
            ForwardRecipient.email == "sales@lanyachem.com"
        )
    )
    assert recipient is not None
    assert recipient.last_used_at is not None
    audit = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.event_type == "handoff.forwarded_to_salesperson"
        )
    )
    assert audit is not None


async def test_process_inbound_skips_human_takeover_cases(
    db_session: AsyncSession,
) -> None:
    case, _email_row, _handoff = await _forward_case(db_session)
    case.status = CaseStatus.HUMAN_TAKEOVER
    await db_session.commit()

    email_row = EmailMessage(
        case_id=case.id,
        direction="INBOUND",
        mailbox="integration-test",
        message_id="<after-takeover@example.com>",
        from_address="customer@example.com",
        to_addresses=["sales-agent@example.com"],
        subject="Re: Please quote",
        body_text="Please send the price.",
        attachment_metadata=[],
        raw_sha256=hashlib.sha256(b"after-takeover").hexdigest(),
    )
    db_session.add(email_row)
    await db_session.commit()

    await process_inbound(db_session, email_row.id)

    assert await db_session.scalar(
        select(func.count())
        .select_from(Outbox)
        .where(Outbox.case_id == case.id)
    ) == 0
    assert await db_session.scalar(
        select(func.count()).select_from(Handoff)
    ) == 1


async def test_forward_outbox_delivers_under_human_takeover(
    db_session: AsyncSession,
) -> None:
    case, _email_row, handoff = await _forward_case(db_session)
    outbox = await forward_handoff_email(
        db_session,
        handoff_id=handoff.id,
        recipient="sales@lanyachem.com",
        actor="admin",
    )
    await db_session.refresh(case)
    assert case.status == CaseStatus.HUMAN_TAKEOVER

    delivered = await send_one_outbox(
        db_session,
        get_settings(),
        at=datetime.now(UTC),
    )
    assert delivered is True
    await db_session.refresh(outbox)
    assert outbox.status == DeliveryStatus.SENT
    assert outbox.recipient == "sales@lanyachem.com"


async def test_forward_recipients_search_and_save(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        get_settings(),
        "forward_recipient_allowlist",
        ["alice@lanyachem.com", "new@lanyachem.com"],
    )
    await save_forward_recipient(
        db_session,
        email="alice@lanyachem.com",
        name="Alice Sales",
    )
    db_session.add(
        ForwardRecipient(
            email="bob@other.com",
            name="Old unauthorized history",
            last_used_at=datetime.now(UTC),
        )
    )
    await db_session.commit()

    matches = await list_forward_recipients(
        db_session,
        query="alice",
    )
    assert [item["email"] for item in matches] == ["alice@lanyachem.com"]
    all_rows = await list_forward_recipients(db_session)
    assert len(all_rows) == 2
    assert [item["email"] for item in all_rows] == [
        "alice@lanyachem.com",
        "new@lanyachem.com",
    ]
    assert all_rows[1]["id"] is None
    with pytest.raises(ValueError, match="not authorized"):
        await save_forward_recipient(
            db_session,
            email="bob@other.com",
            name="Bob",
        )


async def test_empty_forward_allowlist_returns_no_history_and_disables_authorization(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "forward_recipient_allowlist", [])
    db_session.add(
        ForwardRecipient(
            email="historical@lanyachem.com",
            name="Historical only",
            last_used_at=datetime.now(UTC),
        )
    )
    await db_session.commit()

    assert await list_forward_recipients(db_session) == []
    with pytest.raises(ValueError, match="not authorized"):
        await save_forward_recipient(
            db_session,
            email="historical@lanyachem.com",
        )


def _smtp_forward_settings(
    *,
    forward_allowlist: list[str] | None = None,
    safe_allowlist: list[str] | None = None,
) -> Settings:
    return Settings(
        mail_transport="smtp",
        safe_mode=True,
        auto_send_enabled=False,
        recipient_allowlist=safe_allowlist or [],
        forward_recipient_allowlist=forward_allowlist or [],
        commercial_gate_enabled=False,
        email_preflight_enabled=True,
        mx_check_enabled=False,
        min_send_interval_seconds=0,
        send_interval_jitter_seconds=0,
    )


@pytest.mark.parametrize(
    ("policy_case", "expected_status"),
    [
        ("allowed", DeliveryStatus.SENT),
        ("customer_dnc", DeliveryStatus.SENT),
        ("safe_mode", DeliveryStatus.CANCELLED),
        ("unauthorized", DeliveryStatus.CANCELLED),
        ("suppressed", DeliveryStatus.CANCELLED),
        ("preflight_block", DeliveryStatus.CANCELLED),
        ("preflight_defer", DeliveryStatus.PENDING),
        ("cooldown", DeliveryStatus.PENDING),
    ],
)
async def test_forward_delivery_policy_matrix(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    policy_case: str,
    expected_status: DeliveryStatus,
) -> None:
    case, _email_row, handoff = await _forward_case(db_session)
    outbox = await forward_handoff_email(
        db_session,
        handoff_id=handoff.id,
        recipient="sales@lanyachem.com",
        actor="admin",
    )

    settings = _smtp_forward_settings(
        forward_allowlist=(
            [] if policy_case == "unauthorized" else ["sales@lanyachem.com"]
        ),
        safe_allowlist=(
            [] if policy_case == "safe_mode" else ["sales@lanyachem.com"]
        ),
    )
    if policy_case == "customer_dnc":
        customer = await db_session.get(services.Customer, case.customer_id)
        assert customer is not None
        customer.do_not_contact = True
    if policy_case == "suppressed":
        db_session.add(
            EmailAddressStatus(
                email="sales@lanyachem.com",
                domain="lanyachem.com",
                suppressed=True,
                suppression_reason="TEST",
                suppressed_at=datetime.now(UTC),
            )
        )
    if policy_case == "cooldown":
        db_session.add(
            MailboxThrottle(
                mailbox=(
                    settings.gmail_address
                    or parseaddr(settings.mail_from)[1]
                ).casefold(),
                cooldown_until=datetime.now(UTC) + timedelta(minutes=5),
                reason="test cooldown",
            )
        )
    await db_session.commit()

    async def fake_preflight(
        _session: AsyncSession,
        recipient: str,
        _settings: Settings,
    ) -> tuple[str, str, dict[str, object]]:
        if policy_case == "preflight_defer":
            return "DEFER", "temporary DNS failure", {"recipient": recipient}
        if policy_case == "preflight_block":
            return "BLOCK", "invalid MX", {
                "recipient": recipient,
                "auto_suppressed": True,
            }
        return "ALLOW", "test preflight passed", {"recipient": recipient}

    sent: list[str] = []

    class CapturingTransport:
        def send(self, raw_message: str, message_id: str, recipient: str) -> None:
            sent.append(recipient)

    monkeypatch.setattr(services, "_recipient_preflight", fake_preflight)
    monkeypatch.setattr(services, "transport_for", lambda _settings: CapturingTransport())

    assert await send_one_outbox(db_session, settings, at=datetime.now(UTC)) is True
    await db_session.refresh(outbox)
    assert outbox.status == expected_status
    assert sent == (["sales@lanyachem.com"] if expected_status == DeliveryStatus.SENT else [])


async def test_forged_forward_kind_without_approval_is_cancelled(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _case, _email_row, handoff = await _forward_case(db_session)
    outbox = await forward_handoff_email(
        db_session,
        handoff_id=handoff.id,
        recipient="sales@lanyachem.com",
        actor="admin",
    )
    outbox.approval_handoff_id = None
    outbox.human_approved_by = None
    outbox.human_approved_at = None
    await db_session.commit()

    sent: list[str] = []

    class CapturingTransport:
        def send(self, raw_message: str, message_id: str, recipient: str) -> None:
            sent.append(recipient)

    monkeypatch.setattr(services, "transport_for", lambda _settings: CapturingTransport())
    settings = _smtp_forward_settings(
        forward_allowlist=["sales@lanyachem.com"],
        safe_allowlist=["sales@lanyachem.com"],
    )
    assert await send_one_outbox(db_session, settings, at=datetime.now(UTC)) is True
    await db_session.refresh(outbox)
    assert outbox.status == DeliveryStatus.CANCELLED
    assert "complete human approval" in (outbox.last_error or "")
    assert sent == []


@pytest.mark.parametrize(
    ("approved_by", "include_approved_at"),
    [
        (None, True),
        ("reviewer", False),
        ("   ", True),
    ],
)
async def test_outbox_rejects_partial_or_blank_human_approval_metadata(
    db_session: AsyncSession,
    approved_by: str | None,
    include_approved_at: bool,
) -> None:
    case, _email_row, handoff = await _forward_case(db_session)
    key = f"invalid-approval:{approved_by!r}:{include_approved_at}"
    db_session.add(
        Outbox(
            case_id=case.id,
            message_kind="FORWARD",
            business_key=key,
            message_id=f"<{hashlib.sha256(key.encode()).hexdigest()}@example.com>",
            recipient="sales@lanyachem.com",
            raw_message="invalid approval fixture",
            approval_handoff_id=handoff.id,
            human_approved_by=approved_by,
            human_approved_at=(datetime.now(UTC) if include_approved_at else None),
        )
    )

    with pytest.raises(IntegrityError, match="ck_outbox_human_approval_complete"):
        await db_session.flush()
    await db_session.rollback()


async def test_forward_failure_after_outbox_staging_rolls_back_everything(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case, _email_row, handoff = await _forward_case(db_session)
    case_id = case.id
    handoff_id = handoff.id

    async def fail_recipient_touch(*args: object, **kwargs: object) -> ForwardRecipient:
        raise RuntimeError("injected failure after outbox staging")

    monkeypatch.setattr(services, "_touch_forward_recipient", fail_recipient_touch)
    with pytest.raises(RuntimeError, match="injected failure"):
        await forward_handoff_email(
            db_session,
            handoff_id=handoff_id,
            recipient="sales@lanyachem.com",
            actor="admin",
        )

    assert await db_session.scalar(select(func.count()).select_from(Outbox)) == 0
    assert await db_session.scalar(
        select(func.count())
        .select_from(EmailMessage)
        .where(EmailMessage.direction == "OUTBOUND")
    ) == 0
    assert await db_session.scalar(select(func.count()).select_from(ForwardRecipient)) == 0
    assert await db_session.scalar(select(func.count()).select_from(AuditEvent)) == 0
    stored_handoff = await db_session.get(Handoff, handoff_id)
    stored_case = await db_session.get(SalesCase, case_id)
    assert stored_handoff is not None and stored_handoff.status == "OPEN"
    assert stored_case is not None and stored_case.status == CaseStatus.ACTIVE


async def test_concurrent_forward_requests_create_one_business_result(
    db_session: AsyncSession,
) -> None:
    _case, _email_row, handoff = await _forward_case(db_session)
    handoff_id = handoff.id

    async def attempt() -> Outbox | ValueError:
        async with SessionLocal() as session:
            try:
                return await forward_handoff_email(
                    session,
                    handoff_id=handoff_id,
                    recipient="sales@lanyachem.com",
                    actor="admin",
                )
            except ValueError as exc:
                return exc

    results = await asyncio.gather(attempt(), attempt())

    assert sum(isinstance(result, Outbox) for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert await db_session.scalar(
        select(func.count())
        .select_from(Outbox)
        .where(Outbox.business_key == f"handoff-reply:{handoff_id}:forward")
    ) == 1
