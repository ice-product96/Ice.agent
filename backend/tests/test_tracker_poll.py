"""Tests for ice_tracker backlog polling helpers."""

from __future__ import annotations

from types import SimpleNamespace

from app.tracker_poll import (
    build_tracker_poll_instruction,
    extract_board_tasks,
    is_open_tracker_task,
    summarize_tracker_task,
    tracker_settings,
    work_item_tracker_task_id,
)


def test_tracker_settings_requires_uuid() -> None:
    assert tracker_settings({})["tracker_poll_enabled"] is False
    assert tracker_settings({"tracker_project_id": "uraltrade"})["tracker_project_id"] == ""
    enabled = tracker_settings(
        {"tracker_project_id": "a61ed32a-1846-4ff1-97d9-9c0864c2a32d"}
    )
    assert enabled["tracker_poll_enabled"] is True
    disabled = tracker_settings(
        {
            "tracker_project_id": "a61ed32a-1846-4ff1-97d9-9c0864c2a32d",
            "tracker_poll_enabled": False,
        }
    )
    assert disabled["tracker_poll_enabled"] is False


def test_extract_and_filter_open_tasks() -> None:
    payload = {
        "project_id": "a61ed32a-1846-4ff1-97d9-9c0864c2a32d",
        "columns": [
            {
                "section": {"id": "s1", "name": "Todo"},
                "tasks": [
                    {"id": "t1", "name": "Fix UI", "status": "todo"},
                    {
                        "id": "t2",
                        "name": "Done already",
                        "status": "completed",
                        "is_completed": True,
                    },
                ],
            }
        ],
    }
    tasks = extract_board_tasks(payload)
    assert len(tasks) == 2
    open_ones = [t for t in tasks if is_open_tracker_task(t)]
    assert [t["id"] for t in open_ones] == ["t1"]
    summary = summarize_tracker_task(open_ones[0])
    assert summary["tracker_task_id"] == "t1"
    assert summary["section"] == "Todo"


def test_work_item_tracker_task_id_and_instruction() -> None:
    item = SimpleNamespace(
        id=10,
        context_json={"tracker_task_id": "card-1"},
        metadata_json={},
    )
    assert work_item_tracker_task_id(item) == "card-1"
    text = build_tracker_poll_instruction(
        {
            "claimable": [
                {
                    "customer_name": "УралТрейд",
                    "name": "7 правок",
                    "tracker_task_id": "card-1",
                    "status": "todo",
                    "section": "Новые",
                }
            ]
        }
    )
    assert "card-1" in text
    assert "pm_structure_task" in text
    assert build_tracker_poll_instruction({"claimable": []}) == ""
