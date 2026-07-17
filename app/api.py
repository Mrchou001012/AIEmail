import secrets
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.commercial import (
    commercial_update_link,
    get_or_create_current_cycle,
    lock_commercial_scope,
)
from app.db import (
    AIInvocation,
    AuditEvent,
    CaseStatus,
    CommercialDataCycle,
    Contact,
    Customer,
    DeliveryStatus,
    EmailAddressStatus,
    EmailMessage,
    Handoff,
    InventorySnapshot,
    Job,
    JobStatus,
    MailboxCursor,
    MailboxDailyUsage,
    MailboxThrottle,
    Outbox,
    PricePolicy,
    Product,
    Quote,
    SalesCase,
    db_health,
    get_session,
)
from app.history import reconcile_email_history
from app.imports import generate_templates, import_customers, import_prices
from app.products import canonical_product_code
from app.services import (
    active_policy,
    assign_handoff_case,
    create_case_for_handoff,
    enqueue_job,
    ingest_raw_email,
    queue_human_reply,
    seed_demo_data,
)
from app.settings import Settings, get_settings

router = APIRouter()
security = HTTPBasic()
DASHBOARD_PATH = Path(__file__).with_name("dashboard.html")
HANDOFF_REVIEW_PATH = Path(__file__).with_name("handoff_review.html")


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


class HandoffAssignmentRequest(BaseModel):
    case_id: int = Field(gt=0)


class HandoffCaseRequest(BaseModel):
    contact_id: int = Field(gt=0)
    product_id: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)


class HandoffReplyRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=998)
    body_text: str = Field(min_length=1, max_length=50_000)
    note: str = Field(default="", max_length=2_000)
    resume_automation: bool = False


class InventoryItemRequest(BaseModel):
    product_code: str = Field(min_length=1, max_length=64)
    availability: Literal["AVAILABLE", "OUT_OF_STOCK"]
    quantity: Decimal | None = Field(default=None, ge=0)
    warehouse: str | None = Field(default=None, max_length=128)
    external_id: str | None = Field(default=None, max_length=255)


class InventoryConfirmationRequest(BaseModel):
    price_source_ref: str = Field(min_length=1, max_length=255)
    items: list[InventoryItemRequest] = Field(min_length=1, max_length=10_000)
    source_system: str = Field(default="manual", min_length=1, max_length=64)
    source_ref: str | None = Field(default=None, max_length=255)


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
            EmailMessage.contact_id.is_(None),
        )
    )
    unmatched_history_cases = await session.scalar(
        select(func.count()).select_from(EmailMessage).where(
            EmailMessage.is_history.is_(True),
            EmailMessage.case_id.is_(None),
        )
    )
    customer_matched_case_unmatched = await session.scalar(
        select(func.count()).select_from(EmailMessage).where(
            EmailMessage.is_history.is_(True),
            EmailMessage.contact_id.is_not(None),
            EmailMessage.case_id.is_(None),
        )
    )
    bounced_24h = await session.scalar(
        select(func.count()).select_from(EmailMessage).where(
            EmailMessage.is_bounce.is_(True),
            EmailMessage.received_at >= since_day,
        )
    )
    suppressed_addresses = await session.scalar(
        select(func.count()).select_from(EmailAddressStatus).where(
            EmailAddressStatus.suppressed.is_(True)
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
    mailbox_usage = None
    mailbox_throttle = None
    if settings.gmail_address:
        mailbox_usage = await session.get(MailboxDailyUsage, (settings.gmail_address, now.date()))
        mailbox_throttle = await session.get(MailboxThrottle, settings.gmail_address.lower())

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
            "rate_limits": {
                "max_sends_per_hour": settings.max_sends_per_hour,
                "max_sends_per_day": settings.max_sends_per_day,
                "min_send_interval_seconds": settings.min_send_interval_seconds,
                "max_send_interval_seconds": (
                    settings.min_send_interval_seconds + settings.send_interval_jitter_seconds
                ),
                "imap_poll_seconds": settings.imap_poll_seconds,
                "imap_batch_size": settings.imap_batch_size,
                "imap_daily_download_limit_mb": settings.imap_daily_download_limit_mb,
                "imap_downloaded_today_mb": round(
                    (mailbox_usage.imap_download_bytes if mailbox_usage else 0) / 1024 / 1024,
                    2,
                ),
                "cooldown_until": (
                    mailbox_throttle.cooldown_until.isoformat()
                    if mailbox_throttle and mailbox_throttle.cooldown_until
                    else None
                ),
                "cooldown_reason": mailbox_throttle.reason if mailbox_throttle else None,
            },
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
            "unmatched_history_cases": int(unmatched_history_cases or 0),
            "customer_matched_case_unmatched": int(customer_matched_case_unmatched or 0),
            "ai_failures": int(ai_failure_count or 0),
            "bounced_24h": int(bounced_24h or 0),
            "suppressed_addresses": int(suppressed_addresses or 0),
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
                "customer_id": row.customer_id,
                "contact_id": row.contact_id,
                "direction": row.direction,
                "from": row.from_address,
                "to": row.to_addresses,
                "subject": row.subject,
                "received_at": row.received_at.isoformat(),
                "is_history": row.is_history,
                "is_automated_reply": row.is_automated_reply,
                "automated_reply_type": row.automated_reply_type,
                "automated_reply_handled_at": (
                    row.automated_reply_handled_at.isoformat()
                    if row.automated_reply_handled_at
                    else None
                ),
                "is_bounce": row.is_bounce,
                "bounce_type": row.bounce_type,
                "bounce_handled_at": row.bounce_handled_at.isoformat() if row.bounce_handled_at else None,
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
        "customer_id": row.customer_id,
        "contact_id": row.contact_id,
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
        "is_automated_reply": row.is_automated_reply,
        "automated_reply_type": row.automated_reply_type,
        "automated_reply_metadata": row.automated_reply_metadata,
        "automated_reply_handled_at": (
            row.automated_reply_handled_at.isoformat()
            if row.automated_reply_handled_at
            else None
        ),
        "is_bounce": row.is_bounce,
        "bounce_type": row.bounce_type,
        "bounce_metadata": row.bounce_metadata,
        "bounce_handled_at": row.bounce_handled_at.isoformat() if row.bounce_handled_at else None,
        "body_text": row.body_text[:body_limit],
        "body_truncated": len(row.body_text) > body_limit,
    }


@router.get("/admin/outbox/{outbox_id}")
async def outbox_detail(outbox_id: int, _: Admin, session: Session) -> dict[str, Any]:
    row = await session.get(Outbox, outbox_id)
    if row is None:
        raise HTTPException(404, "Outbox record not found")
    message_limit = 30_000
    recipient_status = await session.get(EmailAddressStatus, row.recipient.strip().casefold())
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
        "approval_handoff_id": row.approval_handoff_id,
        "human_approved_by": row.human_approved_by,
        "human_approved_at": row.human_approved_at.isoformat() if row.human_approved_at else None,
        "recipient_deliverability": (
            {
                "format_valid": recipient_status.format_valid,
                "preflight_status": recipient_status.preflight_status,
                "last_preflight_at": (
                    recipient_status.last_preflight_at.isoformat()
                    if recipient_status.last_preflight_at
                    else None
                ),
                "suppressed": recipient_status.suppressed,
                "suppression_reason": recipient_status.suppression_reason,
                "last_bounce_type": recipient_status.last_bounce_type,
                "last_bounce_at": (
                    recipient_status.last_bounce_at.isoformat()
                    if recipient_status.last_bounce_at
                    else None
                ),
            }
            if recipient_status
            else None
        ),
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
    case_unmatched = await session.scalar(
        select(func.count())
        .select_from(EmailMessage)
        .where(EmailMessage.is_history.is_(True), EmailMessage.case_id.is_(None))
    )
    customer_unmatched = await session.scalar(
        select(func.count())
        .select_from(EmailMessage)
        .where(EmailMessage.is_history.is_(True), EmailMessage.contact_id.is_(None))
    )
    customer_matched_case_unmatched = await session.scalar(
        select(func.count())
        .select_from(EmailMessage)
        .where(
            EmailMessage.is_history.is_(True),
            EmailMessage.contact_id.is_not(None),
            EmailMessage.case_id.is_(None),
        )
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
        # Compatibility field: this was historically the case-level count.
        "unmatched_history_messages": case_unmatched or 0,
        "customer_unmatched_history_messages": customer_unmatched or 0,
        "case_unmatched_history_messages": case_unmatched or 0,
        "customer_matched_case_unmatched_messages": customer_matched_case_unmatched or 0,
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
    actor: Admin,
    session: Session,
    file: UploadFile = File(...),
    apply: bool = Query(False),
    replace_active: bool = Query(False),
) -> dict[str, Any]:
    path = await _save_upload(file)
    try:
        result = await import_prices(
            path,
            session,
            apply=apply,
            replace_active=replace_active,
            actor=actor,
        )
        return result.__dict__ | {"ok": result.ok}
    finally:
        path.unlink(missing_ok=True)


async def _commercial_cycle_payload(
    session: AsyncSession,
    settings: Settings,
    cycle: CommercialDataCycle,
) -> dict[str, Any]:
    priced_rows = (
        await session.execute(
            select(Product, PricePolicy)
            .join(PricePolicy, PricePolicy.product_id == Product.id)
            .where(
                PricePolicy.commercial_cycle_id == cycle.id,
                PricePolicy.active.is_(True),
            )
            .order_by(Product.code, PricePolicy.currency)
        )
    ).all()
    snapshots = {
        row.product_id: row
        for row in (
            (
                await session.execute(
                    select(InventorySnapshot).where(InventorySnapshot.cycle_id == cycle.id)
                )
            )
            .scalars()
            .all()
        )
    }
    products: dict[int, dict[str, Any]] = {}
    for product, policy in priced_rows:
        item = products.setdefault(
            product.id,
            {
                "product_id": product.id,
                "product_code": product.code,
                "currencies": [],
            },
        )
        item["currencies"].append(policy.currency)
    missing_inventory: list[str] = []
    product_payload: list[dict[str, Any]] = []
    for product_id, item in products.items():
        snapshot = snapshots.get(product_id)
        if snapshot is None or snapshot.availability == "UNKNOWN":
            missing_inventory.append(item["product_code"])
        product_payload.append(
            {
                **item,
                "inventory": (
                    {
                        "availability": snapshot.availability,
                        "quantity": str(snapshot.quantity) if snapshot.quantity is not None else None,
                        "warehouse": snapshot.warehouse,
                        "source_system": snapshot.source_system,
                        "external_id": snapshot.external_id,
                        "updated_at": snapshot.updated_at.isoformat(),
                    }
                    if snapshot
                    else None
                ),
            }
        )
    return {
        "cycle_id": cycle.id,
        "scope": cycle.scope,
        "week_start": cycle.week_start.isoformat(),
        "week_end": cycle.week_end.isoformat(),
        "price_status": cycle.price_status,
        "inventory_status": cycle.inventory_status,
        "automation_ready": (
            cycle.price_status == "CONFIRMED" and cycle.inventory_status == "CONFIRMED"
        ),
        "price_confirmed_at": (
            cycle.price_confirmed_at.isoformat() if cycle.price_confirmed_at else None
        ),
        "inventory_confirmed_at": (
            cycle.inventory_confirmed_at.isoformat() if cycle.inventory_confirmed_at else None
        ),
        "price_source_system": cycle.price_source_system,
        "price_source_ref": cycle.price_source_ref,
        "inventory_source_system": cycle.inventory_source_system,
        "inventory_source_ref": cycle.inventory_source_ref,
        "reminder_status": cycle.reminder_status,
        "reminder_sent_at": cycle.reminder_sent_at.isoformat() if cycle.reminder_sent_at else None,
        "missing_inventory_products": missing_inventory,
        "products": product_payload,
        "update_url": commercial_update_link(settings, cycle),
    }


@router.get("/admin/commercial/current")
async def current_commercial_cycle(
    _: Admin,
    session: Session,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    cycle = await get_or_create_current_cycle(session, settings)
    await session.commit()
    return await _commercial_cycle_payload(session, settings, cycle)


@router.post("/admin/commercial/current/inventory")
async def confirm_current_inventory(
    request: InventoryConfirmationRequest,
    actor: Admin,
    session: Session,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    await lock_commercial_scope(session, settings.commercial_scope)
    cycle = await get_or_create_current_cycle(session, settings)
    cycle = await session.scalar(
        select(CommercialDataCycle)
        .where(CommercialDataCycle.id == cycle.id)
        .with_for_update()
    )
    if cycle is None:
        raise HTTPException(409, "The current commercial-data cycle no longer exists")
    if cycle.price_status != "CONFIRMED":
        raise HTTPException(409, "Apply the current week's price list before confirming inventory")
    if not cycle.price_source_ref or request.price_source_ref != cycle.price_source_ref:
        raise HTTPException(
            409,
            "The price batch changed; reload current commercial status and confirm inventory again",
        )
    in_flight_quote_id = await session.scalar(
        select(Outbox.id)
        .join(Quote, Quote.id == Outbox.quote_id)
        .join(
            CommercialDataCycle,
            CommercialDataCycle.id == Quote.commercial_cycle_id,
        )
        .where(
            CommercialDataCycle.scope == cycle.scope,
            Outbox.message_kind == "AUTO_QUOTE",
            Outbox.status.in_([DeliveryStatus.CLAIMED, DeliveryStatus.UNKNOWN]),
        )
        .limit(1)
    )
    if in_flight_quote_id is not None:
        raise HTTPException(
            409,
            "Inventory update is temporarily blocked while an automatic quote "
            f"is in flight (outbox {in_flight_quote_id})",
        )

    priced_products = (
        (
            await session.execute(
                select(Product)
                .join(PricePolicy, PricePolicy.product_id == Product.id)
                .where(
                    PricePolicy.commercial_cycle_id == cycle.id,
                    PricePolicy.active.is_(True),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    if not priced_products:
        raise HTTPException(409, "The current price confirmation contains no active products")
    products_by_code = {product.code: product for product in priced_products}
    normalized_codes = [canonical_product_code(item.product_code) for item in request.items]
    if len(set(normalized_codes)) != len(normalized_codes):
        raise HTTPException(422, "Each product may appear only once in an inventory confirmation")
    unknown = sorted(set(normalized_codes) - set(products_by_code))
    if unknown:
        raise HTTPException(
            422,
            f"Inventory contains products outside the current price batch: {', '.join(unknown)}",
        )

    for item, code in zip(request.items, normalized_codes, strict=True):
        product = products_by_code[code]
        snapshot = await session.scalar(
            select(InventorySnapshot).where(
                InventorySnapshot.cycle_id == cycle.id,
                InventorySnapshot.product_id == product.id,
            )
        )
        if snapshot is None:
            snapshot = InventorySnapshot(cycle_id=cycle.id, product_id=product.id)
            session.add(snapshot)
        snapshot.availability = item.availability
        snapshot.quantity = item.quantity
        snapshot.warehouse = item.warehouse.strip() if item.warehouse else None
        snapshot.source_system = request.source_system.strip()
        snapshot.external_id = item.external_id.strip() if item.external_id else None
        snapshot.metadata_json = {"confirmed_by": actor}
    await session.flush()

    confirmed_product_ids = set(
        (
            await session.execute(
                select(InventorySnapshot.product_id).where(
                    InventorySnapshot.cycle_id == cycle.id,
                    InventorySnapshot.availability.in_(["AVAILABLE", "OUT_OF_STOCK"]),
                )
            )
        )
        .scalars()
        .all()
    )
    expected_product_ids = {product.id for product in priced_products}
    complete = expected_product_ids == confirmed_product_ids
    now = datetime.now(UTC)
    cycle.inventory_status = "CONFIRMED" if complete else "PENDING"
    cycle.inventory_confirmed_at = now if complete else None
    cycle.inventory_source_system = request.source_system.strip()
    cycle.inventory_source_ref = request.source_ref or now.isoformat()
    cycle.metadata_json = {
        **(cycle.metadata_json or {}),
        "inventory_confirmed_products": len(confirmed_product_ids),
        "inventory_expected_products": len(expected_product_ids),
        "inventory_confirmed_by": actor,
    }
    session.add(
        AuditEvent(
            actor=actor,
            event_type="commercial.inventory_updated",
            data={
                "cycle_id": cycle.id,
                "complete": complete,
                "confirmed_products": len(confirmed_product_ids),
                "expected_products": len(expected_product_ids),
                "source_system": request.source_system,
                "price_source_ref": request.price_source_ref,
            },
        )
    )
    if complete:
        await session.execute(
            update(Job)
            .where(
                Job.status == JobStatus.PENDING,
                Job.last_error.like("DEFERRED: commercial%"),
            )
            .values(available_at=now, updated_at=now)
        )
    await session.commit()
    return await _commercial_cycle_payload(session, settings, cycle)


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


def _suggested_handoff_reply(
    handoff: Handoff,
    source_email: EmailMessage | None,
    case: SalesCase | None,
) -> dict[str, str]:
    subject = (source_email.subject if source_email else "Your inquiry").strip()
    if not subject.casefold().startswith("re:"):
        subject = f"Re: {subject}"
    contact_name = case.contact.name.strip() if case and case.contact.name.strip() else "Customer"
    opening_by_reason = {
        "PRICE_NEGOTIATION": (
            "Thank you for your feedback on our quotation. We are reviewing your pricing request "
            "and will confirm the best available terms with you."
        ),
        "PACKAGING_REVIEW": (
            "Thank you for your inquiry. We are confirming the applicable packaging details and "
            "will update you shortly."
        ),
        "SHIPPING_REQUEST": (
            "Thank you for your inquiry. We are checking the requested delivery and shipping "
            "details before confirming them."
        ),
        "THREAD_AMBIGUOUS": (
            "Thank you for your email. We are reviewing the related quotation history to make "
            "sure we respond with the correct information."
        ),
        "NEW_INQUIRY_REVIEW": (
            "Thank you for your inquiry. We are reviewing the requested product details and will "
            "reply with the appropriate information."
        ),
        "PERSONNEL_CHANGE": (
            "Thank you for the update. We are reviewing the contact information before making any "
            "changes to our records."
        ),
    }
    opening = opening_by_reason.get(
        handoff.reason_code,
        "Thank you for your email. We are reviewing your request and will respond with the correct information.",
    )
    return {
        "subject": subject[:998],
        "body_text": f"Dear {contact_name},\n\n{opening}\n\nBest regards,",
    }


async def _handoff_case_payload(session: AsyncSession, case_id: int | None) -> dict[str, Any] | None:
    if case_id is None:
        return None
    case = await session.scalar(
        select(SalesCase)
        .options(
            selectinload(SalesCase.customer),
            selectinload(SalesCase.contact),
            selectinload(SalesCase.product),
        )
        .where(SalesCase.id == case_id)
    )
    if case is None:
        return None
    latest_quote = await session.scalar(
        select(Quote).where(Quote.case_id == case.id).order_by(Quote.round_number.desc())
    )
    return {
        "id": case.id,
        "company": case.customer.company_name,
        "contact_name": case.contact.name,
        "contact_email": case.contact.email,
        "product": case.product.code,
        "product_name": case.product.name,
        "currency": case.currency,
        "stage": case.stage.value,
        "status": case.status.value,
        "latest_quote": (
            {
                "id": latest_quote.id,
                "round": latest_quote.round_number,
                "unit_price": str(latest_quote.unit_price),
                "currency": latest_quote.currency,
                "quantity": latest_quote.quantity,
                "valid_until": latest_quote.valid_until.isoformat(),
            }
            if latest_quote
            else None
        ),
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


@router.get("/admin/handoffs/{handoff_id}/review", response_class=HTMLResponse, include_in_schema=False)
async def handoff_review(handoff_id: int, _: Admin, session: Session) -> HTMLResponse:
    if await session.get(Handoff, handoff_id) is None:
        raise HTTPException(404, "Handoff not found")
    return HTMLResponse(
        HANDOFF_REVIEW_PATH.read_text(encoding="utf-8"),
        headers=_dashboard_headers(),
    )


@router.get("/admin/handoffs/{handoff_id}")
async def handoff_detail(handoff_id: int, _: Admin, session: Session) -> dict[str, Any]:
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None:
        raise HTTPException(404, "Handoff not found")
    source_email = (
        await session.get(EmailMessage, handoff.source_email_id)
        if handoff.source_email_id is not None
        else None
    )
    case_payload = await _handoff_case_payload(session, handoff.case_id)
    case = None
    if handoff.case_id is not None:
        case = await session.scalar(
            select(SalesCase)
            .options(selectinload(SalesCase.contact))
            .where(SalesCase.id == handoff.case_id)
        )

    candidate_ids = {
        int(value)
        for key in ("possible_related_case_ids", "recent_related_case_ids")
        for value in handoff.extracted_facts.get(key, [])
        if str(value).isdigit()
    }
    if source_email is not None:
        sender_case_ids = (
            (
                await session.execute(
                    select(SalesCase.id)
                    .join(Contact, SalesCase.contact_id == Contact.id)
                    .where(
                        func.lower(Contact.email) == source_email.from_address.casefold(),
                        SalesCase.status.not_in([CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST]),
                    )
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
        candidate_ids.update(sender_case_ids)
    if handoff.case_id is not None:
        candidate_ids.add(handoff.case_id)
    candidate_cases = [
        payload
        for case_id in sorted(candidate_ids)
        if (payload := await _handoff_case_payload(session, case_id)) is not None
    ]

    matching_contacts: list[Contact] = []
    if source_email is not None:
        matching_contacts = (
            (
                await session.execute(
                    select(Contact)
                    .where(func.lower(Contact.email) == source_email.from_address.casefold())
                    .order_by(Contact.id)
                )
            )
            .scalars()
            .all()
        )
    products = (
        (
            await session.execute(select(Product).where(Product.active.is_(True)).order_by(Product.code))
        )
        .scalars()
        .all()
    )
    product_currency_rows = await session.execute(
        select(PricePolicy.product_id, PricePolicy.currency).where(PricePolicy.active.is_(True))
    )
    currencies_by_product: dict[int, set[str]] = {}
    for product_id, currency in product_currency_rows:
        currencies_by_product.setdefault(product_id, set()).add(currency)
    approved_outbox = await session.scalar(
        select(Outbox).where(Outbox.approval_handoff_id == handoff.id)
    )
    return {
        "id": handoff.id,
        "case_id": handoff.case_id,
        "source_email_id": handoff.source_email_id,
        "reason": handoff.reason_code,
        "summary": handoff.summary,
        "facts": handoff.extracted_facts,
        "status": handoff.status,
        "dingtalk_status": handoff.dingtalk_status,
        "resolution_note": handoff.resolution_note,
        "case": case_payload,
        "source_email": (
            {
                "id": source_email.id,
                "from": source_email.from_address,
                "to": source_email.to_addresses,
                "subject": source_email.subject,
                "received_at": source_email.received_at.isoformat(),
                "body_text": source_email.body_text[:50_000],
                "body_truncated": len(source_email.body_text) > 50_000,
                "attachments": source_email.attachment_metadata,
            }
            if source_email
            else None
        ),
        "candidate_cases": candidate_cases,
        "matching_contacts": [
            {
                "id": contact.id,
                "customer_id": contact.customer_id,
                "name": contact.name,
                "email": contact.email,
            }
            for contact in matching_contacts
        ],
        "products": [
            {
                "id": product.id,
                "code": product.code,
                "name": product.name,
                "currencies": sorted(currencies_by_product.get(product.id, set())),
            }
            for product in products
        ],
        "suggested_reply": _suggested_handoff_reply(handoff, source_email, case),
        "approved_outbox": (
            {
                "id": approved_outbox.id,
                "status": approved_outbox.status.value,
                "human_approved_by": approved_outbox.human_approved_by,
                "human_approved_at": (
                    approved_outbox.human_approved_at.isoformat()
                    if approved_outbox.human_approved_at
                    else None
                ),
            }
            if approved_outbox
            else None
        ),
    }


@router.post("/admin/handoffs/{handoff_id}/assign")
async def assign_handoff(
    handoff_id: int,
    request: HandoffAssignmentRequest,
    admin: Admin,
    session: Session,
) -> dict[str, Any]:
    try:
        handoff = await assign_handoff_case(
            session,
            handoff_id=handoff_id,
            case_id=request.case_id,
            actor=admin,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"id": handoff.id, "case_id": handoff.case_id, "status": handoff.status}


@router.post("/admin/handoffs/{handoff_id}/cases", status_code=201)
async def create_handoff_case(
    handoff_id: int,
    request: HandoffCaseRequest,
    admin: Admin,
    session: Session,
) -> dict[str, Any]:
    try:
        case = await create_case_for_handoff(
            session,
            handoff_id=handoff_id,
            contact_id=request.contact_id,
            product_id=request.product_id,
            currency=request.currency,
            actor=admin,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"case_id": case.id, "status": case.status.value}


@router.post("/admin/handoffs/{handoff_id}/send", status_code=202)
async def send_handoff_reply(
    handoff_id: int,
    request: HandoffReplyRequest,
    admin: Admin,
    session: Session,
) -> dict[str, Any]:
    try:
        outbox = await queue_human_reply(
            session,
            handoff_id=handoff_id,
            subject=request.subject,
            body_text=request.body_text,
            actor=admin,
            note=request.note,
            resume_automation=request.resume_automation,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "queued": outbox.status in {DeliveryStatus.PENDING, DeliveryStatus.FAILED},
        "outbox_id": outbox.id,
        "status": outbox.status.value,
    }


@router.post("/admin/handoffs/{handoff_id}")
async def update_handoff(
    handoff_id: int,
    update: HandoffUpdate,
    admin: Admin,
    session: Session,
) -> dict[str, Any]:
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
    session.add(
        AuditEvent(
            case_id=handoff.case_id,
            actor=admin,
            event_type="handoff.action",
            data={
                "handoff_id": handoff.id,
                "action": update.action,
                "note": update.note,
            },
        )
    )
    await session.commit()
    return {"id": handoff.id, "status": handoff.status, "action": update.action}


@router.post("/admin/templates/regenerate")
async def regenerate_templates(_: Admin) -> dict[str, str]:
    generate_templates(Path("assets/import_templates"))
    return {"status": "generated"}
