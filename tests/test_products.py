import pytest

from app.ai import stub_analyze
from app.catalogs.products import canonical_product_code, find_product_codes, product_codes_match, product_text_key


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("DEMO-P200", "DEMO-P200"),
        ("Intermediate 200", "DEMO-P200"),
        ("P300", "DEMO-P300"),
        ("P301", "DEMO-P301"),
        ("REVIEW", "DEMO-REVIEW"),
        ("P303", "DEMO-P303"),
        ("P304", "DEMO-P304"),
        ("Silicone Fluid 800", "DEMO-P800"),
        ("DEMO-GRADE-A-98", "DEMO-GRADE-A(98%)"),
        ("DEMO-GRADE-A-99", "DEMO-GRADE-A(99%)"),
    ],
)
def test_aliases_resolve_to_customer_standard(value: str, expected: str) -> None:
    assert canonical_product_code(value) == expected


def test_unspecified_demo_grade_defaults_to_98_percent() -> None:
    assert canonical_product_code("DEMO-GRADE-A") == "DEMO-GRADE-A(98%)"
    assert product_codes_match("DEMO-GRADE-A", "DEMO-GRADE-A(98%)")
    assert not product_codes_match("DEMO-GRADE-A", "DEMO-GRADE-A(99%)")


def test_multiple_product_detection_prefers_specific_overlapping_alias() -> None:
    assert find_product_codes("Please quote GRADE-A(99%)") == ["DEMO-GRADE-A(99%)"]
    assert find_product_codes("Please quote DEMO-P300 and DEMO-P301") == ["DEMO-P300", "DEMO-P301"]


def test_stub_recognizes_codes_with_spaces_and_parentheses() -> None:
    result = stub_analyze(
        "Quote request for OH Polymer",
        "Please quote PRODUCT DEMO-P800 quantity 200.",
        [],
    )
    assert result.product_code == "DEMO-P800"

    result = stub_analyze(
        "Quotation",
        "Please quote DEMO-GRADE-A-98, quantity 500.",
        [],
    )
    assert result.product_code == "DEMO-GRADE-A(98%)"


def test_product_text_keys_are_safe_and_stable() -> None:
    assert product_text_key("DEMO-P800") == "demo_p800"
    assert product_text_key("DEMO-GRADE-A(98%)") == "demo_grade_a_98"
