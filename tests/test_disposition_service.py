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

from app.ai import InboundDispositionDecision, inbound_disposition_message_params
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
    ReactivationCampaign,
    ReactivationRecipient,
    SalesCase,
)
from app.disposition_service import (
    _parse_return_until,
    apply_email_disposition,
    backfill_inbound_dispositions,
    build_disposition_plan,
    classify_email_disposition,
    rollback_email_disposition,
)
from app.domain import HandoffReason
from app.inbound_disposition import InboundDisposition, InboundDispositionType
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


class _HashAwareDispositionAI:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.calls = 0

    async def classify_inbound_disposition(self, **kwargs: object):
        self.calls += 1
        _, request_hash = inbound_disposition_message_params(
            settings=self.settings,
            subject=str(kwargs["subject"]),
            body=str(kwargs["body"]),
            sender=str(kwargs["sender"]),
            headers=kwargs.get("headers"),  # type: ignore[arg-type]
        )
        return InboundDispositionDecision(
            disposition_type="BUSINESS",
            confidence=0.91,
            reason="The sender is continuing a business conversation.",
            evidence=["Thank you for following up"],
            replacement_emails=[],
            return_hint=None,
            forwarded_to_replacement=False,
            non_target_reason=None,
            product_list_requested=False,
        ), {
            "provider": "anthropic",
            "model": self.settings.anthropic_model,
            "request_hash": request_hash,
            "request_id": f"req_{self.calls}",
        }


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


@pytest.mark.parametrize(
    ("return_hint", "expected"),
    [
        ("July 30th, 2026", datetime(2026, 7, 31, tzinfo=UTC)),
        ("3rd to 21st August", datetime(2026, 8, 22, tzinfo=UTC)),
        ("6th of August", datetime(2026, 8, 7, tzinfo=UTC)),
    ],
)
def test_parse_return_until_accepts_real_autoreply_date_formats(
    return_hint: str,
    expected: datetime,
) -> None:
    assert _parse_return_until(
        return_hint,
        received_at=datetime(2026, 7, 30, tzinfo=UTC),
    ) == expected


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


async def test_live_processing_reuses_fresh_stored_ai_classification(
    db_session: AsyncSession,
) -> None:
    _, contact = await _seed_contact(
        db_session,
        company="Stored AI Result",
        name="Buyer",
        email="buyer@stored-ai.example",
    )
    row = _email(
        contact=contact,
        subject="Re: Checking in",
        body="Thank you for following up. We will review our requirements.",
        token="w",
    )
    db_session.add(row)
    await db_session.commit()
    settings = Settings(
        _env_file=None,
        ai_provider="anthropic",
        anthropic_api_key="test-only",
        inbound_disposition_ai_enabled=True,
        inbound_disposition_apply_enabled=False,
    )
    client = _HashAwareDispositionAI(settings)

    assert not await apply_email_disposition(
        db_session,
        row,
        settings=settings,
        disposition=await classify_email_disposition(
            row,
            settings=settings,
            ai_client=client,  # type: ignore[arg-type]
        ),
    )
    assert not await apply_email_disposition(
        db_session,
        row,
        settings=settings,
        disposition=await classify_email_disposition(
            row,
            settings=settings,
            ai_client=client,  # type: ignore[arg-type]
        ),
    )

    assert client.calls == 1


async def test_absence_is_primary_and_keeps_ai_referral(
    db_session: AsyncSession,
) -> None:
    _, contact = await _seed_contact(
        db_session,
        company="Temporary Backup",
        name="Pam",
        email="pam@temporary-backup.example",
    )
    row = _email(
        contact=contact,
        subject="Automatic reply: Checking in",
        body=(
            "I am currently on a leave of absence. Please contact Jared Straley "
            "at jared@temporary-backup.example for help in directing your inquiry."
        ),
        token="v",
        auto=True,
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
                disposition_type="CONTACT_REFERRAL",
                confidence=0.95,
                reason="The message directs correspondence to Jared.",
                evidence=["Please contact Jared Straley"],
                replacement_emails=["jared@temporary-backup.example"],
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
        disposition=disposition,
    )

    assert disposition.disposition_type is InboundDispositionType.TEMPORARY_ABSENCE
    assert disposition.replacement_emails == ("jared@temporary-backup.example",)
    assert disposition.normalization_notes == (
        "PRIMARY_CATEGORY_NORMALIZED:CONTACT_REFERRAL->TEMPORARY_ABSENCE",
    )
    assert "SAVE_REFERRALS" in plan["proposed_actions"]


async def test_referral_without_valid_address_becomes_uncertain(
    db_session: AsyncSession,
) -> None:
    _, contact = await _seed_contact(
        db_session,
        company="No Referral Address",
        name="Buyer",
        email="buyer@no-referral.example",
    )
    row = _email(
        contact=contact,
        subject="Re: Checking in",
        body="Please speak with our procurement department going forward.",
        token="u",
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
                disposition_type="CONTACT_REFERRAL",
                confidence=0.94,
                reason="The message recommends another department.",
                evidence=["procurement department"],
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
        disposition=disposition,
    )

    assert disposition.disposition_type is InboundDispositionType.UNCERTAIN
    assert "CONTACT_REFERRAL_WITHOUT_VALID_EMAIL" in disposition.normalization_notes
    assert plan["blockers"] == ["AI_CLASSIFICATION_UNCERTAIN"]


async def test_explicit_supplier_offer_overrides_ai_business_label(
    db_session: AsyncSession,
) -> None:
    _, contact = await _seed_contact(
        db_session,
        company="Supplier Offer",
        name="Supplier",
        email="supplier@supplier-offer.example",
    )
    row = _email(
        contact=contact,
        subject="Re: HMDS",
        body=(
            "The updated price of this week could be CIF Nhava Sheva, India "
            "USD5.40/kg, may I ask is it workable for you?"
        ),
        token="t",
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
                disposition_type="BUSINESS",
                confidence=0.95,
                reason="This is a business quotation.",
                evidence=["updated price of this week"],
                replacement_emails=[],
                return_hint=None,
                forwarded_to_replacement=False,
                non_target_reason=None,
                product_list_requested=False,
            )
        ),  # type: ignore[arg-type]
    )

    assert disposition.disposition_type is InboundDispositionType.NON_TARGET
    assert disposition.non_target_reason == "SUPPLIER_VENDOR"
    assert disposition.normalization_notes == (
        "PRIMARY_CATEGORY_NORMALIZED:BUSINESS->NON_TARGET",
    )


async def test_internal_sender_overrides_ai_business_label(
    db_session: AsyncSession,
) -> None:
    _, contact = await _seed_contact(
        db_session,
        company="Internal Sender",
        name="Internal Test",
        email="zhoulei@lanyachem.com",
    )
    row = _email(
        contact=contact,
        subject="Inquiry for YAC-TEOS40",
        body="Please quote 1000 kg YAC-TEOS40 instead.",
        token="q",
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
                disposition_type="BUSINESS",
                confidence=0.95,
                reason="This is a quote request.",
            )
        ),  # type: ignore[arg-type]
    )

    assert disposition.disposition_type is InboundDispositionType.UNCERTAIN
    assert "INTERNAL_SENDER_REQUIRES_REVIEW" in disposition.normalization_notes


async def test_explicit_business_request_overrides_ai_non_target_label(
    db_session: AsyncSession,
) -> None:
    _, contact = await _seed_contact(
        db_session,
        company="Explicit Buyer",
        name="Buyer",
        email="buyer@explicit-buyer.example",
    )
    row = _email(
        contact=contact,
        subject="Re: Checking in from Lanya Chem",
        body="Give me current best rate.",
        token="r",
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
                disposition_type="NON_TARGET",
                confidence=0.92,
                reason="The sender may be offering services.",
                non_target_reason="SUPPLIER_VENDOR",
            )
        ),  # type: ignore[arg-type]
    )

    assert disposition.disposition_type is InboundDispositionType.BUSINESS
    assert disposition.non_target_reason is None
    assert "EXPLICIT_BUSINESS_REQUEST" in disposition.normalization_notes


async def test_product_list_request_overrides_uncorroborated_ai_non_target(
    db_session: AsyncSession,
) -> None:
    _, contact = await _seed_contact(
        db_session,
        company="Sourcing Agent With Inquiry",
        name="Buyer",
        email="ingredients@sourcing-agent.example",
    )
    row = _email(
        contact=contact,
        subject="Request for Product List",
        body=(
            "We are an international marketing and sourcing agent for raw materials. "
            "Please share your complete product catalogue and let us know if you can "
            "offer the products on our enquiry list.\n\n"
            "Regards\nfloradye@sourcing-agent.example"
        ),
        token="q",
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
                disposition_type="NON_TARGET",
                confidence=0.92,
                reason="The sender describes a sourcing-agent role.",
                evidence=["international marketing and sourcing agent"],
                replacement_emails=["floradye@sourcing-agent.example"],
                return_hint=None,
                forwarded_to_replacement=False,
                non_target_reason="OTHER",
                product_list_requested=True,
            )
        ),  # type: ignore[arg-type]
    )
    plan = await build_disposition_plan(db_session, row, disposition=disposition)

    assert disposition.disposition_type is InboundDispositionType.BUSINESS
    assert disposition.product_list_requested is True
    assert disposition.continue_business_processing is True
    assert disposition.non_target_reason is None
    assert disposition.replacement_emails == ()
    assert disposition.normalization_notes == (
        "UNVERIFIED_NON_TARGET_WITH_PRODUCT_REQUEST->BUSINESS",
        "NON_ACTIONABLE_REPLACEMENT_EMAILS_DROPPED",
    )
    assert plan["proposed_actions"] == ["CONTINUE_BUSINESS_PIPELINE"]
    assert plan["blockers"] == []


@pytest.mark.parametrize("ai_type", ["CONTACT_REFERRAL", "NON_TARGET"])
async def test_identity_mismatch_stops_linked_case_and_creates_review(
    db_session: AsyncSession,
    ai_type: str,
) -> None:
    customer, contact = await _seed_contact(
        db_session,
        company="Identity Review",
        name="Michel",
        email="excel@identity-review.example",
    )
    case = SalesCase(customer_id=customer.id, contact_id=contact.id)
    db_session.add(case)
    await db_session.flush()
    row = _email(
        contact=contact,
        subject="RE: Checking in",
        body="THERE IS NO MICHEL IN OUR COMPANY. Please be aware.",
        token="s" if ai_type == "CONTACT_REFERRAL" else "r",
    )
    row.case_id = case.id
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
                disposition_type=ai_type,  # type: ignore[arg-type]
                confidence=0.95,
                reason="Ambiguous model label.",
                evidence=["THERE IS NO MICHEL IN OUR COMPANY"],
                replacement_emails=[],
                return_hint=None,
                forwarded_to_replacement=False,
                non_target_reason="OTHER" if ai_type == "NON_TARGET" else None,
                product_list_requested=False,
            )
        ),  # type: ignore[arg-type]
    )

    handled = await apply_email_disposition(
        db_session,
        row,
        settings=Settings(
            _env_file=None,
            inbound_disposition_apply_enabled=False,
        ),
        disposition=disposition,
    )
    await db_session.commit()

    handoff = await db_session.scalar(
        select(Handoff).where(Handoff.source_email_id == row.id)
    )
    notify_job = await db_session.scalar(
        select(Job).where(Job.idempotency_key == f"handoff-notify:{handoff.id}")
    ) if handoff else None
    assert handled is True
    assert disposition.disposition_type is InboundDispositionType.CONTACT_IDENTITY_MISMATCH
    assert customer.qualification_status == "UNKNOWN"
    assert contact.suppressed is False
    assert handoff is not None and handoff.status == "OPEN"
    assert notify_job is not None and notify_job.status is JobStatus.PENDING
    assert await db_session.scalar(
        select(func.count()).select_from(InboundDispositionAction)
    ) == 0


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


async def test_unresolved_non_target_has_no_apply_action(
    db_session: AsyncSession,
) -> None:
    row = EmailMessage(
        direction="INBOUND",
        from_address="marketing@unresolved-supplier.example",
        to_addresses=["sales@lanyachem.com"],
        subject="PRODUCT OFFER",
        body_text="Presently we can offer this material at USD 3700/mt.",
        attachment_metadata=[],
        raw_sha256="j" * 64,
        is_history=False,
        is_automated_reply=False,
        automated_reply_metadata={},
        received_at=datetime(2026, 8, 4, 8, tzinfo=UTC),
    )
    db_session.add(row)
    await db_session.commit()

    plan = await build_disposition_plan(db_session, row)

    assert plan["disposition_type"] == "NON_TARGET"
    assert plan["can_apply"] is False
    assert plan["application_blockers"] == ["CUSTOMER_NOT_RESOLVED"]


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
        assert await apply_email_disposition(
            db_session,
            row,
            settings=settings,
            at=datetime(2026, 8, 10, tzinfo=UTC),
        ) is True
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


async def test_expired_absence_is_recorded_without_pausing_contact(
    db_session: AsyncSession,
) -> None:
    _, contact = await _seed_contact(
        db_session,
        company="Expired Leave Test",
        name="Buyer",
        email="buyer@expired-leave.example",
    )
    row = _email(
        contact=contact,
        subject="Automatic reply: Checking in",
        body=(
            "Our offices are closed from 3rd to 21st August. For urgent matters, "
            "please contact backup@expired-leave.example."
        ),
        token="d",
        auto=True,
    )
    db_session.add(row)
    await db_session.commit()
    observed_at = datetime(2026, 9, 3, tzinfo=UTC)

    plan = await build_disposition_plan(db_session, row, at=observed_at)
    assert plan["absence_already_ended"] is True
    assert plan["proposed_actions"] == [
        "IGNORE_AUTOREPLY",
        "RECORD_EXPIRED_ABSENCE",
        "SAVE_REFERRALS",
    ]

    settings = get_settings()
    original = settings.inbound_disposition_apply_enabled
    settings.inbound_disposition_apply_enabled = True
    try:
        assert await apply_email_disposition(
            db_session,
            row,
            settings=settings,
            at=observed_at,
        ) is True
        await db_session.commit()
    finally:
        settings.inbound_disposition_apply_enabled = original

    await db_session.refresh(contact)
    await db_session.refresh(row)
    assert contact.lifecycle_status == "ACTIVE"
    assert contact.unavailable_until is None
    assert row.disposition_handled_at is not None
    assert row.disposition_metadata["applied_actions"] == [
        "IGNORE_AUTOREPLY",
        "RECORD_EXPIRED_ABSENCE",
        "SAVE_REFERRALS",
    ]
    referral = await db_session.scalar(
        select(ContactReferral).where(ContactReferral.source_email_id == row.id)
    )
    assert referral is not None
    assert referral.referred_email == "backup@expired-leave.example"

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
        reason="Expired absence rollback test",
    )
    await db_session.refresh(contact)
    await db_session.refresh(row)
    assert contact.lifecycle_status == "ACTIVE"
    assert row.disposition_handled_at is None
    assert await db_session.scalar(
        select(func.count())
        .select_from(ContactReferral)
        .where(ContactReferral.source_email_id == row.id)
    ) == 0


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


async def test_cross_domain_forward_resolves_exact_reactivation_parent_for_review(
    db_session: AsyncSession,
) -> None:
    customer, original = await _seed_contact(
        db_session,
        company="Resinova (Now Astral Adhesives)",
        name="Original Buyer",
        email="purchase@resinova.example",
    )
    campaign = ReactivationCampaign(
        name="Cross-domain parent test",
        status="RUNNING",
        subject_template="Checking in",
        body_template="Hello",
        created_by="test",
    )
    db_session.add(campaign)
    await db_session.flush()
    sent_at = datetime(2026, 7, 30, 6, tzinfo=UTC)
    parent = Outbox(
        message_kind="REACTIVATION",
        business_key="reactivation:cross-domain-test",
        message_id="<cross-domain-parent@lanyachem.example>",
        recipient=original.email,
        raw_message="parent",
        status=DeliveryStatus.SENT,
        sent_at=sent_at,
    )
    db_session.add(parent)
    await db_session.flush()
    db_session.add(
        ReactivationRecipient(
            campaign_id=campaign.id,
            customer_id=customer.id,
            contact_id=original.id,
            outbox_id=parent.id,
            status="SENT",
            eligible=True,
            selected=True,
            sent_at=sent_at,
        )
    )
    row = EmailMessage(
        direction="INBOUND",
        message_id="<cross-domain-reply@astral.example>",
        in_reply_to=parent.message_id,
        references_json=[parent.message_id],
        from_address="rishi@astral.example",
        to_addresses=["sales@lanyachem.com", "manthan@astral.example"],
        subject="RE: Checking in",
        body_text=(
            "I have marked copy to Manthan in this communication. "
            "He will revert to you."
        ),
        attachment_metadata=[],
        raw_sha256="6" * 64,
        is_history=False,
        is_automated_reply=False,
        automated_reply_metadata={},
        received_at=datetime(2026, 7, 31, 4, tzinfo=UTC),
    )
    db_session.add(row)
    await db_session.commit()
    settings = Settings(
        _env_file=None,
        ai_provider="anthropic",
        anthropic_api_key="test-only",
        inbound_disposition_ai_enabled=True,
        inbound_disposition_apply_enabled=True,
    )
    disposition = await classify_email_disposition(
        row,
        settings=settings,
        ai_client=_DispositionAI(
            InboundDispositionDecision(
                disposition_type="CONTACT_REFERRAL",
                confidence=0.97,
                reason="The sender copied a colleague who will handle the inquiry.",
                evidence=["I have marked copy to Manthan"],
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
        settings=settings,
        disposition=disposition,
    )
    assert plan["disposition_type"] == "FORWARDED_TO_COLLEAGUE"
    assert plan["customer_id"] == customer.id
    assert plan["customer_name"] == "Resinova (Now Astral Adhesives)"
    assert plan["contact_id"] == original.id
    assert plan["contact_email"] == "purchase@resinova.example"
    assert plan["sender_contact_id"] is None
    assert plan["contact_resolution_source"] == "EXACT_REACTIVATION_PARENT"
    assert plan["reactivation_parent_message_id"] == parent.message_id
    assert plan["replacement_emails"] == ["manthan@astral.example"]
    assert plan["referral_candidates"][0]["same_company_domain"] is True
    assert plan["blockers"] == [
        "CROSS_DOMAIN_REACTIVATION_PARENT_REQUIRES_REVIEW"
    ]
    assert plan["can_apply"] is True

    assert await apply_email_disposition(
        db_session,
        row,
        settings=settings,
        disposition=disposition,
    ) is False
    assert row.disposition_handled_at is None
    assert await db_session.scalar(
        select(func.count())
        .select_from(ContactReferral)
        .where(ContactReferral.source_email_id == row.id)
    ) == 0

    assert await apply_email_disposition(
        db_session,
        row,
        settings=settings,
        actor="admin:test",
        force_manual=True,
        allow_referral_outreach=False,
        disposition=disposition,
    ) is True
    await db_session.commit()

    referral = await db_session.scalar(
        select(ContactReferral).where(ContactReferral.source_email_id == row.id)
    )
    assert referral is not None
    assert referral.customer_id == customer.id
    assert referral.original_contact_id == original.id
    assert referral.referred_email == "manthan@astral.example"
    assert referral.forwarded_already is True
    assert referral.status == "WAITING_FOR_FORWARDED_REPLY"
    assert await db_session.scalar(
        select(func.count())
        .select_from(Outbox)
        .where(Outbox.message_kind == "REFERRAL_OUTREACH")
    ) == 0

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
        reason="Cross-domain referral rollback test",
    )
    assert await db_session.scalar(
        select(func.count())
        .select_from(ContactReferral)
        .where(ContactReferral.source_email_id == row.id)
    ) == 0
    await db_session.refresh(original)
    assert original.lifecycle_status == "ACTIVE"
    assert original.suppressed is False


async def test_quoted_parent_recovers_departed_contact_and_continues_product_list(
    db_session: AsyncSession,
) -> None:
    customer, original = await _seed_contact(
        db_session,
        company="Shanghai Witofly Chemical Co.,Ltd",
        name="Pooja Raut",
        email="globalsourcing@witofly.com",
    )
    outbound_body = (
        "Dear Pooja Raut,\n\n"
        "I hope you are doing well. It has been some time since we last spoke. "
        "I am writing to reconnect and ask whether you currently have any "
        "requirements for our products. If useful, please send the product and "
        "quantity you need. We can then confirm availability and price."
    )
    outbound = EmailMessage(
        customer_id=customer.id,
        contact_id=original.id,
        direction="OUTBOUND",
        message_id="<quoted-parent@lanyachem.com>",
        from_address="shreyasaxena@lanyachemindia.com",
        to_addresses=[original.email],
        subject="Checking in from Lanya Chem",
        body_text=outbound_body,
        attachment_metadata=[],
        raw_sha256="v" * 64,
        is_history=False,
        is_automated_reply=False,
        automated_reply_metadata={},
        received_at=datetime(2026, 8, 3, 9, tzinfo=UTC),
    )
    db_session.add(outbound)
    await db_session.flush()
    inbound = EmailMessage(
        direction="INBOUND",
        message_id="<reply-without-thread-headers@witofly.com>",
        from_address="marketing001@witofly.com",
        to_addresses=["shreyasaxena@lanyachemindia.com"],
        subject="Checking in from Lanya Chem",
        body_text=(
            "Dear Shreya,\n\n"
            "Ms. Pooja no longer works in our company. Please send us your "
            "product list. It would be better to mark your gold products.\n\n"
            "Thanks and Best regards\nJudy\nJudy Ao\n"
            "Shanghai Witofly Chemical Co.,Ltd\n\n"
            "From: shreyasaxena@lanyachemindia.com\n"
            "Date: August 3, 2026\n"
            "To: globalsourcing@witofly.com\n"
            "Subject: Checking in from Lanya Chem\n\n"
            f"{outbound_body}"
        ),
        attachment_metadata=[],
        raw_sha256="w" * 64,
        is_history=False,
        is_automated_reply=False,
        automated_reply_metadata={},
        received_at=datetime(2026, 8, 4, 2, tzinfo=UTC),
    )
    db_session.add(inbound)
    await db_session.flush()
    handoff = Handoff(
        source_email_id=inbound.id,
        reason_code=HandoffReason.THREAD_AMBIGUOUS.value,
        summary="Thread could not be linked",
        extracted_facts={},
    )
    db_session.add(handoff)
    await db_session.commit()

    disposition = InboundDisposition(
        disposition_type=InboundDispositionType.DEPARTED,
        confidence=0.99,
        reason="sender or referenced employee is no longer with the company",
        authored_text=(
            "Ms. Pooja no longer works in our company. "
            "Please send us your product list.\n\nThanks and Best regards\nJudy\nJudy Ao"
        ),
        product_list_requested=True,
        classifier_source="anthropic",
        classifier_model="claude-test",
        evidence=("Ms. Pooja no longer works in our company",),
    )
    settings = Settings(
        _env_file=None,
        inbound_disposition_enabled=True,
        inbound_disposition_apply_enabled=True,
    )
    plan = await build_disposition_plan(
        db_session,
        inbound,
        settings=settings,
        disposition=disposition,
    )

    assert plan["customer_id"] == customer.id
    assert plan["contact_id"] == original.id
    assert plan["contact_resolution_source"] == "QUOTED_OUTBOUND_PARENT"
    assert plan["parent_email_id"] == outbound.id
    assert plan["reply_contact_candidate_email"] == "marketing001@witofly.com"
    assert plan["reply_contact_candidate_name"] == "Judy"
    assert plan["application_blockers"] == []
    assert "QUOTED_PARENT_REQUIRES_REVIEW" in plan["blockers"]
    assert "CREATE_REPLY_CONTACT" in plan["proposed_actions"]
    assert "CONTINUE_BUSINESS_PIPELINE" in plan["proposed_actions"]

    # The recovered relationship is never applied without explicit review.
    assert await apply_email_disposition(
        db_session,
        inbound,
        settings=settings,
        disposition=disposition,
    ) is False
    assert original.lifecycle_status == "ACTIVE"

    assert await apply_email_disposition(
        db_session,
        inbound,
        settings=settings,
        actor="admin:test",
        force_manual=True,
        allow_referral_outreach=False,
        disposition=disposition,
    ) is False
    await db_session.commit()

    await db_session.refresh(original)
    await db_session.refresh(inbound)
    await db_session.refresh(handoff)
    reply_contact = await db_session.scalar(
        select(Contact).where(Contact.email == "marketing001@witofly.com")
    )
    sales_case = await db_session.get(SalesCase, inbound.case_id)
    referral = await db_session.scalar(
        select(ContactReferral).where(ContactReferral.source_email_id == inbound.id)
    )
    action = await db_session.scalar(
        select(InboundDispositionAction).where(
            InboundDispositionAction.source_email_id == inbound.id
        )
    )
    assert original.lifecycle_status == "DEPARTED"
    assert original.suppressed is True
    assert reply_contact is not None
    assert reply_contact.name == "Judy"
    assert reply_contact.customer_id == customer.id
    assert inbound.customer_id == customer.id
    assert inbound.contact_id == reply_contact.id
    assert sales_case is not None
    assert sales_case.contact_id == reply_contact.id
    assert sales_case.status.value == "WAITING_HUMAN"
    assert referral is not None
    assert referral.new_contact_id == reply_contact.id
    assert referral.status == "ACTIVE_CONTACT"
    assert handoff.case_id == sales_case.id
    assert handoff.reason_code == HandoffReason.PRODUCT_LIST_REVIEW.value
    assert handoff.status == "OPEN"
    assert await db_session.scalar(
        select(func.count()).select_from(Outbox)
    ) == 0
    assert action is not None

    await rollback_email_disposition(
        db_session,
        action_id=action.id,
        actor="admin:test",
        reason="Quoted-parent mixed reply rollback test",
    )
    await db_session.commit()

    await db_session.refresh(original)
    await db_session.refresh(inbound)
    await db_session.refresh(handoff)
    assert original.lifecycle_status == "ACTIVE"
    assert original.suppressed is False
    assert inbound.customer_id is None
    assert inbound.contact_id is None
    assert inbound.case_id is None
    assert handoff.case_id is None
    assert handoff.reason_code == HandoffReason.THREAD_AMBIGUOUS.value
    assert await db_session.scalar(
        select(func.count())
        .select_from(Contact)
        .where(Contact.email == "marketing001@witofly.com")
    ) == 0
    assert await db_session.scalar(
        select(func.count())
        .select_from(ContactReferral)
        .where(ContactReferral.source_email_id == inbound.id)
    ) == 0
    assert await db_session.scalar(
        select(func.count())
        .select_from(SalesCase)
        .where(SalesCase.customer_id == customer.id)
    ) == 0


async def test_copied_same_domain_recipient_is_audited_without_duplicate_outreach(
    db_session: AsyncSession,
) -> None:
    _, contact = await _seed_contact(
        db_session,
        company="Copied Colleague",
        name="Rishi",
        email="rishi.shukla@astralltd.com",
    )
    row = _email(
        contact=contact,
        subject="RE: Checking in from Lanya Chem",
        body=(
            "Mr Girish Dalal has already retired and Mr Manthan Parmar is now "
            "your contact point. I have marked copy to Manthan in this "
            "communication. Please get in touch with him."
        ),
        token="p",
    )
    row.to_addresses = [
        "sales@lanyachem.com",
        "manthan.parmar@astralltd.com",
    ]
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
                disposition_type="CONTACT_REFERRAL",
                confidence=0.96,
                reason="The sender identifies Manthan as the new contact.",
                evidence=["I have marked copy to Manthan in this communication"],
                replacement_emails=[],
                return_hint=None,
                forwarded_to_replacement=False,
                non_target_reason=None,
                product_list_requested=False,
            )
        ),  # type: ignore[arg-type]
    )
    plan = await build_disposition_plan(db_session, row, disposition=disposition)

    assert disposition.disposition_type is InboundDispositionType.FORWARDED_TO_COLLEAGUE
    assert disposition.forwarded_to_replacement is True
    assert disposition.replacement_emails == ("manthan.parmar@astralltd.com",)
    assert (
        "REPLACEMENT_FROM_RECIPIENT_HEADER:manthan.parmar@astralltd.com"
        in disposition.normalization_notes
    )
    assert plan["blockers"] == []
    assert "WAIT_FOR_FORWARDED_REPLY" in plan["proposed_actions"]

    assert await apply_email_disposition(
        db_session,
        row,
        actor="admin:test",
        force_manual=True,
        allow_referral_outreach=True,
        disposition=disposition,
    ) is True
    await db_session.commit()

    referral = await db_session.scalar(
        select(ContactReferral).where(ContactReferral.source_email_id == row.id)
    )
    assert referral is not None
    assert referral.referred_email == "manthan.parmar@astralltd.com"
    assert referral.forwarded_already is True
    assert referral.metadata_json["source_location"] == "recipient_header"
    assert await db_session.scalar(
        select(func.count())
        .select_from(Outbox)
        .where(Outbox.message_kind == "REFERRAL_OUTREACH")
    ) == 0


async def test_multiple_referral_addresses_have_canonical_order(
    db_session: AsyncSession,
) -> None:
    _, contact = await _seed_contact(
        db_session,
        company="Canonical Referrals",
        name="Former Buyer",
        email="former@lbbspecialties.com",
    )
    row = _email(
        contact=contact,
        subject="Automatic reply: Checking in from Lanya Chem",
        body=(
            "The person you are trying to reach is no longer employed. "
            "Please send your order to orders@charkit.com. If you need assistance, "
            "please send your email to rclinger@lbbspecialties.com or "
            "pyannopoulos@lbbspecialties.com."
        ),
        token="s",
        auto=True,
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
                confidence=0.99,
                reason="The original contact has departed.",
                replacement_emails=[
                    "rclinger@lbbspecialties.com",
                    "orders@charkit.com",
                    "pyannopoulos@lbbspecialties.com",
                ],
            )
        ),  # type: ignore[arg-type]
    )

    assert disposition.replacement_emails == (
        "orders@charkit.com",
        "pyannopoulos@lbbspecialties.com",
        "rclinger@lbbspecialties.com",
    )


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
    plan = await build_disposition_plan(db_session, row)
    assert "RETURN_DATE_NOT_RELIABLE" in plan["blockers"]
    assert plan["can_apply"] is True
    assert plan["application_blockers"] == []

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
    assert plan["can_apply"] is True
    assert plan["application_blockers"] == []
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
