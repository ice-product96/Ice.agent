from pathlib import Path

import json

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.approval_gate import (
    actor_from_context,
    detect_approval,
    judge_scope,
    legacy_subject,
    scope_block_message,
)
from app.db import Agent, Base, RuntimeSettings, WorkItem
from app.judgment import JudgmentService


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


def service_with(kind: str, mode: str) -> JudgmentService:
    service = JudgmentService(settings=None)
    service.bind_runtime_settings(RuntimeSettings(id=1, judge_modes={kind: mode}))
    return service


def approval_reply(verdict: str, *, subject: str = "spec", by: str = "customer", confidence: float = 0.92, conditions=None) -> dict:
    return {
        "verdict": verdict,
        "confidence": confidence,
        "evidence": [{"source": "customer_message", "text": "ок, ТЗ подходит"}],
        "missing": [],
        "reasoning": "explicit yes to the spec question",
        "subject": subject,
        "approved_by": by,
        "conditions": conditions or [],
    }


def scope_reply(verdict: str, *, size: str = "small", risk: str = "low", ready: bool = True, outside=None, confidence: float = 0.9) -> dict:
    return {
        "verdict": verdict,
        "confidence": confidence,
        "evidence": [{"source": "spec", "text": "Учёт складов"}],
        "missing": [],
        "reasoning": "matches in_scope item",
        "size": size,
        "risk": risk,
        "outside_items": outside or [],
        "ready_to_execute": ready,
    }


def test_actor_comes_from_routing_not_text() -> None:
    assert actor_from_context({"source": "telegram", "message_id": "1", "is_admin": True}) == "manager"
    assert actor_from_context({"source": "telegram", "message_id": "1"}) == "customer"
    assert actor_from_context({"source": "employee_tick"}, "customer") == "customer"
    assert actor_from_context({"source": "employee_tick"}, "administrator@company") == "unknown"
    assert actor_from_context({"source": "employee_tick"}, "") == "unknown"


def test_legacy_subject_still_maps_for_shadow_comparison() -> None:
    assert legacy_subject("ТЗ", "да") == "spec"
    assert legacy_subject("стоимость", "ок") == "cost"
    assert legacy_subject("дизайн", "синий") == "other"


@pytest.mark.asyncio
async def test_enforced_approval_rejects_topic_discussion(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "approval.db")
    service = service_with("approval_detect", "enforce")
    client = FakeStructuredClient([approval_reply("not_an_approval")])
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.flush()
        item = WorkItem(agent_id=agent.id, project_id="mysell", title="Склады", pm_phase="REQUIREMENTS_READY")
        db.add(item)
        await db.flush()
        decision = await detect_approval(
            db,
            judgment=service,
            context={"source": "telegram", "message_id": "m1", "chat_id": "c1", "_user_message": "а по технической части что там?"},
            subject="spec",
            topic="ТЗ",
            decision="согласовано",
            confirmed_by="",
            item=item,
            client=client,
        )
        assert decision.approved is False
        assert decision.verdict == "not_an_approval"
        assert client.calls[0]["user"]["sender_role"] == "customer"
    await engine.dispose()


@pytest.mark.asyncio
async def test_enforced_approval_accepts_with_conditions(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "approval-ok.db")
    service = service_with("approval_detect", "enforce")
    client = FakeStructuredClient([approval_reply("partial", conditions=["без экспорта в Excel"])])
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.flush()
        item = WorkItem(agent_id=agent.id, project_id="mysell", title="Склады", pm_phase="REQUIREMENTS_READY")
        db.add(item)
        await db.flush()
        decision = await detect_approval(
            db,
            judgment=service,
            context={"source": "telegram", "message_id": "m1", "chat_id": "c1", "_user_message": "да, только без экспорта"},
            subject="spec",
            topic="ТЗ",
            decision="ок",
            confirmed_by="",
            item=item,
            client=client,
        )
        assert decision.customer_approved is True
        assert decision.conditions == ["без экспорта в Excel"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_shadow_approval_keeps_agent_claim(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "approval-shadow.db")
    service = service_with("approval_detect", "shadow")
    client = FakeStructuredClient([approval_reply("not_an_approval")])
    async with sessions() as db:
        decision = await detect_approval(
            db,
            judgment=service,
            context={"source": "telegram", "message_id": "m1", "_user_message": "техническая часть норм"},
            subject="spec",
            topic="ТЗ",
            decision="ок",
            confirmed_by="",
            item=None,
            client=client,
        )
        assert decision.legacy is True and decision.approved is True
        assert decision.judgment is not None and decision.judgment.agreed is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_internal_tick_without_live_message_uses_claim(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "approval-tick.db")
    service = service_with("approval_detect", "enforce")
    client = FakeStructuredClient([])
    async with sessions() as db:
        decision = await detect_approval(
            db,
            judgment=service,
            context={"source": "employee_tick", "_user_message": "tick"},
            subject="cost",
            topic="стоимость",
            decision="5000",
            confirmed_by="customer",
            item=None,
            client=client,
        )
        assert decision.legacy is True and decision.subject == "cost" and decision.actor == "customer"
        assert client.calls == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_scope_enforce_blocks_outside_spec_and_allows_inside(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "scope.db")
    service = service_with("scope_judge", "enforce")
    client = FakeStructuredClient(
        [
            scope_reply("outside_spec", outside=["Интеграция с Ozon API"]),
            scope_reply("inside_spec", size="medium"),
            scope_reply("inside_spec", size="epic", risk="high", ready=False),
        ]
    )
    spec = {"status": "confirmed", "summary": "Учёт складов", "in_scope": ["склады", "остатки"], "out_of_scope": [], "goals": [], "constraints": [], "modules": []}
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.flush()
        item = WorkItem(agent_id=agent.id, project_id="mysell", title="Ozon", goal="Интеграция", pm_phase="READY_FOR_DEV", requirements=["a"], acceptance_criteria=["b"])
        db.add(item)
        await db.flush()
        outside = await judge_scope(db, item, spec=spec, autonomy_level="LEVEL_2", judgment=service, client_confirmed=False, client=client)
        assert outside.inside_scope is False
        blocked = scope_block_message(outside, client_confirmed=False)
        assert blocked and "Интеграция с Ozon API" in blocked
        # explicit customer confirmation of the slice unblocks an outside request
        assert scope_block_message(outside, client_confirmed=True) is None

        inside = await judge_scope(db, item, spec=spec, autonomy_level="LEVEL_2", judgment=service, client_confirmed=False, client=client, project_config=None)
        # cache key is identical → judge reuses the first verdict; force a different payload
        item.title = "Склады v2"
        inside = await judge_scope(db, item, spec=spec, autonomy_level="LEVEL_2", judgment=service, client_confirmed=False, client=client)
        assert inside.inside_scope is True and inside.small_fix is False
        assert scope_block_message(inside, client_confirmed=False) is None

        item.title = "Весь продукт"
        epic = await judge_scope(db, item, spec=spec, autonomy_level="LEVEL_2", judgment=service, client_confirmed=True, client=client)
        assert epic.high_risk is True and epic.ready is False
        assert "not concrete enough" in (scope_block_message(epic, client_confirmed=True) or "")
    await engine.dispose()


@pytest.mark.asyncio
async def test_scope_shadow_keeps_legacy_flags(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "scope-shadow.db")
    service = service_with("scope_judge", "shadow")
    client = FakeStructuredClient([scope_reply("outside_spec")])
    async with sessions() as db:
        item = WorkItem(id=7, agent_id=1, project_id="p", title="t", context_json={"inside_agreed_scope": True, "small_fix": True})
        decision = await judge_scope(db, item, spec={"status": "confirmed"}, autonomy_level="LEVEL_1", judgment=service, client_confirmed=False, client=client)
        assert decision.legacy is True and decision.inside_scope is True and decision.small_fix is True
        assert scope_block_message(decision, client_confirmed=False) is None
        assert decision.judgment is not None and decision.judgment.agreed is False
    await engine.dispose()
