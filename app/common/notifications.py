"""Notifications workflow."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import CommercialDataCycle, Handoff, SalesCase
from app.integrations import DingTalkNotifier


async def notify_handoff(session: AsyncSession, handoff_id: int) -> None:
    handoff = await session.get(Handoff, handoff_id)
    if handoff is None or handoff.dingtalk_status == "SENT":
        return
    if handoff.status != "OPEN":
        handoff.dingtalk_status = "CANCELLED"
        await session.commit()
        return
    case = await session.get(SalesCase, handoff.case_id) if handoff.case_id else None
    try:
        handoff.dingtalk_status = await DingTalkNotifier().notify(handoff, case)
    except Exception as exc:
        handoff.dingtalk_status = "FAILED"
        raise RuntimeError(str(exc)) from exc
    finally:
        await session.commit()


async def notify_commercial_refresh(session: AsyncSession, cycle_id: int) -> None:
    cycle = await session.get(CommercialDataCycle, cycle_id)
    if cycle is None or cycle.reminder_status in {"SENT", "LOGGED", "NOT_REQUIRED"}:
        return
    if cycle.price_status == "CONFIRMED" and cycle.inventory_status == "CONFIRMED":
        cycle.reminder_status = "NOT_REQUIRED"
        await session.commit()
        return
    try:
        cycle.reminder_status = await DingTalkNotifier().notify_commercial_refresh(cycle)
        cycle.reminder_sent_at = datetime.now(UTC)
    except Exception as exc:
        cycle.reminder_status = "FAILED"
        raise RuntimeError(str(exc)) from exc
    finally:
        await session.commit()
