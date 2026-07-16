from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from app.db import CaseStage, CaseStatus

MONEY_QUANTUM = Decimal("0.0001")


class Intent(StrEnum):
    QUOTE_REQUEST = "quote_request"
    COUNTEROFFER = "counteroffer"
    SAMPLE_REQUEST = "sample_request"
    ORDER = "order"
    SHIPPING = "shipping"
    TECHNICAL = "technical"
    COMPLAINT = "complaint"
    UNSUBSCRIBE = "unsubscribe"
    OTHER = "other"


class HandoffReason(StrEnum):
    SAMPLE_REQUEST = "SAMPLE_REQUEST"
    ORDER_COMMITMENT = "ORDER_COMMITMENT"
    SHIPPING_REQUEST = "SHIPPING_REQUEST"
    TECHNICAL_REQUEST = "TECHNICAL_REQUEST"
    COMPLAINT = "COMPLAINT"
    BELOW_FLOOR = "BELOW_FLOOR"
    NONSTANDARD = "NONSTANDARD"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    ATTACHMENT_REVIEW = "ATTACHMENT_REVIEW"
    SUPPRESSED = "SUPPRESSED"
    HUMAN_CONTROL = "HUMAN_CONTROL"
    AI_FAILURE = "AI_FAILURE"
    MAIL_FAILURE = "MAIL_FAILURE"
    THREAD_AMBIGUOUS = "THREAD_AMBIGUOUS"
    PRICE_NEGOTIATION = "PRICE_NEGOTIATION"
    PREBOOK_REQUEST = "PREBOOK_REQUEST"
    PACKAGING_REVIEW = "PACKAGING_REVIEW"
    PERSONNEL_CHANGE = "PERSONNEL_CHANGE"
    AUTOMATED_REPLY_REVIEW = "AUTOMATED_REPLY_REVIEW"
    EMAIL_DELIVERABILITY = "EMAIL_DELIVERABILITY"
    BOUNCE_REVIEW = "BOUNCE_REVIEW"
    NEW_INQUIRY_REVIEW = "NEW_INQUIRY_REVIEW"


@dataclass(frozen=True)
class PricingPolicy:
    standard_price: Decimal
    absolute_floor: Decimal
    max_discount_pct: Decimal
    concession_step_pct: Decimal
    max_negotiation_rounds: int
    min_quantity: int
    max_quantity: int | None
    currency: str
    standard_incoterm: str
    allowed_incoterms: tuple[str, ...]
    standard_payment_term: str
    allowed_payment_terms: tuple[str, ...]
    tier_1_max_multiple: Decimal | None = None
    tier_1_markup_pct: Decimal = Decimal("0")
    tier_2_max_multiple: Decimal | None = None
    tier_2_markup_pct: Decimal = Decimal("0")


@dataclass(frozen=True)
class PricingDecision:
    approved: bool
    unit_price: Decimal | None
    hard_minimum: Decimal
    reason: str | None = None
    applied_markup_pct: Decimal = Decimal("0")


def money(value: Decimal | str | int) -> Decimal:
    return Decimal(value).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def hard_minimum(policy: PricingPolicy) -> Decimal:
    discount_floor = policy.standard_price * (Decimal("1") - policy.max_discount_pct)
    return money(max(policy.absolute_floor, discount_floor))


def initial_quote(policy: PricingPolicy, quantity: int) -> PricingDecision:
    floor = hard_minimum(policy)
    if quantity < policy.min_quantity or (policy.max_quantity is not None and quantity > policy.max_quantity):
        return PricingDecision(False, None, floor, "quantity_out_of_policy")
    markup = Decimal("0")
    tier = "standard_price"
    if (
        policy.tier_1_max_multiple is not None
        and Decimal(quantity) < Decimal(policy.min_quantity) * policy.tier_1_max_multiple
    ):
        markup = policy.tier_1_markup_pct
        tier = "tier_1"
    elif (
        policy.tier_2_max_multiple is not None
        and Decimal(quantity) <= Decimal(policy.min_quantity) * policy.tier_2_max_multiple
    ):
        markup = policy.tier_2_markup_pct
        tier = "tier_2"
    return PricingDecision(
        True,
        money(policy.standard_price * (Decimal("1") + markup)),
        floor,
        tier,
        markup,
    )


def quote_valid_until(
    *,
    quote_valid_days: int,
    quote_valid_weekday: int | None,
    today: date | None = None,
) -> date:
    current = today or date.today()
    if quote_valid_weekday is not None:
        if not 0 <= quote_valid_weekday <= 6:
            raise ValueError("quote_valid_weekday must be between Monday=0 and Sunday=6")
        return current + timedelta(days=(quote_valid_weekday - current.weekday()) % 7)
    return current + timedelta(days=quote_valid_days)


def counteroffer(
    policy: PricingPolicy,
    current_price: Decimal,
    requested_price: Decimal,
    round_number: int,
    quantity: int,
) -> PricingDecision:
    floor = hard_minimum(policy)
    requested = money(requested_price)
    current = money(current_price)
    if current < floor:
        return PricingDecision(False, None, floor, "current_quote_below_hard_minimum")
    if quantity < policy.min_quantity or (policy.max_quantity is not None and quantity > policy.max_quantity):
        return PricingDecision(False, None, floor, "quantity_out_of_policy")
    if round_number >= policy.max_negotiation_rounds:
        return PricingDecision(False, None, floor, "max_rounds_exceeded")
    # Requests at/crossing the absolute floor are explicitly human-reviewed even when
    # the discount-derived floor is stricter.
    if requested <= money(policy.absolute_floor):
        return PricingDecision(False, None, floor, "requested_at_or_below_absolute_floor")
    if requested < floor:
        return PricingDecision(False, None, floor, "requested_below_hard_minimum")
    max_concession = money(policy.standard_price * policy.concession_step_pct)
    next_price = money(max(floor, current - max_concession, requested))
    if next_price >= current:
        return PricingDecision(True, current, floor, "no_additional_concession")
    return PricingDecision(True, next_price, floor)


ALLOWED_TRANSITIONS: dict[CaseStage, set[CaseStage]] = {
    CaseStage.QUOTING: {CaseStage.NEGOTIATING, CaseStage.SAMPLE_REQUEST, CaseStage.DEAL_ORDER_DECISION, CaseStage.FOLLOW_UP},
    CaseStage.NEGOTIATING: {CaseStage.NEGOTIATING, CaseStage.SAMPLE_REQUEST, CaseStage.DEAL_ORDER_DECISION, CaseStage.FOLLOW_UP},
    CaseStage.SAMPLE_REQUEST: {CaseStage.DEAL_ORDER_DECISION, CaseStage.FOLLOW_UP},
    CaseStage.DEAL_ORDER_DECISION: {CaseStage.SHIPPING, CaseStage.FOLLOW_UP},
    CaseStage.SHIPPING: {CaseStage.FOLLOW_UP, CaseStage.TECHNICAL_AFTER_SALES},
    CaseStage.FOLLOW_UP: {CaseStage.NEGOTIATING, CaseStage.DEAL_ORDER_DECISION, CaseStage.TECHNICAL_AFTER_SALES},
    CaseStage.TECHNICAL_AFTER_SALES: {CaseStage.FOLLOW_UP},
}

RISKY_STAGES = {
    CaseStage.SAMPLE_REQUEST,
    CaseStage.DEAL_ORDER_DECISION,
    CaseStage.SHIPPING,
    CaseStage.TECHNICAL_AFTER_SALES,
}


def can_transition(current: CaseStage, target: CaseStage) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def transition(current: CaseStage, target: CaseStage) -> CaseStage:
    if not can_transition(current, target):
        raise ValueError(f"forbidden stage transition: {current.value} -> {target.value}")
    return target


@dataclass(frozen=True)
class SendContext:
    intent: Intent
    stage: CaseStage
    status: CaseStatus
    intent_confidence: float
    product_confidence: float
    numeric_confidence: float
    auto_send_allowed: bool
    contact_suppressed: bool
    do_not_contact: bool
    has_risky_attachment: bool
    currency_standard: bool = True
    quantity_standard: bool = True
    incoterm_standard: bool = True
    payment_standard: bool = True
    product_known: bool = True
    prebook_requested: bool = False
    packaging_requested: bool = False
    delivery_requested: bool = False
    ready_stock_available: bool = False


@dataclass(frozen=True)
class PolicyDecision:
    allow_send: bool
    reason: HandoffReason | None = None


def evaluate_send_policy(
    ctx: SendContext,
    *,
    intent_threshold: float = 0.80,
    product_threshold: float = 0.85,
    numeric_threshold: float = 0.90,
) -> PolicyDecision:
    risk_map = {
        Intent.COUNTEROFFER: HandoffReason.PRICE_NEGOTIATION,
        Intent.SAMPLE_REQUEST: HandoffReason.SAMPLE_REQUEST,
        Intent.ORDER: HandoffReason.ORDER_COMMITMENT,
        Intent.SHIPPING: HandoffReason.SHIPPING_REQUEST,
        Intent.TECHNICAL: HandoffReason.TECHNICAL_REQUEST,
        Intent.COMPLAINT: HandoffReason.COMPLAINT,
    }
    if ctx.intent in risk_map:
        return PolicyDecision(False, risk_map[ctx.intent])
    if ctx.prebook_requested:
        return PolicyDecision(False, HandoffReason.PREBOOK_REQUEST)
    if ctx.packaging_requested:
        return PolicyDecision(False, HandoffReason.PACKAGING_REVIEW)
    if ctx.delivery_requested and not ctx.ready_stock_available:
        return PolicyDecision(False, HandoffReason.SHIPPING_REQUEST)
    if ctx.stage in RISKY_STAGES or ctx.status in {
        CaseStatus.WAITING_HUMAN,
        CaseStatus.PAUSED,
        CaseStatus.HUMAN_TAKEOVER,
        CaseStatus.CLOSED_WON,
        CaseStatus.CLOSED_LOST,
    }:
        return PolicyDecision(False, HandoffReason.HUMAN_CONTROL)
    if ctx.contact_suppressed or ctx.do_not_contact or not ctx.auto_send_allowed:
        return PolicyDecision(False, HandoffReason.SUPPRESSED)
    if ctx.has_risky_attachment:
        return PolicyDecision(False, HandoffReason.ATTACHMENT_REVIEW)
    if ctx.intent_confidence < intent_threshold or ctx.product_confidence < product_threshold or ctx.numeric_confidence < numeric_threshold:
        return PolicyDecision(False, HandoffReason.LOW_CONFIDENCE)
    if not all(
        (
            ctx.currency_standard,
            ctx.quantity_standard,
            ctx.incoterm_standard,
            ctx.payment_standard,
            ctx.product_known,
        )
    ):
        return PolicyDecision(False, HandoffReason.NONSTANDARD)
    return PolicyDecision(True)
