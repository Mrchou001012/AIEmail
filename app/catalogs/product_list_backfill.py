"Historical product-list request backfill workflow."

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalogs.product_list_backfill_apply import apply_backfill_candidate
from app.catalogs.product_list_backfill_prepare import prepare_backfill_candidate
from app.common.service_core import _atomic_business_operation
from app.db import (
    Handoff,
    ProductCategory,
)
from app.settings import get_settings


@_atomic_business_operation
async def backfill_product_list_requests(
    session: AsyncSession,
    *,
    apply: bool = False,
    limit: int = 500,
    max_age_days: int = 30,
    handoff_ids: tuple[int, ...] = (),
    include_history: bool = False,
    company_research: bool = False,
) -> dict[str, Any]:
    """Safely queue replies for explicit, unresolved product-list requests.

    Preview mode is strictly read-only. Apply mode revalidates every delivery
    guard, normally requires a unique active catalog category, preserves the
    original reply thread, and resolves the obsolete handoff only after an
    idempotent PRODUCT_LIST outbox row exists. ``company_research`` is an
    explicit opt-in for selected, category-less handoffs and remains governed
    by both company-research feature switches.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if max_age_days <= 0:
        raise ValueError("max_age_days must be positive")
    if apply and not handoff_ids:
        raise ValueError("apply mode requires explicitly selected handoff_ids")

    query = (
        select(Handoff)
        .where(
            Handoff.status == "OPEN",
            Handoff.source_email_id.is_not(None),
        )
        .order_by(Handoff.id)
        .limit(limit)
    )
    if handoff_ids:
        query = query.where(Handoff.id.in_(handoff_ids))
    if apply:
        query = query.with_for_update(skip_locked=True)
    handoffs = list((await session.scalars(query)).all())

    active_categories = list((await session.scalars(select(ProductCategory).where(ProductCategory.active.is_(True)))).all())
    categories_by_id = {category.id: category for category in active_categories}
    categories_by_key = {category.key: category for category in active_categories}
    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    candidates: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    queued: list[dict[str, Any]] = []

    def exclude(handoff: Handoff, reason: str, **details: Any) -> None:
        exclusions.append(
            {
                "handoff_id": handoff.id,
                "email_id": handoff.source_email_id,
                "reason": reason,
                **details,
            }
        )

    for handoff in handoffs:
        plan = await prepare_backfill_candidate(
            session,
            handoff=handoff,
            include_history=include_history,
            cutoff=cutoff,
            company_research=company_research,
            settings=settings,
            categories_by_id=categories_by_id,
            categories_by_key=categories_by_key,
            exclude=exclude,
        )
        if plan is None:
            continue
        candidates.append(plan.candidate)
        if apply:
            result = await apply_backfill_candidate(session, plan, exclusions)
            if result is not None:
                queued.append(result)

    exclusion_counts: dict[str, int] = {}
    for item in exclusions:
        reason = str(item["reason"])
        exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
    return {
        "apply": apply,
        "include_history": include_history,
        "company_research": company_research,
        "max_age_days": max_age_days,
        "scanned": len(handoffs),
        "candidate_count": len(candidates),
        "queued_count": len(queued),
        "exclusion_counts": exclusion_counts,
        "candidates": candidates,
        "queued": queued,
        "exclusions": exclusions,
    }
