"""Human reply service workflow."""

import html
from datetime import UTC, datetime
from email.utils import parseaddr

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.common.email_identity import strip_duplicate_signature_lead as _strip_duplicate_signature_lead
from app.common.email_threading import _reply_references, _reply_source
from app.common.service_core import audit
from app.db import CaseStatus, EmailAddressStatus, EmailMessage, Handoff, Outbox, Quote, SalesCase
from app.handoffs.agent_runtime import finalize_handoff_agent_run
from app.imports import load_content
from app.mail import OutboundAttachment, append_quoted_reply, build_message, parse_mime
from app.settings import get_settings


async def queue_human_reply(
    session: AsyncSession,
    *,
    handoff_id: int,
    subject: str,
    body_text: str,
    actor: str,
    note: str = "",
    resume_automation: bool = False,
    attachments: tuple[OutboundAttachment, ...] = (),
    quote: Quote | None = None,
) -> Outbox:
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None:
        raise ValueError("handoff not found")
    existing = await session.scalar(
        select(Outbox).where(
            or_(
                Outbox.approval_handoff_id == handoff.id,
                Outbox.business_key == f"handoff-reply:{handoff.id}",
            )
        )
    )
    if existing is not None:
        return existing
    if handoff.status != "OPEN":
        raise ValueError("handoff is already resolved")
    if handoff.case_id is None or handoff.source_email_id is None:
        raise ValueError("associate the handoff with a case before replying")
    source_email = await session.get(EmailMessage, handoff.source_email_id)
    case = await session.scalar(
        select(SalesCase)
        .options(
            selectinload(SalesCase.customer),
            selectinload(SalesCase.contact),
            selectinload(SalesCase.product),
        )
        .where(SalesCase.id == handoff.case_id)
    )
    if source_email is None or case is None:
        raise ValueError("source email or associated case not found")
    if source_email.direction != "INBOUND":
        raise ValueError("human reply requires an inbound source email")
    if source_email.from_address.casefold() != case.contact.email.casefold():
        raise ValueError("source sender does not match the associated case contact")
    if case.status in {CaseStatus.CLOSED_WON, CaseStatus.CLOSED_LOST}:
        raise ValueError("closed case cannot send a reviewed reply")
    if case.customer.do_not_contact or case.contact.suppressed:
        raise ValueError("recipient is suppressed or marked do-not-contact")
    address_status = await session.get(EmailAddressStatus, case.contact.email.casefold())
    if address_status is not None and address_status.suppressed:
        raise ValueError("recipient address is permanently suppressed")

    clean_subject = subject.strip()
    clean_body = body_text.strip()
    if not clean_subject or "\r" in clean_subject or "\n" in clean_subject:
        raise ValueError("subject must be a single non-empty line")
    if not clean_body:
        raise ValueError("reply body cannot be empty")
    settings = get_settings()
    bundle = load_content(settings.content_dir)
    clean_body = _strip_duplicate_signature_lead(
        clean_body,
        bundle.signature_text,
    )
    if not clean_body:
        raise ValueError("reply body cannot contain only the signature sign-off")
    signed_text = "\n".join([clean_body, "", bundle.signature_text.strip()])
    html_lines = [f"<p>{html.escape(line) if line else '&nbsp;'}</p>" for line in clean_body.splitlines()]
    signed_html = "".join(html_lines) + bundle.signature_html
    source = _reply_source(source_email)
    signed_text, signed_html = append_quoted_reply(
        signed_text,
        signed_html,
        from_address=source_email.from_address,
        source_body=source.body_text,
        source_html=source.body_html,
        occurred_at=source_email.received_at,
    )
    references = _reply_references(source_email)
    business_key = f"handoff-reply:{handoff.id}"
    message_id, raw = build_message(
        from_address=get_settings().mail_from,
        recipient=case.contact.email,
        subject=clean_subject,
        text_body=signed_text,
        html_body=signed_html,
        stable_key=business_key,
        in_reply_to=source_email.message_id,
        references=references,
        inline_images=source.inline_images,
        attachments=attachments,
    )
    parsed_outbound = parse_mime(raw.encode("utf-8"))
    now = datetime.now(UTC)
    outbox = Outbox(
        case_id=case.id,
        quote_id=quote.id if quote is not None else None,
        message_kind="HUMAN_REPLY",
        business_key=business_key,
        message_id=message_id,
        recipient=case.contact.email,
        raw_message=raw,
        approval_handoff_id=handoff.id,
        human_approved_by=actor[:128],
        human_approved_at=now,
    )
    session.add(outbox)
    await session.flush()
    session.add(
        EmailMessage(
            case_id=case.id,
            customer_id=case.customer_id,
            contact_id=case.contact_id,
            direction="OUTBOUND",
            message_id=message_id,
            in_reply_to=source_email.message_id,
            references_json=references,
            from_address=parseaddr(get_settings().mail_from)[1],
            to_addresses=[case.contact.email],
            subject=clean_subject,
            body_text=signed_text,
            body_html=signed_html,
            attachment_metadata=parsed_outbound.attachments,
            raw_sha256=parsed_outbound.raw_sha256,
        )
    )
    handoff.status = "RESOLVED"
    handoff.resolution_note = note.strip() or f"Reply approved by {actor}"
    case.status = CaseStatus.ACTIVE if resume_automation else CaseStatus.HUMAN_TAKEOVER
    await finalize_handoff_agent_run(
        session,
        handoff_id=handoff.id,
        actor=actor,
        outcome="human-reply",
    )
    await audit(
        session,
        "handoff.reply_approved",
        case_id=case.id,
        actor=actor,
        data={
            "handoff_id": handoff.id,
            "outbox_id": outbox.id,
            "message_id": message_id,
            "resume_automation": resume_automation,
            "attachments": [
                {
                    "filename": item["filename"],
                    "content_type": item["content_type"],
                    "size": item["size"],
                    "sha256": item["sha256"],
                }
                for item in parsed_outbound.attachments
                if item.get("disposition") == "attachment"
            ],
        },
    )
    await session.commit()
    return outbox
