"""Keep old workflow imports usable while callers migrate to domain modules."""

import importlib

import pytest

from app import services


@pytest.mark.parametrize(
    ("name", "module"),
    [
        ("process_inbound", "inbound.inbound_processing"),
        ("ingest_raw_email", "inbound.email_ingestion"),
        ("resume_agent_run", "handoffs.agent_resume"),
        ("queue_prepared_coa_reply", "coa.coa_service"),
        ("queue_prepared_quote_reply", "quotations.prepared_quote_service"),
        ("queue_prepared_multi_quote_reply", "quotations.prepared_quote_service"),
        ("quote_with_manual_price", "quotations.manual_quote_service"),
        ("queue_prepared_product_list_reply", "catalogs.product_list_service"),
        ("backfill_product_list_requests", "catalogs.product_list_backfill"),
        ("generate_handoff_draft_preview", "handoffs.handoff_preview"),
        ("queue_human_reply", "handoffs.human_reply_service"),
        ("replace_handoff_recipient", "delivery.contact_delivery_service"),
        ("_normalize_forward_recipient", "delivery.delivery_safety"),
    ],
)
def test_legacy_workflow_import_resolves_to_owning_module(name: str, module: str) -> None:
    assert getattr(services, name) is getattr(importlib.import_module(f"app.{module}"), name)
