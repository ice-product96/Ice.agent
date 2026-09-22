"""Operator overrides: accept QA, force Cursor submit, cancel a judge verdict."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import AgentJudgment, WorkItem, utcnow
from .judgment import judgment_json
from .work_items import add_event, work_item_json


async def list_work_item_judgments(
    db: AsyncSession,
    work_item_id: int,
    *,
    limit: int = 80,
) -> list[dict[str, Any]]:
    rows = list(
        await db.scalars(
            select(AgentJudgment)
            .where(AgentJudgment.work_item_id == work_item_id)
            .order_by(AgentJudgment.id.desc())
            .limit(limit)
        )
    )
    return [judgment_json(row) for row in rows]


async def override_judgment(
    db: AsyncSession,
    row: AgentJudgment,
    *,
    verdict: str,
    note: str = "",
    operator: str = "operator",
) -> dict[str, Any]:
    row.overridden_by = (operator or "operator")[:64]
    row.override_verdict = (verdict or "")[:64]
    if note:
        payload = dict(row.payload_json or {})
        payload["operator_note"] = note[:1000]
        row.payload_json = payload
    if row.work_item_id:
        item = await db.get(WorkItem, row.work_item_id)
        if item is not None:
            await add_event(
                db,
                item,
                kind="operator",
                title=f"Вердикт {row.kind} отменён",
                detail=f"{row.verdict} → {verdict}. {note}".strip()[:1000],
                payload={"judgment_id": row.id, "kind": row.kind, "override": verdict},
            )
    await db.commit()
    await db.refresh(row)
    return judgment_json(row)


async def operator_accept_qa(
    db: AsyncSession,
    item: WorkItem,
    *,
    note: str = "",
    scheduler: Any = None,
    mcp: Any = None,
) -> dict[str, Any]:
    from .pm_state import transition_pm_phase
    from .work_items import cancel_work_item_schedules

    if item.pm_phase not in {"QA", "CLIENT_REVIEW", "DEV_COMPLETE"}:
        raise ValueError(f"Case phase {item.pm_phase} is not ready for operator QA accept")
    await transition_pm_phase(db, item, "DONE", detail="Operator accepted QA", mcp=mcp)
    item.status = "done"
    item.active_cursor_run_id = None
    item.wait_owner = "none"
    item.next_action = ""
    meta = dict(item.metadata_json or {})
    meta["cursor_in_flight"] = False
    meta["operator_accepted_qa"] = True
    meta.pop("qa_fix_request", None)
    meta.pop("qa_hold_marker", None)
    item.metadata_json = meta
    await add_event(
        db,
        item,
        kind="accepted",
        title="QA accepted (operator)",
        detail=(note or "Operator override")[:1000],
        payload={"operator": True, "note": note},
    )
    await cancel_work_item_schedules(db, item, scheduler, mark_aborted=False)
    latest = (
        await db.scalars(
            select(AgentJudgment)
            .where(AgentJudgment.work_item_id == item.id, AgentJudgment.kind == "qa_verifier")
            .order_by(AgentJudgment.id.desc())
            .limit(1)
        )
    ).first()
    if latest is not None:
        latest.overridden_by = "operator"
        latest.override_verdict = "accept"
    await db.commit()
    return {"ok": True, "item": work_item_json(item)}


async def operator_submit_cursor(
    db: AsyncSession,
    item: WorkItem,
    *,
    note: str = "",
    scheduler: Any = None,
    agent_id: int | None = None,
) -> dict[str, Any]:
    from .employee import schedule_immediate_tick

    meta = dict(item.metadata_json or {})
    meta["operator_force_submit"] = True
    item.metadata_json = meta
    ctx = dict(item.context_json or {})
    ctx["owner_approved"] = True
    item.context_json = ctx
    item.paused = False
    item.next_action = "operator: submit_development_task"
    item.wait_owner = "self"
    item.wait_until = utcnow()
    await add_event(
        db,
        item,
        kind="operator",
        title="Отправка в Cursor (оператор)",
        detail=(note or "Operator override: submit_development_task")[:1000],
        payload={"operator_force_submit": True},
    )
    await db.commit()
    if scheduler is not None:
        await schedule_immediate_tick(
            db,
            scheduler,
            agent_id or item.agent_id,
            reason="operator_submit",
            work_item_id=item.id,
            manager_answer=(note or "operator override: submit_development_task").strip(),
        )
    return {
        "ok": True,
        "item": work_item_json(item),
        "message": "Тик запланирован — кейс уйдёт в Cursor.",
    }


def turn_cost_payload(judgment: Any, trace_id: str | None) -> dict[str, Any]:
    if judgment is None:
        return {}
    cost = judgment.turn_cost(trace_id)
    return {
        "decision_trace_id": trace_id,
        **cost,
    }
