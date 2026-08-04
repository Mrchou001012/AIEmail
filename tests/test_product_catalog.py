from io import BytesIO

import pytest
from openpyxl import load_workbook

from app.ai import stub_analyze
from app.db import Customer, Product, ProductCategory
from app.product_catalog import (
    build_product_list_attachment,
    classify_category_interests,
    customer_interest_keys,
    load_catalog_yaml,
    merge_customer_interests,
    render_product_list_email,
    validate_product_list_email,
)
from app.products import (
    canonical_product_code,
    find_product_codes,
    load_product_aliases,
)


def test_catalog_yaml_has_three_categories_and_unique_products() -> None:
    payload = load_catalog_yaml()
    categories = payload["categories"]
    products = payload["products"]

    assert [item["key"] for item in categories] == [
        "industrial_silanes",
        "pharmaceutical",
        "rubber_plastics",
    ]
    keys = {item["key"] for item in categories}
    codes = [item["code"] for item in products]
    assert len(codes) == len(set(codes)) == 71
    assert all(item["category"] in keys for item in products)
    assert any(item["code"] == "YAC-A110" for item in products)
    assert any(item["code"] == "YAC-HMDS" for item in products)
    acac = next(item for item in products if item["code"] == "ACAC")
    assert acac["category"] == "pharmaceutical"
    n113 = next(item for item in products if item["code"] == "YAC-N113")
    assert n113["cas_no"] == "1185-55-3"
    source_blank_cas_codes = {
        "YAC-BDAC",
        "YAC-TOS",
        "YAC-POS",
        "YAC-MTMS",
        "YAC-TMOS",
        "YAC-TEOS28",
        "ACAC",
        "OH-Polymer 80K",
        "SBM-55",
        "DBM-83",
        "CAA",
        "ZAA",
        "THEIC",
        "AAA",
        "AO-168",
        "AO-1010",
        "AO-1076",
        "UV-770",
        "UV-944",
        "UV-783",
        "UV-622",
        "UV-P",
        "UV-531",
    }
    actual_blank_cas_codes = {
        item["code"] for item in products if item.get("cas_no") is None
    }
    assert actual_blank_cas_codes == source_blank_cas_codes
    assert any(item["code"] == "UV-531" for item in products)
    assert any(item["code"] == "OH-Polymer 80K" for item in products)
    assert any(item["code"] == "YAC-N823(99%)" for item in products)


def test_category_keyword_classification() -> None:
    assert classify_category_interests("工业硅烷") == ["industrial_silanes"]
    assert classify_category_interests("we are interested in industrial silane") == [
        "industrial_silanes"
    ]
    assert classify_category_interests("医药 API") == ["pharmaceutical"]
    assert classify_category_interests("橡塑 PVC heat stabilizers antioxidants") == [
        "rubber_plastics"
    ]
    assert classify_category_interests("rubber and plastics") == ["rubber_plastics"]
    assert classify_category_interests("hello world") == []
    assert classify_category_interests("工业硅烷 and 医药") == [
        "industrial_silanes",
        "pharmaceutical",
    ]


def test_customer_interest_metadata_merges_and_deduplicates() -> None:
    customer = Customer(company_name="Ethachem", metadata_json={})
    assert customer_interest_keys(customer) == []

    merge_customer_interests(
        customer,
        [
            {
                "category_key": "industrial_silanes",
                "category_name": "Industrial Silanes",
                "source": "full_customer_workbook",
                "value": "工业硅烷",
            }
        ],
    )
    merge_customer_interests(
        customer,
        [
            {
                "category_key": "industrial_silanes",
                "category_name": "Industrial Silanes",
                "source": "full_customer_workbook",
                "value": "工业硅烷",
            }
        ],
    )
    assert customer_interest_keys(customer) == ["industrial_silanes"]
    assert len(customer.metadata_json["interests"]) == 1


def test_new_aliases_resolve_product_codes() -> None:
    load_product_aliases.cache_clear()
    try:
        assert canonical_product_code("YAC A110") == "YAC-A110"
        assert canonical_product_code("A110") == "YAC-A110"
        assert canonical_product_code("BCP") == "YAC-BCP"
        assert canonical_product_code("HMDS") == "YAC-HMDS"
        assert canonical_product_code("Acetyl Acetone") == "ACAC"
        assert canonical_product_code("2,4-Pentanedione") == "ACAC"
        assert canonical_product_code("LANNOX 168") == "AO-168"
        assert canonical_product_code("UV 770") == "UV-770"
        assert canonical_product_code("YAC-N823") == "YAC-N823(98%)"
        assert find_product_codes("Please quote BCP for our pharma plant.") == ["YAC-BCP"]
    finally:
        load_product_aliases.cache_clear()


def test_product_list_email_rendering_is_deterministic_and_price_free() -> None:
    category = ProductCategory(key="industrial_silanes", name="Industrial Silanes")
    products = [
        Product(
            code="YAC-A110",
            name="3-AMINOPROPYLTRIETHOXYSILANE",
            brand="YAC",
            cas_no="919-30-2",
            content="95%/97%/98%",
            series="Amine Silane Series (A)",
            sort_order=1,
            id=1,
        ),
        Product(
            code="YAC-N113",
            name="METHYLTRIMETHOXYSILANE",
            brand="YAC",
            cas_no="1185-55-3",
            content="99%",
            series="Alkyl Silane Series (N)",
            sort_order=2,
            id=2,
        ),
    ]

    text, html_body = render_product_list_email(
        contact_name="Alice Buyer",
        category=category,
        products=products,
        subject="Re: product list",
        signature_text="Best regards,\nLanya Sales Team",
        signature_html="<p>Best regards,</p><p>Lanya Sales Team</p>",
    )

    assert "Dear Alice Buyer," in text
    assert "Industrial Silanes" in text
    assert "YAC-A110" in text and "919-30-2" in text
    assert "Amine Silane Series (A)" in text
    assert "Alkyl Silane Series (N)" in text
    assert "US$" not in text and "USD" not in text
    assert "<table" in html_body
    assert "YAC-N113" in html_body
    assert text.count("Best regards,") == 1
    assert html_body.count("Best regards,") == 1


def test_product_list_email_rejects_money_and_commitments() -> None:
    with pytest.raises(ValueError, match="monetary"):
        validate_product_list_email("The price is USD 12.50 per kg.")
    with pytest.raises(ValueError, match="commitment"):
        validate_product_list_email("We guarantee delivery.")


def test_product_list_workbook_uses_only_curated_values_and_keeps_missing_cas_blank() -> None:
    category = ProductCategory(key="pharmaceutical", name="Pharmaceuticals")
    products = [
        Product(
            code="ACAC",
            name="ACETYL ACETONE",
            cas_no=None,
            content="99%",
            series="Pharmaceutical Intermediates",
            sort_order=1,
            id=1,
        ),
        Product(
            code="YAC-N113",
            name="METHYLTRIMETHOXYSILANE",
            cas_no="1185-55-3",
            content="99%",
            series="Alkyl Silane Series (N)",
            sort_order=2,
            id=2,
        ),
    ]

    attachment = build_product_list_attachment(
        category=category,
        products=products,
        file_format="xlsx",
    )
    workbook = load_workbook(BytesIO(attachment.payload), data_only=False)
    sheet = workbook["Product List"]

    assert attachment.filename == "Lanya_Chem_pharmaceutical_product_list.xlsx"
    assert sheet["A1"].value == "No."
    assert sheet["E1"].value == "CAS No."
    assert sheet["C2"].value == "ACAC"
    assert sheet["E2"].value is None
    assert sheet["C3"].value == "YAC-N113"
    assert sheet["E3"].value == "1185-55-3"


def test_attached_product_list_email_requires_a_real_attachment_context() -> None:
    category = ProductCategory(key="pharmaceutical", name="Pharmaceuticals")
    product = Product(
        code="ACAC",
        name="ACETYL ACETONE",
        cas_no=None,
        content="99%",
        sort_order=1,
        id=1,
    )

    text, _ = render_product_list_email(
        contact_name="Alice",
        category=category,
        products=[product],
        subject="Product list in Excel",
        signature_text="Best regards,\nLanya Sales Team",
        signature_html="<p>Best regards,</p><p>Lanya Sales Team</p>",
        attachment_filename="Lanya_Chem_pharmaceutical_product_list.xlsx",
    )

    assert "Please find attached" in text
    with pytest.raises(ValueError, match="attachment claim"):
        validate_product_list_email("Please find attached our product list.")


def test_stub_analysis_classifies_category_only_inquiries_as_product_list() -> None:
    analysis = stub_analyze(
        "Re: inquiry",
        "We are interested in industrial silane. Please send your product list.",
        [],
    )
    assert analysis.intent.value == "product_list_request"
    assert analysis.product_code is None
    assert analysis.intent_confidence == 0.95
    assert analysis.product_confidence == 0.93
    assert "product_code" not in analysis.missing_fields

    analysis = stub_analyze(
        "Re: inquiry",
        "Please send your full product catalog.",
        [],
    )
    assert analysis.intent.value == "product_list_request"

    analysis = stub_analyze(
        "Re: inquiry",
        "PRODUCT YAC-A110. Please quote 100 kg.",
        [],
    )
    assert analysis.intent.value == "quote_request"
    assert analysis.product_code == "YAC-A110"


def test_explicit_product_code_extraction_ignores_list_catalog_words() -> None:
    from app.services import _explicit_product_codes

    assert _explicit_product_codes("Please send your product list for industrial silane.") == []
    assert _explicit_product_codes("Please send your full product catalog.") == []
    assert _explicit_product_codes("Please send the product brochure.") == []
    assert _explicit_product_codes("Please send a sample of your industrial silane products.") == []
    assert _explicit_product_codes("PRODUCTS for the rubber industry, please.") == []
    assert _explicit_product_codes("PRODUCT YAC-A110. Please send your product list.") == [
        "YAC-A110"
    ]
    assert _explicit_product_codes("Please quote PRODUCT WIDGET-100 quantity 100 kg.") == [
        "WIDGET-100"
    ]


def test_stub_analysis_extracts_multi_product_requests_with_quantities() -> None:
    analysis = stub_analyze(
        "Re: quotation",
        "Please quote PRODUCT WIDGET-100 100 kg and PRODUCT WIDGET-200 200 kg.",
        [],
    )
    assert len(analysis.product_requests) == 2
    by_code = {line.product_code: line.quantity for line in analysis.product_requests}
    assert by_code.get("WIDGET-100") == 100
    assert by_code.get("WIDGET-200") == 200

    partial = stub_analyze(
        "Re: quotation",
        "Please quote PRODUCT WIDGET-100 100 kg and PRODUCT WIDGET-200.",
        [],
    )
    by_code = {line.product_code: line.quantity for line in partial.product_requests}
    assert by_code.get("WIDGET-100") == 100
    assert by_code.get("WIDGET-200") is None
