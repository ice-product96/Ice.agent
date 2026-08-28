"""Periodic ice_tracker backlog → PM work-item intake."""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .cursorremote_drive import mcp_call, parse_mcp_payload
from .db import Customer, ProjectState, WorkItem
from .project_schedule import _as_bool

logger = logging.getLogger(__name__)

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

MappingLike = dict[str, Any]

# ice_tracker Task.status values that still need PM/dev attention.
OPEN_TRACKER_STATUSES = frozenset({"todo", "in_progress"})
DONE_TRACKER_STATUSES = frozenset({"completed", "cancelled", "done", "canceled"})


def tracker_settings(config: dict[str, Any] | None) -> dict[str, Any]:
    cfg = dict(config or {})
    tracker_project_id = str(cfg.get("tracker_project_id") or "").strip()
    poll_raw = cfg.get("tracker_poll_enabled")
    if poll_raw in (None, ""):
        poll_enabled = bool(tracker_project_id)
    else:
        poll_enabled = _as_bool(poll_raw, bool(tracker_project_id))
    return {
        "tracker_project_id": tracker_project_id if UUID_RE.match(tracker_project_id) else "",
        "tracker_poll_enabled": poll_enabled and bool(UUID_RE.match(tracker_project_id or "")),
    }


def find_ice_tracker_session(mcp: Any) -> tuple[Any | None, str | None]:
    sessions = getattr(mcp, "sessions", None) or {}
    for name, session in list(sessions.items()):
        normalized = str(name or "").lower().replace("-", "_")
        if "ice_tracker" in normalized or normalized == "icetracker":
            return session, str(name)
    return None, None


async def list_tracker_bindings(
    db: AsyncSession,
    agent_id: int,
) -> list[dict[str, Any]]:
    """Projects for this agent that should be polled in ice_tracker."""
    customers = (
        await db.scalars(select(Customer).where(Customer.agent_id == agent_id))
    ).all()
    bindings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for customer in customers:
        project_id = (customer.project_id or customer.id or "").strip()
        if not project_id:
            continue
        state = await db.get(ProjectState, project_id)
        settings = tracker_settings(state.config if state is not None else None)
        if not settings["tracker_poll_enabled"]:
            continue
        key = settings["tracker_project_id"]
        if key in seen:
            continue
        seen.add(key)
        bindings.append(
            {
                "customer_id": customer.id,
                "customer_name": customer.name,
                "project_id": project_id,
                "tracker_project_id": key,
                "cursor_workspace": customer.cursor_workspace or "",
            }
        )
    return bindings


def _task_id(raw: MappingLike | Any) -> str:
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("id") or raw.get("task_id") or "").strip()


def _task_name(raw: MappingLike | Any) -> str:
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("name") or raw.get("title") or "").strip()


def _task_status(raw: MappingLike | Any) -> str:
    if not isinstance(raw, dict):
        return ""
    if raw.get("is_completed") is True:
        return "completed"
    return str(raw.get("status") or "").strip().lower()


def is_open_tracker_task(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    if not _task_id(raw):
        return False
    status = _task_status(raw)
    if status in DONE_TRACKER_STATUSES:
        return False
    if status in OPEN_TRACKER_STATUSES:
        return True
    # Unknown status but explicitly not completed → treat as open.
    return not bool(raw.get("is_completed"))


def extract_board_tasks(payload: Any) -> list[dict[str, Any]]:
    data = parse_mcp_payload(payload)
    if not isinstance(data, dict):
        return []
    tasks: list[dict[str, Any]] = []
    columns = data.get("columns")
    if isinstance(columns, list):
        for column in columns:
            if not isinstance(column, dict):
                continue
            for task in column.get("tasks") or []:
                if isinstance(task, dict):
                    section = column.get("section")
                    section_name = ""
                    section_id = ""
                    if isinstance(section, dict):
                        section_name = str(section.get("name") or "")
                        section_id = str(section.get("id") or "")
                    enriched = dict(task)
                    if section_name and "section_name" not in enriched:
                        enriched["section_name"] = section_name
                    if section_id and "section_id" not in enriched:
                        enriched["section_id"] = section_id
                    tasks.append(enriched)
    listed = data.get("tasks")
    if isinstance(listed, list):
        for task in listed:
            if isinstance(task, dict):
                tasks.append(task)
    return tasks


def work_item_tracker_task_id(item: WorkItem) -> str:
    ctx = item.context_json if isinstance(item.context_json, dict) else {}
    meta = item.metadata_json if isinstance(item.metadata_json, dict) else {}
    pm = meta.get("pm") if isinstance(meta.get("pm"), dict) else {}
    for source in (ctx, pm, meta):
        if not isinstance(source, dict):
            continue
        for key in ("tracker_task_id", "card_id"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return ""


def work_item_tracker_project_id(item: WorkItem) -> str:
    ctx = item.context_json if isinstance(item.context_json, dict) else {}
    meta = item.metadata_json if isinstance(item.metadata_json, dict) else {}
    pm = meta.get("pm") if isinstance(meta.get("pm"), dict) else {}
    for source in (ctx, pm, meta):
        if not isinstance(source, dict):
            continue
        value = str(source.get("tracker_project_id") or "").strip()
        if value:
            return value
    return ""


def tracker_intake_message_id(tracker_task_id: str) -> str:
    """Stable unique source_message_id so each tracker card can have its own WorkItem."""
    tid = str(tracker_task_id or "").strip()
    if not tid:
        return ""
    return f"tracker:{tid}"[:64]


def work_item_is_closed(item: WorkItem | None) -> bool:
    if item is None:
        return False
    status = str(getattr(item, "status", "") or "").strip().lower()
    phase = str(getattr(item, "pm_phase", "") or "").strip().upper()
    return status in {"done", "cancelled", "canceled"} or phase in {"DONE", "CANCELLED"}


def can_reuse_work_item_for_structure(
    item: WorkItem | None,
    *,
    tracker_task_id: str = "",
    create_new_task: bool = False,
) -> bool:
    """Reuse only the same tracker card. Closed cases must not steal a new card."""
    if item is None:
        return False
    wanted = str(tracker_task_id or "").strip()
    existing = work_item_tracker_task_id(item)
    if wanted:
        return existing == wanted
    if create_new_task:
        return False
    if work_item_is_closed(item):
        return False
    return True


async def indexed_tracker_work_items(
    db: AsyncSession,
    agent_id: int,
) -> dict[str, WorkItem]:
    rows = (
        await db.scalars(select(WorkItem).where(WorkItem.agent_id == agent_id))
    ).all()
    index: dict[str, WorkItem] = {}
    for item in rows:
        tid = work_item_tracker_task_id(item)
        if tid and tid not in index:
            index[tid] = item
    return index


async def find_work_item_for_tracker_task(
    db: AsyncSession,
    agent_id: int,
    tracker_task_id: str,
) -> WorkItem | None:
    tid = str(tracker_task_id or "").strip()
    if not tid:
        return None
    index = await indexed_tracker_work_items(db, agent_id)
    return index.get(tid)


async def fetch_open_tracker_tasks(
    session: Any,
    tracker_project_id: str,
    *,
    limit: int = 80,
) -> list[dict[str, Any]]:
    """Load unfinished ice_tracker tasks for one board."""
    open_tasks: list[dict[str, Any]] = []
    seen: set[str] = set()

    def absorb(raw_list: list[dict[str, Any]]) -> None:
        for task in raw_list:
            if not is_open_tracker_task(task):
                continue
            tid = _task_id(task)
            if not tid or tid in seen:
                continue
            seen.add(tid)
            open_tasks.append(task)

    try:
        board = await mcp_call(
            session, "get_project_board", {"project_id": tracker_project_id}
        )
        absorb(extract_board_tasks(board))
    except Exception as exc:
        logger.info("ice_tracker get_project_board failed: %s", exc)

    if not open_tasks:
        for status in ("todo", "in_progress"):
            try:
                found = await mcp_call(
                    session,
                    "search_tasks",
                    {
                        "project_id": tracker_project_id,
                        "status": status,
                        "limit": limit,
                    },
                )
                data = parse_mcp_payload(found)
                if isinstance(data, dict) and isinstance(data.get("tasks"), list):
                    absorb([t for t in data["tasks"] if isinstance(t, dict)])
                else:
                    absorb(extract_board_tasks(found))
            except Exception as exc:
                logger.info("ice_tracker search_tasks(%s) failed: %s", status, exc)

    return open_tasks[:limit]


def summarize_tracker_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "tracker_task_id": _task_id(task),
        "name": _task_name(task),
        "status": _task_status(task),
        "section": str(task.get("section_name") or ""),
        "priority": str(task.get("priority") or ""),
        "description": str(task.get("description") or "")[:800],
    }


async def poll_tracker_backlog(
    db: AsyncSession,
    mcp: Any,
    agent_id: int,
    *,
    limit_per_project: int = 40,
) -> dict[str, Any]:
    """Compare ice_tracker unfinished cards with existing WorkItems."""
    session, server_name = find_ice_tracker_session(mcp)
    bindings = await list_tracker_bindings(db, agent_id)
    if not bindings:
        return {
            "ok": True,
            "enabled": False,
            "server": server_name,
            "bindings": [],
            "claimable": [],
            "already_tracked": [],
            "count_claimable": 0,
        }
    if session is None:
        return {
            "ok": False,
            "enabled": True,
            "server": None,
            "error": "ice_tracker MCP is not connected",
            "bindings": bindings,
            "claimable": [],
            "already_tracked": [],
            "count_claimable": 0,
        }

    index = await indexed_tracker_work_items(db, agent_id)
    claimable: list[dict[str, Any]] = []
    already: list[dict[str, Any]] = []

    for binding in bindings:
        tasks = await fetch_open_tracker_tasks(
            session,
            binding["tracker_project_id"],
            limit=limit_per_project,
        )
        for task in tasks:
            summary = summarize_tracker_task(task)
            summary.update(
                {
                    "project_id": binding["project_id"],
                    "customer_id": binding["customer_id"],
                    "customer_name": binding["customer_name"],
                    "tracker_project_id": binding["tracker_project_id"],
                }
            )
            existing = index.get(summary["tracker_task_id"])
            if existing is not None:
                summary["work_item_id"] = existing.id
                summary["work_item_status"] = existing.status
                summary["pm_phase"] = existing.pm_phase
                already.append(summary)
            else:
                claimable.append(summary)

    return {
        "ok": True,
        "enabled": True,
        "server": server_name,
        "bindings": bindings,
        "claimable": claimable,
        "already_tracked": already,
        "count_claimable": len(claimable),
        "count_already_tracked": len(already),
    }


def build_tracker_poll_instruction(backlog: dict[str, Any]) -> str:
    claimable = list(backlog.get("claimable") or [])
    if not claimable:
        return ""
    lines = [
        "Периодическая проверка ice_tracker: есть незакрытые задачи без кейса PM.",
        "Возьми в работу ОДНУ задачу (приоритетнее in_progress / выше в списке).",
        "Шаги: pm_structure_task (project_id = slug заказчика, не UUID трекера; "
        "в context_json обязательно tracker_task_id + tracker_project_id) → оценка/"
        "согласование по правилам проекта → при готовности submit_development_task.",
        "Не дублируй задачи, которые уже в already_tracked. Не пиши заказчику про "
        "сам факт проверки трекера. Карточку двигает платформа по фазе PM; "
        "не вызывай move_task/complete_task вручную.",
        "Очередь:",
    ]
    for item in claimable[:8]:
        lines.append(
            f"- [{item.get('customer_name') or item.get('project_id')}] "
            f"{item.get('name') or 'без названия'} "
            f"(tracker_task_id={item.get('tracker_task_id')}, "
            f"status={item.get('status') or '?'}, "
            f"section={item.get('section') or '—'})"
        )
    return "\n".join(lines)


def should_attach_tracker_poll(item: WorkItem | None, backlog: dict[str, Any] | None) -> bool:
    """Do not glue a new tracker card onto an already focused unrelated case."""
    if not int((backlog or {}).get("count_claimable") or 0):
        return False
    if item is None:
        return True
    status = str(getattr(item, "status", "") or "").strip().lower()
    phase = str(getattr(item, "pm_phase", "") or "").strip().upper()
    if status in {"done", "failed"} or phase in {"DONE", "CANCELLED"}:
        return True
    return False


_runtime_mcp: Any = None

# Board column names (lowercase) that correspond to a work lane.
TRACKER_LANE_ALIASES: dict[str, tuple[str, ...]] = {
    "todo": (
        "новые",
        "new",
        "todo",
        "to do",
        "backlog",
        "бэклог",
        "очередь",
        "inbox",
        "к работе",
        "постановка",
    ),
    "in_progress": (
        "в работе",
        "in progress",
        "in_progress",
        "doing",
        "dev",
        "разработка",
        "работа",
        "coding",
    ),
    "qa": (
        "qa",
        "тест",
        "проверка",
        "review",
        "ревью",
        "testing",
        "контроль",
        "приёмка",
        "приемка",
    ),
    "blocked": ("блок", "blocked", "ожидание", "stuck", "hold"),
    "completed": ("готово", "done", "completed", "закрыто", "готовые", "done.", "закрыт"),
    "cancelled": ("отмена", "cancelled", "canceled", "отменено", "cancel"),
}


def set_tracker_mcp(mcp: Any) -> None:
    global _runtime_mcp
    _runtime_mcp = mcp


def tracker_lane_for_phase(phase: str) -> str:
    name = str(phase or "").strip().upper()
    if name in {"QA", "DEV_COMPLETE", "CLIENT_REVIEW"}:
        return "qa"
    if name == "BLOCKED":
        return "blocked"
    if name == "DONE":
        return "completed"
    if name == "CANCELLED":
        return "cancelled"
    if name in {
        "REQUIREMENTS_READY",
        "CLIENT_CONFIRMED",
        "READY_FOR_DEV",
        "IN_DEVELOPMENT",
        "CHANGES_REQUESTED",
    }:
        return "in_progress"
    return "todo"


def tracker_status_for_phase(phase: str) -> str:
    lane = tracker_lane_for_phase(phase)
    if lane in {"qa", "blocked"}:
        return "in_progress"
    if lane == "completed":
        return "completed"
    if lane == "cancelled":
        return "cancelled"
    return lane


def _norm_section_name(value: str) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def extract_sections(payload: Any) -> list[dict[str, str]]:
    data = parse_mcp_payload(payload)
    items: list[Any] = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        raw = data.get("sections") or data.get("columns") or data.get("items") or []
        if isinstance(raw, list):
            items = raw
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        section = item.get("section") if isinstance(item.get("section"), dict) else item
        if not isinstance(section, dict):
            continue
        sid = str(section.get("id") or item.get("section_id") or "").strip()
        name = str(section.get("name") or item.get("section_name") or "").strip()
        key = sid or name
        if not key or key in seen:
            continue
        seen.add(key)
        found.append({"id": sid, "name": name})
    return found


def match_section_for_lane(
    sections: list[dict[str, str]],
    lane: str,
) -> dict[str, str] | None:
    aliases = TRACKER_LANE_ALIASES.get(lane) or ()
    for section in sections:
        name = _norm_section_name(section.get("name") or "")
        if not name:
            continue
        for alias in aliases:
            want = _norm_section_name(alias)
            if not want:
                continue
            if name == want or want in name or name in want:
                return section
    if lane == "qa":
        return match_section_for_lane(sections, "in_progress")
    if lane == "blocked":
        return match_section_for_lane(sections, "in_progress")
    return None


def _task_section_id(task: MappingLike | None) -> str:
    if not isinstance(task, dict):
        return ""
    section = task.get("section")
    if isinstance(section, dict):
        return str(section.get("id") or "").strip()
    return str(
        task.get("section_id") or task.get("column_id") or ""
    ).strip()


def _task_section_name(task: MappingLike | None) -> str:
    if not isinstance(task, dict):
        return ""
    section = task.get("section")
    if isinstance(section, dict):
        return str(section.get("name") or "").strip()
    return str(task.get("section_name") or task.get("column") or "").strip()


async def _mcp_try(
    session: Any,
    tool: str,
    attempts: list[dict[str, Any]],
) -> Any:
    last_error = ""
    for arguments in attempts:
        try:
            return await mcp_call(session, tool, arguments)
        except Exception as exc:
            last_error = str(exc)[:500]
            logger.info("tracker.%s failed %s: %s", tool, arguments, last_error)
    raise RuntimeError(last_error or f"{tool} failed")


async def sync_work_item_tracker_card(
    item: WorkItem,
    *,
    phase: str | None = None,
    mcp: Any = None,
    session: Any = None,
) -> dict[str, Any]:
    """Move the bound ice_tracker card to the column/status for this PM phase."""
    task_id = work_item_tracker_task_id(item)
    if not task_id:
        return {"skipped": True, "reason": "no_tracker_card"}
    to_phase = str(phase or item.pm_phase or "").strip().upper()
    lane = tracker_lane_for_phase(to_phase)
    status = tracker_status_for_phase(to_phase)
    tracker_session = session
    if tracker_session is None:
        tracker_session, _ = find_ice_tracker_session(mcp if mcp is not None else _runtime_mcp)
    if tracker_session is None:
        logger.info(
            "tracker.sync skipped work_item=%s task=%s reason=no_session phase=%s",
            getattr(item, "id", None),
            task_id,
            to_phase,
        )
        return {"skipped": True, "reason": "no_session", "phase": to_phase, "lane": lane}
    logger.info(
        "tracker.sync begin work_item=%s task=%s phase=%s lane=%s status=%s",
        getattr(item, "id", None),
        task_id,
        to_phase,
        lane,
        status,
    )
    current: dict[str, Any] = {}
    try:
        raw = await _mcp_try(
            tracker_session,
            "get_task",
            [{"task_id": task_id}, {"id": task_id}],
        )
        parsed = parse_mcp_payload(raw)
        if isinstance(parsed, dict):
            current = parsed
    except Exception as exc:
        logger.info("tracker.get_task failed work_item=%s: %s", getattr(item, "id", None), exc)
    already_done = _task_status(current) in DONE_TRACKER_STATUSES or bool(
        current.get("is_completed")
    )
    if to_phase == "DONE" and already_done:
        return {
            "ok": True,
            "skipped": True,
            "reason": "already_completed",
            "phase": to_phase,
            "task_id": task_id,
        }
    project_id = work_item_tracker_project_id(item) or str(
        current.get("project_id") or ""
    ).strip()
    sections: list[dict[str, str]] = []
    if project_id:
        try:
            listed = await _mcp_try(
                tracker_session,
                "list_sections",
                [{"project_id": project_id}, {"id": project_id}],
            )
            sections = extract_sections(listed)
        except Exception as exc:
            logger.info("tracker.list_sections failed: %s", exc)
            try:
                board = await mcp_call(
                    tracker_session,
                    "get_project_board",
                    {"project_id": project_id},
                )
                # Reuse columns from the board payload.
                data = parse_mcp_payload(board)
                sections = extract_sections(data if data is not None else board)
                if not sections and isinstance(data, dict):
                    sections = extract_sections({"columns": data.get("columns") or []})
            except Exception as board_exc:
                logger.info("tracker.get_project_board failed: %s", board_exc)
    target = match_section_for_lane(sections, lane)
    moved = False
    completed = False
    updated_status = False
    current_section_id = _task_section_id(current)
    result: dict[str, Any] = {
        "ok": True,
        "phase": to_phase,
        "lane": lane,
        "status": status,
        "task_id": task_id,
        "from_section": _task_section_name(current) or current_section_id or None,
        "to_section": (target or {}).get("name") if target else None,
    }
    try:
        if target and target.get("id") and target["id"] != current_section_id:
            await _mcp_try(
                tracker_session,
                "move_task",
                [
                    {"task_id": task_id, "section_id": target["id"]},
                    {"id": task_id, "section_id": target["id"]},
                    {"task_id": task_id, "to_section_id": target["id"]},
                ],
            )
            moved = True
            logger.info(
                "tracker.move work_item=%s task=%s section=%s (%s)",
                getattr(item, "id", None),
                task_id,
                target.get("name"),
                target.get("id"),
            )
        elif not target and status in {"todo", "in_progress", "cancelled"}:
            current_status = _task_status(current)
            if current_status != status:
                await _mcp_try(
                    tracker_session,
                    "update_task",
                    [
                        {"task_id": task_id, "status": status},
                        {"id": task_id, "status": status},
                    ],
                )
                updated_status = True
                logger.info(
                    "tracker.status work_item=%s task=%s status=%s",
                    getattr(item, "id", None),
                    task_id,
                    status,
                )
        if to_phase == "DONE" and not already_done:
            await _mcp_try(
                tracker_session,
                "complete_task",
                [{"task_id": task_id}, {"id": task_id}],
            )
            completed = True
            logger.info(
                "tracker.complete work_item=%s task=%s",
                getattr(item, "id", None),
                task_id,
            )
        if to_phase == "CANCELLED" and not already_done and not updated_status:
            try:
                await _mcp_try(
                    tracker_session,
                    "update_task",
                    [
                        {"task_id": task_id, "status": "cancelled"},
                        {"id": task_id, "status": "canceled"},
                    ],
                )
                updated_status = True
            except Exception as exc:
                logger.info("tracker.cancel status failed: %s", exc)
    except Exception as exc:
        error = str(exc)[:800]
        logger.warning(
            "tracker.sync failed work_item=%s task=%s phase=%s: %s",
            getattr(item, "id", None),
            task_id,
            to_phase,
            error,
        )
        return {
            "ok": False,
            "error": error,
            "phase": to_phase,
            "lane": lane,
            "task_id": task_id,
        }
    result.update(
        {
            "moved": moved,
            "completed": completed,
            "updated_status": updated_status,
            "skipped": not (moved or completed or updated_status),
        }
    )
    logger.info(
        "tracker.sync done work_item=%s task=%s moved=%s completed=%s status_updated=%s",
        getattr(item, "id", None),
        task_id,
        moved,
        completed,
        updated_status,
    )
    return result
