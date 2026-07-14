import secrets
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import (
    AIInvocation,
    AuditEvent,
    CaseStatus,
    Customer,
    DeliveryStatus,
    EmailMessage,
    Handoff,
    Job,
    MailboxCursor,
    Outbox,
    Product,
    Quote,
    SalesCase,
    db_health,
    get_session,
)
from app.history import reconcile_email_history
from app.imports import generate_templates, import_customers, import_prices
from app.services import active_policy, enqueue_job, ingest_raw_email, seed_demo_data
from app.settings import Settings, get_settings

router = APIRouter()
security = HTTPBasic()
DASHBOARD_PATH = Path(__file__).with_name("dashboard.html")


def require_admin(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    valid_user = secrets.compare_digest(credentials.username, settings.admin_username)
    valid_password = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (valid_user and valid_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid administration credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


Admin = Annotated[str, Depends(require_admin)]
Session = Annotated[AsyncSession, Depends(get_session)]


def require_demo_mode(settings: Annotated[Settings, Depends(get_settings)]) -> None:
    if not settings.demo_mode:
        raise HTTPException(status_code=404, detail="Demo endpoints are disabled")


DemoMode = Annotated[None, Depends(require_demo_mode)]


class DemoOutreachRequest(BaseModel):
    recipient: EmailStr = "internal@example.com"
    quantity: int = Field(default=100, ge=1, le=1_000_000)


class CaseOutreachRequest(BaseModel):
    quantity: int = Field(ge=1, le=1_000_000)


class HandoffUpdate(BaseModel):
    action: str
    note: str = ""


def _dashboard_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }


@router.get("/health")
async def health() -> JSONResponse:
    database_ok = await db_health()
    return JSONResponse(
        status_code=status.HTTP_200_OK if database_ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "ok" if database_ok else "degraded", "database": database_ok},
    )


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(_: Admin) -> HTMLResponse:
    return HTMLResponse(DASHBOARD_PATH.read_text(encoding="utf-8"), headers=_dashboard_headers())


@router.get("/admin/dashboard/data")
async def dashboard_data(
    _: Admin,
    session: Session,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    now = datetime.now(UTC)
    since_day = now - timedelta(hours=24)

    inbound_24h = await session.scalar(
        select(func.count()).select_from(EmailMessage).where(
            EmailMessage.direction == "INBOUND",
            EmailMessage.received_at >= since_day,
        )
    )
    sent_24h = await session.scalar(
        select(func.count()).select_from(Outbox).where(Outbox.sent_at >= since_day)
    )
    pending_outbox = await session.scalar(
        select(func.count()).select_from(Outbox).where(
            Outbox.status.in_([DeliveryStatus.PENDING, DeliveryStatus.CLAIMED, DeliveryStatus.FAILED])
        )
    )
    open_handoffs = await session.scalar(
        select(func.count()).select_from(Handoff).where(Handoff.status == "OPEN")
    )
    failed_jobs = await session.scalar(
        select(func.count()).select_from(Job).where(Job.status == "FAILED")
    )
    unmatched_history = await session.scalar(
        select(func.count()).select_from(EmailMessage).where(
            EmailMessage.is_history.is_(True),
            EmailMessage.case_id.is_(None),
        )
    )

    case_status_counts = await session.execute(
        select(SalesCase.status, func.count()).group_by(SalesCase.status)
    )
    email_rows = (
        (
            await session.execute(
                select(EmailMessage).order_by(EmailMessage.received_at.desc(), EmailMessage.id.desc()).limit(40)
            )
        )
        .scalars()
        .all()
    )
    outbox_rows = (
        (
            await session.execute(
                select(Outbox).order_by(Outbox.created_at.desc(), Outbox.id.desc()).limit(30)
            )
        )
        .scalars()
        .all()
    )
    handoff_rows = (
        (
            await session.execute(
                select(Handoff).order_by(Handoff.created_at.desc(), Handoff.id.desc()).limit(30)
            )
        )
        .scalars()
        .all()
    )
    job_rows = (
        (
            await session.execute(
                select(Job).order_by(Job.created_at.desc(), Job.id.desc()).limit(30)
            )
        )
        .scalars()
        .all()
    )
    audit_rows = (
        (
            await session.execute(
                select(AuditEvent).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(40)
            )
        )
        .scalars()
        .all()
    )
    mailbox_rows = (
        (
            await session.execute(
                select(MailboxCursor).order_by(MailboxCursor.mailbox, MailboxCursor.folder)
            )
        )
        .scalars()
        .all()
    )
    quote_rows = (
        await session.execute(
            select(Quote, Customer.company_name, Product.code)
            .join(SalesCase, Quote.case_id == SalesCase.id)
            .join(Customer, SalesCase.customer_id == Customer.id)
            .join(Product, SalesCase.product_id == Product.id)
            .order_by(Quote.created_at.desc(), Quote.id.desc())
            .limit(30)
        )
    ).all()
    ai_failure_count = await session.scalar(
        select(func.count()).select_from(AIInvocation).where(AIInvocation.success.is_(False))
    )

    return {
        "generated_at": now.isoformat(),
        "runtime": {
            "demo_mode": settings.demo_mode,
            "ai_provider": settings.ai_provider,
            "mail_transport": settings.mail_transport,
            "dingtalk_transport": settings.dingtalk_transport,
            "safe_mode": settings.safe_mode,
            "auto_send_enabled": settings.auto_send_enabled,
            "imap_sync_enabled": settings.imap_sync_enabled,
            "credentials": {
                "ai": bool(settings.anthropic_api_key),
                "gmail": bool(settings.gmail_address and settings.gmail_app_password),
                "dingtalk": bool(settings.dingtalk_webhook_url),
            },
        },
        "metrics": {
            "inbound_24h": int(inbound_24h or 0),
            "sent_24h": int(sent_24h or 0),
            "pending_outbox": int(pending_outbox or 0),
            "open_handoffs": int(open_handoffs or 0),
            "failed_jobs": int(failed_jobs or 0),
            "unmatched_history": int(unmatched_history or 0),
            "ai_failures": int(ai_failure_count or 0),
        },
        "cases_by_status": {status_key.value: count for status_key, count in case_status_counts.all()},
        "mailboxes": [
            {
                "mailbox": row.mailbox,
                "folder": row.folder,
                "last_uid": row.last_uid,
                "history_cutoff_uid": row.history_cutoff_uid,
                "history_complete": row.history_complete,
                "updated_at": row.updated_at.isoformat(),
            }
            for row in mailbox_rows
        ],
        "emails": [
            {
                "id": row.id,
                "case_id": row.case_id,
                "direction": row.direction,
                "from": row.from_address,
                "to": row.to_addresses,
                "subject": row.subject,
                "received_at": row.received_at.isoformat(),
                "is_history": row.is_history,
                "folder": row.mailbox_folder,
                "attachments": len(row.attachment_metadata),
            }
            for row in email_rows
        ],
        "outbox": [
            {
                "id": row.id,
                "case_id": row.case_id,
                "recipient": row.recipient,
                "message_id": row.message_id,
                "status": row.status.value,
                "attempts": row.attempts,
                "last_error": row.last_error,
                "created_at": row.created_at.isoformat(),
                "sent_at": row.sent_at.isoformat() if row.sent_at else None,
            }
            for row in outbox_rows
        ],
        "handoffs": [
            {
                "id": row.id,
                "case_id": row.case_id,
                "reason": row.reason_code,
                "summary": row.summary,
                "status": row.status,
                "dingtalk_status": row.dingtalk_status,
                "created_at": row.created_at.isoformat(),
            }
            for row in handoff_rows
        ],
        "jobs": [
            {
                "id": row.id,
                "kind": row.kind,
                "status": row.status.value,
                "attempts": row.attempts,
                "max_attempts": row.max_attempts,
                "last_error": row.last_error,
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
            }
            for row in job_rows
        ],
        "quotes": [
            {
                "id": quote.id,
                "case_id": quote.case_id,
                "company": company,
                "product": product_code,
                "round": quote.round_number,
                "unit_price": str(quote.unit_price),
                "currency": quote.currency,
                "quantity": quote.quantity,
                "incoterm": quote.incoterm,
                "valid_until": quote.valid_until.isoformat(),
                "created_at": quote.created_at.isoformat(),
            }
            for quote, company, product_code in quote_rows
        ],
        "audit": [
            {
                "id": row.id,
                "case_id": row.case_id,
                "actor": row.actor,
                "event_type": row.event_type,
                "data": row.data,
                "created_at": row.created_at.isoformat(),
            }
            for row in audit_rows
        ],
    }


@router.get("/admin/emails/{email_id}")
async def email_detail(email_id: int, _: Admin, session: Session) -> dict[str, Any]:
    row = await session.get(EmailMessage, email_id)
    if row is None:
        raise HTTPException(404, "Email not found")
    body_limit = 30_000
    return {
        "id": row.id,
        "case_id": row.case_id,
        "direction": row.direction,
        "folder": row.mailbox_folder,
        "from": row.from_address,
        "to": row.to_addresses,
        "subject": row.subject,
        "message_id": row.message_id,
        "in_reply_to": row.in_reply_to,
        "references": row.references_json,
        "attachments": row.attachment_metadata,
        "received_at": row.received_at.isoformat(),
        "is_history": row.is_history,
        "body_text": row.body_text[:body_limit],
        "body_truncated": len(row.body_text) > body_limit,
    }


@router.get("/admin/outbox/{outbox_id}")
async def outbox_detail(outbox_id: int, _: Admin, session: Session) -> dict[str, Any]:
    row = await session.get(Outbox, outbox_id)
    if row is None:
        raise HTTPException(404, "Outbox record not found")
    message_limit = 30_000
    return {
        "id": row.id,
        "case_id": row.case_id,
        "recipient": row.recipient,
        "message_id": row.message_id,
        "status": row.status.value,
        "attempts": row.attempts,
        "last_error": row.last_error,
        "created_at": row.created_at.isoformat(),
        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
        "raw_message": row.raw_message[:message_limit],
        "message_truncated": len(row.raw_message) > message_limit,
    }


@router.get("/admin/status")
async def admin_status(_: Admin, session: Session, settings: Annotated[Settings, Depends(get_settings)]) -> dict[str, Any]:
    jobs = await session.execute(select(Job.status, func.count()).group_by(Job.status))
    outbox = await session.execute(select(Outbox.status, func.count()).group_by(Outbox.status))
    handoffs = await session.scalar(select(func.count()).select_from(Handoff).where(Handoff.status == "OPEN"))
    return {
        "demo_mode": settings.demo_mode,
        "ai_provider": settings.ai_provider,
        "mail_transport": settings.mail_transport,
        "dingtalk_transport": settings.dingtalk_transport,
        "safe_mode": settings.safe_mode,
        "auto_send_enabled": settings.auto_send_enabled,
        "imap_sync_enabled": settings.imap_sync_enabled,
        "credentials_present": {
            "anthropic": bool(settings.anthropic_api_key),
            "gmail": bool(settings.gmail_address and settings.gmail_app_password),
            "dingtalk": bool(settings.dingtalk_webhook_url),
        },
        "jobs": {str(key.value): count for key, count in jobs.all()},
        "outbox": {str(key.value): count for key, count in outbox.all()},
        "open_handoffs": handoffs or 0,
    }


@router.get("/admin/history/status")
async def history_status(_: Admin, session: Session) -> dict[str, Any]:
    direction_counts = await session.execute(
        select(EmailMessage.direction, func.count())
        .where(EmailMessage.is_history.is_(True))
        .group_by(EmailMessage.direction)
    )
    unmatched = await session.scalar(
        select(func.count())
        .select_from(EmailMessage)
        .where(EmailMessage.is_history.is_(True), EmailMessage.case_id.is_(None))
    )
    cursors = (
        (
            await session.execute(
                select(MailboxCursor).order_by(MailboxCursor.mailbox, MailboxCursor.folder)
            )
        )
        .scalars()
        .all()
    )
    return {
        "history_messages": {direction: count for direction, count in direction_counts.all()},
        "unmatched_history_messages": unmatched or 0,
        "folders": [
            {
                "mailbox": cursor.mailbox,
                "folder": cursor.folder,
                "last_uid": cursor.last_uid,
                "history_cutoff_uid": cursor.history_cutoff_uid,
                "history_complete": cursor.history_complete,
            }
            for cursor in cursors
        ],
    }


@router.post("/admin/history/reconcile")
async def history_reconcile(_: Admin, session: Session) -> dict[str, int]:
    result = await reconcile_email_history(session)
    return result.__dict__


@router.post("/admin/demo/seed")
async def demo_seed(_: Admin, __: DemoMode, session: Session) -> dict[str, int]:
    return await seed_demo_data(session)


@router.post("/admin/demo/outreach", status_code=202)
async def demo_outreach(request: DemoOutreachRequest, _: Admin, __: DemoMode, session: Session) -> dict[str, Any]:
    job = await enqueue_job(
        session,
        "demo_outreach",
        request.model_dump(mode="json"),
        f"demo-outreach:{request.recipient}:{request.quantity}",
    )
    return {"queued": job is not None, "job_id": job.id if job else None}


@router.post("/admin/demo/inbound", status_code=202)
async def demo_inbound(
    _: Admin,
    __: DemoMode,
    session: Session,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    if not (file.filename or "").lower().endswith(".eml"):
        raise HTTPException(400, "Only .eml files are accepted")
    raw = await file.read()
    if len(raw) > 10_000_000:
        raise HTTPException(413, "Message is too large")
    row = await ingest_raw_email(session, raw)
    return {"email_id": row.id if row else None, "accepted": bool(row)}


async def _save_upload(file: UploadFile) -> Path:
    suffix = Path(file.filename or "upload.xlsx").suffix.lower()
    if suffix not in {".xlsx", ".csv"}:
        raise HTTPException(400, "Only .xlsx and UTF-8 .csv files are accepted")
    raw = await file.read()
    if len(raw) > 10_000_000:
        raise HTTPException(413, "Workbook is too large")
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    handle.write(raw)
    handle.close()
    return Path(handle.name)


@router.post("/admin/imports/customers")
async def customers_import(
    _: Admin,
    session: Session,
    file: UploadFile = File(...),
    apply: bool = Query(False),
) -> dict[str, Any]:
    path = await _save_upload(file)
    try:
        result = await import_customers(path, session, apply=apply)
        return result.__dict__ | {"ok": result.ok}
    finally:
        path.unlink(missing_ok=True)


@router.post("/admin/imports/prices")
async def prices_import(
    _: Admin,
    session: Session,
    file: UploadFile = File(...),
    apply: bool = Query(False),
) -> dict[str, Any]:
    path = await _save_upload(file)
    try:
        result = await import_prices(path, session, apply=apply)
        return result.__dict__ | {"ok": result.ok}
    finally:
        path.unlink(missing_ok=True)


@router.get("/admin/cases")
async def list_cases(_: Admin, session: Session) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(SalesCase)
                .options(
                    selectinload(SalesCase.customer),
                    selectinload(SalesCase.contact),
                    selectinload(SalesCase.product),
                )
                .order_by(SalesCase.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": row.id,
            "company": row.customer.company_name,
            "contact": row.contact.email,
            "product": row.product.code,
            "currency": row.currency,
            "stage": row.stage.value,
            "status": row.status.value,
            "negotiation_round": row.negotiation_round,
        }
        for row in rows
    ]


@router.post("/admin/cases/{case_id}/outreach", status_code=202)
async def queue_case_outreach(
    case_id: int,
    request: CaseOutreachRequest,
    _: Admin,
    session: Session,
) -> dict[str, Any]:
    case = await session.get(SalesCase, case_id)
    if case is None:
        raise HTTPException(404, "Case not found")
    policy = await active_policy(session, case.product_id, case.currency)
    if policy is None:
        raise HTTPException(409, "No active price policy exists for this case currency")
    if request.quantity < policy.min_quantity or (policy.max_quantity is not None and request.quantity > policy.max_quantity):
        raise HTTPException(
            422,
            f"Quantity must be between {policy.min_quantity} and {policy.max_quantity or 'unlimited'}",
        )
    job = await enqueue_job(
        session,
        "case_outreach",
        {"case_id": case_id, "quantity": request.quantity},
        f"case-outreach:{case_id}",
    )
    return {"queued": job is not None, "job_id": job.id if job else None}


@router.get("/admin/cases/{case_id}")
async def case_detail(case_id: int, _: Admin, session: Session) -> dict[str, Any]:
    row = await session.get(SalesCase, case_id)
    if row is None:
        raise HTTPException(404, "Case not found")
    quotes = (await session.execute(select(Quote).where(Quote.case_id == case_id).order_by(Quote.id))).scalars().all()
    return {
        "id": row.id,
        "currency": row.currency,
        "stage": row.stage.value,
        "status": row.status.value,
        "quotes": [
            {
                "id": quote.id,
                "round": quote.round_number,
                "unit_price": str(quote.unit_price),
                "currency": quote.currency,
                "quantity": quote.quantity,
                "snapshot": quote.pricing_snapshot,
            }
            for quote in quotes
        ],
    }


@router.get("/admin/handoffs")
async def list_handoffs(_: Admin, session: Session) -> list[dict[str, Any]]:
    rows = (await session.execute(select(Handoff).order_by(Handoff.id.desc()))).scalars().all()
    return [
        {
            "id": row.id,
            "case_id": row.case_id,
            "reason": row.reason_code,
            "summary": row.summary,
            "status": row.status,
            "dingtalk_status": row.dingtalk_status,
        }
        for row in rows
    ]


@router.get("/admin/handoffs/{handoff_id}")
async def handoff_detail(handoff_id: int, _: Admin, session: Session) -> dict[str, Any]:
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None:
        raise HTTPException(404, "Handoff not found")
    return {
        "id": handoff.id,
        "case_id": handoff.case_id,
        "reason": handoff.reason_code,
        "summary": handoff.summary,
        "facts": handoff.extracted_facts,
        "status": handoff.status,
        "dingtalk_status": handoff.dingtalk_status,
        "resolution_note": handoff.resolution_note,
    }


@router.post("/admin/handoffs/{handoff_id}")
async def update_handoff(handoff_id: int, update: HandoffUpdate, _: Admin, session: Session) -> dict[str, Any]:
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None:
        raise HTTPException(404, "Handoff not found")
    actions = {"approve", "reject", "resolve", "pause", "resume", "takeover"}
    if update.action not in actions:
        raise HTTPException(400, f"action must be one of {sorted(actions)}")
    handoff.status = "RESOLVED" if update.action in {"approve", "reject", "resolve"} else "OPEN"
    handoff.resolution_note = update.note
    if handoff.case_id:
        case = await session.get(SalesCase, handoff.case_id)
        if case:
            mapping = {
                "pause": CaseStatus.PAUSED,
                "resume": CaseStatus.ACTIVE,
                "takeover": CaseStatus.HUMAN_TAKEOVER,
                "resolve": CaseStatus.ACTIVE,
                "approve": CaseStatus.ACTIVE,
                "reject": CaseStatus.WAITING_HUMAN,
            }
            case.status = mapping[update.action]
            if update.action in {"pause", "takeover", "reject"}:
                pending = (
                    (
                        await session.execute(
                            select(Outbox).where(
                                Outbox.case_id == case.id,
                                Outbox.status.in_(
                                    [
                                        DeliveryStatus.PENDING,
                                        DeliveryStatus.FAILED,
                                        DeliveryStatus.CLAIMED,
                                    ]
                                ),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for row in pending:
                    row.status = DeliveryStatus.CANCELLED
                    row.last_error = f"cancelled by handoff action: {update.action}"
    await session.commit()
    return {"id": handoff.id, "status": handoff.status, "action": update.action}


@router.post("/admin/templates/regenerate")
async def regenerate_templates(_: Admin) -> dict[str, str]:
    generate_templates(Path("assets/import_templates"))
    return {"status": "generated"}
