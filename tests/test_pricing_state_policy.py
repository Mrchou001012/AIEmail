from datetime import date
from decimal import Decimal

import pytest

from app.db import CaseStage, CaseStatus
from app.domain import (
    HandoffReason,
    Intent,
    PricingPolicy,
    SendContext,
    counteroffer,
    evaluate_send_policy,
    hard_minimum,
    initial_quote,
    quote_valid_until,
    transition,
)


@pytest.fixture
def policy() -> PricingPolicy:
    return PricingPolicy(
        standard_price=Decimal("100"),
        absolute_floor=Decimal("82"),
        max_discount_pct=Decimal("0.15"),
        concession_step_pct=Decimal("0.03"),
        max_negotiation_rounds=2,
        min_quantity=10,
        max_quantity=10000,
        currency="USD",
        standard_incoterm="EXW",
        allowed_incoterms=("EXW", "FCA"),
        standard_payment_term="100% before shipment",
        allowed_payment_terms=("100% before shipment",),
    )


def test_hard_floor_uses_stricter_floor(policy: PricingPolicy) -> None:
    assert hard_minimum(policy) == Decimal("85.0000")


def test_counteroffer_never_crosses_floor(policy: PricingPolicy) -> None:
    for requested in range(85, 101):
        decision = counteroffer(policy, Decimal("100"), Decimal(requested), 0, 100)
        assert decision.approved
        assert decision.unit_price is not None
        assert decision.unit_price >= Decimal("85")


def test_below_floor_hands_off(policy: PricingPolicy) -> None:
    decision = counteroffer(policy, Decimal("100"), Decimal("80"), 0, 100)
    assert not decision.approved
    assert "floor" in (decision.reason or "")


def test_allowed_state_transition() -> None:
    assert transition(CaseStage.QUOTING, CaseStage.NEGOTIATING) == CaseStage.NEGOTIATING


def test_forbidden_state_transition() -> None:
    with pytest.raises(ValueError):
        transition(CaseStage.QUOTING, CaseStage.SHIPPING)


@pytest.mark.parametrize(
    ("intent", "reason"),
    [
        (Intent.COUNTEROFFER, HandoffReason.PRICE_NEGOTIATION),
        (Intent.SAMPLE_REQUEST, HandoffReason.SAMPLE_REQUEST),
        (Intent.ORDER, HandoffReason.ORDER_COMMITMENT),
        (Intent.SHIPPING, HandoffReason.SHIPPING_REQUEST),
        (Intent.TECHNICAL, HandoffReason.TECHNICAL_REQUEST),
        (Intent.COMPLAINT, HandoffReason.COMPLAINT),
    ],
)
def test_risky_intents_always_handoff(intent: Intent, reason: HandoffReason) -> None:
    decision = evaluate_send_policy(
        SendContext(
            intent=intent,
            stage=CaseStage.QUOTING,
            status=CaseStatus.ACTIVE,
            intent_confidence=1,
            product_confidence=1,
            numeric_confidence=1,
            auto_send_allowed=True,
            contact_suppressed=False,
            do_not_contact=False,
            has_risky_attachment=False,
        )
    )
    assert not decision.allow_send
    assert decision.reason == reason


def test_low_confidence_hands_off() -> None:
    decision = evaluate_send_policy(
        SendContext(
            intent=Intent.QUOTE_REQUEST,
            stage=CaseStage.QUOTING,
            status=CaseStatus.ACTIVE,
            intent_confidence=0.7,
            product_confidence=1,
            numeric_confidence=1,
            auto_send_allowed=True,
            contact_suppressed=False,
            do_not_contact=False,
            has_risky_attachment=False,
        )
    )
    assert not decision.allow_send
    assert decision.reason == HandoffReason.LOW_CONFIDENCE


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("prebook_requested", HandoffReason.PREBOOK_REQUEST),
        ("packaging_requested", HandoffReason.PACKAGING_REVIEW),
        ("delivery_requested", HandoffReason.SHIPPING_REQUEST),
    ],
)
def test_unresolved_commercial_details_handoff(field: str, reason: HandoffReason) -> None:
    values = {field: True}
    decision = evaluate_send_policy(
        SendContext(
            intent=Intent.QUOTE_REQUEST,
            stage=CaseStage.QUOTING,
            status=CaseStatus.ACTIVE,
            intent_confidence=1,
            product_confidence=1,
            numeric_confidence=1,
            auto_send_allowed=True,
            contact_suppressed=False,
            do_not_contact=False,
            has_risky_attachment=False,
            **values,
        )
    )
    assert not decision.allow_send
    assert decision.reason == reason


def test_ready_stock_answers_lead_time_without_date_commitment_handoff() -> None:
    decision = evaluate_send_policy(
        SendContext(
            intent=Intent.QUOTE_REQUEST,
            stage=CaseStage.QUOTING,
            status=CaseStatus.ACTIVE,
            intent_confidence=1,
            product_confidence=1,
            numeric_confidence=1,
            auto_send_allowed=True,
            contact_suppressed=False,
            do_not_contact=False,
            has_risky_attachment=False,
            delivery_requested=True,
            ready_stock_available=True,
        )
    )
    assert decision.allow_send
    assert decision.reason is None


def test_ready_stock_quantity_tiers_use_four_and_twelve_times_moq() -> None:
    tiered = PricingPolicy(
        standard_price=Decimal("100"),
        absolute_floor=Decimal("100"),
        max_discount_pct=Decimal("0"),
        concession_step_pct=Decimal("0"),
        max_negotiation_rounds=0,
        min_quantity=100,
        max_quantity=1200,
        currency="INR",
        standard_incoterm="EXW",
        allowed_incoterms=("EXW",),
        standard_payment_term="Prepayment",
        allowed_payment_terms=("Prepayment",),
        tier_1_max_multiple=Decimal("4"),
        tier_1_markup_pct=Decimal("0.25"),
        tier_2_max_multiple=Decimal("12"),
        tier_2_markup_pct=Decimal("0.20"),
    )

    assert initial_quote(tiered, 100).unit_price == Decimal("125.0000")
    assert initial_quote(tiered, 399).unit_price == Decimal("125.0000")
    assert initial_quote(tiered, 400).unit_price == Decimal("120.0000")
    assert initial_quote(tiered, 1200).unit_price == Decimal("120.0000")
    assert not initial_quote(tiered, 1201).approved


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 7, 13), date(2026, 7, 17)),
        (date(2026, 7, 17), date(2026, 7, 17)),
        (date(2026, 7, 18), date(2026, 7, 24)),
    ],
)
def test_quote_validity_ends_on_friday(today: date, expected: date) -> None:
    assert quote_valid_until(quote_valid_days=7, quote_valid_weekday=4, today=today) == expected
