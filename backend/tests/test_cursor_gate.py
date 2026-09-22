from pathlib import Path

import json

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.cursor_callback import normalize_cursor_callback_payload
from app.cursor_gate import apply_completion_to_result, assess_completion, completion_signals
from app.cursorremote_drive import (
    cursor_has_active_work,
    pin_cursor_followup_message,
    set_text_heuristics,
    summary_looks_incomplete,
    CURSOR_CHECK_ONLY_MESSAGE,
)
from app.db import Agent, Base, CursorRun, RuntimeSettings, WorkItem
from app.judgment import JudgmentService
from app.runtime import _apply_pm_cursor_result


class FakeStructuredClient:
    def __init__(self, replies: list[dict]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.model = "fake"

    async def structured(self, *, system, user, schema, schema_name, model=None, timeout=45.0):
        self.calls.append(json.loads(user))
        return json.dumps(self.replies.pop(0), ensure_ascii=False), {"model": "fake", "prompt_tokens": 1, "completion_tokens": 1}


async def sessions_for(path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def service_with(mode: str, *, configured: bool = False) -> JudgmentService:
    service = JudgmentService(settings=None)
    if configured:
        service._clients["cheap"] = object()
    service.bind_runtime_settings(RuntimeSettings(id=1, judge_modes={"cursor_completion": mode}))
    return service


def completion_reply(verdict: str, *, confidence: float = 0.9, summary: str = "Каталог в шапке готов.") -> dict:
    return {
        "verdict": verdict,
        "confidence": confidence,
        "evidence": [{"source": "cursor_summary", "text": summary[:30]}],
        "missing": [],
        "reasoning": "r",
        "summary": summary,
    }


@pytest.fixture(autouse=True)
def _restore_heuristics():
    yield
    set_text_heuristics(True)


def test_idle_is_not_finished_in_callback() -> None:
    payload = normalize_cursor_callback_payload({"event": "statusChange", "status": "idle", "summary": "x"})
    assert payload["done"] is False
    finished = normalize_cursor_callback_payload({"event": "task_completed", "done": True, "summary": "Готово."})
    assert finished["done"] is True


def test_enforced_completion_judge_disables_text_heuristics() -> None:
    service_with("shadow")
    assert summary_looks_incomplete("Запускаю dev-сервер и проверю порт") is True
    assert pin_cursor_followup_message("проверь cursor") == CURSOR_CHECK_ONLY_MESSAGE
    assert cursor_has_active_work({"messages": [{"type": "assistant", "text": "поиск по файлам"}]}) is True

    service_with("enforce", configured=True)
    assert summary_looks_incomplete("Запускаю dev-сервер и проверю порт") is False
    assert pin_cursor_followup_message("проверь cursor", cursor_in_flight=False) == "проверь cursor"
    assert pin_cursor_followup_message("любой текст", cursor_in_flight=True) == CURSOR_CHECK_ONLY_MESSAGE
    assert cursor_has_active_work({"messages": [{"type": "assistant", "text": "поиск по файлам"}]}) is False
    assert cursor_has_active_work({"messages": [{"type": "tool", "text": "grep"}]}) is True


def test_signals_are_machine_facts() -> None:
    item = WorkItem(id=3, agent_id=1, title="t", metadata_json={"cursor_composer_id": "c-1"})
    signals = completion_signals(
        {
            "done": True,
            "status": "idle",
            "last": {"agentStatus": "idle", "agentActivityLive": False, "pendingApprovalCount": 0},
            "summary": "s",
            "baseline_summary": "s",
            "cursor_composer_id": "c-2",
            "result": {"implementation": {"files_changed": ["a.py"]}},
        },
        item=item,
    )
    assert signals["summary_equals_baseline"] is True
    assert signals["composer_id"] == "c-2" and signals["expected_composer_id"] == "c-1"
    assert signals["files_changed"] == ["a.py"]


@pytest.mark.asyncio
async def test_plan_like_summary_is_working_when_judge_says_so(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "completion-working.db")
    service = service_with("enforce")
    client = FakeStructuredClient([completion_reply("working", summary="")])
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.flush()
        item = WorkItem(agent_id=agent.id, project_id="p", title="Каталог", status="waiting_external", pm_phase="IN_DEVELOPMENT", acceptance_criteria=["Каталог в шапке"], metadata_json={"cursor_in_flight": True})
        db.add(item)
        await db.flush()
        run = CursorRun(work_item_id=item.id, project_id="p", attempt=1, idempotency_key="k1", status="running", request_json={"brief": "# Task"})
        db.add(run)
        await db.flush()
        item.active_cursor_run_id = run.id
        await db.commit()
        result = {"done": True, "status": "idle", "prompt_sent": True, "seen_busy": True, "started": True, "summary": "Запускаю dev-сервер, затем проверю вёрстку.", "last": {"agentStatus": "idle"}}
        applied = await _apply_pm_cursor_result(db, item, run, result, judgment=service, client=client)
        assert applied["done"] is False
        assert item.pm_phase == "IN_DEVELOPMENT"
        assert item.status == "waiting_external"
        assert run.status == "running"
    await engine.dispose()


@pytest.mark.asyncio
async def test_finished_prose_reaches_qa_under_judge(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "completion-finished.db")
    service = service_with("enforce")
    client = FakeStructuredClient([completion_reply("finished")])
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.flush()
        item = WorkItem(agent_id=agent.id, project_id="p", title="Каталог", status="waiting_external", pm_phase="IN_DEVELOPMENT", acceptance_criteria=["Каталог в шапке"], metadata_json={"cursor_in_flight": True})
        db.add(item)
        await db.flush()
        run = CursorRun(work_item_id=item.id, project_id="p", attempt=1, idempotency_key="k2", status="running", request_json={"brief": "# Task"})
        db.add(run)
        await db.flush()
        item.active_cursor_run_id = run.id
        await db.commit()
        # Idle without seen_busy would be "leftover" for the legacy flags; the judge says finished.
        result = {"done": True, "status": "idle", "prompt_sent": True, "summary": "Каталог в шапке готов. Открой / и проверь.", "last": {"agentStatus": "idle"}}
        applied = await _apply_pm_cursor_result(db, item, run, result, judgment=service, client=client)
        assert applied["done"] is True and applied["qa_required"] is True
        assert item.pm_phase == "QA"
        assert run.status == "completed"
        assert run.result_json["native_summary"] is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_foreign_result_is_treated_as_leftover(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "completion-foreign.db")
    service = service_with("enforce")
    client = FakeStructuredClient([completion_reply("foreign_result")])
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.flush()
        item = WorkItem(agent_id=agent.id, project_id="p", title="Каталог", status="waiting_external", pm_phase="IN_DEVELOPMENT", acceptance_criteria=["Каталог в шапке"], metadata_json={"cursor_in_flight": True})
        db.add(item)
        await db.flush()
        run = CursorRun(work_item_id=item.id, project_id="p", attempt=1, idempotency_key="k3", status="running", request_json={"brief": "# Task"})
        db.add(run)
        await db.flush()
        item.active_cursor_run_id = run.id
        await db.commit()
        result = {"done": True, "status": "idle", "prompt_sent": True, "seen_busy": True, "summary": "Отчёт по другой задаче: склады добавлены.", "last": {"agentStatus": "idle"}}
        applied = await _apply_pm_cursor_result(db, item, run, result, judgment=service, client=client)
        assert applied.get("leftover") is True or applied.get("done") is False
        assert item.pm_phase != "QA"
    await engine.dispose()


@pytest.mark.asyncio
async def test_shadow_completion_keeps_legacy_flags(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "completion-shadow.db")
    service = service_with("shadow")
    client = FakeStructuredClient([completion_reply("working")])
    item = WorkItem(id=1, agent_id=1, title="t", metadata_json={})
    async with sessions() as db:
        decision = await assess_completion(db, item, None, {"done": True, "seen_busy": True, "summary": "Готово."}, judgment=service, client=client)
        assert decision.judged is False and decision.state == "finished"
        assert decision.judgment is not None and decision.judgment.agreed is False
        assert apply_completion_to_result({"done": True}, decision) == {"done": True}
    await engine.dispose()
