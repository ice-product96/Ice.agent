from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Agent, Base, Consultation, EmployeeNeed
from app.employee import EmployeeService


async def sessions_for(path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_dismiss_closes_open_consultation(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "dismiss.db")
    employee = EmployeeService()
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.flush()
        consult = Consultation(
            agent_id=agent.id,
            question="Что делать дальше?",
            context="idle",
            status="open",
        )
        db.add(consult)
        await db.flush()
        need = EmployeeNeed(
            agent_id=agent.id,
            kind="decision",
            title="Consult: Что делать дальше?",
            detail="idle",
            status="waiting",
            consultation_id=consult.id,
        )
        db.add(need)
        await db.commit()
        consult_id = consult.id

        item = await employee.dismiss_consultation(db, consult_id, reason="Не актуально")
        assert item.status == "dismissed"
        assert item.answer_text == "Не актуально"
        refreshed_need = await db.get(EmployeeNeed, need.id)
        assert refreshed_need is not None
        assert refreshed_need.status == "dropped"
    await engine.dispose()


@pytest.mark.asyncio
async def test_create_consultation_dedupes_open_duplicate_question(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "dedupe.db")
    employee = EmployeeService()
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        first = await employee.create_consultation(
            db, agent, question="Нет открытых задач", context="tick"
        )
        second = await employee.create_consultation(
            db, agent, question="Нет открытых задач", context="tick again"
        )
        assert first["consultation"]["id"] == second["consultation"]["id"]
        assert second.get("duplicate") is True
        total = len((await db.scalars(
            __import__("sqlalchemy").select(Consultation).where(Consultation.agent_id == agent.id)
        )).all())
        assert total == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_resolve_sets_answer_and_closes(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "resolve.db")
    employee = EmployeeService()
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        created = await employee.create_consultation(
            db, agent, question="Где репозиторий?", context="LAVVE"
        )
        consult_id = int(created["consultation"]["id"])
        item = await employee.resolve_consultation(
            db,
            consult_id,
            status="answered",
            answer_text="repo в /projects/lavve",
            schedule_tick=False,
        )
        assert item.status == "answered"
        assert item.answer_text == "repo в /projects/lavve"
    await engine.dispose()
