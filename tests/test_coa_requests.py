from app.ai import InboundAnalysis, ProductLine
from app.coa_requests import (
    coa_outbox_business_key,
    outstanding_coa_queries,
    plan_coa_request,
)
from app.domain import Intent


def test_followup_plan_targets_only_corrected_missing_item_and_keeps_other_missing() -> None:
    analysis = InboundAnalysis(
        intent=Intent.COA_REQUEST,
        intent_confidence=0.99,
        coa_requested=True,
        requested_product_name="YAC-TMCS",
        product_confidence=0.99,
        product_requests=[],
        numeric_confidence=1.0,
    )
    plan = plan_coa_request(
        analysis=analysis,
        analysis_facts={
            "partial_coa_outbox_id": 10,
            "missing_coa_queries": ["YAC-TMCS", "YAC-A110"],
            "coa_retry_original_query": "YAC-TMCS",
        },
    )

    assert plan.product_queries == ("YAC-TMCS",)
    assert outstanding_coa_queries(response_missing=(), plan=plan) == ["YAC-A110"]
    assert coa_outbox_business_key(
        email_id=99,
        product_queries=plan.product_queries,
        followup=True,
    ).startswith("inbound-coa-followup:99:")


def test_initial_multi_product_plan_keeps_every_explicit_product() -> None:
    analysis = InboundAnalysis(
        intent=Intent.QUOTE_REQUEST,
        intent_confidence=0.99,
        quote_requested=True,
        coa_requested=True,
        requested_product_name="YAC-HMDS",
        product_confidence=0.99,
        product_requests=[
            ProductLine(product_code="YAC-HMDS", quantity=3_000),
            ProductLine(product_code="YAC-TMCS", quantity=2_000),
        ],
        numeric_confidence=1.0,
    )

    plan = plan_coa_request(analysis=analysis, analysis_facts={})

    assert plan.primary_request is False
    assert plan.product_queries == ("YAC-HMDS", "YAC-TMCS")
    assert coa_outbox_business_key(
        email_id=98,
        product_queries=plan.product_queries,
        followup=False,
    ) == "inbound-coa:98"
