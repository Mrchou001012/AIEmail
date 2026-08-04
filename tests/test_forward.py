import hashlib
from email import policy
from email.parser import BytesParser

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import (
    AuditEvent,
    CaseStage,
    CaseStatus,
    EmailMessage,
    ForwardRecipient,
    Handoff,
    Outbox,
    SalesCase,
)
from app.domain import HandoffReason
from app.services import (
    forward_handoff_email,
    list_forward_recipients,
    process_inbound,
    save_forward_recipient,
    seed_demo_data,
)

pytestmark = pytest.mark.integration


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


async def test_forward_recipients_search_and_save(
    db_session: AsyncSession,
) -> None:
    await save_forward_recipient(
        db_session,
        email="alice@lanyachem.com",
        name="Alice Sales",
    )
    await save_forward_recipient(
        db_session,
        email="bob@other.com",
        name="Bob",
    )

    matches = await list_forward_recipients(
        db_session,
        query="alice",
    )
    assert [item["email"] for item in matches] == ["alice@lanyachem.com"]
    all_rows = await list_forward_recipients(db_session)
    assert len(all_rows) == 2
    assert all_rows[0]["email"] == "bob@other.com"
