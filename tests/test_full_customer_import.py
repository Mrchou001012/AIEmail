from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Contact, Customer, SalesCase
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
    read_full_customer_workbook,
)

HEADERS = [
    COMPANY_HEADER,
    CONTACT_HEADER,
    EMAIL_HEADER,
    OTHER_EMAIL_HEADER,
    PRODUCT_HEADER,
    FIRST_CONTACT_HEADER,
    LAST_CONTACT_HEADER,
    NO_AI_HEADER,
]


def _workbook(path: Path, rows: list[list[object]]) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "customers"
    sheet.append(HEADERS)
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    return path


def test_full_customer_parser_expands_all_addresses_without_inventing_people(
    tmp_path: Path,
) -> None:
    path = _workbook(
        tmp_path / "customers.xlsx",
        [
            [
                "Acme India",
                "Alice Buyer",
                "alice@acme.example",
                "sales@acme.example; branch@acme.example",
                "",
                "2020-01-01",
                "2024-01-01",
                "",
            ]
        ],
    )

    parsed = read_full_customer_workbook(path, ZoneInfo("Asia/Kolkata"))

    assert parsed.source_rows == 1
    assert parsed.rows_with_email == 1
    assert parsed.address_occurrences == 3
    assert set(parsed.endpoints) == {
        "alice@acme.example",
        "sales@acme.example",
        "branch@acme.example",
    }
    assert parsed.endpoints["alice@acme.example"].preferred_contact_name == "Alice Buyer"
    assert parsed.endpoints["sales@acme.example"].preferred_contact_name == "Customer"
    assert parsed.endpoints["branch@acme.example"].preferred_contact_name == "Customer"
    assert parsed.endpoints["sales@acme.example"].preferred_company_name == "Acme India"
    assert parsed.endpoints["alice@acme.example"].first_contact_at.date() == date(
        2020, 1, 1
    )
    assert parsed.unparsed_email_cells == []


def test_full_customer_parser_preserves_ambiguous_company_associations(
    tmp_path: Path,
) -> None:
    path = _workbook(
        tmp_path / "ambiguous.xlsx",
        [
            [
                "Acme India",
                "Alice Buyer",
                "shared@example.com",
                "",
                "YAC-TES",
                "2020-01-01",
                "2024-01-01",
                "",
            ],
            [
                "Acme Branch",
                "Bob Buyer",
                "shared@example.com",
                "",
                "",
                "2021-01-01",
                "2025-01-01",
                "",
            ],
        ],
    )

    parsed = read_full_customer_workbook(path, ZoneInfo("Asia/Kolkata"))
    endpoint = parsed.endpoints["shared@example.com"]

    assert len(parsed.endpoints) == 1
    assert len(endpoint.associations) == 2
    assert {item.company_name for item in endpoint.associations} == {
        "Acme India",
        "Acme Branch",
    }
    assert endpoint.preferred_contact_name == "Customer"
    assert endpoint.first_contact_at.date() == date(2020, 1, 1)
    assert endpoint.last_contact_at.date() == date(2025, 1, 1)


def test_full_customer_parser_reports_unparsed_at_fragments(tmp_path: Path) -> None:
    path = _workbook(
        tmp_path / "invalid.xlsx",
        [["Acme", "", "broken@address", "", "", "", "", ""]],
    )

    parsed = read_full_customer_workbook(path, ZoneInfo("Asia/Kolkata"))

    assert parsed.endpoints == {}
    assert parsed.unparsed_email_cells == [
        {"row": 2, "column": EMAIL_HEADER, "value": "broken@address"}
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_customer_import_is_globally_deduplicated_and_idempotent(
    tmp_path: Path,
    db_session: AsyncSession,
) -> None:
    path = _workbook(
        tmp_path / "customers.xlsx",
        [
            [
                "Acme India",
                "Alice Buyer",
                "shared@example.com",
                "sales@acme.example",
                "",
                "2020-01-01",
                "2024-01-01",
                "",
            ],
            [
                "Acme Branch",
                "Bob Buyer",
                "shared@example.com",
                "",
                "",
                "2021-01-01",
                "2025-01-01",
                "",
            ],
        ],
    )

    preview = await import_full_customer_workbook(path, db_session)
    first = await import_full_customer_workbook(
        path,
        db_session,
        apply=True,
        enable_auto_send=True,
    )
    second = await import_full_customer_workbook(
        path,
        db_session,
        apply=True,
        enable_auto_send=True,
    )

    assert preview.unique_addresses == 2
    assert preview.new_addresses == 2
    assert first.created_contacts == 2
    assert first.created_customers == 1
    assert second.created_contacts == 0
    assert second.created_customers == 0
    assert await db_session.scalar(select(func.count()).select_from(Contact)) == 2
    assert await db_session.scalar(select(func.count()).select_from(Customer)) == 1
    assert await db_session.scalar(select(func.count()).select_from(SalesCase)) == 0
    shared = await db_session.scalar(
        select(Contact).where(Contact.email == "shared@example.com")
    )
    assert shared is not None
    assert shared.name == "Customer"
    assert shared.first_contact_at.date() == date(2020, 1, 1)
    assert shared.last_contact_at.date() == date(2025, 1, 1)
    associations = shared.metadata_json["source_associations"]
    assert len(associations) == 2
    assert {item["company_name"] for item in associations} == {
        "Acme India",
        "Acme Branch",
    }
