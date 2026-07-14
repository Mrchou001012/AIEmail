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
            intent=Intent.COUNTEROFFER,
            stage=CaseStage.NEGOTIATING,
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
