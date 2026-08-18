"""First-class employee work items: state, timeline, notify policy, watchdog."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .action_reports import audit_tool_result, cursor_finished_in_audit
from .db import Agent, Consultation, WorkItem, WorkItemEvent, utcnow
from .job_result import origin_chat_id, origin_phone, send_origin_reply

logger = logging.getLogger(__name__)

OPEN_STATUSES = (
    "open",
    "in_progress",
    "waiting_external",
    "waiting_customer",
    "waiting_manager",
)
ACTIVE_STATUSES = OPEN_STATUSES + ("paused",)
TERMINAL_STATUSES = ("done", "failed")
MAX_RETRIES = 2
WAIT_EXTERNAL_SLA = timedelta(minutes=30)
WAIT_MANAGER_SLA = timedelta(minutes=60)

STATUS_LABELS = {
    "open": "Новый",
    "in_progress": "В работе",
    "waiting_external": "Жду Cursor",
    "waiting_customer": "Жду клиента",
    "waiting_manager": "Жду руководителя",
    "paused": "Пауза",
    "done": "Готово",
    "failed": "Ошибка",
}

# Dashboard always. Page/ticket only for high-signal events.
NOTIFY_CHANNELS: dict[str, set[str]] = {
    "created": {"ui"},
    "progress": {"ui"},
    "waiting_external": {"ui"},
    "waiting_customer": {"ui"},
    "waiting_manager": {"manager", "ui"},
    "done": {"ui"},
    "failed": {"manager", "ui"},
    "failed_customer": {"customer", "ui"},
    "resumed": {"ui"},
    "paused": {"ui"},
    "closed": {"ui"},
}


def notify_channels(event: str) -> set[str]:
    return set(NOTIFY_CHANNELS.get(event, {"ui"}))


def work_item_json(item: WorkItem, *, events: list[WorkItemEvent] | None = None) -> dict[str, Any]:
    data = {
        "id": item.id,
        "agent_id": item.agent_id,
        "title": item.title,
        "goal": item.goal,
        "status": item.status,
        "status_label": STATUS_LABELS.get(item.status, item.status),
        "next_action": item.next_action,
        "wait_owner": item.wait_owner,
        "wait_until": item.wait_until.isoformat() if item.wait_until else None,
        "source": item.source,
        "chat_id": item.chat_id,
        "reply_phone": item.reply_phone,
        "sender_id": item.sender_id,
        "sender_username": item.sender_username,
        "is_admin": item.is_admin,
        "project_id": item.project_id,
        "customer_id": item.customer_id,
        "paused": item.paused,
        "retry_count": item.retry_count,
        "last_error": item.last_error,
        "consultation_id": item.consultation_id,
        "cron_job_id": item.cron_job_id,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }
    if events is not None:
        data["events"] = [work_event_json(event) for event in events]
    return data


def work_event_json(event: WorkItemEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "created_at": event.created_at.isoformat() if event.created_at else None,
        "kind": event.kind,
        "title": event.title,
        "detail": event.detail,
        "payload": event.payload or {},
    }


def _clip(text: str, limit: int = 300) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def _as_int(value: Any) -> int | None:
    if value in (None, "", False):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def add_event(
    db: AsyncSession,
    item: WorkItem,
    *,
    kind: str,
    title: str,
    detail: str = "",
    payload: dict[str, Any] | None = None,
    commit: bool = False,
) -> WorkItemEvent:
    event = WorkItemEvent(
        work_item_id=item.id,
        kind=kind,
        title=_clip(title, 300),
        detail=(detail or "")[:4000],
        payload=payload or {},
    )
    db.add(event)
    if commit:
        await db.commit()
        await db.refresh(event)
    return event


async def list_open_work_items(db: AsyncSession, agent_id: int, *, include_paused: bool = True) -> list[WorkItem]:
    statuses = list(OPEN_STATUSES)
    if include_paused:
        statuses.append("paused")
    return list(
        await db.scalars(
            select(WorkItem)
            .where(WorkItem.agent_id == agent_id, WorkItem.status.in_(statuses))
            .order_by(WorkItem.updated_at.desc())
        )
    )


async def list_work_items(
    db: AsyncSession,
    agent_id: int,
    *,
    status: str | None = None,
    limit: int = 80,
) -> list[WorkItem]:
    stmt = select(WorkItem).where(WorkItem.agent_id == agent_id)
    if status and status != "all":
        if status == "open":
            stmt = stmt.where(WorkItem.status.in_(OPEN_STATUSES + ("paused",)))
        else:
            stmt = stmt.where(WorkItem.status == status)
    return list(await db.scalars(stmt.order_by(WorkItem.updated_at.desc()).limit(limit)))


async def list_events(db: AsyncSession, work_item_id: int, *, limit: int = 80) -> list[WorkItemEvent]:
    return list(
        await db.scalars(
            select(WorkItemEvent)
            .where(WorkItemEvent.work_item_id == work_item_id)
            .order_by(WorkItemEvent.id.desc())
            .limit(limit)
        )
    )


async def counts_for_agent(db: AsyncSession, agent_id: int) -> dict[str, int]:
    items = await list_work_items(db, agent_id, status="all", limit=400)
    counts = {
        "open": 0,
        "in_progress": 0,
        "waiting_external": 0,
        "waiting_customer": 0,
        "waiting_manager": 0,
        "failed": 0,
        "paused": 0,
        "done": 0,
        "actionable": 0,
    }
    for item in items:
        if item.status in counts:
            counts[item.status] += 1
        if item.status in {"failed", "waiting_manager"} or (
            item.status in OPEN_STATUSES and not item.paused
        ):
            counts["actionable"] += 1
    return counts


async def get_work_item(db: AsyncSession, work_item_id: int | None) -> WorkItem | None:
    item_id = _as_int(work_item_id)
    if item_id is None:
        return None
    return await db.get(WorkItem, item_id)


async def find_open_for_chat(db: AsyncSession, agent_id: int, chat_id: Any) -> WorkItem | None:
    if chat_id in (None, "", False):
        return None
    return await db.scalar(
        select(WorkItem)
        .where(
            WorkItem.agent_id == agent_id,
            WorkItem.chat_id == str(chat_id),
            WorkItem.status.in_(OPEN_STATUSES + ("paused", "failed")),
        )
        .order_by(WorkItem.id.desc())
    )


def apply_origin(item: WorkItem, context: dict[str, Any]) -> None:
    chat = origin_chat_id(context)
    phone = origin_phone(context, item.reply_phone)
    if chat not in (None, "", False):
        item.chat_id = str(chat)
    if phone:
        item.reply_phone = phone
    if context.get("sender_id") not in (None, ""):
        item.sender_id = str(context.get("sender_id"))
    if context.get("sender_username"):
        item.sender_username = str(context.get("sender_username"))
    if "is_admin" in context:
        item.is_admin = bool(context.get("is_admin"))
    if context.get("project_id"):
        item.project_id = str(context["project_id"])
    if context.get("customer_id"):
        item.customer_id = str(context["customer_id"])


def _context_from_item(context: dict[str, Any], item: WorkItem) -> None:
    context["work_item_id"] = item.id
    if item.chat_id:
        context.setdefault("reply_chat_id", item.chat_id)
        context.setdefault("chat_id", item.chat_id)
    if item.reply_phone:
        context.setdefault("reply_phone", item.reply_phone)
        context.setdefault("phone", item.reply_phone)
    if item.sender_id:
        context.setdefault("sender_id", item.sender_id)


async def create_work_item(
    db: AsyncSession,
    agent: Agent,
    *,
    title: str,
    goal: str = "",
    context: dict[str, Any] | None = None,
    source: str = "telegram",
) -> WorkItem:
    context = context or {}
    item = WorkItem(
        agent_id=agent.id,
        title=_clip(title, 300) or "Задача",
        goal=(goal or title or "")[:4000],
        status="open",
        next_action="Разобрать входящую задачу",
        wait_owner="self",
        source=source,
        paused=False,
    )
    apply_origin(item, context)
    db.add(item)
    await db.flush()
    await add_event(db, item, kind="created", title="Кейс создан", detail=item.goal)
    await db.commit()
    await db.refresh(item)
    return item


async def bind_work_item(
    db: AsyncSession,
    agent: Agent,
    context: dict[str, Any],
    message: str,
) -> WorkItem | None:
    """Attach or create a work item for this run. Heartbeat without id stays a watchdog."""
    existing = await get_work_item(db, context.get("work_item_id"))
    if existing is not None and existing.agent_id == agent.id:
        context["work_item_id"] = existing.id
        apply_origin(existing, context)
        if existing.status in {"open", "failed", "paused"}:
            existing.status = "in_progress"
            existing.paused = False
            existing.wait_owner = "self"
        await add_event(
            db,
            existing,
            kind="progress",
            title="Продолжение кейса",
            detail=_clip(message, 400),
            payload={"source": context.get("source")},
        )
        await db.commit()
        await db.refresh(existing)
        _context_from_item(context, existing)
        return existing

    source = str(context.get("source") or "")
    is_watchdog = bool(
        context.get("employee_tick")
        or source in {"employee_heartbeat", "employee_tick", "consult_resolved"}
    )
    if is_watchdog and not context.get("work_item_id"):
        return None

    chat_id = origin_chat_id(context)
    if chat_id not in (None, "", False):
        found = await find_open_for_chat(db, agent.id, chat_id)
        if found is not None:
            context["work_item_id"] = found.id
            apply_origin(found, context)
            if found.status in {"open", "failed", "paused"}:
                found.status = "in_progress"
                found.paused = False
                found.wait_owner = "self"
            await add_event(
                db,
                found,
                kind="message_in",
                title="Новое сообщение по кейсу",
                detail=_clip(message, 400),
            )
            await db.commit()
            await db.refresh(found)
            _context_from_item(context, found)
            return found

    if source in {"telegram", "ui", "scheduled"} or context.get("reply_chat_id"):
        item = await create_work_item(
            db,
            agent,
            title=message or "Задача",
            goal=message,
            context=context,
            source=source or "telegram",
        )
        item.status = "in_progress"
        context["work_item_id"] = item.id
        await db.commit()
        await db.refresh(item)
        _context_from_item(context, item)
        return item
    return None


async def set_status(
    db: AsyncSession,
    item: WorkItem,
    status: str,
    *,
    next_action: str | None = None,
    wait_owner: str | None = None,
    wait_until: datetime | None = None,
    error: str | None = None,
    event_title: str | None = None,
    event_detail: str = "",
    commit: bool = True,
) -> WorkItem:
    previous = item.status
    if status:
        item.status = status
    if next_action is not None:
        item.next_action = next_action[:2000]
    if wait_owner is not None:
        item.wait_owner = wait_owner
    if wait_until is not None or status in {"in_progress", "open", "done", "failed"}:
        item.wait_until = wait_until
    if error is not None:
        item.last_error = error[:2000]
    if status == "paused":
        item.paused = True
    elif status not in {"paused"}:
        item.paused = False
    item.updated_at = utcnow()
    await add_event(
        db,
        item,
        kind=status if status in NOTIFY_CHANNELS else "progress",
        title=event_title or f"{STATUS_LABELS.get(previous, previous)} → {STATUS_LABELS.get(status, status)}",
        detail=event_detail or next_action or error or "",
        payload={"from": previous, "to": status},
    )
    if commit:
        await db.commit()
        await db.refresh(item)
    return item


async def after_agent_run(
    db: AsyncSession,
    agent: Agent,
    context: dict[str, Any],
    result: str,
    audit: list[dict[str, Any]] | None,
    *,
    employee: Any | None = None,
) -> WorkItem | None:
    item = await get_work_item(db, context.get("work_item_id"))
    if item is None:
        return None
    audit = audit or []
    notes = []
    cursor_busy = False
    cursor_done = cursor_finished_in_audit(audit)
    scheduled_at: datetime | None = None
    consult_id: int | None = None
    for call in audit:
        if not isinstance(call, dict):
            continue
        tool = str(call.get("tool") or "")
        payload = audit_tool_result(call)
        title = f"{tool}: {call.get('status')}"
        detail = ""
        if isinstance(payload, dict):
            detail = str(payload.get("summary") or payload.get("reason") or payload.get("error") or "")[:800]
            if tool in {"cursorremote_check", "cursorremote_do"}:
                cursor_busy = not bool(payload.get("done"))
            if tool == "schedule_self" and payload.get("run_at"):
                try:
                    scheduled_at = datetime.fromisoformat(str(payload["run_at"]).replace("Z", "+00:00"))
                except ValueError:
                    scheduled_at = utcnow() + timedelta(minutes=2)
            if tool in {"consult_manager", "request_approval"}:
                consult = payload.get("consultation") if isinstance(payload.get("consultation"), dict) else {}
                consult_id = _as_int(consult.get("id"))
        await add_event(
            db,
            item,
            kind="tool",
            title=title,
            detail=detail,
            payload={"tool": tool, "status": call.get("status")},
        )
        notes.append(title)

    if context.get("_origin_already_sent") or context.get("_deliver_origin_reply"):
        await add_event(
            db,
            item,
            kind="message_out",
            title="Ответ заказчику",
            detail=_clip(result, 700),
        )

    if consult_id:
        item.consultation_id = consult_id
        await set_status(
            db,
            item,
            "waiting_manager",
            next_action="Жду ответа руководителя",
            wait_owner="manager",
            wait_until=utcnow() + WAIT_MANAGER_SLA,
            event_title="Нужен руководитель",
            event_detail=result,
        )
        if employee is not None:
            consult = await db.get(Consultation, consult_id)
            if consult is not None:
                consult.work_item_id = item.id
                await db.commit()
        return item

    if cursor_busy:
        await set_status(
            db,
            item,
            "waiting_external",
            next_action="Проверить Cursor, когда закончит",
            wait_owner="external",
            wait_until=scheduled_at or (utcnow() + timedelta(minutes=2)),
            event_title="Жду Cursor",
            event_detail=_clip(result, 400),
        )
        return item

    if cursor_done:
        await set_status(
            db,
            item,
            "done",
            next_action="",
            wait_owner="none",
            event_title="Кейс выполнен",
            event_detail=_clip(result, 700),
        )
        return item

    if scheduled_at:
        await set_status(
            db,
            item,
            "waiting_external",
            next_action=item.next_action or "Повторная проверка по расписанию",
            wait_owner="external",
            wait_until=scheduled_at,
            event_title="Отложено",
        )
        return item

    if result.strip() and str(context.get("source")) == "telegram":
        await set_status(
            db,
            item,
            "in_progress",
            next_action="Продолжить при следующем сообщении или тике",
            wait_owner="self",
            event_title="Шаг выполнен",
            event_detail=_clip(result, 400),
        )
    return item


async def handle_run_failure(
    db: AsyncSession,
    agent: Agent,
    payload: dict[str, Any],
    error: BaseException,
    employee: Any | None = None,
) -> WorkItem | None:
    item = await get_work_item(db, payload.get("work_item_id"))
    if item is None:
        chat_id = origin_chat_id(payload)
        if chat_id not in (None, "", False):
            item = await find_open_for_chat(db, agent.id, chat_id)
    if item is None:
        return None
    text = str(error)
    item.retry_count = int(item.retry_count or 0) + 1
    await set_status(
        db,
        item,
        "failed",
        next_action="Разобрать ошибку и продолжить",
        wait_owner="manager" if item.retry_count >= MAX_RETRIES else "self",
        error=text,
        event_title="Сбой выполнения",
        event_detail=text[:1500],
        commit=True,
    )
    if item.retry_count >= MAX_RETRIES and employee is not None:
        existing = None
        if item.consultation_id:
            existing = await db.get(Consultation, item.consultation_id)
        if existing is None or existing.status != "open":
            created = await employee.create_consultation(
                db,
                agent,
                question=f"Кейс #{item.id} «{item.title}» остановился с ошибкой",
                context=text[:1500],
                requires_approval=False,
                need_kind="decision",
                work_item_id=item.id,
            )
            consult = created.get("consultation") or {}
            item.consultation_id = _as_int(consult.get("id"))
            item.status = "waiting_manager"
            item.wait_owner = "manager"
            await db.commit()
        meta = dict(item.metadata_json or {})
        if not meta.get("customer_fail_notified"):
            phone = item.reply_phone or origin_phone(payload)
            if phone and item.chat_id:
                sent = await send_origin_reply(
                    getattr(employee, "telegram", None),
                    phone,
                    item.chat_id,
                    "Застрял на задаче, уже эскалировал руководителю. Вернусь, как разберём.",
                )
                if sent.get("sent"):
                    meta["customer_fail_notified"] = True
                    item.metadata_json = meta
                    await add_event(
                        db,
                        item,
                        kind="notify",
                        title="Клиенту написано о застревании",
                        commit=True,
                    )
    elif item.retry_count < MAX_RETRIES and getattr(employee, "scheduler", None) is not None:
        from .employee import save_once_job

        run_at = utcnow() + timedelta(minutes=2)
        job = await save_once_job(
            db,
            employee.scheduler,
            agent_id=agent.id,
            name=f"retry-{item.id}",
            payload={
                "message": (
                    f"Продолжи кейс #{item.id} после ошибки: {text[:400]}. "
                    "Не создавай новый кейс."
                ),
                "run_once_at": run_at.isoformat(),
                "timezone": "UTC",
                "source": "scheduled",
                "work_item_id": item.id,
                "reply_chat_id": item.chat_id,
                "reply_phone": item.reply_phone,
                "chat_id": item.chat_id,
                "phone": item.reply_phone,
            },
            current_job_id=payload.get("_cron_job_id"),
        )
        item.cron_job_id = job.id
        item.wait_until = run_at
        item.next_action = "Автоповтор после ошибки"
        await db.commit()
    return item


async def watchdog_items(db: AsyncSession, agent_id: int) -> list[WorkItem]:
    now = utcnow()
    actionable: list[WorkItem] = []
    for item in await list_open_work_items(db, agent_id, include_paused=False):
        if item.status == "failed":
            actionable.append(item)
            continue
        if item.status == "waiting_manager" and item.updated_at and now - item.updated_at > WAIT_MANAGER_SLA:
            actionable.append(item)
            continue
        if item.status == "waiting_external":
            due = item.wait_until or (
                item.updated_at + WAIT_EXTERNAL_SLA if item.updated_at else now
            )
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            if due <= now:
                actionable.append(item)
                continue
        if item.status in {"open", "in_progress"}:
            actionable.append(item)
    return actionable


def build_watchdog_instruction(items: list[WorkItem]) -> str:
    if not items:
        return ""
    lines = [
        "Это сторожевой тик. Работай только по открытым кейсам ниже.",
        "Не пиши руководителю и клиенту «результат тика». Сообщения — только если кейс требует человека или готово.",
        "Не создавай новый кейс, если уже есть номер.",
    ]
    for item in items[:12]:
        wait = f" до {item.wait_until.isoformat()}" if item.wait_until else ""
        error = f" Ошибка: {item.last_error[:180]}" if item.last_error else ""
        lines.append(
            f"- кейс #{item.id} [{STATUS_LABELS.get(item.status, item.status)}] "
            f"{item.title} | следующее: {item.next_action or '—'} | ждёт {item.wait_owner}{wait}.{error}"
        )
    return "\n".join(lines)


async def resume_work_item(
    db: AsyncSession,
    item: WorkItem,
    *,
    note: str = "",
    status: str = "in_progress",
) -> WorkItem:
    item.paused = False
    item.last_error = None
    return await set_status(
        db,
        item,
        status,
        next_action=note or "Продолжить по указанию руководителя",
        wait_owner="self",
        event_title="Руководитель продолжил кейс",
        event_detail=note,
    )


async def pause_work_item(db: AsyncSession, item: WorkItem, *, note: str = "") -> WorkItem:
    return await set_status(
        db,
        item,
        "paused",
        next_action=note or "Пауза по указанию руководителя",
        wait_owner="manager",
        event_title="Кейс на паузе",
        event_detail=note,
    )


async def close_work_item(
    db: AsyncSession,
    item: WorkItem,
    *,
    note: str = "",
    status: str = "done",
) -> WorkItem:
    if status not in {"done", "failed"}:
        status = "done"
    return await set_status(
        db,
        item,
        status,
        next_action="",
        wait_owner="none",
        event_title="Кейс закрыт руководителем",
        event_detail=note,
    )


def work_items_context_lines(items: list[WorkItem]) -> list[str]:
    if not items:
        return ["Открытые кейсы: (нет — если пришла новая работа, заведи её через входящее, не через cron)."]
    lines = ["Открытые кейсы (это и есть работа, не heartbeat/cron):"]
    for item in items[:15]:
        lines.append(
            f"- #{item.id} [{STATUS_LABELS.get(item.status, item.status)}] {item.title} "
            f"| {item.next_action or 'нет next_action'} | ждёт {item.wait_owner}"
        )
    return lines
