from datetime import UTC, datetime

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.ai import InboundDispositionDecision
from app.api import (
    InboundDispositionApplyRequest,
    InboundDispositionRollbackRequest,
    inbound_disposition_apply,
    inbound_disposition_rollback,
)
from app.db import (
    AgentRun,
    AgentRunStatus,
    AgentStep,
    AgentStepStatus,
    AssistanceRequest,
    AssistanceStatus,
    Base,
    Contact,
    ContactReferral,
    Customer,
    DeliveryStatus,
    EmailMessage,
    Handoff,
    InboundDispositionAction,
    Job,
    JobStatus,
    Outbox,
    SalesCase,
)
from app.disposition_service import (
    apply_email_disposition,
    backfill_inbound_dispositions,
    build_disposition_plan,
    classify_email_disposition,
    rollback_email_disposition,
)
from app.domain import HandoffReason
from app.inbound_disposition import InboundDispositionType
from app.services import _handle_automated_reply
from app.settings import Settings, get_settings


class _DispositionAI:
    def __init__(self, decision: InboundDispositionDecision) -> None:
        self.decision = decision

    async def classify_inbound_disposition(self, **_: object):
        return self.decision, {
            "provider": "anthropic",
            "model": "claude-test",
            "request_hash": "a" * 64,
            "request_id": "req_test",
        }


class _FailingDispositionAI:
    async def classify_inbound_disposition(self, **_: object):
        raise PermissionError("test model access denied")


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    """Exercise disposition transactions without touching a configured database."""

    isolated_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with isolated_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(isolated_engine, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await isolated_engine.dispose()


async def _seed_contact(
    session: AsyncSession,
    *,
    company: str,
    name: str,
    email: str,
) -> tuple[Customer, Contact]:
    customer = Customer(
        company_name=company,
        auto_send_allowed=True,
        consent_basis="historical relationship",
        metadata_json={},
    )
    session.add(customer)
    await session.flush()
    contact = Contact(
        customer_id=customer.id,
        name=name,
        email=email,
        metadata_json={},
    )
    session.add(contact)
    await session.flush()
    return customer, contact


def _email(
    *,
    contact: Contact,
    subject: str,
    body: str,
    token: str,
    auto: bool = False,
) -> EmailMessage:
    return EmailMessage(
        customer_id=contact.customer_id,
        contact_id=contact.id,
        direction="INBOUND",
        from_address=contact.email,
        to_addresses=["sales@lanyachem.com"],
        subject=subject,
        body_text=body,
        attachment_metadata=[],
        raw_sha256=token * 64,
        is_history=False,
        is_automated_reply=auto,
        automated_reply_metadata=(
            {"headers": {"Auto-Submitted": "auto-replied"}} if auto else {}
        ),
        received_at=datetime(2026, 8, 4, 8, tzinfo=UTC),
    )


async def test_ai_semantic_disposition_supplies_confidence_and_evidence(
    db_session: AsyncSession,
) -> None:
    _, contact = await _seed_contact(
        db_session,
        company="AI Referral Review",
        name="Original Buyer",
        email="buyer@ai-referral.example",
    )
    row = _email(
        contact=contact,
        subject="Re: Checking in",
        body=(
            "Please contact our new procurement manager Maya at "
            "maya@ai-referral.example for future requirements."
        ),
        token="z",
    )
    db_session.add(row)
    await db_session.commit()
    decision = InboundDispositionDecision(
        disposition_type="CONTACT_REFERRAL",
        confidence=0.87,
        reason="The sender explicitly names the future procurement contact.",
        evidence=["Please contact our new procurement manager Maya"],
        replacement_emails=[
            "maya@ai-referral.example",
            "hallucinated@ai-referral.example",
        ],
        return_hint=None,
        forwarded_to_replacement=False,
        non_target_reason=None,
        product_list_requested=False,
    )
    settings = Settings(
        _env_file=None,
        ai_provider="anthropic",
        anthropic_api_key="test-only",
        inbound_disposition_ai_enabled=True,
    )

    disposition = await classify_email_disposition(
        row,
        settings=settings,
        ai_client=_DispositionAI(decision),  # type: ignore[arg-type]
    )
    plan = await build_disposition_plan(
        db_session,
        row,
        settings=settings,
        disposition=disposition,
    )

    assert disposition.disposition_type is InboundDispositionType.CONTACT_REFERRAL
    assert disposition.classifier_source == "anthropic"
    assert disposition.confidence == 0.87
    assert disposition.replacement_emails == ("maya@ai-referral.example",)
    assert plan["classifier_model"] == "claude-test"
    assert plan["evidence"] == ["Please contact our new procurement manager Maya"]


async def test_low_confidence_ai_mutation_is_blocked(
    db_session: AsyncSession,
) -> None:
    _, contact = await _seed_contact(
        db_session,
        company="AI Confidence Gate",
        name="Maybe Buyer",
        email="buyer@ai-confidence.example",
    )
    row = _email(
        contact=contact,
        subject="Re: Checking in",
        body="We may be changing responsibilities internally.",
        token="y",
    )
    db_session.add(row)
    await db_session.commit()
    disposition = await classify_email_disposition(
        row,
        settings=Settings(
            _env_file=None,
            ai_provider="anthropic",
            anthropic_api_key="test-only",
            inbound_disposition_ai_enabled=True,
        ),
        ai_client=_DispositionAI(
            InboundDispositionDecision(
                disposition_type="DEPARTED",
                confidence=0.55,
                reason="The wording might indicate a personnel change.",
                evidence=["changing responsibilities internally"],
                replacement_emails=[],
                return_hint=None,
                forwarded_to_replacement=False,
                non_target_reason=None,
                product_list_requested=False,
            )
        ),  # type: ignore[arg-type]
    )
    plan = await build_disposition_plan(
        db_session,
        row,
        settings=Settings(_env_file=None),
        disposition=disposition,
    )

    assert "AI_CONFIDENCE_BELOW_THRESHOLD" in plan["blockers"]


async def test_ai_failure_falls_back_with_mutation_blocker(
    db_session: AsyncSession,
) -> None:
    _, contact = await _seed_contact(
        db_session,
        company="AI Failure Gate",
        name="Departed Buyer",
        email="buyer@ai-failure.example",
    )
    row = _email(
        contact=contact,
        subject="Automatic reply: Checking in",
        body="The buyer is no longer employed here.",
        token="x",
        auto=True,
    )
    db_session.add(row)
    await db_session.commit()
    settings = Settings(
        _env_file=None,
        ai_provider="anthropic",
        anthropic_api_key="test-only",
        inbound_disposition_ai_enabled=True,
    )

    disposition = await classify_email_disposition(
        row,
        settings=settings,
        ai_client=_FailingDispositionAI(),  # type: ignore[arg-type]
    )
    plan = await build_disposition_plan(
        db_session,
        row,
        settings=settings,
        disposition=disposition,
    )

    assert disposition.classifier_source == "deterministic_fallback"
    assert disposition.classification_error == "PermissionError"
    assert "AI_CLASSIFICATION_UNAVAILABLE" in plan["blockers"]


async def test_dry_run_does_not_mutate_customer_or_contact(
    db_session: AsyncSession,
) -> None:
    customer, contact = await _seed_contact(
        db_session,
        company="Transworld Dry Run",
        name="Anil",
        email="anil@dryrun-logistics.example",
    )
    row = _email(
        contact=contact,
        subject="Re: Checking in",
        body="I am a logistics service provider; we can assist with shipments.",
        token="a",
    )
    db_session.add(row)
    await db_session.commit()

    result = await backfill_inbound_dispositions(db_session, apply=False)

    await db_session.refresh(customer)
    await db_session.refresh(row)
    assert result["counts"] == {"NON_TARGET": 1}
    assert customer.qualification_status == "UNKNOWN"
    assert row.disposition_handled_at is None


async def test_apply_marks_non_target_without_using_do_not_contact(
    db_session: AsyncSession,
) -> None:
    customer, contact = await _seed_contact(
        db_session,
        company="Transworld Apply",
        name="Anil",
        email="anil@apply-logistics.example",
    )
    row = _email(
        contact=contact,
        subject="Re: Checking in",
        body="I am a logistics service provider; we can assist with shipments.",
        token="b",
    )
    db_session.add(row)
    await db_session.commit()
    settings = get_settings()
    original = settings.inbound_disposition_apply_enabled
    settings.inbound_disposition_apply_enabled = True
    try:
        assert await apply_email_disposition(db_session, row, settings=settings) is True
        await db_session.commit()
    finally:
        settings.inbound_disposition_apply_enabled = original

    await db_session.refresh(customer)
    assert customer.qualification_status == "NON_TARGET"
    assert customer.qualification_reason == "LOGISTICS_SERVICE_PROVIDER"
    assert customer.do_not_contact is False


async def test_apply_departed_suppresses_old_endpoint_and_saves_referral(
    db_session: AsyncSession,
) -> None:
    _, contact = await _seed_contact(
        db_session,
        company="GLS Polyfilms Test",
        name="Raksha",
        email="raksha@glspolyfilms.example",
    )
    row = _email(
        contact=contact,
        subject="Automatic reply: Checking in",
        body=(
            "Raksha is no longer employed here. Please direct future correspondence "
            "to Astha at astha@glspolyfilms.example. This email has been automatically "
            "forwarded to Astha."
        ),
        token="c",
        auto=True,
    )
    db_session.add(row)
    await db_session.commit()
    settings = get_settings()
    original = settings.inbound_disposition_apply_enabled
    settings.inbound_disposition_apply_enabled = True
    try:
        assert await apply_email_disposition(db_session, row, settings=settings) is True
        await db_session.commit()
    finally:
        settings.inbound_disposition_apply_enabled = original

    await db_session.refresh(contact)
    referral = await db_session.scalar(
        select(ContactReferral).where(ContactReferral.source_email_id == row.id)
    )
    assert contact.lifecycle_status == "DEPARTED"
    assert contact.suppressed is True
    assert referral is not None
    assert referral.referred_email == "astha@glspolyfilms.example"
    assert referral.status == "WAITING_FOR_FORWARDED_REPLY"


async def test_verified_changed_sender_retires_original_and_keeps_business_flow(
    db_session: AsyncSession,
) -> None:
    customer, original = await _seed_contact(
        db_session,
        company="Verified Changed Sender",
        name="Former Buyer",
        email="former@changed-sender.example",
    )
    reply_contact = Contact(
        customer_id=customer.id,
        name="New Buyer",
        email="new@changed-sender.example",
        metadata_json={},
    )
    db_session.add(reply_contact)
    await db_session.flush()
    row = _email(
        contact=reply_contact,
        subject="Re: Checking in",
        body=(
            "Former Buyer no longer works here. Please send us your product list."
        ),
        token="n",
    )
    row.disposition_metadata = {
        "verified_reactivation_parent": True,
        "original_contact_id": original.id,
        "reply_contact_id": reply_contact.id,
        "sender_changed": True,
    }
    db_session.add(row)
    await db_session.commit()

    plan = await build_disposition_plan(db_session, row)
    assert plan["blockers"] == []
    assert plan["contact_id"] == original.id
    assert plan["sender_contact_id"] == reply_contact.id

    assert (
        await apply_email_disposition(
            db_session,
            row,
            actor="admin:test",
            force_manual=True,
        )
        is False
    )
    await db_session.commit()

    await db_session.refresh(original)
    await db_session.refresh(reply_contact)
    referral = await db_session.scalar(
        select(ContactReferral).where(ContactReferral.source_email_id == row.id)
    )
    assert original.suppressed is True
    assert original.lifecycle_status == "DEPARTED"
    assert reply_contact.suppressed is False
    assert referral is not None
    assert referral.new_contact_id == reply_contact.id
    assert referral.status == "ACTIVE_CONTACT"
    assert row.disposition_metadata["continue_business_processing"] is True

    action = await db_session.scalar(
        select(InboundDispositionAction).where(
            InboundDispositionAction.source_email_id == row.id
        )
    )
    assert action is not None
    await rollback_email_disposition(
        db_session,
        action_id=action.id,
        actor="admin:test",
        reason="Original contact is still active",
    )
    await db_session.refresh(original)
    assert original.suppressed is False
    assert original.lifecycle_status == "ACTIVE"
    assert await db_session.get(Contact, reply_contact.id) is not None


async def test_apply_out_of_office_sets_resume_boundary(
    db_session: AsyncSession,
) -> None:
    _, contact = await _seed_contact(
        db_session,
        company="Fisvi Test",
        name="Barbara",
        email="barbara@fisvi.example",
    )
    row = _email(
        contact=contact,
        subject="Automatic reply: Checking in",
        body=(
            "Our offices are closed from 3rd to 21st August. We will respond upon "
            "our return."
        ),
        token="d",
        auto=True,
    )
    db_session.add(row)
    await db_session.commit()
    settings = get_settings()
    original = settings.inbound_disposition_apply_enabled
    settings.inbound_disposition_apply_enabled = True
    try:
        assert await apply_email_disposition(db_session, row, settings=settings) is True
        await db_session.commit()
    finally:
        settings.inbound_disposition_apply_enabled = original

    await db_session.refresh(contact)
    assert contact.lifecycle_status == "TEMPORARILY_UNAVAILABLE"
    assert contact.unavailable_until is not None
    observed_until = contact.unavailable_until.replace(
        tzinfo=contact.unavailable_until.tzinfo or UTC
    )
    assert observed_until == datetime(2026, 8, 22, tzinfo=UTC)
    assert await db_session.scalar(select(func.count()).select_from(ContactReferral)) == 0


async def test_forwarded_to_colleague_saves_contact_without_duplicate_outreach(
    db_session: AsyncSession,
) -> None:
    _, contact = await _seed_contact(
        db_session,
        company="Forwarded Colleague",
        name="Manager",
        email="manager@forwarded-colleague.example",
    )
    row = _email(
        contact=contact,
        subject="Re: Checking in",
        body=(
            "I have forwarded your email to procurement. Please contact Maya at "
            "maya@forwarded-colleague.example for future inquiries."
        ),
        token="o",
    )
    db_session.add(row)
    await db_session.commit()

    assert (
        await apply_email_disposition(
            db_session,
            row,
            actor="admin:test",
            force_manual=True,
            allow_referral_outreach=True,
        )
        is True
    )
    await db_session.commit()

    referral = await db_session.scalar(
        select(ContactReferral).where(ContactReferral.source_email_id == row.id)
    )
    assert referral is not None
    assert referral.referred_email == "maya@forwarded-colleague.example"
    assert referral.forwarded_already is True
    assert referral.status == "WAITING_FOR_FORWARDED_REPLY"
    assert await db_session.scalar(
        select(func.count())
        .select_from(Outbox)
        .where(Outbox.message_kind == "REFERRAL_OUTREACH")
    ) == 0


async def test_automatic_apply_respects_blockers_but_manual_confirmation_can_override(
    db_session: AsyncSession,
) -> None:
    _, contact = await _seed_contact(
        db_session,
        company="Undated Leave",
        name="Buyer",
        email="buyer@undated-leave.example",
    )
    row = _email(
        contact=contact,
        subject="Automatic reply: Checking in",
        body="I am currently on leave and will respond when I return.",
        token="i",
        auto=True,
    )
    db_session.add(row)
    await db_session.commit()
    settings = get_settings()
    original = settings.inbound_disposition_apply_enabled
    settings.inbound_disposition_apply_enabled = True
    try:
        assert await apply_email_disposition(db_session, row, settings=settings) is False
        assert row.disposition_type == "TEMPORARY_ABSENCE"
        assert row.disposition_handled_at is None
        await db_session.refresh(contact)
        assert contact.lifecycle_status == "ACTIVE"

        assert (
            await apply_email_disposition(
                db_session,
                row,
                settings=settings,
                actor="admin:test",
                force_manual=True,
            )
            is True
        )
        await db_session.commit()
    finally:
        settings.inbound_disposition_apply_enabled = original

    await db_session.refresh(contact)
    assert contact.lifecycle_status == "TEMPORARILY_UNAVAILABLE"
    assert contact.unavailable_until is None


async def test_unique_same_domain_referral_can_queue_bounded_outreach(
    db_session: AsyncSession,
) -> None:
    _, contact = await _seed_contact(
        db_session,
        company="Referral Outreach Test",
        name="Old Buyer",
        email="old.buyer@referral-company.example",
    )
    row = _email(
        contact=contact,
        subject="Automatic reply: Checking in",
        body=(
            "Old Buyer is no longer employed here. Please direct future "
            "correspondence to New Buyer at new.buyer@referral-company.example."
        ),
        token="e",
        auto=True,
    )
    db_session.add(row)
    await db_session.commit()
    settings = get_settings()
    original_apply = settings.inbound_disposition_apply_enabled
    original_referral = settings.referral_auto_contact_enabled
    settings.inbound_disposition_apply_enabled = True
    settings.referral_auto_contact_enabled = True
    try:
        assert await apply_email_disposition(db_session, row, settings=settings) is True
        await db_session.commit()
    finally:
        settings.inbound_disposition_apply_enabled = original_apply
        settings.referral_auto_contact_enabled = original_referral

    new_contact = await db_session.scalar(
        select(Contact).where(Contact.email == "new.buyer@referral-company.example")
    )
    referral = await db_session.scalar(
        select(ContactReferral).where(ContactReferral.source_email_id == row.id)
    )
    outbox = await db_session.scalar(
        select(Outbox).where(Outbox.message_kind == "REFERRAL_OUTREACH")
    )
    assert new_contact is not None
    assert referral is not None and referral.new_contact_id == new_contact.id
    assert referral.status == "OUTREACH_QUEUED"
    assert outbox is not None and outbox.recipient == new_contact.email


async def test_manual_apply_bypasses_global_apply_gate_and_can_be_rolled_back(
    db_session: AsyncSession,
) -> None:
    customer, contact = await _seed_contact(
        db_session,
        company="Rollback Logistics",
        name="Anil",
        email="anil@rollback-logistics.example",
    )
    row = _email(
        contact=contact,
        subject="Re: Checking in",
        body="I am a logistics service provider; we can assist with shipments.",
        token="f",
    )
    db_session.add(row)
    await db_session.commit()
    settings = get_settings()
    original = settings.inbound_disposition_apply_enabled
    settings.inbound_disposition_apply_enabled = False
    try:
        assert (
            await apply_email_disposition(
                db_session,
                row,
                settings=settings,
                actor="admin:test",
                force_manual=True,
            )
            is True
        )
        await db_session.commit()
    finally:
        settings.inbound_disposition_apply_enabled = original

    action = await db_session.scalar(
        select(InboundDispositionAction).where(
            InboundDispositionAction.source_email_id == row.id
        )
    )
    assert action is not None and action.status == "APPLIED"
    await db_session.refresh(customer)
    assert customer.qualification_status == "NON_TARGET"

    result = await rollback_email_disposition(
        db_session,
        action_id=action.id,
        actor="admin:test",
        reason="Classification corrected",
    )

    await db_session.refresh(customer)
    await db_session.refresh(row)
    assert result["status"] == "ROLLED_BACK"
    assert customer.qualification_status == "UNKNOWN"
    assert customer.qualification_reason is None
    assert row.disposition_handled_at is None
    assert row.disposition_type is None


async def test_rollback_refuses_outreach_that_has_entered_delivery(
    db_session: AsyncSession,
) -> None:
    _, contact = await _seed_contact(
        db_session,
        company="Irreversible Referral",
        name="Old Buyer",
        email="old@irreversible-referral.example",
    )
    row = _email(
        contact=contact,
        subject="Automatic reply: Checking in",
        body=(
            "Old Buyer is no longer employed here. Please contact New Buyer at "
            "new@irreversible-referral.example."
        ),
        token="g",
        auto=True,
    )
    db_session.add(row)
    await db_session.commit()
    settings = get_settings()
    original_referral = settings.referral_auto_contact_enabled
    settings.referral_auto_contact_enabled = True
    try:
        await apply_email_disposition(
            db_session,
            row,
            settings=settings,
            actor="admin:test",
            force_manual=True,
            allow_referral_outreach=True,
        )
        await db_session.commit()
    finally:
        settings.referral_auto_contact_enabled = original_referral

    action = await db_session.scalar(
        select(InboundDispositionAction).where(
            InboundDispositionAction.source_email_id == row.id
        )
    )
    outbox = await db_session.scalar(
        select(Outbox).where(Outbox.message_kind == "REFERRAL_OUTREACH")
    )
    assert action is not None and outbox is not None
    outbox.status = DeliveryStatus.SENT
    await db_session.commit()

    with pytest.raises(ValueError, match="IRREVERSIBLE"):
        await rollback_email_disposition(
            db_session,
            action_id=action.id,
            actor="admin:test",
            reason="Too late to retract",
        )


async def test_rollback_cancels_staged_outreach_and_removes_action_created_contact(
    db_session: AsyncSession,
) -> None:
    _, contact = await _seed_contact(
        db_session,
        company="Reversible Referral",
        name="Old Buyer",
        email="old@reversible-referral.example",
    )
    row = _email(
        contact=contact,
        subject="Automatic reply: Checking in",
        body=(
            "Old Buyer is no longer employed here. Please contact New Buyer at "
            "new@reversible-referral.example."
        ),
        token="k",
        auto=True,
    )
    db_session.add(row)
    await db_session.commit()
    settings = get_settings()
    original_referral = settings.referral_auto_contact_enabled
    settings.referral_auto_contact_enabled = True
    try:
        await apply_email_disposition(
            db_session,
            row,
            settings=settings,
            actor="admin:test",
            force_manual=True,
            allow_referral_outreach=True,
        )
        await db_session.commit()
    finally:
        settings.referral_auto_contact_enabled = original_referral

    action = await db_session.scalar(
        select(InboundDispositionAction).where(
            InboundDispositionAction.source_email_id == row.id
        )
    )
    outbox = await db_session.scalar(
        select(Outbox).where(Outbox.message_kind == "REFERRAL_OUTREACH")
    )
    new_contact = await db_session.scalar(
        select(Contact).where(Contact.email == "new@reversible-referral.example")
    )
    assert action is not None and outbox is not None and new_contact is not None
    new_contact_id = new_contact.id

    result = await rollback_email_disposition(
        db_session,
        action_id=action.id,
        actor="admin:test",
        reason="Referral was incorrect",
    )

    await db_session.refresh(contact)
    await db_session.refresh(outbox)
    assert result["removed_contact_ids"] == [new_contact_id]
    assert outbox.status is DeliveryStatus.CANCELLED
    assert contact.lifecycle_status == "ACTIVE"
    assert contact.suppressed is False
    assert await db_session.get(Contact, new_contact_id) is None
    assert await db_session.scalar(
        select(func.count())
        .select_from(ContactReferral)
        .where(ContactReferral.source_email_id == row.id)
    ) == 0
    assert await db_session.scalar(
        select(func.count())
        .select_from(EmailMessage)
        .where(EmailMessage.message_id == outbox.message_id)
    ) == 0


async def test_bulk_backfill_never_queues_referral_outreach(
    db_session: AsyncSession,
) -> None:
    _, contact = await _seed_contact(
        db_session,
        company="Backfill Referral",
        name="Old Buyer",
        email="old@backfill-referral.example",
    )
    row = _email(
        contact=contact,
        subject="Automatic reply: Checking in",
        body=(
            "Old Buyer is no longer employed here. Please contact New Buyer at "
            "new@backfill-referral.example."
        ),
        token="h",
        auto=True,
    )
    db_session.add(row)
    await db_session.commit()
    settings = get_settings()
    original_apply = settings.inbound_disposition_apply_enabled
    original_referral = settings.referral_auto_contact_enabled
    settings.inbound_disposition_apply_enabled = True
    settings.referral_auto_contact_enabled = True
    try:
        result = await backfill_inbound_dispositions(
            db_session,
            apply=True,
            settings=settings,
        )
    finally:
        settings.inbound_disposition_apply_enabled = original_apply
        settings.referral_auto_contact_enabled = original_referral

    assert result["applied_count"] == 1
    assert await db_session.scalar(
        select(func.count())
        .select_from(Outbox)
        .where(Outbox.message_kind == "REFERRAL_OUTREACH")
    ) == 0


async def test_rollback_restores_handoff_agent_and_notification_state(
    db_session: AsyncSession,
) -> None:
    customer, contact = await _seed_contact(
        db_session,
        company="Handoff Rollback",
        name="Logistics Contact",
        email="contact@handoff-rollback.example",
    )
    row = _email(
        contact=contact,
        subject="Re: Checking in",
        body="We are a freight forwarding company offering logistics services.",
        token="j",
    )
    db_session.add(row)
    await db_session.flush()
    handoff = Handoff(
        source_email_id=row.id,
        reason_code=HandoffReason.THREAD_AMBIGUOUS.value,
        summary="Review inbound role",
        extracted_facts={},
        status="OPEN",
        dingtalk_status="PENDING",
    )
    db_session.add(handoff)
    await db_session.flush()
    run = AgentRun(
        source_email_id=row.id,
        handoff_id=handoff.id,
        goal="Resolve inbound email",
        status=AgentRunStatus.WAITING_HUMAN,
        current_step="waiting-human",
        context_json={},
    )
    db_session.add(run)
    await db_session.flush()
    step = AgentStep(
        run_id=run.id,
        sequence=1,
        kind="HUMAN_ASSISTANCE",
        idempotency_key="wait-for-human",
        status=AgentStepStatus.WAITING,
        input_json={},
        output_json={},
    )
    request = AssistanceRequest(
        run_id=run.id,
        handoff_id=handoff.id,
        request_key="review-role",
        request_type="ROLE_REVIEW",
        question="Is this a target customer?",
        response_schema={},
        options_json=[],
        status=AssistanceStatus.OPEN,
    )
    job = Job(
        kind="notify_handoff",
        payload={"handoff_id": handoff.id},
        idempotency_key=f"handoff-notify:{handoff.id}",
        status=JobStatus.PENDING,
    )
    db_session.add_all([step, request, job])
    await db_session.commit()

    await apply_email_disposition(
        db_session,
        row,
        actor="admin:test",
        force_manual=True,
    )
    await db_session.commit()
    await db_session.refresh(handoff)
    await db_session.refresh(run)
    await db_session.refresh(step)
    await db_session.refresh(request)
    await db_session.refresh(job)
    assert handoff.status == "RESOLVED"
    assert run.status is AgentRunStatus.COMPLETED
    assert step.status is AgentStepStatus.CANCELLED
    assert request.status is AssistanceStatus.CANCELLED
    assert job.status is JobStatus.DONE

    action = await db_session.scalar(
        select(InboundDispositionAction).where(
            InboundDispositionAction.source_email_id == row.id
        )
    )
    assert action is not None
    await rollback_email_disposition(
        db_session,
        action_id=action.id,
        actor="admin:test",
        reason="Restore human review",
    )

    await db_session.refresh(customer)
    await db_session.refresh(handoff)
    await db_session.refresh(run)
    await db_session.refresh(step)
    await db_session.refresh(request)
    await db_session.refresh(job)
    assert customer.qualification_status == "UNKNOWN"
    assert handoff.status == "OPEN"
    assert run.status is AgentRunStatus.WAITING_HUMAN
    assert step.status is AgentStepStatus.WAITING
    assert step.completed_at is None
    assert request.status is AssistanceStatus.OPEN
    assert job.status is JobStatus.PENDING


async def test_human_personnel_message_never_suppresses_the_sender(
    db_session: AsyncSession,
) -> None:
    customer, contact = await _seed_contact(
        db_session,
        company="Human Personnel Notice",
        name="Judy",
        email="marketing@human-personnel.example",
    )
    case = SalesCase(customer_id=customer.id, contact_id=contact.id)
    db_session.add(case)
    await db_session.flush()
    row = _email(
        contact=contact,
        subject="Re: Checking in",
        body="Ms. Pooja no longer works here. Please send us your product list.",
        token="l",
    )
    row.case_id = case.id
    row.is_automated_reply = True
    row.automated_reply_type = "DEPARTED"
    row.disposition_type = "DEPARTED"
    row.disposition_metadata = {
        "automated_transport_signal": False,
        "product_list_requested": True,
    }
    db_session.add(row)
    await db_session.commit()

    handled = await _handle_automated_reply(
        db_session,
        case=case,
        email_row=row,
    )
    await db_session.commit()

    await db_session.refresh(contact)
    await db_session.refresh(row)
    assert handled is False
    assert contact.suppressed is False
    assert row.automated_reply_handled_at is None
    assert row.disposition_metadata["personnel_observation_recorded"] is True


async def test_blocked_automated_departure_creates_review_without_suppression(
    db_session: AsyncSession,
) -> None:
    customer, contact = await _seed_contact(
        db_session,
        company="Automated Personnel Notice",
        name="Former Buyer",
        email="former@automated-personnel.example",
    )
    case = SalesCase(customer_id=customer.id, contact_id=contact.id)
    db_session.add(case)
    await db_session.flush()
    row = _email(
        contact=contact,
        subject="Automatic reply: Checking in",
        body="Former Buyer is no longer employed here.",
        token="m",
        auto=True,
    )
    row.case_id = case.id
    row.automated_reply_type = "DEPARTED"
    row.disposition_type = "DEPARTED"
    row.disposition_metadata = {
        "automated_transport_signal": True,
        "replacement_emails": [],
    }
    db_session.add(row)
    await db_session.commit()

    handled = await _handle_automated_reply(
        db_session,
        case=case,
        email_row=row,
    )

    await db_session.refresh(contact)
    handoff = await db_session.scalar(
        select(Handoff).where(Handoff.source_email_id == row.id)
    )
    assert handled is True
    assert contact.suppressed is False
    assert handoff is not None and handoff.status == "OPEN"


async def test_admin_api_applies_reviewed_plan_and_rolls_it_back(
    db_session: AsyncSession,
) -> None:
    customer, contact = await _seed_contact(
        db_session,
        company="API Review Logistics",
        name="Provider",
        email="provider@api-review-logistics.example",
    )
    row = _email(
        contact=contact,
        subject="Re: Checking in",
        body="We are a logistics service provider offering freight forwarding.",
        token="p",
    )
    db_session.add(row)
    await db_session.commit()
    plan = await build_disposition_plan(db_session, row)
    settings = get_settings()
    original_apply = settings.inbound_disposition_apply_enabled
    settings.inbound_disposition_apply_enabled = False
    try:
        applied_plan = await inbound_disposition_apply(
            row.id,
            InboundDispositionApplyRequest(
                expected_disposition_type=plan["disposition_type"],
                expected_plan_token=plan["plan_token"],
                acknowledged_blockers=plan["blockers"],
                queue_referral_outreach=False,
            ),
            "reviewer",
            db_session,
            settings,
        )
    finally:
        settings.inbound_disposition_apply_enabled = original_apply

    await db_session.refresh(customer)
    assert customer.qualification_status == "NON_TARGET"
    action = applied_plan["latest_action"]
    assert action is not None and action["status"] == "APPLIED"
    assert action["applied_by"] == "admin:reviewer"

    result = await inbound_disposition_rollback(
        action["id"],
        InboundDispositionRollbackRequest(reason="Reviewer corrected the role"),
        "reviewer",
        db_session,
    )

    await db_session.refresh(customer)
    assert result["status"] == "ROLLED_BACK"
    assert customer.qualification_status == "UNKNOWN"
    assert result["plan"]["latest_action"]["status"] == "ROLLED_BACK"


async def test_admin_api_rejects_stale_plan_without_mutating_customer(
    db_session: AsyncSession,
) -> None:
    customer, contact = await _seed_contact(
        db_session,
        company="Stale API Plan",
        name="Provider",
        email="provider@stale-api-plan.example",
    )
    row = _email(
        contact=contact,
        subject="Re: Checking in",
        body="We are a freight forwarding company.",
        token="q",
    )
    db_session.add(row)
    await db_session.commit()
    plan = await build_disposition_plan(db_session, row)

    with pytest.raises(HTTPException) as raised:
        await inbound_disposition_apply(
            row.id,
            InboundDispositionApplyRequest(
                expected_disposition_type=plan["disposition_type"],
                expected_plan_token="0" * 64,
                acknowledged_blockers=plan["blockers"],
                queue_referral_outreach=False,
            ),
            "reviewer",
            db_session,
            get_settings(),
        )

    await db_session.rollback()
    await db_session.refresh(customer)
    assert raised.value.status_code == 409
    assert customer.qualification_status == "UNKNOWN"
