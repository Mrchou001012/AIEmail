from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime import answer_coa_lookup_assistance
from app.ai import InboundAnalysis
from app.coa_catalog import COACatalog, COACatalogScanner
from app.db import (
    AgentRun,
    AgentRunStatus,
    AssistanceRequest,
    AssistanceStatus,
    CaseStage,
    CaseStatus,
    Contact,
    Customer,
    EmailMessage,
    SalesCase,
)
from app.domain import HandoffReason, Intent
from app.services import create_handoff, queue_prepared_coa_reply, resume_agent_run
from app.settings import get_settings

pytestmark = pytest.mark.integration


async def test_coa_human_correction_resumes_to_verified_review_draft(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "PRODUCT DOCS"
    product_dir = root / "SILANES" / "YAC-TEST"
    product_dir.mkdir(parents=True)
    (product_dir / "COA-YAC-TEST customer.pdf").write_bytes(b"customer-specific")
    catalog_path = tmp_path / "runtime" / "coa_catalog.json"
    monkeypatch.setattr(
        "app.coa_catalog.extract_document_bounded",
        lambda path, timeout_seconds: "Product YAC-TEST",
    )
    scanner = COACatalogScanner(
        root=root,
        output_path=catalog_path,
        product_catalog_path=None,
    )
    scanner.scan()
    lookup = COACatalog(catalog_path).find("YAC-TEST")

    customer = Customer(
        company_name="COA Resume Test Customer",
        language="en",
        auto_send_allowed=True,
        consent_basis="test",
        metadata_json={},
    )
    db_session.add(customer)
    await db_session.flush()
    contact = Contact(
        customer_id=customer.id,
        name="Alice Buyer",
        email="coa-resume@example.com",
        language="en",
        metadata_json={},
    )
    db_session.add(contact)
    await db_session.flush()
    sales_case = SalesCase(
        customer_id=customer.id,
        contact_id=contact.id,
        product_id=None,
        category_id=None,
        currency="USD",
        stage=CaseStage.QUOTING,
        status=CaseStatus.ACTIVE,
        subject_key="coa-request",
    )
    db_session.add(sales_case)
    await db_session.flush()
    email = EmailMessage(
        case_id=sales_case.id,
        customer_id=customer.id,
        contact_id=contact.id,
        direction="INBOUND",
        message_id="<coa-resume@example.com>",
        references_json=[],
        from_address=contact.email,
        to_addresses=["sales@example.com"],
        subject="COA for YAC-TEST",
        body_text="Please send the COA for YAC-TEST.",
        attachment_metadata=[],
        raw_sha256="1" * 64,
    )
    db_session.add(email)
    await db_session.flush()
    analysis = InboundAnalysis(
        intent=Intent.COA_REQUEST,
        intent_confidence=0.99,
        coa_requested=True,
        requested_product_name="YAC-TEST",
        product_confidence=0.99,
        numeric_confidence=1.0,
    )
    handoff = await create_handoff(
        db_session,
        case=sales_case,
        reason=HandoffReason.COA_REVIEW,
        summary="No unique approved standard English COA was found",
        facts={
            **analysis.model_dump(mode="json"),
            "coa_query": "YAC-TEST",
            "coa_lookup": lookup.as_dict(),
        },
        source_email_id=email.id,
    )
    run = await db_session.scalar(select(AgentRun).where(AgentRun.handoff_id == handoff.id))
    assert run is not None
    request = await db_session.scalar(
        select(AssistanceRequest).where(AssistanceRequest.run_id == run.id)
    )
    assert request is not None
    assert request.request_type == "COA_LOOKUP_CORRECTION"
    assert request.status == AssistanceStatus.OPEN

    (product_dir / "COA-YAC-TEST.pdf").write_bytes(b"approved-standard")
    scanner.scan()
    settings = get_settings()
    monkeypatch.setattr(settings, "coa_catalog_enabled", True)
    monkeypatch.setattr(settings, "coa_catalog_path", catalog_path)
    monkeypatch.setattr(settings, "coa_auto_send_enabled", False)

    answer = await answer_coa_lookup_assistance(
        db_session,
        request_id=request.id,
        product_query="YAC-TEST",
        cas_number=None,
        actor="reviewer",
        note="Standard file added to NAS",
    )
    assert answer.newly_answered is True
    assert answer.job is not None
    await resume_agent_run(
        db_session,
        run_id=run.id,
        expected_version=run.version,
        assistance_request_id=request.id,
    )

    await db_session.refresh(handoff)
    await db_session.refresh(run)
    await db_session.refresh(request)
    assert handoff.status == "OPEN"
    assert handoff.reason_code == HandoffReason.COA_REVIEW.value
    assert handoff.extracted_facts["prepared_coa"]["path"].endswith(
        "YAC-TEST/COA-YAC-TEST.pdf"
    )
    assert request.status == AssistanceStatus.APPLIED
    assert run.status == AgentRunStatus.WAITING_HUMAN
    assert run.current_step == "approve-coa-draft"

    preview = handoff.extracted_facts["ai_draft_preview"]
    outbox = await queue_prepared_coa_reply(
        db_session,
        handoff_id=handoff.id,
        subject=preview["subject"],
        body_text=preview["body_text"],
        actor="reviewer",
        note="Verified standard COA",
    )
    repeated = await queue_prepared_coa_reply(
        db_session,
        handoff_id=handoff.id,
        subject=preview["subject"],
        body_text=preview["body_text"],
        actor="reviewer",
    )
    assert repeated.id == outbox.id
    assert outbox.approval_handoff_id == handoff.id
    assert outbox.human_approved_by == "reviewer"
    await db_session.refresh(handoff)
    await db_session.refresh(run)
    assert handoff.status == "RESOLVED"
    assert run.status == AgentRunStatus.COMPLETED
