from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db import Base, Contact, Customer, EmailMessage
from app.quoted_reply_resolution import resolve_quoted_outbound_parent


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


async def _parent(
    session: AsyncSession,
    *,
    customer: Customer,
    contact: Contact,
    token: str,
    body: str,
) -> EmailMessage:
    row = EmailMessage(
        customer_id=customer.id,
        contact_id=contact.id,
        direction="OUTBOUND",
        message_id=f"<{token}@lanyachem.com>",
        from_address="sales@lanyachem.com",
        to_addresses=[contact.email],
        subject="Checking in from Lanya Chem",
        body_text=body,
        attachment_metadata=[],
        raw_sha256=token * 64,
        is_history=False,
        is_automated_reply=False,
        automated_reply_metadata={},
        received_at=datetime(2026, 8, 3, 9, tzinfo=UTC),
    )
    session.add(row)
    await session.flush()
    return row


async def test_quoted_parent_requires_one_unique_body_fingerprint(
    db_session: AsyncSession,
) -> None:
    customer = Customer(company_name="Witofly", metadata_json={})
    db_session.add(customer)
    await db_session.flush()
    contact = Contact(
        customer_id=customer.id,
        name="Pooja",
        email="globalsourcing@witofly.com",
        metadata_json={},
    )
    db_session.add(contact)
    await db_session.flush()
    parent_body = (
        "Dear Pooja, I hope you are doing well. It has been some time since we "
        "last spoke. I am writing to reconnect and ask whether you currently "
        "have any requirements for our products."
    )
    parent = await _parent(
        db_session,
        customer=customer,
        contact=contact,
        token="y",
        body=parent_body,
    )
    reply = EmailMessage(
        direction="INBOUND",
        from_address="marketing@witofly.com",
        to_addresses=["sales@lanyachem.com"],
        subject="Re: Checking in from Lanya Chem",
        body_text=f"Pooja left. Please send your catalogue.\n\n{parent_body}",
        attachment_metadata=[],
        raw_sha256="z" * 64,
        is_history=False,
        is_automated_reply=False,
        automated_reply_metadata={},
        received_at=datetime(2026, 8, 4, 2, tzinfo=UTC),
    )
    db_session.add(reply)
    await db_session.commit()

    resolved = await resolve_quoted_outbound_parent(
        db_session,
        reply,
        excluded_domains=frozenset({"gmail.com"}),
    )

    assert resolved is not None
    assert resolved.email_id == parent.id
    assert resolved.customer_id == customer.id
    assert resolved.contact_id == contact.id

    await _parent(
        db_session,
        customer=customer,
        contact=contact,
        token="x",
        body=parent_body,
    )
    await db_session.commit()

    assert await resolve_quoted_outbound_parent(
        db_session,
        reply,
        excluded_domains=frozenset({"gmail.com"}),
    ) is None


async def test_quoted_parent_rejects_free_mail_sender(db_session: AsyncSession) -> None:
    customer = Customer(company_name="Free Mail", metadata_json={})
    db_session.add(customer)
    await db_session.flush()
    contact = Contact(
        customer_id=customer.id,
        name="Buyer",
        email="buyer@gmail.com",
        metadata_json={},
    )
    db_session.add(contact)
    await db_session.flush()
    body = "This is a sufficiently long outbound body " * 5
    await _parent(
        db_session,
        customer=customer,
        contact=contact,
        token="f",
        body=body,
    )
    reply = EmailMessage(
        direction="INBOUND",
        from_address="other@gmail.com",
        to_addresses=["sales@lanyachem.com"],
        subject="Checking in from Lanya Chem",
        body_text=body,
        attachment_metadata=[],
        raw_sha256="g" * 64,
        is_history=False,
        is_automated_reply=False,
        automated_reply_metadata={},
        received_at=datetime(2026, 8, 4, 2, tzinfo=UTC),
    )
    db_session.add(reply)
    await db_session.commit()

    assert await resolve_quoted_outbound_parent(
        db_session,
        reply,
        excluded_domains=frozenset({"gmail.com"}),
    ) is None
