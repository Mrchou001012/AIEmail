"""Explicit handoff between historical product-list validation and application."""

from dataclasses import dataclass
from typing import Any

from app.ai import InboundAnalysis
from app.db import Contact, Customer, EmailMessage, Handoff, Outbox, ProductCategory, SalesCase


@dataclass
class BackfillCandidate:
    handoff: Handoff
    source: EmailMessage
    sales_case: SalesCase | None
    contact: Contact
    customer: Customer
    category: ProductCategory | None
    analysis: InboundAnalysis
    research_required: bool
    existing_product_list: Outbox | None
    prepared: dict[str, Any]
    candidate: dict[str, Any]
