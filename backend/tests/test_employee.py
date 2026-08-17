from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from types import SimpleNamespace

from app.employee import (
    CONSULT_CMD_RE,
    build_employee_context_block,
    heartbeat_cron,
    period_bounds,
)
from app.employee_policy import build_employee_tick_instruction


def test_heartbeat_cron_divisors() -> None:
    assert heartbeat_cron(15) == "*/15 * * * *"
    assert heartbeat_cron(10) == "*/10 * * * *"
    assert heartbeat_cron(7).startswith("*/")


def test_period_bounds_day() -> None:
    now = datetime(2026, 8, 15, 12, 30, tzinfo=timezone.utc)
    start, end = period_bounds("day", now, ZoneInfo("UTC"))
    assert start.day == 15
    assert (end - start).days == 1


def test_consult_command_parse() -> None:
    match = CONSULT_CMD_RE.match("/approve 12")
    assert match is not None
    assert match.group(1).lower() == "approve"
    assert match.group(2) == "12"
    match = CONSULT_CMD_RE.match("/answer 3 yes, do it")
    assert match is not None
    assert match.group(3).strip() == "yes, do it"


def test_employee_context_uses_schedule_not_plans() -> None:
    profile = SimpleNamespace(
        role_title="PM",
        mission="LAVVE",
        autonomy_enabled=True,
        paused=False,
        workday_start="09:00",
        workday_end="18:00",
        timezone="Asia/Yekaterinburg",
        ticks_used_today=2,
        budget_ticks_per_day=48,
    )
    job = SimpleNamespace(
        id=12,
        name="once-check-cursor",
        cron="@once",
        payload={
            "run_once_at": "2026-08-17T13:00:00+00:00",
            "message": "cursorremote_check LAVVE",
            "last_result": {"title": "Выполнено", "summary": "Cursor закончил работу."},
        },
    )
    block = build_employee_context_block(profile, [job], [], [])
    assert "Расписание" in block
    assert "cursorremote_check LAVVE" in block
    assert "итог: Cursor закончил работу" in block
    assert "hour/day/week/month" in block
    assert "Активные планы" not in block


def test_tick_instruction_uses_scheduler() -> None:
    text = build_employee_tick_instruction(SimpleNamespace(config_json={}))
    assert "schedule_self" in text
    assert "cursorremote_check" in text
    assert "планы (час/день" not in text
    assert "новый schedule_self не ставь" in text


def test_once_job_status_is_completed_after_run() -> None:
    from app.contract import cron_job_status

    done = SimpleNamespace(
        cron="@once",
        enabled=False,
        last_run_at=datetime.now(timezone.utc),
        payload={"run_once_at": "2026-08-17T14:00:00+00:00"},
    )
    paused = SimpleNamespace(
        cron="*/15 * * * *",
        enabled=False,
        last_run_at=None,
        payload={},
    )
    active = SimpleNamespace(cron="@once", enabled=True, last_run_at=None, payload={})
    assert cron_job_status(done) == "completed"
    assert cron_job_status(paused) == "paused"
    assert cron_job_status(active) == "active"
