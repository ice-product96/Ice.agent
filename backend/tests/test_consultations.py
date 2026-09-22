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


def test_collect_sent_message_ids_flattens_chunks() -> None:
    from app.employee import collect_sent_message_ids

    assert collect_sent_message_ids({"id": 11}) == [11]
    assert collect_sent_message_ids([{"id": 1}, [{"message_id": 2}, {"id": 3}]]) == [1, 2, 3]


def test_consultation_telegram_text_asks_for_reply() -> None:
    from app.employee import build_consultation_telegram_text

    item = Consultation(
        id=35,
        agent_id=1,
        work_item_id=52,
        question="Cursor не выполняет работу",
        context="run #6 план",
        requires_approval=False,
    )
    text = build_consultation_telegram_text(agent_name="Макс", item=item)
    assert "Вопрос руководителю #35" in text
    assert "Кейс #52" in text
    assert "/answer 35" in text
    assert "Ответьте на это сообщение" in text


def test_reply_status_for_approval() -> None:
    from app.routing import consultation_status_for_reply

    assert consultation_status_for_reply(False, "перезапусти composer") == "answered"
    assert consultation_status_for_reply(True, "ок") == "approved"
    assert consultation_status_for_reply(True, "нет, рано") == "rejected"


@pytest.mark.asyncio
async def test_create_consultation_notifies_telegram_admins(tmp_path: Path) -> None:
    from app.db import TelegramAccount
    from app.employee import EmployeeService

    engine, sessions = await sessions_for(tmp_path / "tg.db")
    sent: list[str] = []

    class FakeTelegram:
        admin_ids = {183}

        async def notify_admins(self, phone: str, text: str, exclude_ids=None):
            sent.append(text)
            return [{"id": 9001}, {"id": 9002}]

    employee = EmployeeService(telegram=FakeTelegram())
    async with sessions() as db:
        account = TelegramAccount(phone="+7000", session_path="s1")
        db.add(account)
        await db.flush()
        agent = Agent(name="Макс", telegram_account_id=account.id)
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        created = await employee.create_consultation(
            db, agent, question="Нужен перезапуск Composer", context="кейс 52"
        )
        consult = created["consultation"]
        assert consult["telegram_delivered"] is True
        assert consult["telegram_message_ids"] == [9001, 9002]
        assert sent and "/answer" in sent[0]
        assert "Нужен перезапуск Composer" in sent[0]
    await engine.dispose()


@pytest.mark.asyncio
async def test_persist_tech_log_writes_message_log(tmp_path: Path) -> None:
    from app.db import MessageLog
    from app.employee import persist_tech_log
    from sqlalchemy import select

    engine, sessions = await sessions_for(tmp_path / "tech.db")
    async with sessions() as db:
        agent = Agent(name="Макс")
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        await persist_tech_log(
            db,
            agent_id=agent.id,
            text="[Ice.agent] Тик сотрудника",
            kind="tick",
            context={"work_item_id": 52, "source": "employee_tick"},
        )
        rows = (await db.scalars(select(MessageLog))).all()
        assert len(rows) == 1
        assert rows[0].direction == "tech"
        assert rows[0].metadata_json["kind"] == "tick"
        assert rows[0].work_item_id == 52
    await engine.dispose()
