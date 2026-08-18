from pathlib import Path

import pytest

from app.db import Agent, CronJob, WorkItem
from app.work_items import abort_work_item, cancel_work_item_schedules, counts_for_agent, is_inbox_actionable, work_item_aborted

from test_intake import sessions_for


class FakeScheduler:
    def __init__(self) -> None:
        self.removed: list[int] = []

    def remove(self, job_id: int) -> None:
        self.removed.append(job_id)


@pytest.mark.asyncio
async def test_cancel_work_item_schedules_disables_linked_cron(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "cancel.db")
    scheduler = FakeScheduler()
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.flush()
        item = WorkItem(agent_id=agent.id, title="LAVVE", status="waiting_external")
        db.add(item)
        await db.flush()
        job = CronJob(
            name=f"intake-flush-{item.id}",
            agent_id=agent.id,
            cron="@once",
            payload={"work_item_id": item.id, "kind": "intake_flush"},
            enabled=True,
        )
        db.add(job)
        await db.commit()
        await db.refresh(item)
        cancelled = await cancel_work_item_schedules(db, item, scheduler)
        assert job.id in cancelled
        assert scheduler.removed == [job.id]
        assert work_item_aborted(item)
    await engine.dispose()


@pytest.mark.asyncio
async def test_abort_work_item_marks_done_and_aborted(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "abort.db")
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.flush()
        item = WorkItem(agent_id=agent.id, title="stuck", status="in_progress")
        db.add(item)
        await db.commit()
        await db.refresh(item)
        aborted = await abort_work_item(db, item, note="stop", scheduler=None)
        assert aborted.status == "done"
        assert work_item_aborted(aborted)
    await engine.dispose()


@pytest.mark.asyncio
async def test_counts_exclude_waiting_external_and_aborted(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "counts.db")
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.flush()
        db.add_all([
            WorkItem(agent_id=agent.id, title="active", status="in_progress"),
            WorkItem(agent_id=agent.id, title="cursor", status="waiting_external"),
            WorkItem(agent_id=agent.id, title="cursor2", status="waiting_external"),
            WorkItem(
                agent_id=agent.id,
                title="cancelled",
                status="done",
                metadata_json={"aborted": True},
            ),
        ])
        await db.commit()
        counts = await counts_for_agent(db, agent.id)
        assert counts["actionable"] == 1
        assert counts["waiting_external"] == 2
        assert counts["done"] == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_is_inbox_actionable_matches_ui_filter(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "actionable.db")
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.flush()
        waiting = WorkItem(agent_id=agent.id, title="wait", status="waiting_external")
        db.add(waiting)
        await db.commit()
        assert not is_inbox_actionable(waiting)
    await engine.dispose()
