import enum
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.settings import get_settings


class Base(DeclarativeBase):
    pass


def enum_column(enum_type: type[enum.Enum], default: enum.Enum | None = None) -> Mapped[Any]:
    return mapped_column(
        Enum(enum_type, native_enum=False, length=64),
        default=default,
        nullable=default is None,
    )


class CaseStage(str, enum.Enum):
    QUOTING = "QUOTING"
    NEGOTIATING = "NEGOTIATING"
    SAMPLE_REQUEST = "SAMPLE_REQUEST"
    DEAL_ORDER_DECISION = "DEAL_ORDER_DECISION"
    SHIPPING = "SHIPPING"
    FOLLOW_UP = "FOLLOW_UP"
    TECHNICAL_AFTER_SALES = "TECHNICAL_AFTER_SALES"


class CaseStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    WAITING_HUMAN = "WAITING_HUMAN"
    PAUSED = "PAUSED"
    HUMAN_TAKEOVER = "HUMAN_TAKEOVER"
    CLOSED_WON = "CLOSED_WON"
    CLOSED_LOST = "CLOSED_LOST"


class DeliveryStatus(str, enum.Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    SENT = "SENT"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


class JobStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )


class Customer(Base, TimestampMixin):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_name: Mapped[str] = mapped_column(String(255), unique=True)
    language: Mapped[str] = mapped_column(String(16), default="en")
    auto_send_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_basis: Mapped[str | None] = mapped_column(String(255))
    do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    contacts: Mapped[list["Contact"]] = relationship(back_populates="customer")


class Contact(Base, TimestampMixin):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("customer_id", "email", name="uq_contact_customer_email"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(320), index=True)
    language: Mapped[str] = mapped_column(String(16), default="en")
    suppressed: Mapped[bool] = mapped_column(Boolean, default=False)
    customer: Mapped[Customer] = relationship(back_populates="contacts")


class Product(Base, TimestampMixin):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    unit: Mapped[str] = mapped_column(String(32), default="unit")
    approved_text_key: Mapped[str] = mapped_column(String(128))
    margin_class: Mapped[str | None] = mapped_column(String(1))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class PricePolicy(Base, TimestampMixin):
    __tablename__ = "price_policies"
    __table_args__ = (Index("ix_price_policy_lookup", "product_id", "currency", "valid_from", "valid_to"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    currency: Mapped[str] = mapped_column(String(3))
    standard_price: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    absolute_floor: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    max_discount_pct: Mapped[Decimal] = mapped_column(Numeric(7, 4), default=Decimal("0"))
    max_negotiation_rounds: Mapped[int] = mapped_column(Integer, default=2)
    concession_step_pct: Mapped[Decimal] = mapped_column(Numeric(7, 4), default=Decimal("0.02"))
    min_quantity: Mapped[int] = mapped_column(Integer, default=1)
    max_quantity: Mapped[int | None] = mapped_column(Integer)
    tier_1_max_multiple: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    tier_1_markup_pct: Mapped[Decimal] = mapped_column(Numeric(7, 4), default=Decimal("0"))
    tier_2_max_multiple: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    tier_2_markup_pct: Mapped[Decimal] = mapped_column(Numeric(7, 4), default=Decimal("0"))
    quote_valid_days: Mapped[int] = mapped_column(Integer, default=30)
    quote_valid_weekday: Mapped[int | None] = mapped_column(Integer)
    standard_incoterm: Mapped[str] = mapped_column(String(32), default="EXW")
    allowed_incoterms: Mapped[list[str]] = mapped_column(JSON, default=list)
    standard_payment_term: Mapped[str] = mapped_column(String(128), default="100% before shipment")
    allowed_payment_terms: Mapped[list[str]] = mapped_column(JSON, default=list)
    taxes_included: Mapped[bool] = mapped_column(Boolean, default=False)
    freight_included: Mapped[bool] = mapped_column(Boolean, default=False)
    valid_from: Mapped[date] = mapped_column(Date, default=date.today)
    valid_to: Mapped[date | None] = mapped_column(Date)
    source_hash: Mapped[str] = mapped_column(String(64), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    product: Mapped[Product] = relationship()


class SalesCase(Base, TimestampMixin):
    __tablename__ = "cases"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    stage: Mapped[CaseStage] = mapped_column(Enum(CaseStage, native_enum=False, length=64), default=CaseStage.QUOTING)
    status: Mapped[CaseStatus] = mapped_column(Enum(CaseStatus, native_enum=False, length=64), default=CaseStatus.ACTIVE)
    subject_key: Mapped[str | None] = mapped_column(String(255), index=True)
    negotiation_round: Mapped[int] = mapped_column(Integer, default=0)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    customer: Mapped[Customer] = relationship()
    contact: Mapped[Contact] = relationship()
    product: Mapped[Product] = relationship()


class EmailMessage(Base):
    __tablename__ = "emails"
    __table_args__ = (
        UniqueConstraint("mailbox", "mailbox_folder", "uid_validity", "imap_uid", name="uq_mailbox_uid"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("cases.id"), index=True)
    direction: Mapped[str] = mapped_column(String(16))
    mailbox: Mapped[str | None] = mapped_column(String(320))
    mailbox_folder: Mapped[str | None] = mapped_column(String(255))
    uid_validity: Mapped[int | None] = mapped_column(Integer)
    imap_uid: Mapped[int | None] = mapped_column(Integer)
    message_id: Mapped[str | None] = mapped_column(String(998), index=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(998), index=True)
    references_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    from_address: Mapped[str] = mapped_column(String(320))
    to_addresses: Mapped[list[str]] = mapped_column(JSON, default=list)
    subject: Mapped[str] = mapped_column(String(998), default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    body_html: Mapped[str | None] = mapped_column(Text)
    attachment_metadata: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    raw_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    is_history: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_automated_reply: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    automated_reply_type: Mapped[str | None] = mapped_column(String(32), index=True)
    automated_reply_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    automated_reply_handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_bounce: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    bounce_type: Mapped[str | None] = mapped_column(String(32), index=True)
    bounce_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    bounce_handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class EmailDomainStatus(Base, TimestampMixin):
    __tablename__ = "email_domain_statuses"
    domain: Mapped[str] = mapped_column(String(255), primary_key=True)
    mx_status: Mapped[str] = mapped_column(String(32), index=True)
    mx_records: Mapped[list[str]] = mapped_column(JSON, default=list)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class EmailAddressStatus(Base, TimestampMixin):
    __tablename__ = "email_address_statuses"
    email: Mapped[str] = mapped_column(String(320), primary_key=True)
    domain: Mapped[str | None] = mapped_column(String(255), index=True)
    format_valid: Mapped[bool | None] = mapped_column(Boolean)
    preflight_status: Mapped[str | None] = mapped_column(String(32), index=True)
    last_preflight_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_preflight_detail: Mapped[str | None] = mapped_column(Text)
    suppressed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    suppression_reason: Mapped[str | None] = mapped_column(String(64), index=True)
    suppression_source_email_id: Mapped[int | None] = mapped_column(
        ForeignKey("emails.id", ondelete="SET NULL")
    )
    suppressed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_bounce_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_bounce_type: Mapped[str | None] = mapped_column(String(32), index=True)
    last_bounce_diagnostic: Mapped[str | None] = mapped_column(Text)


class MailboxCursor(Base):
    __tablename__ = "mailbox_cursors"
    mailbox: Mapped[str] = mapped_column(String(320), primary_key=True)
    folder: Mapped[str] = mapped_column(String(255), primary_key=True)
    uid_validity: Mapped[int] = mapped_column(Integer)
    last_uid: Mapped[int] = mapped_column(Integer, default=0)
    history_cutoff_uid: Mapped[int | None] = mapped_column(Integer)
    history_complete: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class MailboxDailyUsage(Base):
    __tablename__ = "mailbox_daily_usage"
    mailbox: Mapped[str] = mapped_column(String(320), primary_key=True)
    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    imap_download_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )


class MailboxThrottle(Base):
    __tablename__ = "mailbox_throttles"
    mailbox: Mapped[str] = mapped_column(String(320), primary_key=True)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )


class Quote(Base):
    __tablename__ = "quotes"
    __table_args__ = (UniqueConstraint("case_id", "round_number", name="uq_quote_case_round"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
    price_policy_id: Mapped[int] = mapped_column(ForeignKey("price_policies.id"))
    round_number: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    currency: Mapped[str] = mapped_column(String(3))
    quantity: Mapped[int] = mapped_column(Integer)
    incoterm: Mapped[str] = mapped_column(String(32))
    payment_term: Mapped[str] = mapped_column(String(128))
    valid_until: Mapped[date] = mapped_column(Date)
    pricing_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class Handoff(Base, TimestampMixin):
    __tablename__ = "handoffs"
    __table_args__ = (UniqueConstraint("source_email_id", name="uq_handoff_source_email"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("cases.id"), index=True)
    source_email_id: Mapped[int | None] = mapped_column(
        ForeignKey("emails.id", name="fk_handoffs_source_email_id_emails")
    )
    reason_code: Mapped[str] = mapped_column(String(64), index=True)
    summary: Mapped[str] = mapped_column(Text)
    extracted_facts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="OPEN")
    dingtalk_status: Mapped[str] = mapped_column(String(32), default="PENDING")
    resolution_note: Mapped[str | None] = mapped_column(Text)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_job_idempotency"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus, native_enum=False, length=32), default=JobStatus.PENDING, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(128))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        UniqueConstraint("business_key", name="uq_outbox_business_key"),
        UniqueConstraint("message_id", name="uq_outbox_message_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("cases.id"), index=True)
    business_key: Mapped[str] = mapped_column(String(255))
    message_id: Mapped[str] = mapped_column(String(998))
    recipient: Mapped[str] = mapped_column(String(320))
    raw_message: Mapped[str] = mapped_column(Text)
    status: Mapped[DeliveryStatus] = mapped_column(
        Enum(DeliveryStatus, native_enum=False, length=32), default=DeliveryStatus.PENDING, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_via: Mapped[str | None] = mapped_column(String(32))
    approval_handoff_id: Mapped[int | None] = mapped_column(
        ForeignKey("handoffs.id", ondelete="SET NULL"), unique=True, index=True
    )
    human_approved_by: Mapped[str | None] = mapped_column(String(128))
    human_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class AIInvocation(Base):
    __tablename__ = "ai_invocations"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("cases.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    purpose: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    parsed_output: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    success: Mapped[bool] = mapped_column(Boolean)
    error_type: Mapped[str | None] = mapped_column(String(128))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("cases.id"), index=True)
    actor: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


settings = get_settings()
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def db_health() -> bool:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
