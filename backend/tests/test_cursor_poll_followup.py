from pathlib import Path
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Agent, Base, CronJob, CursorRun, WorkItem, utcnow
from app.employee import EmployeeService
from app.runtime import AgentRuntime, _apply_pm_cursor_result


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

    async def running(_session):
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

    async def completed(_session):
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
