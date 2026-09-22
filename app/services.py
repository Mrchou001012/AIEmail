"""Legacy callable imports; implementations live in capability modules."""

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.catalogs.category_product_list import _maybe_send_product_list as _maybe_send_product_list
from app.catalogs.general_product_list import _maybe_send_general_product_list as _maybe_send_general_product_list
from app.catalogs.product_list_backfill import backfill_product_list_requests as backfill_product_list_requests
from app.catalogs.product_list_service import _product_list_outbound_attachments as _product_list_outbound_attachments
from app.catalogs.product_list_service import _validated_prepared_product_list as _validated_prepared_product_list
from app.catalogs.product_list_service import prepared_product_list_attachment as prepared_product_list_attachment
from app.catalogs.product_list_service import product_list_missing_business_facts as product_list_missing_business_facts
from app.catalogs.product_list_service import queue_prepared_product_list_reply as queue_prepared_product_list_reply
from app.coa.coa_service import _maybe_handle_coa_request as _maybe_handle_coa_request
from app.coa.coa_service import queue_prepared_coa_reply as queue_prepared_coa_reply
from app.common.demo_service import create_demo_outreach as create_demo_outreach
from app.common.demo_service import seed_demo_data as seed_demo_data
from app.common.email_identity import reply_contact_name as _reply_contact_name  # noqa: F401
from app.common.email_identity import strip_duplicate_signature_lead as _strip_duplicate_signature_lead  # noqa: F401
from app.common.email_threading import _extract_forward_attachments as _extract_forward_attachments
from app.common.email_threading import _forwarded_message_bodies as _forwarded_message_bodies
from app.common.email_threading import _reply_references as _reply_references
from app.common.email_threading import _reply_source as _reply_source
from app.common.notifications import notify_commercial_refresh as notify_commercial_refresh
from app.common.notifications import notify_handoff as notify_handoff
from app.common.service_core import CaseLessReactivationParent as CaseLessReactivationParent
from app.common.service_core import CustomerPaymentTerm as CustomerPaymentTerm
from app.common.service_core import HumanApproval as HumanApproval
from app.common.service_core import NewInquiryResolution as NewInquiryResolution
from app.common.service_core import _all_products_catalog_category as _all_products_catalog_category
from app.common.service_core import _atomic_business_operation as _atomic_business_operation
from app.common.service_core import _catalog_category_breakdown as _catalog_category_breakdown
from app.common.service_core import _customer_payment_term as _customer_payment_term
from app.common.service_core import _human_approval_from_outbox as _human_approval_from_outbox
from app.common.service_core import _payment_details_requested as _payment_details_requested
from app.common.service_core import _payment_term_sentence as _payment_term_sentence
from app.common.service_core import _pricing_policy as _pricing_policy
from app.common.service_core import _retrieve_historical_style_examples as _retrieve_historical_style_examples
from app.common.service_core import active_policy as active_policy
from app.common.service_core import audit as audit
from app.common.service_core import create_handoff as create_handoff
from app.common.service_core import stage_outbox as stage_outbox
from app.db import Customer as Customer
from app.db import Outbox
from app.delivery.contact_delivery_service import add_customer_contact_endpoint as add_customer_contact_endpoint
from app.delivery.contact_delivery_service import replace_handoff_recipient as replace_handoff_recipient
from app.delivery.contact_delivery_service import resolve_deliverability_handoff as resolve_deliverability_handoff
from app.delivery.contact_delivery_service import suppress_contact_endpoint as suppress_contact_endpoint
from app.delivery.deliverability import lookup_mx
from app.delivery.delivery_safety import _normalize_forward_recipient as _normalize_forward_recipient
from app.delivery.delivery_safety import recipient_preflight
from app.delivery.outbound_delivery import send_one_outbox as _send_one_outbox_impl
from app.delivery.outbound_guards import _case_outbound_gate as _case_outbound_gate
from app.delivery.outbound_guards import _final_recipient_delivery_guard as _final_recipient_delivery_guard
from app.delivery.outbound_pacing import _mailbox_sent_events_since as _mailbox_sent_events_since
from app.delivery.outbound_pacing import _message_activity_key as _message_activity_key
from app.delivery.outbound_pacing import _send_interval_seconds as _send_interval_seconds
from app.delivery.outbound_pacing import _set_mailbox_cooldown as _set_mailbox_cooldown
from app.delivery.outbound_pacing import _smtp_rate_limit_cooldown_seconds as _smtp_rate_limit_cooldown_seconds
from app.delivery.outbound_recovery import reconcile_unknown_outbox as reconcile_unknown_outbox
from app.handoffs.agent_resume import resume_agent_run as resume_agent_run
from app.handoffs.forwarding_service import _touch_forward_recipient as _touch_forward_recipient
from app.handoffs.forwarding_service import forward_handoff_email as _forward_handoff_email_impl
from app.handoffs.forwarding_service import list_forward_recipients as list_forward_recipients
from app.handoffs.forwarding_service import save_forward_recipient as save_forward_recipient
from app.handoffs.handoff_prepared_preview import _prepared_handoff_draft_preview as _prepared_handoff_draft_preview
from app.handoffs.handoff_preview import generate_handoff_draft_preview as generate_handoff_draft_preview
from app.handoffs.handoff_preview import stream_handoff_draft_preview as stream_handoff_draft_preview
from app.handoffs.handoff_service import assign_handoff_case as assign_handoff_case
from app.handoffs.handoff_service import create_case_for_handoff as create_case_for_handoff
from app.handoffs.handoff_service import update_handoff_case_product as update_handoff_case_product
from app.handoffs.human_reply_service import queue_human_reply as queue_human_reply
from app.inbound.automated_reply_service import _handle_automated_reply as _handle_automated_reply
from app.inbound.bounce_service import _apply_correlated_hard_bounce as _apply_correlated_hard_bounce
from app.inbound.bounce_service import _handle_bounce as _handle_bounce
from app.inbound.bounce_service import _match_bounce_outbox as _match_bounce_outbox
from app.inbound.bounce_service import reconcile_permanent_bounce_handoffs as reconcile_permanent_bounce_handoffs
from app.inbound.email_ingestion import ingest_raw_email as ingest_raw_email
from app.inbound.inbound_followup import _ensure_inbound_follow_up as _ensure_inbound_follow_up
from app.inbound.inbound_processing import process_inbound as process_inbound
from app.inbound.inquiry_matching import PRIOR_THREAD_MARKERS as PRIOR_THREAD_MARKERS
from app.inbound.inquiry_matching import _explicit_product_codes as _explicit_product_codes
from app.inbound.inquiry_matching import _prior_thread_marker as _prior_thread_marker
from app.inbound.inquiry_matching import _product_lookup_conditions as _product_lookup_conditions
from app.inbound.inquiry_matching import _resolve_category_inquiry_case as _resolve_category_inquiry_case
from app.inbound.inquiry_resolution import _resolve_new_inquiry_case as _resolve_new_inquiry_case
from app.inbound.reactivation_threading import _case_less_reactivation_parent as _case_less_reactivation_parent
from app.jobs import JOB_HANDLERS as JOB_HANDLERS
from app.jobs import JobDeferred as JobDeferred
from app.jobs import claim_and_run_job as claim_and_run_job
from app.jobs import enqueue_job as enqueue_job
from app.mail import transport_for
from app.quotations.commercial_service import _commercial_quote_context as _commercial_quote_context
from app.quotations.commercial_service import ensure_weekly_commercial_refresh as ensure_weekly_commercial_refresh
from app.quotations.manual_quote_service import _manual_pricing_policy as _manual_pricing_policy
from app.quotations.manual_quote_service import quote_with_manual_price as quote_with_manual_price
from app.quotations.multi_product_quote import _maybe_send_multi_product_quote as _maybe_send_multi_product_quote
from app.quotations.outreach_service import create_case_outreach as create_case_outreach
from app.quotations.prepared_quote_service import queue_prepared_multi_quote_reply as queue_prepared_multi_quote_reply
from app.quotations.prepared_quote_service import queue_prepared_quote_reply as queue_prepared_quote_reply
from app.quotations.quote_clarification import _maybe_send_quote_clarification as _maybe_send_quote_clarification
from app.quotations.quote_clarification import _moq_price_rows as _moq_price_rows
from app.quotations.quote_clarification import _pluralized_unit as _pluralized_unit
from app.quotations.quote_clarification import _quantity_unit_label as _quantity_unit_label
from app.quotations.quote_context import _augment_pending_quote_context as _augment_pending_quote_context
from app.quotations.quote_rendering import render_quote as render_quote
from app.quotations.quote_rendering import standard_quote_valid_until as standard_quote_valid_until
from app.research.company_research_context import _cached_company_research as _cached_company_research
from app.research.company_research_context import _company_research_gate as _company_research_gate
from app.research.company_research_context import _nonfree_email_domain as _nonfree_email_domain
from app.research.company_research_context import _source_hostname as _source_hostname
from app.research.company_research_context import _store_company_research_cache as _store_company_research_cache
from app.research.company_research_service import _company_research_catalog as _company_research_catalog
from app.research.company_research_service import _maybe_research_and_send_product_list as _maybe_research_and_send_product_list
from app.settings import Settings


async def _recipient_preflight(
    session: AsyncSession,
    recipient: str,
    settings: Settings,
) -> tuple[str, str, dict[str, Any]]:
    """Compatibility seam for callers that replace the MX resolver."""
    return await recipient_preflight(
        session,
        recipient,
        settings,
        mx_lookup=lookup_mx,
    )


async def send_one_outbox(
    session: AsyncSession,
    settings: Settings | None = None,
    *,
    at: datetime | None = None,
) -> bool:
    """Compatibility entrypoint for outbound delivery."""
    return await _send_one_outbox_impl(
        session,
        settings,
        at=at,
        recipient_preflight_fn=_recipient_preflight,
        transport_factory=transport_for,
    )


async def forward_handoff_email(
    session: AsyncSession,
    *,
    handoff_id: int,
    recipient: str,
    actor: str,
    note: str = "",
) -> Outbox:
    """Compatibility entrypoint for human-approved forwarding."""
    return await _forward_handoff_email_impl(
        session,
        handoff_id=handoff_id,
        recipient=recipient,
        actor=actor,
        note=note,
        touch_recipient_fn=_touch_forward_recipient,
    )
