import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.catalogs.product_list_service import (
    official_product_catalog_attachment,
    official_product_catalog_sha256,
)
from app.db import Base, Contact, Customer
from app.inbound.inquiry_resolution import _resolve_new_inquiry_case
from app.mail import ParsedEmail
from app.settings import get_settings


def test_official_product_catalog_is_the_versioned_pdf() -> None:
    expected = (get_settings().content_dir / "Catalog.pdf").read_bytes()

    attachment = official_product_catalog_attachment()

    assert attachment.filename == "Catalog.pdf"
    assert attachment.content_type == "application/pdf"
    assert attachment.payload == expected
    assert attachment.payload.startswith(b"%PDF-")
    assert official_product_catalog_sha256(attachment) == hashlib.sha256(
        expected
    ).hexdigest()


@pytest.mark.parametrize("payload", [None, b"not a pdf"])
def test_official_product_catalog_fails_closed(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes | None,
) -> None:
    monkeypatch.setattr(get_settings(), "content_dir", tmp_path)
    if payload is not None:
        (tmp_path / "Catalog.pdf").write_bytes(payload)

    with pytest.raises(ValueError, match="official product catalog"):
        official_product_catalog_attachment()


@pytest.mark.asyncio
async def test_plain_product_list_resolution_skips_customer_categories() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            customer = Customer(
                company_name="Catalog Buyer",
                language="en",
                auto_send_allowed=True,
                consent_basis="existing relationship",
                do_not_contact=False,
                metadata_json={
                    "interests": [
                        {"category_key": "industrial_silanes"},
                        {"category_key": "pharmaceutical"},
                    ]
                },
            )
            session.add(customer)
            await session.flush()
            session.add(
                Contact(
                    customer_id=customer.id,
                    name="Alice",
                    email="alice@catalog-buyer.example",
                    language="en",
                    suppressed=False,
                    metadata_json={},
                )
            )
            await session.commit()

            result = await _resolve_new_inquiry_case(
                session,
                ParsedEmail(
                    message_id="<catalog-request@example.com>",
                    in_reply_to=None,
                    references=[],
                    from_address="alice@catalog-buyer.example",
                    to_addresses=["sales@example.com"],
                    subject="Product list",
                    body_text="Please send your complete product catalog.",
                    body_html=None,
                    attachments=[],
                    header_metadata={},
                    raw_sha256="a" * 64,
                    occurred_at=datetime.now(UTC),
                ),
            )

            assert result.case is not None
            assert result.case.product_id is None
            assert result.case.category_id is None
            assert result.facts["category_pending"] is False
            assert result.facts["match_basis"] == "official_product_catalog_request"
    finally:
        await engine.dispose()
