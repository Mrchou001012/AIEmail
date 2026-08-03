from datetime import date
from decimal import Decimal
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import (
    Contact,
    Customer,
    DeliveryStatus,
    Handoff,
    Outbox,
    PricePolicy,
    Product,
    ProductCategory,
    SalesCase,
)
from app.domain import HandoffReason
from app.product_catalog import import_product_catalog
from app.services import ingest_raw_email, process_inbound, send_one_outbox
from app.settings import get_settings

pytestmark = pytest.mark.integration


async def _seed_catalog_and_interest(
    db_session: AsyncSession,
    *,
    interests: list[str],
    auto_send_allowed: bool = True,
) -> tuple[int, int]:
    await import_product_catalog(db_session, apply=True)
    customer = Customer(
        company_name="Ethachem",
        language="en",
        auto_send_allowed=auto_send_allowed,
        consent_basis="existing CRM/customer-list relationship",
        metadata_json={},
    )
    db_session.add(customer)
    await db_session.flush()
    if interests:
        from app.product_catalog import category_names_by_key, interest_entry, merge_customer_interests

        names = await category_names_by_key(db_session)
        merge_customer_interests(
            customer,
            [
                interest_entry(
                    category_key=key,
                    category_name=names.get(key, key),
                    source="test",
                    value=key,
                )
                for key in interests
            ],
        )
    contact = Contact(
        customer_id=customer.id,
        name="Alice Buyer",
        email="api@ethachem.example",
        language="en",
    )
    db_session.add(contact)
    await db_session.commit()
    return customer.id, contact.id


def _message(
    subject: str,
    body: str,
    message_id: str = "product-list@example.com",
    *,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = "Alice Buyer <api@ethachem.example>"
    message["To"] = "sales-agent@example.com"
    message["Subject"] = subject
    message["Message-ID"] = f"<{message_id}>"
    if in_reply_to is not None:
        message["In-Reply-To"] = in_reply_to
    if references is not None:
        message["References"] = references
    message.set_content(body)
    return message.as_bytes()


async def _queued_product_list(db_session: AsyncSession) -> Outbox | None:
    return await db_session.scalar(
        select(Outbox).where(Outbox.message_kind == "PRODUCT_LIST")
    )


async def test_crm_interest_triggers_automatic_product_list_reply(
    db_session: AsyncSession,
) -> None:
    await _seed_catalog_and_interest(db_session, interests=["industrial_silanes"])
    email_row = await ingest_raw_email(
        db_session,
        _message(
            "Product list inquiry",
            "Please send us your product list for industrial silane.",
        ),
        mailbox="integration-test",
    )

    assert email_row is not None and email_row.case_id is not None
    case = await db_session.get(SalesCase, email_row.case_id)
    assert case is not None
    assert case.product_id is None
    assert case.category_id is not None
    category = await db_session.get(ProductCategory, case.category_id)
    assert category is not None and category.key == "industrial_silanes"
    assert await db_session.scalar(select(func.count()).select_from(Handoff)) == 0

    await process_inbound(db_session, email_row.id)

    outbox = await _queued_product_list(db_session)
    assert outbox is not None
    assert outbox.business_key == f"inbound-product-list:{email_row.id}"
    assert "YAC-A110" in outbox.raw_message
    assert "919-30-2" in outbox.raw_message
    assert "USD" not in outbox.raw_message
    assert "Please send us your product list for industrial silane." in outbox.raw_message
    # One copy in the plain part and one in the HTML part; neither part should
    # duplicate the sign-off already supplied by the configured signature.
    assert outbox.raw_message.count("Best regards,") == 2
    assert await db_session.scalar(select(func.count()).select_from(Handoff)) == 0

    assert await send_one_outbox(db_session) is True
    await db_session.refresh(outbox)
    assert outbox.status == DeliveryStatus.SENT
    outbox_dir = get_settings().runtime_dir / "demo_outbox"
    assert any(outbox_dir.glob("*.eml"))


async def test_generic_category_interest_email_auto_replies(
    db_session: AsyncSession,
) -> None:
    await _seed_catalog_and_interest(db_session, interests=["industrial_silanes"])
    email_row = await ingest_raw_email(
        db_session,
        _message(
            "Hello",
            "We are interested in industrial silane.",
            message_id="generic-interest@example.com",
        ),
        mailbox="integration-test",
    )

    assert email_row is not None and email_row.case_id is not None
    await process_inbound(db_session, email_row.id)

    outbox = await _queued_product_list(db_session)
    assert outbox is not None
    assert "YAC-S313" in outbox.raw_message


async def test_productless_quote_with_category_interest_sends_one_clarification(
    db_session: AsyncSession,
) -> None:
    await _seed_catalog_and_interest(db_session, interests=["industrial_silanes"])
    email_row = await ingest_raw_email(
        db_session,
        _message(
            "Quotation request",
            "Please quote quantity: 100 kg.",
            message_id="productless-quote@example.com",
        ),
        mailbox="integration-test",
    )

    assert email_row is not None and email_row.case_id is not None
    await process_inbound(db_session, email_row.id)

    clarification = await db_session.scalar(
        select(Outbox).where(Outbox.message_kind == "QUOTE_CLARIFICATION")
    )
    assert clarification is not None
    assert clarification.business_key == (
        f"inbound-reply:{email_row.id}:clarification"
    )
    assert "quotation request for 100 kg" in clarification.raw_message
    assert "confirm the product name or Lanya product code" in clarification.raw_message
    assert "Please quote quantity: 100 kg." in clarification.raw_message
    mime = BytesParser(policy=policy.default).parsebytes(
        clarification.raw_message.encode("utf-8")
    )
    assert mime["In-Reply-To"] == email_row.message_id
    assert await db_session.scalar(select(func.count()).select_from(Handoff)) == 0


async def test_productless_quote_recovers_product_from_complete_quoted_thread(
    db_session: AsyncSession,
) -> None:
    await _seed_catalog_and_interest(db_session, interests=["industrial_silanes"])
    email_row = await ingest_raw_email(
        db_session,
        _message(
            "Re: Our requirement",
            (
                "Please quote 100 kg.\n\n"
                "On Monday Alice Buyer wrote:\n"
                "> We are interested in YAC-A110."
            ),
            message_id="quoted-product@example.com",
        ),
        mailbox="integration-test",
    )

    assert email_row is not None and email_row.case_id is not None
    assert "YAC-A110" not in email_row.body_text
    await process_inbound(db_session, email_row.id)

    case = await db_session.get(SalesCase, email_row.case_id)
    assert case is not None
    await db_session.refresh(case, ["product"])
    assert case.product is not None and case.product.code == "YAC-A110"
    assert await db_session.scalar(
        select(func.count())
        .select_from(Outbox)
        .where(Outbox.message_kind == "QUOTE_CLARIFICATION")
    ) == 0


async def test_second_productless_quote_after_clarification_routes_to_human(
    db_session: AsyncSession,
) -> None:
    await _seed_catalog_and_interest(db_session, interests=["industrial_silanes"])
    first = await ingest_raw_email(
        db_session,
        _message(
            "Quotation request",
            "Please quote quantity: 100 kg.",
            message_id="clarification-first@example.com",
        ),
        mailbox="integration-test",
    )
    assert first is not None and first.case_id is not None
    await process_inbound(db_session, first.id)
    clarification = await db_session.scalar(
        select(Outbox).where(Outbox.message_kind == "QUOTE_CLARIFICATION")
    )
    assert clarification is not None

    second = await ingest_raw_email(
        db_session,
        _message(
            f"Re: {first.subject}",
            "Yes, please quote 100 kg.",
            message_id="clarification-second@example.com",
            in_reply_to=clarification.message_id,
            references=clarification.message_id,
        ),
        mailbox="integration-test",
    )
    assert second is not None and second.case_id == first.case_id
    await process_inbound(db_session, second.id)

    assert await db_session.scalar(
        select(func.count())
        .select_from(Outbox)
        .where(Outbox.message_kind == "QUOTE_CLARIFICATION")
    ) == 1
    handoff = await db_session.scalar(
        select(Handoff).where(Handoff.source_email_id == second.id)
    )
    assert handoff is not None
    assert handoff.reason_code == HandoffReason.HUMAN_CONTROL.value
    assert "still unclear" in handoff.summary


async def test_sample_request_still_requires_human(db_session: AsyncSession) -> None:
    await _seed_catalog_and_interest(db_session, interests=["industrial_silanes"])
    email_row = await ingest_raw_email(
        db_session,
        _message(
            "Sample request",
            "Please send a sample of your industrial silane products.",
            message_id="sample@example.com",
        ),
        mailbox="integration-test",
    )

    assert email_row is not None and email_row.case_id is not None
    await process_inbound(db_session, email_row.id)

    assert await _queued_product_list(db_session) is None
    handoff = await db_session.scalar(
        select(Handoff).where(Handoff.source_email_id == email_row.id)
    )
    assert handoff is not None
    assert handoff.reason_code == HandoffReason.SAMPLE_REQUEST.value


async def test_unknown_interest_routes_to_human(db_session: AsyncSession) -> None:
    await _seed_catalog_and_interest(db_session, interests=[])
    email_row = await ingest_raw_email(
        db_session,
        _message(
            "Product list inquiry",
            "Please send us your product list.",
            message_id="no-interest@example.com",
        ),
        mailbox="integration-test",
    )

    assert email_row is not None and email_row.case_id is None
    handoff = await db_session.scalar(
        select(Handoff).where(Handoff.source_email_id == email_row.id)
    )
    assert handoff is not None
    assert handoff.reason_code == HandoffReason.NEW_INQUIRY_REVIEW.value
    assert handoff.extracted_facts["interest_categories"] == []
    assert await _queued_product_list(db_session) is None


async def test_multiple_interests_route_to_human(db_session: AsyncSession) -> None:
    await _seed_catalog_and_interest(
        db_session,
        interests=["industrial_silanes", "pharmaceutical"],
    )
    email_row = await ingest_raw_email(
        db_session,
        _message(
            "Product list inquiry",
            "Please send us your product list.",
            message_id="multi-interest@example.com",
        ),
        mailbox="integration-test",
    )

    assert email_row is not None and email_row.case_id is None
    handoff = await db_session.scalar(
        select(Handoff).where(Handoff.source_email_id == email_row.id)
    )
    assert handoff is not None
    assert handoff.reason_code == HandoffReason.NEW_INQUIRY_REVIEW.value
    assert handoff.extracted_facts["active_interest_categories"] == [
        "industrial_silanes",
        "pharmaceutical",
    ]


async def test_product_specific_list_request_sends_product_category(
    db_session: AsyncSession,
) -> None:
    await _seed_catalog_and_interest(db_session, interests=["industrial_silanes"])
    product = await db_session.scalar(
        select(Product).where(Product.code == "YAC-A110")
    )
    assert product is not None
    db_session.add(
        PricePolicy(
            product_id=product.id,
            currency="USD",
            standard_price=Decimal("10.0000"),
            absolute_floor=Decimal("8.0000"),
            max_discount_pct=Decimal("0.1000"),
            max_negotiation_rounds=2,
            concession_step_pct=Decimal("0.0200"),
            min_quantity=1,
            max_quantity=100000,
            quote_valid_days=30,
            standard_incoterm="EXW",
            allowed_incoterms=["EXW"],
            standard_payment_term="100% before shipment",
            allowed_payment_terms=["100% before shipment"],
            valid_from=date.today(),
            source_hash="test-product-list",
        )
    )
    await db_session.commit()
    email_row = await ingest_raw_email(
        db_session,
        _message(
            "YAC-A110 product list",
            "PRODUCT YAC-A110. Please send your product list.",
            message_id="a110-list@example.com",
        ),
        mailbox="integration-test",
    )

    assert email_row is not None and email_row.case_id is not None
    case = await db_session.get(SalesCase, email_row.case_id)
    assert case is not None and case.product_id is not None
    product = await db_session.get(Product, case.product_id)
    assert product is not None and product.code == "YAC-A110"

    await process_inbound(db_session, email_row.id)

    outbox = await _queued_product_list(db_session)
    assert outbox is not None
    assert "YAC-A110" in outbox.raw_message
    assert "YAC-N113" in outbox.raw_message


async def test_catalog_import_is_idempotent(db_session: AsyncSession) -> None:
    first = await import_product_catalog(db_session, apply=True)
    second = await import_product_catalog(db_session, apply=True)

    assert first["products_created"] == 70
    assert first["categories_created"] == 3
    assert second["products_created"] == 0
    assert second["products_updated"] == 70
    product_count = await db_session.scalar(select(func.count()).select_from(Product))
    category_count = await db_session.scalar(
        select(func.count()).select_from(ProductCategory)
    )
    assert product_count == 70
    assert category_count == 3


async def test_suppressed_customer_never_auto_sends(db_session: AsyncSession) -> None:
    await _seed_catalog_and_interest(
        db_session,
        interests=["industrial_silanes"],
        auto_send_allowed=False,
    )
    email_row = await ingest_raw_email(
        db_session,
        _message(
            "Product list inquiry",
            "Please send us your product list for industrial silane.",
            message_id="no-auto-send@example.com",
        ),
        mailbox="integration-test",
    )

    assert email_row is not None and email_row.case_id is not None
    await process_inbound(db_session, email_row.id)

    assert await _queued_product_list(db_session) is None
    handoff = await db_session.scalar(
        select(Handoff).where(Handoff.source_email_id == email_row.id)
    )
    assert handoff is not None
    assert handoff.reason_code == HandoffReason.SUPPRESSED.value


async def test_full_customer_workbook_stores_interest_category(
    db_session: AsyncSession,
    tmp_path,
) -> None:
    from datetime import date

    from openpyxl import Workbook

    from app.full_customer_import import (
        COMPANY_HEADER,
        CONTACT_HEADER,
        EMAIL_HEADER,
        FIRST_CONTACT_HEADER,
        LAST_CONTACT_HEADER,
        NO_AI_HEADER,
        OTHER_EMAIL_HEADER,
        PRODUCT_HEADER,
        import_full_customer_workbook,
    )

    await import_product_catalog(db_session, apply=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "customers"
    sheet.append(
        [
            COMPANY_HEADER,
            CONTACT_HEADER,
            EMAIL_HEADER,
            OTHER_EMAIL_HEADER,
            PRODUCT_HEADER,
            FIRST_CONTACT_HEADER,
            LAST_CONTACT_HEADER,
            NO_AI_HEADER,
        ]
    )
    sheet.append(
        [
            "Ethachem",
            "Alice Buyer",
            "api@ethachem.example",
            "",
            "工业硅烷",
            date(2020, 1, 1),
            date(2024, 1, 1),
            "",
        ]
    )
    path = tmp_path / "customers.xlsx"
    workbook.save(path)

    await import_full_customer_workbook(
        path,
        db_session,
        apply=True,
        enable_auto_send=True,
    )

    customer = await db_session.scalar(
        select(Customer).where(Customer.company_name == "Ethachem")
    )
    assert customer is not None
    interests = (customer.metadata_json or {}).get("interests")
    assert interests is not None
    assert interests[0]["category_key"] == "industrial_silanes"
    assert interests[0]["value"] == "工业硅烷"
    assert customer.auto_send_allowed is True

    # The stored interest now drives an automatic product-list reply.
    contact = await db_session.scalar(
        select(Contact).where(Contact.email == "api@ethachem.example")
    )
    assert contact is not None
    email_row = await ingest_raw_email(
        db_session,
        _message(
            "Product list inquiry",
            "Please send us your product list for industrial silane.",
            message_id="workbook-import@example.com",
        ),
        mailbox="integration-test",
    )
    assert email_row is not None and email_row.case_id is not None
    await process_inbound(db_session, email_row.id)
    outbox = await _queued_product_list(db_session)
    assert outbox is not None
