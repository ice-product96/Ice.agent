"""Tests for ice_tracker backlog polling helpers."""

from __future__ import annotations

from types import SimpleNamespace

from app.tracker_poll import (
    build_tracker_poll_instruction,
    can_reuse_work_item_for_structure,
    extract_board_tasks,
    extract_sections,
    is_open_tracker_task,
    match_section_for_lane,
    should_attach_tracker_poll,
    summarize_tracker_task,
    sync_work_item_tracker_card,
    tracker_intake_message_id,
    tracker_lane_for_phase,
    tracker_settings,
    work_item_is_closed,
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
    assert "ask_customer_about_cost" in text
    assert "оплат" in text
    assert build_tracker_poll_instruction({"claimable": []}) == ""


def test_closed_case_is_not_reuse_for_another_tracker_card() -> None:
    closed = SimpleNamespace(
        id=31,
        status="done",
        pm_phase="DONE",
        context_json={"tracker_task_id": "a75de1b6-aaaa-bbbb-cccc-ddddeeeeffff"},
        metadata_json={},
    )
    wanted = "7f157a72-3408-474e-a183-d1777638fc4c"
    assert work_item_is_closed(closed) is True
    assert tracker_intake_message_id(wanted) == f"tracker:{wanted}"
    assert (
        can_reuse_work_item_for_structure(
            closed, tracker_task_id=wanted, create_new_task=True
        )
        is False
    )
    assert (
        can_reuse_work_item_for_structure(
            closed,
            tracker_task_id="a75de1b6-aaaa-bbbb-cccc-ddddeeeeffff",
            create_new_task=True,
        )
        is True
    )
    assert can_reuse_work_item_for_structure(closed, create_new_task=True) is False


def test_tracker_lane_matches_russian_columns() -> None:
    assert tracker_lane_for_phase("READY_FOR_DEV") == "in_progress"
    assert tracker_lane_for_phase("IN_DEVELOPMENT") == "in_progress"
    assert tracker_lane_for_phase("QA") == "qa"
    assert tracker_lane_for_phase("DONE") == "completed"
    sections = extract_sections(
        {
            "sections": [
                {"id": "s1", "name": "Новые"},
                {"id": "s2", "name": "В работе"},
                {"id": "s3", "name": "QA"},
                {"id": "s4", "name": "Готово"},
            ]
        }
    )
    assert match_section_for_lane(sections, "todo")["id"] == "s1"
    assert match_section_for_lane(sections, "in_progress")["id"] == "s2"
    assert match_section_for_lane(sections, "qa")["id"] == "s3"
    assert match_section_for_lane(sections, "completed")["id"] == "s4"


class _Item:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def model_dump(self) -> dict:
        import json

        if isinstance(self.payload, str):
            return {"type": "text", "text": self.payload}
        return {"type": "text", "text": json.dumps(self.payload)}


class _Resp:
    def __init__(self, payload: object) -> None:
        self.content = [_Item(payload)]
        self.isError = False


class FakeTrackerSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.task = {
            "id": "card-1",
            "status": "todo",
            "project_id": "proj-1",
            "section": {"id": "s-todo", "name": "Новые"},
        }

    async def call_tool(self, tool: str, arguments: dict | None = None):
        args = dict(arguments or {})
        self.calls.append((tool, args))
        if tool == "get_task":
            return _Resp(self.task)
        if tool == "list_sections":
            return _Resp(
                {
                    "sections": [
                        {"id": "s-todo", "name": "Новые"},
                        {"id": "s-dev", "name": "В работе"},
                        {"id": "s-qa", "name": "QA"},
                        {"id": "s-done", "name": "Готово"},
                    ]
                }
            )
        if tool == "move_task":
            sid = str(args.get("section_id") or args.get("to_section_id") or "")
            names = {
                "s-todo": "Новые",
                "s-dev": "В работе",
                "s-qa": "QA",
                "s-done": "Готово",
            }
            self.task["section"] = {"id": sid, "name": names.get(sid, sid)}
            if sid == "s-dev":
                self.task["status"] = "in_progress"
            return _Resp({"ok": True})
        if tool == "complete_task":
            self.task["status"] = "completed"
            self.task["is_completed"] = True
            return _Resp({"ok": True})
        if tool == "update_task":
            if args.get("status"):
                self.task["status"] = args["status"]
            return _Resp({"ok": True})
        return _Resp({})


def test_sync_moves_card_through_work_lanes() -> None:
    import asyncio

    session = FakeTrackerSession()
    item = SimpleNamespace(
        id=33,
        pm_phase="IN_DEVELOPMENT",
        context_json={
            "tracker_task_id": "card-1",
            "tracker_project_id": "proj-1",
        },
        metadata_json={},
    )
    moved = asyncio.run(
        sync_work_item_tracker_card(item, phase="IN_DEVELOPMENT", session=session)
    )
    assert moved["moved"] is True
    assert moved["to_section"] == "В работе"
    assert any(tool == "move_task" for tool, _ in session.calls)

    qa = asyncio.run(sync_work_item_tracker_card(item, phase="QA", session=session))
    assert qa["moved"] is True
    assert qa["to_section"] == "QA"

    done = asyncio.run(sync_work_item_tracker_card(item, phase="DONE", session=session))
    assert done["completed"] is True
    assert any(tool == "complete_task" for tool, _ in session.calls)


def test_sync_skips_item_without_tracker_card() -> None:
    import asyncio

    item = SimpleNamespace(
        id=1, pm_phase="IN_DEVELOPMENT", context_json={}, metadata_json={}
    )
    result = asyncio.run(sync_work_item_tracker_card(item, phase="IN_DEVELOPMENT"))
    assert result["skipped"] is True
    assert result["reason"] == "no_tracker_card"


def test_tracker_poll_not_attached_to_unrelated_open_case() -> None:
    backlog = {"count_claimable": 1, "claimable": [{"tracker_task_id": "card-1"}]}
    open_case = SimpleNamespace(
        status="in_progress",
        pm_phase="DISCUSSION",
        context_json={},
    )
    assert should_attach_tracker_poll(None, backlog) is True
    assert should_attach_tracker_poll(open_case, backlog) is False
    assert should_attach_tracker_poll(open_case, {"count_claimable": 0}) is False
    done = SimpleNamespace(status="done", pm_phase="DONE", context_json={})
    assert should_attach_tracker_poll(done, backlog) is True
