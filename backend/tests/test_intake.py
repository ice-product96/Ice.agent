from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Agent, Base, WorkItem, utcnow
from app.employee_policy import customer_intake_flush_instruction, customer_intake_instruction, intake_debounce_minutes
from app.work_items import (
    begin_customer_intake,
    compile_intake_brief,
    mark_intake_executing,
    should_collect_customer_intake,
    watchdog_items,
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


def test_default_intake_debounce_is_15_minutes() -> None:
    from types import SimpleNamespace

    assert intake_debounce_minutes(SimpleNamespace(config_json={})) == 15
    assert intake_debounce_minutes(SimpleNamespace(config_json={"policy": {"intake_debounce_minutes": 0}})) == 0


def test_intake_reply_forbids_mentioning_the_wait() -> None:
    text = customer_intake_instruction()
    assert "Do NOT mention a delay" in text
    assert "cursorremote_do" in text


def test_flush_instruction_keeps_progress_off_customer() -> None:
    text = customer_intake_flush_instruction()
    assert "progress/result" not in text
    assert "ONLY the finished result" in text
    assert "manager" in text.lower()
    assert "SINGLE cursorremote_do" in text


def test_should_collect_customer_telegram_not_admin() -> None:
    item = WorkItem(id=1, agent_id=1, title="x", status="in_progress")
    assert should_collect_customer_intake(
        item, {"source": "telegram", "is_admin": False}, minutes=15
    )
    assert not should_collect_customer_intake(
        item, {"source": "telegram", "is_admin": True}, minutes=15
    )
    assert not should_collect_customer_intake(
        item, {"source": "telegram", "is_admin": False}, minutes=0
    )
    assert not should_collect_customer_intake(
        item, {"source": "intake_flush", "is_admin": False}, minutes=15
    )
    item.status = "waiting_external"
    assert not should_collect_customer_intake(
        item, {"source": "telegram", "is_admin": False}, minutes=15
    )


@pytest.mark.asyncio
async def test_begin_intake_accumulates_and_resets_wait(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "intake.db")
    scheduler = FakeScheduler()
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        item = WorkItem(agent_id=agent.id, title="Задача", status="in_progress", chat_id="77")
        db.add(item)
        await db.commit()
        await db.refresh(item)
        first = await begin_customer_intake(
            db, item, "Сделай карусель", minutes=15, scheduler=scheduler, agent_id=agent.id
        )
        assert first.status == "collecting"
        assert first.wait_until is not None
        first_until = first.wait_until
        assert "Сделай карусель" in compile_intake_brief(first)
        assert scheduler.ids
        second = await begin_customer_intake(
            db, first, "и цены тоже", minutes=15, scheduler=scheduler, agent_id=agent.id
        )
        assert second.status == "collecting"
        brief = compile_intake_brief(second)
        assert "карусель" in brief and "цены" in brief
        assert second.wait_until >= first_until
        assert intake_blob_count(second) == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_intake_keeps_customer_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.cursor_assets.assets_root", lambda: tmp_path / "assets")
    engine, sessions = await sessions_for(tmp_path / "img.db")
    scheduler = FakeScheduler()
    png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/"
        "x8AAwMCAO+ip1sAAAAASUVORK5CYII="
    )
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        item = WorkItem(agent_id=agent.id, title="Задача", status="in_progress", chat_id="77")
        db.add(item)
        await db.commit()
        await db.refresh(item)
        saved = await begin_customer_intake(
            db,
            item,
            "Поставь это в шапку",
            minutes=15,
            scheduler=scheduler,
            agent_id=agent.id,
            attachments=[{
                "kind": "image",
                "filename": "hero.png",
                "mime_type": "image/png",
                "data_b64": png,
            }],
        )
        brief = compile_intake_brief(saved)
        assert "шапку" in brief
        assert "hero.png" in brief
        images = (saved.metadata_json or {}).get("customer_images") or []
        assert images and Path(images[0]["path"]).is_file()
    await engine.dispose()


def intake_blob_count(item: WorkItem) -> int:
    blob = (item.metadata_json or {}).get("intake") or {}
    return len(list(blob.get("messages") or []))


@pytest.mark.asyncio
async def test_watchdog_skips_future_collecting(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "wd.db")
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        future = WorkItem(
            agent_id=agent.id,
            title="later",
            status="collecting",
            wait_until=utcnow() + timedelta(minutes=10),
        )
        overdue = WorkItem(
            agent_id=agent.id,
            title="due",
            status="collecting",
            wait_until=utcnow() - timedelta(minutes=1),
        )
        db.add_all([future, overdue])
        await db.commit()
        items = await watchdog_items(db, agent.id)
        ids = {item.id for item in items}
        assert overdue.id in ids
        assert future.id not in ids
    await engine.dispose()


@pytest.mark.asyncio
async def test_watchdog_skips_collecting_when_flush_job_exists(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "wd-job.db")
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        item = WorkItem(
            agent_id=agent.id,
            title="due-with-job",
            status="collecting",
            wait_until=utcnow() - timedelta(minutes=1),
            cron_job_id=99,
            metadata_json={"intake": {"armed": True, "flush_job_id": 99}},
        )
        db.add(item)
        await db.commit()
        items = await watchdog_items(db, agent.id)
        assert item.id not in {row.id for row in items}
    await engine.dispose()


@pytest.mark.asyncio
async def test_reset_cursor_assignment_clears_in_flight(tmp_path: Path) -> None:
    from app.work_items import reset_cursor_assignment

    engine, sessions = await sessions_for(tmp_path / "reset-cursor.db")
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        item = WorkItem(
            agent_id=agent.id,
            title="LAVVE image",
            status="waiting_external",
            metadata_json={"cursor_in_flight": True, "cursor_assignment_seq": 3},
        )
        db.add(item)
        await db.commit()
        await db.refresh(item)
        reset = await reset_cursor_assignment(db, item, note="manual reset")
        assert reset.status == "in_progress"
        assert not (reset.metadata_json or {}).get("cursor_in_flight")
        assert int((reset.metadata_json or {}).get("cursor_assignment_seq") or 0) == 4
    await engine.dispose()


@pytest.mark.asyncio
async def test_mark_intake_executing_clears_stale_cursor_flag(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "flush.db")
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        item = WorkItem(
            agent_id=agent.id,
            title="LAVVE",
            status="collecting",
            metadata_json={"cursor_in_flight": True, "intake": {"armed": True, "messages": [{"text": "скругления"}]}},
        )
        db.add(item)
        await db.commit()
        await db.refresh(item)
        flushed = await mark_intake_executing(db, item)
        assert flushed.status == "in_progress"
        assert not (flushed.metadata_json or {}).get("cursor_in_flight")
        assert (flushed.metadata_json or {}).get("intake", {}).get("armed") is False
        flushed.metadata_json = {**(flushed.metadata_json or {}), "cursor_in_flight": True}
        again = await mark_intake_executing(db, flushed)
        assert not (again.metadata_json or {}).get("cursor_in_flight")
        assert again.status == "in_progress"
        assert int((again.metadata_json or {}).get("cursor_assignment_seq") or 0) >= 2
    await engine.dispose()
