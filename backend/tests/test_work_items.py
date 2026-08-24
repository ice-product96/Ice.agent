from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Agent, Base, Consultation, WorkItem
from app.employee import EmployeeService, save_once_job
from app.job_result import build_followup_payload
from app.work_items import (
    MAX_RETRIES,
    after_agent_run,
    bind_work_item,
    handle_run_failure,
    notify_channels,
    resume_work_item,
)


class FakeScheduler:
    def __init__(self) -> None:
        self.ids: list[int] = []

    def upsert(self, job) -> None:
        self.ids.append(job.id)


async def sessions_for(path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_notify_channels_failed_pages_manager_not_everyone() -> None:
    assert "manager" in notify_channels("failed")
    assert "ui" in notify_channels("failed")
    assert "customer" not in notify_channels("failed")
    assert notify_channels("progress") == {"ui"}
    assert notify_channels("waiting_manager") == {"manager", "ui"}


def test_followup_payload_keeps_work_item_id() -> None:
    payload = build_followup_payload(
        message="check cursor",
        run_at_iso="2026-08-17T15:00:00+00:00",
        timezone="UTC",
        context={"work_item_id": 42, "reply_chat_id": "100", "reply_phone": "+1"},
        account_phone="+1",
    )
    assert payload["work_item_id"] == 42
    assert payload["reply_chat_id"] == "100"
    assert payload["source"] == "scheduled"


@pytest.mark.asyncio
async def test_bind_reuses_open_case_for_chat(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "work.db")
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        first = await bind_work_item(
            db,
            agent,
            {"source": "telegram", "reply_chat_id": "777", "chat_id": "777"},
            "Собери ветку",
        )
        second = await bind_work_item(
            db,
            agent,
            {"source": "telegram", "reply_chat_id": "777", "chat_id": "777"},
            "ещё уточнение",
        )
        assert first is not None and second is not None
        assert first.id == second.id
        assert second.status == "in_progress"
    await engine.dispose()


@pytest.mark.asyncio
async def test_heartbeat_without_id_does_not_create_case(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "watch.db")
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        item = await bind_work_item(
            db,
            agent,
            {"source": "employee_heartbeat", "employee_tick": True},
            "сторож",
        )
        assert item is None
        leftover = await db.scalar(select(func.count()).select_from(WorkItem))
        assert leftover == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_pm_cursor_done_requires_explicit_qa_acceptance(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "pm-integrity.db")
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.flush()
        item = WorkItem(
            agent_id=agent.id,
            title="Structured task",
            goal="Do the work",
            status="waiting_external",
            pm_phase="QA",
            task_type="technical",
            requirements=["Implement the change"],
            acceptance_criteria=["Tests pass"],
        )
        db.add(item)
        await db.commit()
        await after_agent_run(
            db,
            agent,
            {"work_item_id": item.id, "_pm_mode": True},
            "Cursor says done",
            [
                {
                    "tool": "cursorremote_do",
                    "status": "success",
                    "result": {"done": True, "summary": "implemented"},
                }
            ],
        )
        assert item.pm_phase == "QA"
        assert item.status == "in_progress"
        assert item.next_action.startswith("Verify acceptance")
    await engine.dispose()


@pytest.mark.asyncio
async def test_handle_run_failure_retries_then_consults(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "fail.db")
    scheduler = FakeScheduler()
    employee = EmployeeService(telegram=None, scheduler=scheduler)
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        item = WorkItem(
            agent_id=agent.id,
            title="LAVVE",
            goal="собрать",
            status="waiting_external",
            chat_id="10",
            reply_phone="+1",
        )
        db.add(item)
        await db.commit()
        await db.refresh(item)
        first = await handle_run_failure(
            db, agent, {"work_item_id": item.id, "_cron_job_id": None}, RuntimeError("boom"), employee
        )
        assert first is not None
        assert first.status == "failed"
        assert first.retry_count == 1
        assert first.retry_count < MAX_RETRIES or first.consultation_id is None
        assert scheduler.ids

        second = await handle_run_failure(
            db, agent, {"work_item_id": item.id}, RuntimeError("boom again"), employee
        )
        assert second is not None
        assert second.retry_count >= MAX_RETRIES
        assert second.consultation_id is not None
        consult = await db.get(Consultation, second.consultation_id)
        assert consult is not None
        assert consult.work_item_id == item.id
    await engine.dispose()


@pytest.mark.asyncio
async def test_resume_work_item_sets_in_progress(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "resume.db")
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        item = WorkItem(agent_id=agent.id, title="кейс", status="failed", wait_owner="manager")
        db.add(item)
        await db.commit()
        await db.refresh(item)
        resumed = await resume_work_item(db, item, note="продолжи")
        assert resumed.status == "in_progress"
        assert resumed.wait_owner == "self"
        assert resumed.paused is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_save_once_job_keeps_work_item_id(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "once.db")
    scheduler = FakeScheduler()
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        job = await save_once_job(
            db,
            scheduler,
            agent_id=agent.id,
            name="retry-1",
            payload={
                "message": "продолжи",
                "run_once_at": "2026-08-17T15:00:00+00:00",
                "timezone": "UTC",
                "source": "scheduled",
                "work_item_id": 9,
            },
        )
        assert (job.payload or {}).get("work_item_id") == 9
    await engine.dispose()
