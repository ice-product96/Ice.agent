from pathlib import Path

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import AgentJudgment, Base, RuntimeSettings, WorkItemEvent, WorkItem, Agent
from app.judgment import (
    JUDGE_SPECS,
    IntentVerdict,
    JudgmentService,
    QaVerdict,
    judge_mode_for,
    judge_threshold_for,
    payload_digest,
    strict_json_schema,
)
from app.trace import current_trace_id, trace_scope


class FakeStructuredClient:
    def __init__(self, replies: list[dict], *, fail: Exception | None = None) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.model = "fake-judge"
        self.fail = fail

    async def structured(self, *, system, user, schema, schema_name, model=None, timeout=45.0):
        self.calls.append({"system": system, "user": user, "schema": schema, "schema_name": schema_name, "model": model})
        if self.fail is not None:
            raise self.fail
        reply = self.replies.pop(0)
        return json.dumps(reply, ensure_ascii=False), {
            "model": model or self.model,
            "prompt_tokens": 100,
            "completion_tokens": 20,
        }


async def sessions_for(path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _intent(verdict: str = "work_request", confidence: float = 0.9) -> dict:
    return {
        "verdict": verdict,
        "confidence": confidence,
        "evidence": [{"source": "customer_message", "text": "добавь экспорт"}],
        "missing": [],
        "reasoning": "asks for a feature",
        "is_work": verdict in {"work_request", "change_request", "bug_report"},
        "continues_open_case": False,
        "wipe_scope": None,
        "summary": "export to excel",
    }


def test_strict_schema_marks_all_objects_closed_and_required() -> None:
    schema = strict_json_schema(QaVerdict)

    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node.get("required") or []) == set((node.get("properties") or {}).keys())
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(schema)
    assert "criteria" in schema["properties"]


def test_every_judge_spec_has_strict_schema_and_prompt() -> None:
    for kind, spec in JUDGE_SPECS.items():
        assert spec.kind == kind
        assert spec.system
        schema = strict_json_schema(spec.schema)
        assert schema["additionalProperties"] is False
        assert 0.0 < spec.default_threshold <= 1.0


def test_mode_and_threshold_resolution() -> None:
    settings = RuntimeSettings(id=1, judge_modes={"*": "enforce", "qa_verifier": "shadow"}, judge_thresholds={"*": 0.9})
    assert judge_mode_for("message_intent", settings) == "enforce"
    assert judge_mode_for("qa_verifier", settings) == "shadow"
    assert judge_mode_for("qa_verifier", settings, {"judge_modes": {"qa_verifier": "enforce"}}) == "enforce"
    assert judge_threshold_for("message_intent", settings) == 0.9
    assert judge_threshold_for("message_intent", settings, {"judge_thresholds": {"message_intent": 0.5}}) == 0.5
    assert judge_mode_for("message_intent", None) == JUDGE_SPECS["message_intent"].default_mode
    assert judge_mode_for("message_intent", RuntimeSettings(id=1, judge_modes={"message_intent": "bogus"})) == "shadow"


def test_payload_digest_is_order_independent() -> None:
    assert payload_digest("k", {"a": 1, "b": 2}) == payload_digest("k", {"b": 2, "a": 1})
    assert payload_digest("k", {"a": 1}) != payload_digest("other", {"a": 1})


@pytest.mark.asyncio
async def test_judge_records_verdict_and_caches(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "judge.db")
    service = JudgmentService(settings=None, events=None)
    service.bind_runtime_settings(RuntimeSettings(id=1, judge_modes={"message_intent": "enforce"}))
    client = FakeStructuredClient([_intent()])
    async with sessions() as db:
        with trace_scope() as trace_id:
            first = await service.judge(
                "message_intent",
                {"message": "добавь экспорт", "sender_role": "customer"},
                db=db,
                chat_id="42",
                legacy={"verdict": "small_talk"},
                client=client,
            )
            await db.commit()
        assert first.available
        assert isinstance(first.verdict, IntentVerdict)
        assert first.verdict.verdict == "work_request"
        assert first.enforce is True
        assert first.agreed is False
        assert first.cached is False
        rows = list(await db.scalars(select(AgentJudgment)))
        assert len(rows) == 1
        assert rows[0].decision_trace_id == trace_id
        assert rows[0].mode == "enforce"
        assert rows[0].enforced is True
        assert rows[0].prompt_tokens == 100
        assert rows[0].legacy_json == {"verdict": "small_talk"}

        second = await service.judge(
            "message_intent",
            {"sender_role": "customer", "message": "добавь экспорт"},
            db=db,
            chat_id="42",
            client=client,
        )
        await db.commit()
        assert second.cached is True
        assert len(client.calls) == 1
        assert second.verdict.verdict == "work_request"
    await engine.dispose()


@pytest.mark.asyncio
async def test_judge_off_mode_skips_llm(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "judge-off.db")
    service = JudgmentService(settings=None, events=None)
    service.bind_runtime_settings(RuntimeSettings(id=1, judge_modes={"message_intent": "off"}))
    client = FakeStructuredClient([_intent()])
    async with sessions() as db:
        result = await service.judge("message_intent", {"message": "x"}, db=db, client=client)
        assert not result.available
        assert result.active is False
        assert client.calls == []
        assert list(await db.scalars(select(AgentJudgment))) == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_judge_degrades_without_raising(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "judge-fail.db")
    service = JudgmentService(settings=None, events=None)
    service.bind_runtime_settings(RuntimeSettings(id=1, judge_modes={"qa_verifier": "enforce"}))
    client = FakeStructuredClient([], fail=RuntimeError("provider down"))
    async with sessions() as db:
        result = await service.judge("qa_verifier", {"criteria": ["a"]}, db=db, client=client)
        await db.commit()
        assert result.degraded is True
        assert result.enforce is False
        assert "provider down" in (result.error or "")
        rows = list(await db.scalars(select(AgentJudgment)))
        assert len(rows) == 1 and rows[0].error

        no_client = await service.judge("qa_verifier", {"criteria": ["b"]}, db=db)
        assert no_client.degraded is True
        assert "no judge model" in (no_client.error or "")
    await engine.dispose()


@pytest.mark.asyncio
async def test_low_confidence_does_not_enforce(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "judge-conf.db")
    service = JudgmentService(settings=None, events=None)
    service.bind_runtime_settings(RuntimeSettings(id=1, judge_modes={"message_intent": "enforce"}))
    client = FakeStructuredClient([_intent(confidence=0.4)])
    async with sessions() as db:
        result = await service.judge("message_intent", {"message": "ммм"}, db=db, client=client)
        assert result.available and not result.confident and not result.enforce
    await engine.dispose()


@pytest.mark.asyncio
async def test_judge_many_runs_in_parallel(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "judge-many.db")
    service = JudgmentService(settings=None, events=None)
    service.bind_runtime_settings(RuntimeSettings(id=1))
    client = FakeStructuredClient([_intent(), _intent("small_talk")])
    async with sessions() as db:
        results = await service.judge_many(
            [
                ("message_intent", {"message": "a"}, {"db": db, "client": client}),
                ("message_intent", {"message": "b"}, {"db": db, "client": client}),
            ]
        )
        assert [item.value() for item in results] == ["work_request", "small_talk"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_trace_id_stamps_work_item_events(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "trace.db")
    async with sessions() as db:
        agent = Agent(name="pm-trace")
        db.add(agent)
        await db.flush()
        item = WorkItem(agent_id=agent.id, title="t", goal="g")
        db.add(item)
        await db.flush()
        with trace_scope("abc123") as trace_id:
            assert current_trace_id() == trace_id == "abc123"
            db.add(WorkItemEvent(work_item_id=item.id, kind="note", title="inside"))
            await db.flush()
        assert current_trace_id() is None
        db.add(WorkItemEvent(work_item_id=item.id, kind="note", title="outside"))
        await db.flush()
        rows = {row.title: row.decision_trace_id for row in await db.scalars(select(WorkItemEvent))}
        assert rows == {"inside": "abc123", "outside": None}
    await engine.dispose()
