"""AI wording for reviewable product-list replies.

Catalog selection, attachment bytes, and commercial facts remain deterministic.
The model is allowed to draft only the human-facing prose around those facts.
"""

from datetime import UTC, datetime
from typing import Any

from app.ai import AIClient, render_draft_preview
from app.db import Product, ProductCategory
from app.settings import Settings


async def generate_product_list_ai_preview(
    *,
    settings: Settings,
    subject: str,
    contact_name: str,
    customer_message: str,
    category: ProductCategory,
    products: list[Product],
    attachment_filename: str | None,
    actor: str,
    payment_sentence: str | None = None,
) -> dict[str, Any]:
    """Generate prose grounded exclusively in an approved catalog snapshot."""

    preview, metadata = await AIClient(settings).draft_preview(
        {
            "draft_purpose": "product_list_reply",
            "subject": subject,
            "contact_name": contact_name,
            "customer_message": customer_message[:12_000],
            "intent": "product_list_request",
            "approved_product_category": {
                "key": category.key,
                "name": category.name,
            },
            "approved_product_catalog": [
                {
                    "code": product.catalog_code,
                    "name": product.name,
                    "cas_no": product.cas_no,
                    "content": product.content,
                    "series": product.series,
                }
                for product in products
            ],
            "approved_attachments": (
                [
                    {
                        "filename": attachment_filename,
                        "purpose": "verified current product list",
                    }
                ]
                if attachment_filename
                else []
            ),
            "approved_commercial_facts": (
                {"payment_term": payment_sentence} if payment_sentence else {}
            ),
        }
    )
    body_text = render_draft_preview(preview)
    if payment_sentence and payment_sentence.casefold() not in body_text.casefold():
        body_text = f"{body_text.rstrip()}\n\n{payment_sentence}"
    return {
        "subject": preview.subject,
        "body_text": body_text,
        "generated_at": datetime.now(UTC).isoformat(),
        "generated_by": actor,
        "provider": metadata["provider"],
        "model": metadata["model"],
        "input_tokens": metadata.get("input_tokens"),
        "output_tokens": metadata.get("output_tokens"),
        "rag_enabled": False,
        "rag_matches": [],
        "delivery_created": False,
    }
