from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Agent, Base, CronJob
from app.employee import save_once_job


class FakeScheduler:
    def __init__(self) -> None:
        self.ids: list[int] = []

    def upsert(self, job: CronJob) -> None:
        self.ids.append(job.id)


async def sessions_for(path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_save_once_job_does_not_collide_with_completed_name(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "cron.db")
    scheduler = FakeScheduler()
    async with sessions() as db:
        agent = Agent(name="scheduler-agent")
        db.add(agent)
        await db.flush()
        done = CronJob(
            name="check-lavve",
            agent_id=agent.id,
            cron="@once",
            payload={"run_once_at": "2026-08-17T15:00:00+00:00"},
            enabled=False,
            last_run_at=datetime.now(timezone.utc),
        )
        db.add(done)
        await db.commit()
        agent_id, done_id = agent.id, done.id

        created = await save_once_job(
            db,
            scheduler,
            agent_id=agent_id,
            name="check-lavve",
            payload={"message": "cursorremote_check", "run_once_at": "2026-08-17T16:00:00+00:00"},
        )
        assert created.id != done_id
        assert created.name != "check-lavve"
        assert created.name.startswith("check-lavve-")
        assert created.enabled is True
        leftover = CronJob(
            name="another-followup",
            agent_id=agent_id,
            cron="@once",
            payload={},
            enabled=True,
        )
        db.add(leftover)
        await db.commit()
        assert leftover.id is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_save_once_job_updates_pending_job_with_same_name(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "cron-update.db")
    scheduler = FakeScheduler()
    async with sessions() as db:
        agent = Agent(name="scheduler-agent-2")
        db.add(agent)
        await db.flush()
        pending = CronJob(
            name="check-lavve",
            agent_id=agent.id,
            cron="@once",
            payload={"run_once_at": "2026-08-17T15:00:00+00:00", "reply_chat_id": 42},
            enabled=True,
        )
        db.add(pending)
        await db.commit()
        pending_id = pending.id

        updated = await save_once_job(
            db,
            scheduler,
            agent_id=agent.id,
            name="check-lavve",
            payload={"message": "check again", "run_once_at": "2026-08-17T16:10:00+00:00"},
        )
        assert updated.id == pending_id
        assert updated.payload["run_once_at"] == "2026-08-17T16:10:00+00:00"
        assert updated.payload["reply_chat_id"] == 42
    await engine.dispose()


@pytest.mark.asyncio
async def test_save_once_job_does_not_mutate_currently_running_job(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "cron-running.db")
    async with sessions() as db:
        agent = Agent(name="scheduler-agent-3")
        db.add(agent)
        await db.flush()
        running = CronJob(
            name="check-lavve",
            agent_id=agent.id,
            cron="@once",
            payload={"run_once_at": "2026-08-17T15:00:00+00:00"},
            enabled=True,
        )
        db.add(running)
        await db.commit()

        followup = await save_once_job(
            db,
            SimpleNamespace(upsert=lambda job: None),
            agent_id=agent.id,
            name="check-lavve",
            payload={"message": "later", "run_once_at": "2026-08-17T16:00:00+00:00"},
            current_job_id=running.id,
        )
        assert followup.id != running.id
        assert followup.name != "check-lavve"
        original = await db.get(CronJob, running.id)
        assert original is not None
        assert original.payload["run_once_at"] == "2026-08-17T15:00:00+00:00"
    await engine.dispose()
