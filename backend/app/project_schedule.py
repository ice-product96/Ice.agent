"""Per-project working hours, estimates, and commercial settings."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .db import CursorRun, EmployeeProfile, ProjectState, WorkItem, utcnow
from .timezones import normalize_timezone

DEFAULT_TIMEZONE = "Asia/Yekaterinburg"
DEFAULT_WORKDAY_START = "09:00"
DEFAULT_WORKDAY_END = "18:00"
# Monday=0 … Sunday=6
DEFAULT_WORKDAYS = (0, 1, 2, 3, 4)


def _parse_hhmm(value: str, *, fallback: str) -> tuple[int, int]:
    raw = str(value or fallback).strip() or fallback
    try:
        hour_s, minute_s = raw.split(":", 1)
        hour = max(0, min(23, int(hour_s)))
        minute = max(0, min(59, int(minute_s)))
        return hour, minute
    except (TypeError, ValueError):
        return _parse_hhmm(fallback, fallback=fallback)


def _as_int(value: Any, default: int | None = None) -> int | None:
    if value in (None, "", False):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float | None = None) -> float | None:
    if value in (None, "", False):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "да"}


def project_commerce_settings(
    project: ProjectState | None,
    *,
    profile: EmployeeProfile | None = None,
) -> dict[str, Any]:
    """Resolve schedule + rate settings: project config overrides employee profile."""
    config = dict(project.config or {}) if project is not None else {}
    profile_tz = (profile.timezone if profile is not None else "") or ""
    profile_start = (
        profile.workday_start if profile is not None else ""
    ) or DEFAULT_WORKDAY_START
    profile_end = (
        profile.workday_end if profile is not None else ""
    ) or DEFAULT_WORKDAY_END
    tz_name = normalize_timezone(
        str(config.get("timezone") or profile_tz or DEFAULT_TIMEZONE),
        default=DEFAULT_TIMEZONE,
    )
    workdays_raw = config.get("workdays")
    if isinstance(workdays_raw, list) and workdays_raw:
        workdays = sorted(
            {
                int(day)
                for day in workdays_raw
                if str(day).isdigit() or isinstance(day, int)
            }
        )
        workdays = [day for day in workdays if 0 <= day <= 6]
    else:
        workdays = list(DEFAULT_WORKDAYS)
    if not workdays:
        workdays = list(DEFAULT_WORKDAYS)
    hourly_rate = _as_float(config.get("hourly_rate"), 0.0) or 0.0
    return {
        "timezone": tz_name,
        "workday_start": str(
            config.get("workday_start") or profile_start or DEFAULT_WORKDAY_START
        ),
        "workday_end": str(
            config.get("workday_end") or profile_end or DEFAULT_WORKDAY_END
        ),
        "workdays": workdays,
        "hourly_rate": hourly_rate,
        "currency": str(config.get("currency") or "RUB"),
        "cost_requires_customer_approval": _as_bool(
            config.get("cost_requires_customer_approval"), False
        ),
        "min_execution_ratio": max(
            0.0,
            float(_as_float(config.get("min_execution_ratio"), 1.0) or 1.0),
        ),
    }


def is_within_project_workday(
    settings: dict[str, Any],
    now: datetime | None = None,
) -> bool:
    now = now or utcnow()
    tz = ZoneInfo(str(settings["timezone"]))
    local = now.astimezone(tz)
    workdays = list(settings.get("workdays") or DEFAULT_WORKDAYS)
    if local.weekday() not in workdays:
        return False
    sh, sm = _parse_hhmm(str(settings["workday_start"]), fallback=DEFAULT_WORKDAY_START)
    eh, em = _parse_hhmm(str(settings["workday_end"]), fallback=DEFAULT_WORKDAY_END)
    start_m = sh * 60 + sm
    end_m = eh * 60 + em
    cur = local.hour * 60 + local.minute
    if start_m == end_m:
        return True
    if start_m < end_m:
        return start_m <= cur < end_m
    return cur >= start_m or cur < end_m


def next_workday_open(
    settings: dict[str, Any],
    now: datetime | None = None,
) -> datetime:
    """Next datetime (UTC) when Cursor work may start for this project."""
    now = now or utcnow()
    if is_within_project_workday(settings, now):
        return now
    tz = ZoneInfo(str(settings["timezone"]))
    local = now.astimezone(tz)
    sh, sm = _parse_hhmm(str(settings["workday_start"]), fallback=DEFAULT_WORKDAY_START)
    workdays = list(settings.get("workdays") or DEFAULT_WORKDAYS)
    for day_offset in range(0, 14):
        candidate_date = (local + timedelta(days=day_offset)).date()
        if candidate_date.weekday() not in workdays:
            continue
        open_local = datetime(
            candidate_date.year,
            candidate_date.month,
            candidate_date.day,
            sh,
            sm,
            tzinfo=tz,
        )
        if open_local > local:
            return open_local.astimezone(ZoneInfo("UTC"))
    tomorrow = local + timedelta(days=1)
    open_local = datetime(
        tomorrow.year, tomorrow.month, tomorrow.day, sh, sm, tzinfo=tz
    )
    return open_local.astimezone(ZoneInfo("UTC"))


def apply_task_estimate(
    item: WorkItem,
    *,
    estimated_duration_minutes: int | None,
    hourly_rate: float = 0.0,
    min_execution_ratio: float = 1.0,
    currency: str = "RUB",
) -> dict[str, Any]:
    """Stamp estimate/cost onto work-item context; returns the commerce snapshot."""
    ctx = dict(item.context_json or {})
    minutes = _as_int(estimated_duration_minutes, None)
    if minutes is None:
        minutes = _as_int(ctx.get("estimated_duration_minutes"), None)
    if minutes is not None and minutes > 0:
        ctx["estimated_duration_minutes"] = minutes
        min_minutes = max(1, int(round(minutes * max(0.0, min_execution_ratio))))
        ctx["min_execution_minutes"] = min_minutes
        ctx["currency"] = currency
        if hourly_rate > 0:
            hours = minutes / 60.0
            ctx["estimated_cost"] = round(hours * hourly_rate, 2)
            ctx["hourly_rate"] = hourly_rate
    item.context_json = ctx
    return task_commerce_snapshot(item, hourly_rate=hourly_rate, currency=currency)


def task_commerce_snapshot(
    item: WorkItem,
    *,
    hourly_rate: float = 0.0,
    currency: str = "RUB",
) -> dict[str, Any]:
    ctx = dict(item.context_json or {}) if isinstance(item.context_json, dict) else {}
    minutes = _as_int(ctx.get("estimated_duration_minutes"), None)
    min_minutes = _as_int(ctx.get("min_execution_minutes"), None)
    rate = _as_float(ctx.get("hourly_rate"), hourly_rate) or 0.0
    cost = _as_float(ctx.get("estimated_cost"), None)
    if cost is None and minutes and rate > 0:
        cost = round((minutes / 60.0) * rate, 2)
    return {
        "estimated_duration_minutes": minutes,
        "min_execution_minutes": min_minutes,
        "estimated_cost": cost,
        "hourly_rate": rate or None,
        "currency": str(ctx.get("currency") or currency),
        "cost_approved": _as_bool(ctx.get("cost_approved"), False),
        "cost_decision_id": ctx.get("cost_decision_id"),
        "scheduled_cursor_at": ctx.get("scheduled_cursor_at"),
    }


def mark_cost_approved(item: WorkItem, *, decision_id: int | None = None) -> None:
    ctx = dict(item.context_json or {})
    ctx["cost_approved"] = True
    if decision_id is not None:
        ctx["cost_decision_id"] = decision_id
    item.context_json = ctx


def topic_is_cost_approval(topic: str, decision: str = "") -> bool:
    blob = f"{topic} {decision}".casefold()
    markers = (
        "стоимост",
        "цен",
        "оплат",
        "бюджет",
        "cost",
        "price",
        "payment",
        "budget",
        "hourly",
        "ставк",
    )
    return any(marker in blob for marker in markers)


def cursor_elapsed_minutes(runs: list[CursorRun]) -> float:
    total = 0.0
    now = utcnow()
    for run in runs:
        started = run.started_at
        if started is None:
            continue
        ended = run.completed_at or (
            now if run.status in {"running", "pending"} else started
        )
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=ZoneInfo("UTC"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=ZoneInfo("UTC"))
        delta = (ended - started).total_seconds()
        if delta > 0:
            total += delta
    return total / 60.0


def min_execution_remaining_minutes(
    item: WorkItem,
    runs: list[CursorRun],
) -> float:
    ctx = dict(item.context_json or {}) if isinstance(item.context_json, dict) else {}
    required = _as_int(ctx.get("min_execution_minutes"), None)
    if required is None or required <= 0:
        return 0.0
    elapsed = cursor_elapsed_minutes(runs)
    return max(0.0, float(required) - elapsed)


def schedule_snapshot(
    settings: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or utcnow()
    within = is_within_project_workday(settings, now)
    next_open = next_workday_open(settings, now)
    tz = ZoneInfo(str(settings["timezone"]))
    return {
        "timezone": settings["timezone"],
        "workday_start": settings["workday_start"],
        "workday_end": settings["workday_end"],
        "workdays": list(settings.get("workdays") or DEFAULT_WORKDAYS),
        "within_workday": within,
        "next_cursor_window_at": None if within else next_open.isoformat(),
        "next_cursor_window_local": None
        if within
        else next_open.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z"),
        "hourly_rate": settings.get("hourly_rate") or 0,
        "currency": settings.get("currency") or "RUB",
        "cost_requires_customer_approval": bool(
            settings.get("cost_requires_customer_approval")
        ),
        "min_execution_ratio": settings.get("min_execution_ratio") or 1.0,
        "now_local": now.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z"),
    }


def enrich_project_state_payload(
    state: ProjectState,
    *,
    profile: EmployeeProfile | None = None,
) -> dict[str, Any]:
    settings = project_commerce_settings(state, profile=profile)
    return {
        "schedule": schedule_snapshot(settings),
        "commerce": {
            "hourly_rate": settings["hourly_rate"],
            "currency": settings["currency"],
            "cost_requires_customer_approval": settings[
                "cost_requires_customer_approval"
            ],
            "min_execution_ratio": settings["min_execution_ratio"],
        },
    }


def enrich_work_item_commerce(
    item: WorkItem,
    project: ProjectState | None,
    runs: list[CursorRun] | None = None,
    *,
    profile: EmployeeProfile | None = None,
) -> dict[str, Any]:
    settings = project_commerce_settings(project, profile=profile)
    commerce = task_commerce_snapshot(
        item,
        hourly_rate=float(settings["hourly_rate"] or 0),
        currency=str(settings["currency"]),
    )
    runs = list(runs or [])
    remaining = min_execution_remaining_minutes(item, runs)
    schedule = schedule_snapshot(settings)
    next_event = None
    if item.wait_until is not None:
        next_event = {
            "at": item.wait_until.isoformat(),
            "action": item.next_action or "ожидание",
            "owner": item.wait_owner or "self",
        }
    elif not schedule["within_workday"] and item.pm_phase in {
        "READY_FOR_DEV",
        "CLIENT_CONFIRMED",
        "CHANGES_REQUESTED",
    }:
        next_event = {
            "at": schedule["next_cursor_window_at"],
            "action": "Запуск Cursor в рабочие часы",
            "owner": "self",
        }
    return {
        "schedule": schedule,
        "commerce": {
            **commerce,
            "cost_requires_customer_approval": settings[
                "cost_requires_customer_approval"
            ],
            "elapsed_cursor_minutes": round(cursor_elapsed_minutes(runs), 1),
            "min_execution_remaining_minutes": round(remaining, 1),
            "can_accept_qa": remaining <= 0,
        },
        "next_event": next_event,
    }
