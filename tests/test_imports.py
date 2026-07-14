from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from openpyxl import load_workbook

from app.imports import (
    CUSTOMER_HEADERS,
    PRICE_HEADERS,
    generate_templates,
    import_customers,
    import_prices,
)


def test_template_generation(tmp_path: Path) -> None:
    generate_templates(tmp_path)
    customer = load_workbook(tmp_path / "customer_list_template.xlsx", read_only=True)
    prices = load_workbook(tmp_path / "price_list_template.xlsx", read_only=True)
    assert [cell.value for cell in next(customer.active.iter_rows())] == CUSTOMER_HEADERS
    assert [cell.value for cell in next(prices.active.iter_rows())] == PRICE_HEADERS


@pytest.mark.asyncio
async def test_invalid_customer_email_is_row_error(tmp_path: Path) -> None:
    generate_templates(tmp_path)
    path = tmp_path / "customer_list_template.xlsx"
    workbook = load_workbook(path)
    workbook.active["C2"] = "not-an-email"
    workbook.save(path)
    session = AsyncMock()
    result = await import_customers(path, session, apply=False)
    assert not result.ok
    assert "invalid email" in result.errors[0]["errors"][0]


@pytest.mark.asyncio
async def test_invalid_customer_csv_email_is_row_error(tmp_path: Path) -> None:
    path = tmp_path / "customers.csv"
    path.write_text(
        "company_name,contact_name,email,language,product_code,currency,auto_send_allowed,consent_basis,do_not_contact\n"
        "Demo,Buyer,not-an-email,en,WIDGET-100,USD,true,existing relationship,false\n",
        encoding="utf-8",
    )
    session = AsyncMock()
    result = await import_customers(path, session, apply=False)
    assert not result.ok
    assert "invalid email" in result.errors[0]["errors"][0]


@pytest.mark.asyncio
async def test_floor_above_standard_blocks_price_import(tmp_path: Path) -> None:
    generate_templates(tmp_path)
    path = tmp_path / "price_list_template.xlsx"
    workbook = load_workbook(path)
    workbook.active["E2"] = "50"
    workbook.active["F2"] = "60"
    workbook.save(path)
    session = AsyncMock()
    session.scalar.return_value = None
    result = await import_prices(path, session, apply=False)
    assert not result.ok
    assert any("floor cannot exceed" in error for error in result.errors[0]["errors"])
