import json
from pathlib import Path
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Agent, Base, CronJob, CursorRun, WorkItem, utcnow
from app.employee import EmployeeService
from app.runtime import (
    AgentRuntime,
    _apply_pm_cursor_result,
    _auto_accept_pm_qa,
    _tick_focus_item,
)


class FakeScheduler:
    def __init__(self) -> None:
        self.ids: list[int] = []

    def upsert(self, job: CronJob) -> None:
        self.ids.append(job.id)


class TickEmployee:
    def __init__(self, scheduler: FakeScheduler) -> None:
        self.scheduler = scheduler

    async def prepare_tick_context(self, db, agent, profile, *, force=False):
        return {}

    async def mark_tick(self, db, profile):
        raise AssertionError("cursor-only heartbeat must not spend a tick")


async def sessions_for(path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def make_runtime(session, scheduler: FakeScheduler) -> AgentRuntime:
    runtime = object.__new__(AgentRuntime)
    runtime._agent_locks = {}
    runtime.scheduler = scheduler
    runtime.employee = EmployeeService(telegram=None, scheduler=scheduler)

    async def cursor_session(db, agent):
        return session

    runtime._cursorremote_session = cursor_session
    return runtime


@pytest.mark.asyncio
async def test_poll_only_reschedules_without_llm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = await sessions_for(tmp_path / "poll-running.db")
    scheduler = FakeScheduler()
    runtime = make_runtime(object(), scheduler)

    async def running(_session, **_kwargs):
        return {
            "ok": True,
            "done": False,
            "status": "working",
            "started": True,
            "seen_busy": True,
        }

    monkeypatch.setattr("app.cursorremote_drive.check_and_drive", running)
    async with sessions() as db:
        agent = Agent(name="worker")
        db.add(agent)
        await db.flush()
        item = WorkItem(
            agent_id=agent.id,
            title="Cursor task",
            status="waiting_external",
            wait_owner="external",
            chat_id="777",
            reply_phone="+1",
            metadata_json={"cursor_in_flight": True},
        )
        db.add(item)
        await db.commit()

        outcome = await runtime.poll_cursor_followup(
            db,
            agent,
            {
                "source": "scheduled",
                "work_item_id": item.id,
                "cursor_assignment_seq": 0,
                "reply_chat_id": "777",
                "reply_phone": "+1",
                "_cron_job_id": 99,
            },
        )

        assert outcome["done"] is False
        assert outcome["skipped_llm"] is True
        assert outcome["rescheduled"] is True
        assert scheduler.ids
        job = await db.scalar(
            select(CronJob).where(CronJob.id == outcome["job_id"])
        )
        assert job is not None
        assert job.payload["work_item_id"] == item.id
        assert job.payload["source"] == "scheduled"
    await engine.dispose()


@pytest.mark.asyncio
async def test_poll_only_delivers_once_when_cursor_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = await sessions_for(tmp_path / "poll-done.db")
    scheduler = FakeScheduler()
    runtime = make_runtime(object(), scheduler)

    async def completed(_session, **_kwargs):
        return {
            "ok": True,
            "done": True,
            "status": "completed",
            "summary": "Изменения реализованы и проверены.",
        }

    monkeypatch.setattr("app.cursorremote_drive.check_and_drive", completed)
    async with sessions() as db:
        agent = Agent(name="worker")
        db.add(agent)
        await db.flush()
        item = WorkItem(
            agent_id=agent.id,
            title="Cursor task",
            status="waiting_external",
            wait_owner="external",
            chat_id="777",
            reply_phone="+1",
            metadata_json={"cursor_in_flight": True},
        )
        db.add(item)
        await db.commit()

        outcome = await runtime.poll_cursor_followup(
            db,
            agent,
            {
                "source": "scheduled",
                "work_item_id": item.id,
                "cursor_assignment_seq": 0,
                "reply_chat_id": "777",
                "reply_phone": "+1",
            },
        )

        await db.refresh(item)
        assert outcome["done"] is True
        assert outcome["skipped_llm"] is True
        assert outcome["deliver_origin"] is True
        assert outcome["result"] == "Изменения реализованы и проверены."
        assert item.status == "done"
        assert not scheduler.ids
    await engine.dispose()


@pytest.mark.asyncio
async def test_heartbeat_arms_poll_job_and_skips_llm(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "tick-skip.db")
    scheduler = FakeScheduler()
    runtime = object.__new__(AgentRuntime)
    runtime._agent_locks = {}
    runtime.scheduler = scheduler
    runtime.employee = TickEmployee(scheduler)
    runtime.mcp = None

    async with sessions() as db:
        agent = Agent(name="worker")
        db.add(agent)
        await db.flush()
        item = WorkItem(
            agent_id=agent.id,
            title="Cursor task",
            status="waiting_external",
            wait_owner="external",
            wait_until=utcnow() - timedelta(minutes=1),
            chat_id="777",
            reply_phone="+1",
            metadata_json={"cursor_in_flight": True},
        )
        db.add(item)
        await db.commit()

        outcome = await runtime.tick(db, agent, reason="employee_heartbeat")

        assert outcome["skipped"] is True
        assert outcome["reason"] == "cursor_poll_only"
        assert outcome["cursor_poll_jobs_created"]
        assert scheduler.ids
    await engine.dispose()


@pytest.mark.asyncio
async def test_pm_poll_parser_moves_completed_run_to_qa(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "pm-poll.db")
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.flush()
        item = WorkItem(
            agent_id=agent.id,
            project_id="project",
            title="Structured Cursor task",
            status="waiting_external",
            wait_owner="external",
            pm_phase="IN_DEVELOPMENT",
            metadata_json={"cursor_in_flight": True},
        )
        db.add(item)
        await db.flush()
        run = CursorRun(
            work_item_id=item.id,
            project_id="project",
            attempt=1,
            idempotency_key="pm-poll-test",
            status="running",
        )
        db.add(run)
        await db.flush()
        item.active_cursor_run_id = run.id
        await db.commit()

        result = await _apply_pm_cursor_result(
            db,
            item,
            run,
            {
                "done": True,
                "result": {
                    "task_id": str(item.id),
                    "status": "completed",
                    "implementation": {
                        "summary": "Готово",
                        "files_changed": ["app.py"],
                        "tests": ["pytest"],
                    },
                    "verification": {
                        "tests_passed": True,
                        "lint_passed": True,
                        "acceptance_criteria": [],
                    },
                    "questions": [],
                    "risks": [],
                    "limitations": [],
                },
            },
        )

        assert result["done"] is True
        assert item.pm_phase == "QA"
        assert item.status == "in_progress"
        assert not item.metadata_json["cursor_in_flight"]
        assert run.status == "completed"
    await engine.dispose()


@pytest.mark.asyncio
async def test_pm_mismatched_idle_cursor_result_allows_resubmit(
    tmp_path: Path,
) -> None:
    engine, sessions = await sessions_for(tmp_path / "pm-mismatch.db")
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.flush()
        item = WorkItem(
            agent_id=agent.id,
            project_id="project",
            title="Current task",
            status="waiting_external",
            wait_owner="external",
            pm_phase="IN_DEVELOPMENT",
            metadata_json={"cursor_in_flight": True},
        )
        db.add(item)
        await db.flush()
        run = CursorRun(
            work_item_id=item.id,
            project_id="project",
            attempt=1,
            idempotency_key="pm-mismatch-test",
            status="running",
        )
        db.add(run)
        await db.flush()
        item.active_cursor_run_id = run.id
        await db.commit()

        result = await _apply_pm_cursor_result(
            db,
            item,
            run,
            {
                "done": True,
                "result": {
                    "task_id": str(item.id + 100),
                    "status": "completed",
                    "implementation": {},
                    "verification": {},
                },
            },
        )

        assert result["leftover"] is True
        assert result["resubmit"] is True
        assert item.pm_phase == "READY_FOR_DEV"
        assert item.status == "in_progress"
        assert item.wait_owner == "self"
        assert item.active_cursor_run_id is None
        assert item.next_action == "submit_development_task"
        assert run.status == "cancelled"
        assert not (item.metadata_json or {}).get("automatic_resubmit_blocked")
    await engine.dispose()


@pytest.mark.asyncio
async def test_evidenced_pm_qa_is_closed_without_another_llm_turn(
    tmp_path: Path,
) -> None:
    engine, sessions = await sessions_for(tmp_path / "pm-auto-accept.db")
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.flush()
        criterion = "Cancellation tests pass"
        item = WorkItem(
            agent_id=agent.id,
            project_id="project",
            title="Current task",
            status="in_progress",
            wait_owner="self",
            pm_phase="QA",
            acceptance_criteria=[criterion],
            metadata_json={"cursor_in_flight": False},
        )
        db.add(item)
        await db.flush()
        structured = {
            "task_id": str(item.id),
            "status": "completed",
            "implementation": {"summary": "Готово"},
            "verification": {
                "tests_passed": True,
                "lint_passed": True,
                "acceptance_criteria": [
                    {
                        "criterion": criterion,
                        "passed": True,
                        "evidence": "pytest passed",
                    }
                ],
            },
            "customer_response": "Исправление готово.",
        }
        run = CursorRun(
            work_item_id=item.id,
            project_id="project",
            attempt=1,
            idempotency_key="pm-auto-accept-test",
            status="completed",
            result_json=structured,
        )
        db.add(run)
        await db.flush()
        item.active_cursor_run_id = run.id
        item.last_cursor_summary = json.dumps(structured, ensure_ascii=False)
        await db.commit()

        result = await _auto_accept_pm_qa(db, item)
        await db.commit()

        assert result is not None
        assert result["deliver_origin"] is True
        assert result["result"] == "Исправление готово."
        assert item.pm_phase == "DONE"
        assert item.status == "done"
        assert item.active_cursor_run_id is None
    await engine.dispose()


def test_tick_focus_prefers_older_qa_case() -> None:
    older = WorkItem(id=31, title="old", status="in_progress", pm_phase="QA")
    newer = WorkItem(id=32, title="new", status="in_progress", pm_phase="READY_FOR_DEV")
    assert _tick_focus_item([newer, older]).id == 31
    ops = WorkItem(
        id=34,
        title="проверь все задачи по uraltrade и все сбрось удали все задачи обнулить",
        status="in_progress",
        pm_phase="DISCUSSION",
    )
    assert _tick_focus_item([ops, newer, older]).id == 31
    assert _tick_focus_item([ops, newer]).id == 32


@pytest.mark.asyncio
async def test_pm_leftover_keeps_completed_run_in_qa(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "pm-reuse.db")
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.flush()
        criterion = "List layout matches mockup"
        item = WorkItem(
            agent_id=agent.id,
            project_id="project",
            title="Current task",
            status="waiting_external",
            wait_owner="external",
            pm_phase="IN_DEVELOPMENT",
            acceptance_criteria=[criterion],
            metadata_json={"cursor_in_flight": True},
        )
        db.add(item)
        await db.flush()
        completed = CursorRun(
            work_item_id=item.id,
            project_id="project",
            attempt=1,
            idempotency_key="pm-reuse-completed",
            status="completed",
            result_json={
                "task_id": str(item.id),
                "status": "completed",
                "implementation": {"summary": "Готово"},
                "verification": {
                    "tests_passed": True,
                    "lint_passed": True,
                    "acceptance_criteria": [
                        {
                            "criterion": criterion,
                            "passed": True,
                            "evidence": "screenshot",
                        }
                    ],
                },
            },
        )
        stray = CursorRun(
            work_item_id=item.id,
            project_id="project",
            attempt=2,
            idempotency_key="pm-reuse-stray",
            status="running",
        )
        db.add_all([completed, stray])
        await db.flush()
        item.active_cursor_run_id = stray.id
        await db.commit()

        result = await _apply_pm_cursor_result(
            db,
            item,
            stray,
            {
                "done": True,
                "result": {
                    "task_id": str(item.id + 100),
                    "status": "completed",
                    "implementation": {},
                    "verification": {},
                },
            },
        )

        assert result["reused_completed_run"] is True
        assert result["done"] is True
        assert item.pm_phase == "QA"
        assert item.status == "in_progress"
        assert item.active_cursor_run_id == completed.id
        assert stray.status == "cancelled"
        assert not (item.metadata_json or {}).get("automatic_resubmit_blocked")
    await engine.dispose()


@pytest.mark.asyncio
async def test_auto_accept_uses_latest_completed_run_when_active_cleared(
    tmp_path: Path,
) -> None:
    engine, sessions = await sessions_for(tmp_path / "pm-accept-latest.db")
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.flush()
        criterion = "Cancellation tests pass"
        item = WorkItem(
            agent_id=agent.id,
            project_id="project",
            title="Current task",
            status="in_progress",
            wait_owner="self",
            pm_phase="QA",
            acceptance_criteria=[criterion],
            metadata_json={"cursor_in_flight": False},
        )
        db.add(item)
        await db.flush()
        run = CursorRun(
            work_item_id=item.id,
            project_id="project",
            attempt=1,
            idempotency_key="pm-accept-latest",
            status="completed",
            result_json={
                "task_id": str(item.id),
                "status": "completed",
                "implementation": {"summary": "Готово"},
                "verification": {
                    "tests_passed": True,
                    "lint_passed": True,
                    "acceptance_criteria": [
                        {
                            "criterion": "cancellation tests pass",
                            "passed": True,
                            "evidence": "pytest passed",
                        }
                    ],
                },
                "customer_response": "Исправление готово.",
            },
        )
        db.add(run)
        await db.flush()
        item.active_cursor_run_id = None
        await db.commit()

        result = await _auto_accept_pm_qa(db, item)
        await db.commit()

        assert result is not None
        assert result["deliver_origin"] is True
        assert item.pm_phase == "DONE"
        assert item.status == "done"
    await engine.dispose()


@pytest.mark.asyncio
async def test_stale_poll_job_does_not_replay_completion(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "stale-poll.db")
    scheduler = FakeScheduler()
    runtime = make_runtime(object(), scheduler)
    async with sessions() as db:
        agent = Agent(name="worker")
        db.add(agent)
        await db.flush()
        item = WorkItem(
            agent_id=agent.id,
            title="Already done",
            status="done",
            wait_owner="none",
            metadata_json={"cursor_in_flight": False},
        )
        db.add(item)
        await db.commit()

        outcome = await runtime.poll_cursor_followup(
            db,
            agent,
            {
                "source": "scheduled",
                "work_item_id": item.id,
                "cursor_assignment_seq": 0,
            },
        )

        assert outcome["skipped"] is True
        assert outcome["reason"] == "stale_cursor_poll"
        assert not scheduler.ids
    await engine.dispose()


@pytest.mark.asyncio
async def test_cursor_outage_reschedules_without_llm(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "poll-outage.db")
    scheduler = FakeScheduler()
    runtime = make_runtime(None, scheduler)
    runtime.telegram = None
    async with sessions() as db:
        agent = Agent(name="worker")
        db.add(agent)
        await db.flush()
        item = WorkItem(
            agent_id=agent.id,
            title="Cursor task",
            status="waiting_external",
            wait_owner="external",
            metadata_json={"cursor_in_flight": True},
        )
        db.add(item)
        await db.commit()

        outcome = await runtime.poll_cursor_followup(
            db,
            agent,
            {
                "source": "scheduled",
                "work_item_id": item.id,
                "cursor_assignment_seq": 0,
            },
        )

        assert outcome["skipped_llm"] is True
        assert outcome["rescheduled"] is True
        assert "not connected" in outcome["reason"]
        assert scheduler.ids
        job = await db.get(CronJob, scheduler.ids[-1])
        assert job is not None
        assert job.payload["cursor_poll_errors"] == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_poll_job_is_scoped_to_cursor_assignment(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "assignment-scope.db")
    scheduler = FakeScheduler()
    runtime = make_runtime(object(), scheduler)
    async with sessions() as db:
        agent = Agent(name="worker")
        db.add(agent)
        await db.flush()
        item = WorkItem(
            agent_id=agent.id,
            title="New assignment",
            status="waiting_external",
            wait_owner="external",
            metadata_json={
                "cursor_in_flight": True,
                "cursor_assignment_seq": 2,
            },
        )
        db.add(item)
        await db.commit()

        outcome = await runtime.poll_cursor_followup(
            db,
            agent,
            {
                "source": "scheduled",
                "work_item_id": item.id,
                "cursor_assignment_seq": 1,
            },
        )

        assert outcome["skipped"] is True
        assert outcome["reason"] == "stale_cursor_assignment"
        assert not scheduler.ids
    await engine.dispose()


@pytest.mark.asyncio
async def test_pm_leftover_poll_does_not_fall_back_to_llm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = await sessions_for(tmp_path / "pm-leftover-poll.db")
    scheduler = FakeScheduler()
    runtime = make_runtime(object(), scheduler)
    runtime.telegram = None

    async def foreign(_session, **_kwargs):
        return {
            "done": True,
            "result": {
                "task_id": "31",
                "status": "completed",
                "implementation": {"summary": "other task"},
                "verification": {
                    "tests_passed": True,
                    "lint_passed": True,
                    "acceptance_criteria": [],
                },
            },
        }

    monkeypatch.setattr("app.cursorremote_drive.check_and_drive", foreign)
    monkeypatch.setattr("app.runtime.pm_mode_enabled", lambda _profile: True)
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.flush()
        item = WorkItem(
            agent_id=agent.id,
            project_id="project",
            title="Current task",
            status="waiting_external",
            wait_owner="external",
            pm_phase="IN_DEVELOPMENT",
            metadata_json={"cursor_in_flight": True},
        )
        db.add(item)
        await db.flush()
        run = CursorRun(
            work_item_id=item.id,
            project_id="project",
            attempt=1,
            idempotency_key="pm-leftover-poll",
            status="running",
        )
        db.add(run)
        await db.flush()
        item.active_cursor_run_id = run.id
        await db.commit()

        outcome = await runtime.poll_cursor_followup(
            db,
            agent,
            {
                "source": "scheduled",
                "work_item_id": item.id,
                "cursor_assignment_seq": 0,
            },
        )

        assert outcome["skipped_llm"] is True
        assert outcome.get("fallback_llm") is not True
        assert outcome["leftover"] is True
        assert item.pm_phase == "READY_FOR_DEV"
        assert item.status == "in_progress"
        assert item.wait_owner == "self"
        assert item.next_action == "submit_development_task"
        assert not (item.metadata_json or {}).get("automatic_resubmit_blocked")
        assert outcome.get("resubmit") is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_idle_foreign_json_allows_resubmit(
    tmp_path: Path,
) -> None:
    engine, sessions = await sessions_for(tmp_path / "pm-idle-foreign.db")
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.flush()
        item = WorkItem(
            agent_id=agent.id,
            project_id="uraltrade",
            title="Mobile bottom nav",
            status="waiting_external",
            wait_owner="external",
            pm_phase="IN_DEVELOPMENT",
            metadata_json={"cursor_in_flight": True},
        )
        db.add(item)
        await db.flush()
        run = CursorRun(
            work_item_id=item.id,
            project_id="uraltrade",
            attempt=1,
            idempotency_key="pm-idle-foreign",
            status="running",
        )
        db.add(run)
        await db.flush()
        item.active_cursor_run_id = run.id
        await db.commit()

        result = await _apply_pm_cursor_result(
            db,
            item,
            run,
            {
                "done": True,
                "started": True,
                "status": "idle",
                "prompt_sent": False,
                "summary": '{  "task_id": 35,  "status": "completed",',
            },
        )

        assert result["leftover"] is True
        assert result["resubmit"] is True
        assert item.pm_phase == "READY_FOR_DEV"
        assert item.status == "in_progress"
        assert item.wait_owner == "self"
        assert item.next_action == "submit_development_task"
        assert item.last_error is None
        assert not (item.metadata_json or {}).get("automatic_resubmit_blocked")
        assert run.status == "cancelled"
    await engine.dispose()
