"""Human-approved email forwarding and recipient-directory workflows."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.common.email_threading import _extract_forward_attachments, _forwarded_message_bodies
from app.common.service_core import HumanApproval, _atomic_business_operation, stage_outbox
from app.db import (
    AuditEvent,
    CaseStatus,
    EmailAddressStatus,
    EmailMessage,
    ForwardRecipient,
    Handoff,
    Outbox,
    SalesCase,
)
from app.delivery.delivery_safety import _normalize_forward_recipient
from app.handoffs.agent_runtime import (
    finalize_handoff_agent_run,
)
from app.mail import (
    FullReplySource,
    OutboundAttachment,
    extract_full_reply_source,
    html_requires_mime_resources,
)
from app.settings import get_settings


async def _touch_forward_recipient(
    session: AsyncSession,
    *,
    email: str,
    name: str = "",
) -> ForwardRecipient:
    now = datetime.now(UTC)
    row = await session.scalar(select(ForwardRecipient).where(ForwardRecipient.email == email).with_for_update())
    if row is None:
        try:
            async with session.begin_nested():
                row = ForwardRecipient(
                    email=email,
                    name=name.strip() or None,
                    last_used_at=now,
                )
                session.add(row)
                await session.flush()
        except IntegrityError:
            row = await session.scalar(select(ForwardRecipient).where(ForwardRecipient.email == email).with_for_update())
            if row is None:
                raise
    if name.strip():
        row.name = name.strip()
    row.last_used_at = now
    await session.flush()
    return row


async def list_forward_recipients(
    session: AsyncSession,
    *,
    query: str = "",
    limit: int = 20,
) -> list[dict[str, Any]]:
    conditions = []
    q = query.strip()
    if q:
        pattern = f"%{q}%"
        conditions.append(
            or_(
                ForwardRecipient.email.ilike(pattern),
                ForwardRecipient.name.ilike(pattern),
            )
        )
    rows = (
        (
            await session.execute(
                select(ForwardRecipient)
                .where(*conditions)
                .order_by(
                    ForwardRecipient.last_used_at.desc().nullslast(),
                    ForwardRecipient.id.desc(),
                )
                .limit(max(1, min(limit, 50)))
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": row.id,
            "email": row.email,
            "name": row.name,
            "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        }
        for row in rows
    ]


async def save_forward_recipient(
    session: AsyncSession,
    *,
    email: str,
    name: str = "",
) -> dict[str, Any]:
    normalized = _normalize_forward_recipient(email)
    row = await _touch_forward_recipient(
        session,
        email=normalized,
        name=name,
    )
    await session.commit()
    return {"id": row.id, "email": row.email, "name": row.name}


@_atomic_business_operation
async def forward_handoff_email(
    session: AsyncSession,
    *,
    handoff_id: int,
    recipient: str,
    actor: str,
    note: str = "",
    touch_recipient_fn: Callable[..., Awaitable[ForwardRecipient]] | None = None,
) -> Outbox:
    """Forward the source email to a salesperson and take the case over.

    The forward keeps the original message as a Gmail-style multipart
    alternative (plain text + sanitized HTML) and preserves non-inline
    attachments. The case is set to human takeover so the AI never replies to
    it again.
    """
    handoff = await session.scalar(select(Handoff).where(Handoff.id == handoff_id).with_for_update())
    if handoff is None:
        raise ValueError("handoff not found")
    if handoff.status != "OPEN":
        raise ValueError("handoff is already resolved")
    if handoff.case_id is None or handoff.source_email_id is None:
        raise ValueError("associate the handoff with a case before forwarding")
    case = await session.scalar(select(SalesCase).options(selectinload(SalesCase.contact)).where(SalesCase.id == handoff.case_id))
    if case is None:
        raise ValueError("handoff case no longer exists")
    source_email = await session.get(EmailMessage, handoff.source_email_id)
    if source_email is None:
        raise ValueError("handoff source email no longer exists")
    settings = get_settings()
    recipient = _normalize_forward_recipient(recipient)
    address_status = await session.get(EmailAddressStatus, recipient)
    if address_status is not None and address_status.suppressed:
        raise ValueError("recipient address is permanently suppressed")
    archive_folder = "mail_archive" if source_email.is_history else "inbound_archive"
    archive_path = settings.runtime_dir / archive_folder / f"{source_email.raw_sha256}.eml"
    attachments: tuple[OutboundAttachment, ...] = ()
    try:
        raw = archive_path.read_bytes()
        source = extract_full_reply_source(raw)
        attachments = _extract_forward_attachments(raw)
    except OSError:
        if html_requires_mime_resources(source_email.body_html):
            raise ValueError("the original email archive with inline images is unavailable for forwarding") from None
        source = FullReplySource(
            body_text=source_email.body_text,
            body_html=source_email.body_html,
        )

    text_body, html_body = _forwarded_message_bodies(
        note=note,
        source=source,
        original_from=source_email.from_address,
        original_to=", ".join(source_email.to_addresses),
        original_subject=source_email.subject,
        occurred_at=source_email.received_at,
    )
    subject = f"Fwd: {source_email.subject}" if source_email.subject else "Fwd: (no subject)"
    approved_at = datetime.now(UTC)
    outbox = await stage_outbox(
        session,
        case=case,
        message_kind="FORWARD",
        recipient=recipient,
        subject=subject[:998],
        text_body=text_body,
        html_body=html_body,
        business_key=f"handoff-reply:{handoff.id}:forward",
        in_reply_to=None,
        references=[],
        inline_images=source.inline_images,
        attachments=attachments,
        approval=HumanApproval(
            handoff_id=handoff.id,
            approved_by=actor[:128],
            approved_at=approved_at,
        ),
    )
    if outbox is None:
        raise ValueError("a forward is already queued for this handoff")

    normalized = recipient
    await (touch_recipient_fn or _touch_forward_recipient)(
        session,
        email=normalized,
    )

    handoff.status = "RESOLVED"
    handoff.resolution_note = note.strip() or f"Forwarded by {actor} to {recipient} for human takeover"
    await finalize_handoff_agent_run(
        session,
        handoff_id=handoff.id,
        actor=actor,
        outcome="forwarded-to-salesperson",
        cancelled=True,
    )
    if handoff.dingtalk_status != "SENT":
        handoff.dingtalk_status = "CANCELLED"
    case.status = CaseStatus.HUMAN_TAKEOVER
    session.add(
        AuditEvent(
            case_id=case.id,
            actor=actor,
            event_type="handoff.forwarded_to_salesperson",
            data={
                "handoff_id": handoff.id,
                "outbox_id": outbox.id,
                "recipient": normalized,
                "case_status": case.status.value,
                "attachments": len(attachments),
            },
        )
    )
    await session.commit()
    return outbox
