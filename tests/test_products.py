import pytest

from app.ai import stub_analyze
from app.products import canonical_product_code, find_product_codes, product_codes_match, product_text_key


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("YAC-TEOS40", "YAC-TEOS40"),
        ("YAC-TEOS-40", "YAC-TEOS40"),
        ("TES", "YAC-TES"),
        ("TMCS", "YAC-TMCS"),
        ("TBDMSC", "YAC-TBDMSC"),
        ("HMM", "YAC-HMM"),
        ("BSA", "YAC-BSA"),
        ("SUN-THEIC", "THEIC"),
        ("OH-POLYMER-80K", "OH-Polymer 80K"),
        ("YAC-N823-98", "YAC-N823(98%)"),
        ("YAC-N823-99", "YAC-N823(99%)"),
    ],
)
def test_aliases_resolve_to_customer_standard(value: str, expected: str) -> None:
    assert canonical_product_code(value) == expected


def test_unspecified_n823_defaults_to_98_percent() -> None:
    assert canonical_product_code("YAC-N823") == "YAC-N823(98%)"
    assert product_codes_match("YAC-N823", "YAC-N823(98%)")
    assert not product_codes_match("YAC-N823", "YAC-N823(99%)")


def test_multiple_product_detection_prefers_specific_overlapping_alias() -> None:
    assert find_product_codes("Please quote N823(99%)") == ["YAC-N823(99%)"]
    assert find_product_codes("Please quote YAC-TES and YAC-TMCS") == ["YAC-TES", "YAC-TMCS"]


def test_stub_recognizes_codes_with_spaces_and_parentheses() -> None:
    result = stub_analyze(
        "Quote request for OH Polymer",
        "Please quote PRODUCT OH-Polymer 80K quantity 200.",
        [],
    )
    assert result.product_code == "OH-Polymer 80K"

    result = stub_analyze(
        "Quotation",
        "Please quote YAC-N823-98, quantity 500.",
        [],
    )
    assert result.product_code == "YAC-N823(98%)"


def test_product_text_keys_are_safe_and_stable() -> None:
    assert product_text_key("OH-Polymer 80K") == "oh_polymer_80k"
    assert product_text_key("YAC-N823(98%)") == "yac_n823_98"
