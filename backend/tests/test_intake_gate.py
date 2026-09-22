from pathlib import Path

import json

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.customers import match_customer_from_text
from app.db import Agent, Base, Customer, RuntimeSettings, WorkItem
from app.intake_gate import classify_manager_reply, classify_message, legacy_intent
from app.judgment import JudgmentService
from app.work_items import bind_work_item


class FakeStructuredClient:
    def __init__(self, replies: list[dict]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.model = "fake-judge"

    async def structured(self, *, system, user, schema, schema_name, model=None, timeout=45.0):
        self.calls.append({"schema_name": schema_name, "user": json.loads(user)})
        return json.dumps(self.replies.pop(0), ensure_ascii=False), {"model": "fake", "prompt_tokens": 1, "completion_tokens": 1}


async def sessions_for(path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def service_with(**modes: str) -> JudgmentService:
    service = JudgmentService(settings=None)
    service.bind_runtime_settings(RuntimeSettings(id=1, judge_modes=modes))
    return service


def intent_reply(verdict: str, *, is_work: bool, confidence: float = 0.9, wipe: str | None = None, continues: bool = False) -> dict:
    return {
        "verdict": verdict,
        "confidence": confidence,
        "evidence": [{"source": "customer_message", "text": "да, добавь экспорт"}],
        "missing": [],
        "reasoning": "r",
        "is_work": is_work,
        "continues_open_case": continues,
        "wipe_scope": wipe,
        "summary": "s",
    }


def test_legacy_intent_still_available_for_shadow() -> None:
    assert legacy_intent("спасибо", is_admin=False).is_work is False
    assert legacy_intent("сбрось все задачи", is_admin=True).operational_admin is True
    assert legacy_intent("сбрось все задачи", is_admin=False).operational_admin is False


@pytest.mark.asyncio
async def test_short_yes_with_feature_is_work_under_judge(tmp_path: Path) -> None:
    """'Да, добавь экспорт в Excel' is 6 ack-ish tokens for the word list but real work."""
    engine, sessions = await sessions_for(tmp_path / "intent.db")
    service = service_with(message_intent="enforce")
    client = FakeStructuredClient([intent_reply("work_request", is_work=True)])
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.flush()
        await db.commit()
        context = {
            "source": "telegram",
            "chat_id": "c1",
            "reply_chat_id": "c1",
            "message_id": "m1",
            "_judgment": service,
            "_llm_client": client,
        }
        item = await bind_work_item(db, agent, context, "Да, добавь экспорт в Excel")
        assert item is not None
        assert context["_intent"]["intent"] == "work_request"
        assert client.calls[0]["user"]["sender_role"] == "customer"
    await engine.dispose()


@pytest.mark.asyncio
async def test_thanks_does_not_open_case_and_attaches_recent(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "intent-ack.db")
    service = service_with(message_intent="enforce")
    client = FakeStructuredClient([intent_reply("acknowledgement", is_work=False, continues=True)])
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.flush()
        closed = WorkItem(agent_id=agent.id, chat_id="c1", title="Done case", status="done", pm_phase="DONE")
        db.add(closed)
        await db.commit()
        context = {"source": "telegram", "chat_id": "c1", "reply_chat_id": "c1", "message_id": "m2", "_judgment": service, "_llm_client": client}
        item = await bind_work_item(db, agent, context, "Огромное спасибо, всё отлично работает!")
        assert item is not None and item.id == closed.id
        assert context["_continuation_of_closed"] is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_admin_wipe_order_detected_by_meaning(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "intent-admin.db")
    service = service_with(message_intent="enforce")
    client = FakeStructuredClient([intent_reply("operational_admin", is_work=False, wipe="mysell")])
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.commit()
        context = {"source": "telegram", "chat_id": "adm", "reply_chat_id": "adm", "message_id": "m3", "is_admin": True, "_judgment": service, "_llm_client": client}
        item = await bind_work_item(db, agent, context, "Прибей всё по mysell, начинаем с чистого листа")
        assert item is None
        assert context["_operational_admin"] is True
        assert context["_operational_wipe_scope"] == "mysell"
    await engine.dispose()


@pytest.mark.asyncio
async def test_degraded_intent_judge_treats_message_as_work(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "intent-degraded.db")
    service = service_with(message_intent="enforce")

    class Broken:
        model = "x"

        async def structured(self, **kwargs):
            raise RuntimeError("down")

    async with sessions() as db:
        decision = await classify_message(db, judgment=service, message="ок", context={"source": "telegram"}, client=Broken())
        assert decision.is_work is True and decision.legacy is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_route_customer_by_meaning_not_prefix(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "route.db")
    service = service_with(route_customer="enforce")
    client = FakeStructuredClient(
        [
            {
                "verdict": "matched",
                "confidence": 0.93,
                "evidence": [{"source": "customer_message", "text": "по трейдингу"}],
                "missing": [],
                "reasoning": "trading domain",
                "customer_id": "uraltrading",
                "project_id": "uraltrading",
                "alternatives": [],
            },
            {
                "verdict": "ambiguous",
                "confidence": 0.5,
                "evidence": [],
                "missing": ["which project"],
                "reasoning": "both fit",
                "customer_id": None,
                "project_id": None,
                "alternatives": ["uraltrade", "uraltrading"],
            },
        ]
    )
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.flush()
        db.add_all(
            [
                Customer(id="uraltrade", name="УралТрейд", project_id="uraltrade", agent_id=agent.id),
                Customer(id="uraltrading", name="Uraltrading Terminal", project_id="uraltrading", agent_id=agent.id),
            ]
        )
        await db.commit()
        context = {"_judgment": service, "_llm_client": client}
        chosen = await match_customer_from_text(db, agent, "по трейдингу нужен график", context=context)
        assert chosen is not None and chosen.id == "uraltrading"
        assert {c["customer_id"] for c in client.calls[0]["user"]["candidates"]} == {"uraltrade", "uraltrading"}
        none = await match_customer_from_text(db, agent, "поправь кнопку", context=context)
        assert none is None
        assert context["_routing_ambiguous"] == ["uraltrade", "uraltrading"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_manager_reply_no_but_is_instruction_not_rejection(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "manager.db")
    service = service_with(manager_reply="enforce")
    client = FakeStructuredClient(
        [
            {
                "verdict": "answered",
                "confidence": 0.9,
                "evidence": [{"source": "manager_message", "text": "нет, но давай попробуем через API"}],
                "missing": [],
                "reasoning": "instruction",
                "answer": "Попробовать через API",
            }
        ]
    )
    async with sessions() as db:
        decision = await classify_manager_reply(
            db,
            judgment=service,
            requires_approval=True,
            question="Можно удалить старую БД?",
            action_name="drop_db",
            text="нет, но давай попробуем через API",
            client=client,
        )
        assert decision.status == "answered"
        assert decision.answer == "Попробовать через API"
        from app.routing import consultation_status_for_reply

        assert consultation_status_for_reply(True, "нет, но давай попробуем через API") == "rejected"
    await engine.dispose()
