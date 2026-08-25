"""Tests for per-project schedule and commerce helpers."""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.db import ProjectState, WorkItem
from app.project_schedule import (
    apply_task_estimate,
    is_within_project_workday,
    next_workday_open,
    project_commerce_settings,
    topic_is_cost_approval,
)


def test_project_settings_default_utc_plus_5() -> None:
    state = ProjectState(project_id="uraltrade", autonomy_level="LEVEL_1", config={})
    settings = project_commerce_settings(state)
    assert settings["timezone"] == "Asia/Yekaterinburg"
    assert settings["workday_start"] == "09:00"
    assert settings["workday_end"] == "18:00"
    assert settings["cost_requires_customer_approval"] is False


def test_workday_gate_and_next_open() -> None:
    settings = {
        "timezone": "Asia/Yekaterinburg",
        "workday_start": "09:00",
        "workday_end": "18:00",
        "workdays": [0, 1, 2, 3, 4],
    }
    monday_evening = datetime(2026, 8, 24, 20, 0, tzinfo=ZoneInfo("Asia/Yekaterinburg"))
    assert not is_within_project_workday(settings, monday_evening)
    nxt = next_workday_open(settings, monday_evening)
    local = nxt.astimezone(ZoneInfo("Asia/Yekaterinburg"))
    assert local.hour == 9
    assert local.weekday() == 1  # Tuesday

    monday_noon = datetime(2026, 8, 24, 12, 0, tzinfo=ZoneInfo("Asia/Yekaterinburg"))
    assert is_within_project_workday(settings, monday_noon)


def test_estimate_sets_min_duration_and_cost() -> None:
    item = WorkItem(id=1, agent_id=1, title="t", goal="g", context_json={})
    snap = apply_task_estimate(
        item,
        estimated_duration_minutes=120,
        hourly_rate=1500,
        min_execution_ratio=1.0,
        currency="RUB",
    )
    assert item.context_json["estimated_duration_minutes"] == 120
    assert item.context_json["min_execution_minutes"] == 120
    assert item.context_json["estimated_cost"] == 3000.0
    assert snap["estimated_cost"] == 3000.0
    assert topic_is_cost_approval("Согласование стоимости", "ок 3000")
    assert not topic_is_cost_approval("Цвета кнопок", "синий")
