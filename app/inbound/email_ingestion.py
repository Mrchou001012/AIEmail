"""Email ingestion workflow."""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.service_core import CaseLessReactivationParent, NewInquiryResolution, audit
from app.db import AuditEvent, DeliveryStatus, EmailMessage, Outbox, SalesCase
from app.domain import HandoffReason
from app.history import resolve_unique_contact
from app.inbound.auto_replies import AutomatedReplyType, classify_automated_reply
from app.inbound.bounces import classify_bounce
from app.inbound.inbound_disposition import classify_inbound_disposition
from app.inbound.inbound_followup import _ensure_inbound_follow_up
from app.inbound.inquiry_resolution import _resolve_new_inquiry_case
from app.inbound.reactivation_threading import _case_less_reactivation_parent
from app.mail import match_case, parse_mime
from app.reactivation import record_reactivation_reply
from app.settings import get_settings


async def ingest_raw_email(
    session: AsyncSession,
    raw: bytes,
    *,
    mailbox: str = "demo",
    mailbox_folder: str | None = None,
    uid_validity: int | None = None,
    imap_uid: int | None = None,
    direction: str = "INBOUND",
    is_history: bool = False,
) -> EmailMessage | None:
    direction = direction.upper()
    if direction not in {"INBOUND", "OUTBOUND"}:
        raise ValueError(f"unsupported email direction: {direction}")
    parsed = parse_mime(raw)
    bounce = (
        classify_bounce(
            raw,
            subject=parsed.subject,
            body=parsed.body_text,
            sender=parsed.from_address,
        )
        if direction == "INBOUND"
        else None
    )
    automated_reply = (
        classify_automated_reply(
            subject=parsed.subject,
            body=parsed.body_text,
            headers=parsed.header_metadata,
            sender=parsed.from_address,
        )
        if direction == "INBOUND" and not (bounce and bounce.is_bounce)
        else None
    )
    disposition = (
        classify_inbound_disposition(
            subject=parsed.subject,
            body=parsed.body_text,
            headers=parsed.header_metadata,
            sender=parsed.from_address,
            internal_domains=get_settings().inbound_disposition_internal_domains,
        )
        if direction == "INBOUND" and not (bounce and bounce.is_bounce)
        else None
    )
    duplicate_query = select(EmailMessage).where(
        (EmailMessage.raw_sha256 == parsed.raw_sha256)
        | ((EmailMessage.message_id == parsed.message_id) & EmailMessage.message_id.is_not(None))
    )
    duplicate = await session.scalar(duplicate_query)
    if duplicate:
        if direction == "INBOUND" and duplicate.direction == "INBOUND" and not is_history:
            await _ensure_inbound_follow_up(session, duplicate)
        return duplicate
    # DEPARTED / CONTACT_CHANGE messages are normally written by a human
    # (for example "Pooja no longer works here, please send the product
    # list to this address"). They must enter the normal new-inquiry and
    # product-list pipeline instead of being swallowed as automated noise,
    # while keeping subject-based case matching for threadless replies.
    personnel_reply = bool(
        automated_reply
        and automated_reply.reply_type
        in {
            AutomatedReplyType.DEPARTED,
            AutomatedReplyType.CONTACT_CHANGE,
        }
    )
    live_human_inbound = bool(
        direction == "INBOUND"
        and not is_history
        and not (bounce and bounce.is_bounce)
        and not (automated_reply and automated_reply.is_automated and not personnel_reply)
    )
    is_system_notification = bool(automated_reply and automated_reply.reply_type is AutomatedReplyType.SYSTEM_NOTIFICATION)
    if is_system_notification:
        # Trusted infrastructure notifications are mailbox records, not sales
        # participants. Never attach them to a coincidentally similar thread.
        case, ambiguous = None, False
    else:
        case, ambiguous = await match_case(
            session,
            parsed,
            direction=direction,
            # A live human-authored message may inherit commercial history only
            # through Message-ID/References. Subject-only matching is retained for
            # history reconciliation, outbound mail, and non-sending auto replies.
            allow_subject_fallback=not live_human_inbound or personnel_reply,
        )
    new_inquiry = NewInquiryResolution(case)
    reactivation_parent: CaseLessReactivationParent | None = None
    personnel_change_handled = False
    if live_human_inbound and case is None and not ambiguous:
        has_thread_headers = bool(parsed.in_reply_to or parsed.references)
        if has_thread_headers:
            reactivation_parent = await _case_less_reactivation_parent(session, parsed)
            if reactivation_parent is not None:
                new_inquiry = await _resolve_new_inquiry_case(
                    session,
                    parsed,
                    trusted_reactivation_parent=reactivation_parent,
                )
                case = new_inquiry.case
            else:
                # A concurrent first reply can promote a case-less reactivation
                # while this transaction waits for the parent row lock. Re-run
                # the authoritative header match once before escalating.
                case, ambiguous = await match_case(
                    session,
                    parsed,
                    direction=direction,
                    allow_subject_fallback=False,
                )
                if case is not None:
                    new_inquiry = NewInquiryResolution(case)
                else:
                    new_inquiry = NewInquiryResolution(
                        None,
                        HandoffReason.THREAD_AMBIGUOUS,
                        "Inbound reply contains thread references that do not match a known case",
                        {
                            "new_thread": False,
                            "sender": parsed.from_address,
                            "subject": parsed.subject,
                            "in_reply_to": parsed.in_reply_to,
                            "references": parsed.references,
                        },
                    )
        else:
            new_inquiry = await _resolve_new_inquiry_case(session, parsed)
            case = new_inquiry.case
    if reactivation_parent is not None and case is not None:
        reactivation_parent.outbox.case_id = case.id
        reactivation_parent.recipient.case_id = case.id
        await session.execute(
            update(EmailMessage)
            .where(
                EmailMessage.message_id == reactivation_parent.outbox.message_id,
                EmailMessage.direction == "OUTBOUND",
                EmailMessage.case_id.is_(None),
            )
            .values(case_id=case.id)
        )
        session.add(
            AuditEvent(
                case_id=case.id,
                actor="thread_resolver",
                event_type="reactivation.thread_promoted",
                data={
                    "outbox_id": reactivation_parent.outbox.id,
                    "recipient_id": reactivation_parent.recipient.id,
                },
            )
        )
        if reactivation_parent.sender_changed:
            session.add(
                AuditEvent(
                    case_id=case.id,
                    actor="thread_resolver",
                    event_type="reactivation.sender_endpoint_linked",
                    data={
                        "outbox_id": reactivation_parent.outbox.id,
                        "recipient_id": reactivation_parent.recipient.id,
                        "original_contact_id": (reactivation_parent.original_contact.id),
                        "original_email": (reactivation_parent.original_contact.email),
                        "reply_contact_id": reactivation_parent.reply_contact.id,
                        "reply_email": reactivation_parent.reply_contact.email,
                        "reply_contact_created": (reactivation_parent.reply_contact_created),
                        "matched_domain": reactivation_parent.matched_domain,
                    },
                )
            )
        if (
            automated_reply
            and automated_reply.reply_type
            in {
                AutomatedReplyType.DEPARTED,
                AutomatedReplyType.CONTACT_CHANGE,
            }
            and disposition is not None
            and (reactivation_parent.sender_changed or disposition.automated_transport_signal)
            and not get_settings().inbound_disposition_enabled
        ):
            original = reactivation_parent.original_contact
            if original.id != reactivation_parent.reply_contact.id:
                # The historical recipient left the company / changed roles;
                # retire that endpoint while keeping the new reply contact
                # active so the business request can still be handled.
                original.suppressed = True
                if automated_reply.reply_type is AutomatedReplyType.DEPARTED:
                    original.lifecycle_status = "DEPARTED"
                    original.unavailable_until = None
                personnel_change_handled = True
                session.add(
                    AuditEvent(
                        case_id=case.id,
                        actor="thread_resolver",
                        event_type="contact.suppressed_for_personnel_change",
                        data={
                            "original_contact_id": original.id,
                            "original_email": original.email,
                            "reply_contact_id": reactivation_parent.reply_contact.id,
                            "reply_email": reactivation_parent.reply_contact.email,
                            "automated_reply_type": automated_reply.reply_type.value,
                        },
                    )
                )
            else:
                # Same endpoint is the one that left; suppress it as well.
                original.suppressed = True
                if automated_reply.reply_type is AutomatedReplyType.DEPARTED:
                    original.lifecycle_status = "DEPARTED"
                    original.unavailable_until = None
                personnel_change_handled = True
                session.add(
                    AuditEvent(
                        case_id=case.id,
                        actor="thread_resolver",
                        event_type="contact.suppressed_for_personnel_change",
                        data={
                            "original_contact_id": original.id,
                            "original_email": original.email,
                            "reply_contact_id": reactivation_parent.reply_contact.id,
                            "reply_email": reactivation_parent.reply_contact.email,
                            "automated_reply_type": automated_reply.reply_type.value,
                        },
                    )
                )
    matched_outbox = None
    if bounce and bounce.is_bounce and bounce.original_message_id:
        matched_outbox = await session.scalar(
            select(Outbox).where(
                Outbox.message_id == bounce.original_message_id,
                Outbox.status == DeliveryStatus.SENT,
            )
        )
        if case is None and matched_outbox and matched_outbox.case_id:
            case = await session.get(SalesCase, matched_outbox.case_id)
            ambiguous = False
    bounce_metadata = bounce.metadata() if bounce and bounce.is_bounce else {}
    if matched_outbox is not None:
        bounce_metadata["matched_outbox_id"] = matched_outbox.id
    identity_contact = None
    if case is None and not is_system_notification:
        identity_addresses = [parsed.from_address] if direction == "INBOUND" else parsed.to_addresses
        identity_contact = await resolve_unique_contact(session, identity_addresses)
    identity_customer_id = case.customer_id if case is not None else identity_contact.customer_id if identity_contact is not None else None
    identity_contact_id = case.contact_id if case is not None else identity_contact.id if identity_contact is not None else None
    disposition_metadata = disposition.metadata() if disposition else {}
    if (
        disposition is not None
        and reactivation_parent is not None
        and automated_reply is not None
        and automated_reply.reply_type in {AutomatedReplyType.DEPARTED, AutomatedReplyType.CONTACT_CHANGE}
    ):
        disposition_metadata = {
            **disposition_metadata,
            "verified_reactivation_parent": True,
            "original_contact_id": reactivation_parent.original_contact.id,
            "reply_contact_id": reactivation_parent.reply_contact.id,
            "sender_changed": reactivation_parent.sender_changed,
        }
    try:
        async with session.begin_nested():
            automated_metadata = (
                {**automated_reply.metadata(), "headers": parsed.header_metadata}
                if automated_reply and automated_reply.is_automated
                else {}
            )
            if personnel_change_handled and reactivation_parent is not None:
                automated_metadata["personnel_change_handled"] = True
                automated_metadata["original_contact_id"] = reactivation_parent.original_contact.id
            row = EmailMessage(
                case_id=case.id if case else None,
                customer_id=identity_customer_id,
                contact_id=identity_contact_id,
                direction=direction,
                mailbox=mailbox,
                mailbox_folder=mailbox_folder,
                uid_validity=uid_validity,
                imap_uid=imap_uid,
                message_id=parsed.message_id,
                in_reply_to=parsed.in_reply_to,
                references_json=parsed.references,
                from_address=parsed.from_address,
                to_addresses=parsed.to_addresses,
                subject=parsed.subject,
                body_text=parsed.body_text,
                body_html=parsed.body_html,
                attachment_metadata=parsed.attachments,
                raw_sha256=parsed.raw_sha256,
                is_history=is_history,
                is_automated_reply=bool(automated_reply and automated_reply.is_automated),
                automated_reply_type=(
                    automated_reply.reply_type.value if automated_reply and automated_reply.reply_type is not None else None
                ),
                automated_reply_metadata=automated_metadata,
                disposition_type=(disposition.disposition_type.value if disposition else None),
                disposition_confidence=(Decimal(str(disposition.confidence)) if disposition else None),
                disposition_metadata=disposition_metadata,
                is_bounce=bool(bounce and bounce.is_bounce),
                bounce_type=(bounce.bounce_type.value if bounce and bounce.bounce_type is not None else None),
                bounce_metadata=bounce_metadata,
                received_at=parsed.occurred_at or datetime.now(UTC),
            )
            session.add(row)
            await session.flush()
    except IntegrityError:
        # The personnel-change / thread-linking side effects above live
        # outside the savepoint and must not survive a duplicate-email
        # collision (for example two IMAP syncs racing on the same message).
        await session.rollback()
        duplicate = await session.scalar(duplicate_query)
        if duplicate is None:
            raise
        if direction == "INBOUND" and duplicate.direction == "INBOUND" and not is_history:
            await _ensure_inbound_follow_up(session, duplicate)
        return duplicate

    archive_dir = "mail_archive" if is_history or direction == "OUTBOUND" else "inbound_archive"
    archive = get_settings().runtime_dir / archive_dir / f"{parsed.raw_sha256}.eml"
    archive.write_bytes(raw)
    await audit(
        session,
        "email.history_ingested" if is_history else "email.ingested",
        case_id=case.id if case else None,
        actor="gmail_history" if is_history else ("imap" if mailbox != "demo" else "demo"),
        data={
            "email_id": row.id,
            "message_id": parsed.message_id,
            "direction": direction,
            "mailbox": mailbox,
            "mailbox_folder": mailbox_folder,
            "automated_reply_type": row.automated_reply_type,
            "bounce_type": row.bounce_type,
        },
    )
    if new_inquiry.case is not None and new_inquiry.facts is not None and live_human_inbound:
        await audit(
            session,
            ("email.thread_recovered" if new_inquiry.facts.get("recovered_thread") else "case.created_from_new_inquiry"),
            case_id=new_inquiry.case.id,
            actor="thread_resolver",
            data=new_inquiry.facts,
        )
    if direction == "INBOUND" and not is_history and reactivation_parent is not None:
        await record_reactivation_reply(
            session,
            row,
            recipient_id=reactivation_parent.recipient.id,
            allow_changed_contact=reactivation_parent.sender_changed,
            commit=False,
        )
    await session.commit()
    if direction == "INBOUND" and not is_history:
        await _ensure_inbound_follow_up(
            session,
            row,
            ambiguous=ambiguous,
            review_reason=new_inquiry.reason,
            review_summary=new_inquiry.summary,
            review_facts=new_inquiry.facts,
        )
    return row
