from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.db import (
    CaseStage,
    CaseStatus,
    Contact,
    Customer,
    EmailMessage,
    Handoff,
    Job,
    Outbox,
    Product,
    SalesCase,
    SessionLocal,
)
from app.mail import normalized_subject
from app.services import generate_handoff_draft_preview


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a local review-only handoff from an existing inbound email and "
            "generate an AI/RAG draft preview. This script never creates an outbox row "
            "or a DingTalk notification job."
        )
    )
    parser.add_argument("--email-id", type=int, required=True)
    parser.add_argument("--expected-sender", required=True)
    parser.add_argument("--product-code", required=True)
    parser.add_argument("--customer-name", default="Zhou Lei Local Preview")
    parser.add_argument("--contact-name", default="Zhou Lei")
    parser.add_argument("--currency", default="INR")
    parser.add_argument("--actor", default="local-preview")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> dict[str, object]:
    normalized_sender = args.expected_sender.strip().casefold()
    normalized_currency = args.currency.strip().upper()
    if len(normalized_currency) != 3 or not normalized_currency.isalpha():
        raise ValueError("currency must be a three-letter code")

    async with SessionLocal() as session:
        outbox_before = int(
            await session.scalar(select(func.count(Outbox.id))) or 0
        )
        source_email = await session.get(EmailMessage, args.email_id)
        if source_email is None:
            raise ValueError(f"email {args.email_id} not found")
        if source_email.direction != "INBOUND":
            raise ValueError("selected email is not inbound")
        if source_email.from_address.strip().casefold() != normalized_sender:
            raise ValueError(
                f"selected email sender is {source_email.from_address!r}, "
                f"not {normalized_sender!r}"
            )
        if not source_email.body_text.strip():
            raise ValueError("selected email has no body text")

        product = await session.scalar(
            select(Product).where(
                func.lower(Product.code) == args.product_code.strip().casefold()
            )
        )
        if product is None or not product.active:
            raise ValueError(f"active product {args.product_code!r} not found")

        contact = await session.scalar(
            select(Contact).where(func.lower(Contact.email) == normalized_sender)
        )
        if contact is None:
            customer = await session.scalar(
                select(Customer).where(Customer.company_name == args.customer_name)
            )
            if customer is None:
                customer = Customer(
                    company_name=args.customer_name,
                    language="en",
                    auto_send_allowed=False,
                    consent_basis="local draft preview requested by mailbox owner",
                    metadata_json={"local_preview_only": True},
                )
                session.add(customer)
                await session.flush()
            contact = Contact(
                customer_id=customer.id,
                name=args.contact_name,
                email=normalized_sender,
                language="en",
                suppressed=False,
                metadata_json={"local_preview_only": True},
                first_contact_at=source_email.received_at,
                last_contact_at=source_email.received_at,
            )
            session.add(contact)
            await session.flush()
        else:
            customer = await session.get(Customer, contact.customer_id)
            if customer is None:
                raise ValueError("existing contact has no customer")
            customer.auto_send_allowed = False

        sales_case = (
            await session.get(SalesCase, source_email.case_id)
            if source_email.case_id is not None
            else None
        )
        if sales_case is None:
            sales_case = SalesCase(
                customer_id=contact.customer_id,
                contact_id=contact.id,
                product_id=product.id,
                currency=normalized_currency,
                stage=CaseStage.QUOTING,
                status=CaseStatus.WAITING_HUMAN,
                subject_key=normalized_subject(source_email.subject)[:255],
                last_activity_at=datetime.now(UTC),
            )
            session.add(sales_case)
            await session.flush()
        elif (
            sales_case.contact_id != contact.id
            or sales_case.product_id != product.id
        ):
            raise ValueError("selected email is linked to a different contact or product")
        else:
            sales_case.status = CaseStatus.WAITING_HUMAN

        source_email.case_id = sales_case.id
        source_email.customer_id = contact.customer_id
        source_email.contact_id = contact.id

        handoff = await session.scalar(
            select(Handoff).where(Handoff.source_email_id == source_email.id)
        )
        if handoff is None:
            handoff = Handoff(
                case_id=sales_case.id,
                source_email_id=source_email.id,
                reason_code="NEW_INQUIRY_REVIEW",
                summary="Local AI/RAG draft preview requested by mailbox owner",
                extracted_facts={
                    "preview_only": True,
                    "delivery_authorized": False,
                },
                status="OPEN",
                dingtalk_status="SKIPPED",
            )
            session.add(handoff)
            await session.flush()
        elif handoff.case_id not in {None, sales_case.id}:
            raise ValueError("selected email already has a handoff for another case")
        else:
            handoff.case_id = sales_case.id
            handoff.status = "OPEN"
            handoff.dingtalk_status = "SKIPPED"
            facts = dict(handoff.extracted_facts or {})
            facts.update(
                {
                    "preview_only": True,
                    "delivery_authorized": False,
                }
            )
            handoff.extracted_facts = facts
        await session.commit()

        notification_jobs_before = sum(
            1
            for payload in (
                await session.execute(
                    select(Job.payload).where(Job.kind == "notify_handoff")
                )
            ).scalars()
            if str((payload or {}).get("handoff_id")) == str(handoff.id)
        )
        preview = await generate_handoff_draft_preview(
            session,
            handoff_id=handoff.id,
            actor=args.actor,
        )
        outbox_after = int(
            await session.scalar(select(func.count(Outbox.id))) or 0
        )
        approved_outbox = await session.scalar(
            select(Outbox.id).where(Outbox.approval_handoff_id == handoff.id)
        )
        notification_jobs_after = sum(
            1
            for payload in (
                await session.execute(
                    select(Job.payload).where(Job.kind == "notify_handoff")
                )
            ).scalars()
            if str((payload or {}).get("handoff_id")) == str(handoff.id)
        )
        if (
            outbox_after != outbox_before
            or approved_outbox is not None
            or notification_jobs_after != notification_jobs_before
        ):
            raise RuntimeError(
                "preview-only guard failed: delivery or notification work was created"
            )

        return {
            "email_id": source_email.id,
            "case_id": sales_case.id,
            "handoff_id": handoff.id,
            "review_path": f"/admin/handoffs/{handoff.id}/review",
            "sender": source_email.from_address,
            "subject": source_email.subject,
            "classification": preview["analysis"]["intent"],
            "quantity": preview["analysis"]["quantity"],
            "provider": preview["provider"],
            "model": preview["model"],
            "rag_match_count": len(preview["rag_matches"]),
            "rag_matches": preview["rag_matches"],
            "draft_subject": preview["subject"],
            "draft_body": preview["body_text"],
            "outbox_rows_created": outbox_after - outbox_before,
            "dingtalk_jobs_created": (
                notification_jobs_after - notification_jobs_before
            ),
            "dingtalk_status": handoff.dingtalk_status,
        }


def main() -> None:
    print(json.dumps(asyncio.run(run(parse_args())), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
