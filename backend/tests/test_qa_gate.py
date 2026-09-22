from pathlib import Path

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Agent, AgentJudgment, Base, CursorRun, RuntimeSettings, WorkItem, WorkItemEvent
from app.judgment import JudgmentService
from app.qa_gate import DEFAULT_EVIDENCE_FIX_REQUEST, evaluate_qa, qa_payload
from app.runtime import _auto_accept_pm_qa


class FakeStructuredClient:
    def __init__(self, replies: list[dict]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.model = "fake-judge"

    async def structured(self, *, system, user, schema, schema_name, model=None, timeout=45.0):
        self.calls.append({"schema_name": schema_name, "user": json.loads(user)})
        return json.dumps(self.replies.pop(0), ensure_ascii=False), {
            "model": "fake-judge",
            "prompt_tokens": 10,
            "completion_tokens": 5,
        }


async def sessions_for(path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


CRITERIA = ["Пользователь может создать склад", "Остатки увеличиваются после прихода"]


def qa_reply(verdict: str, *, confidence: float = 0.95, fix: str = "", criteria: list[str] | None = None) -> dict:
    rows = []
    for text in CRITERIA:
        rows.append(
            {
                "criterion": text,
                "verdict": "pass" if (criteria is None or text in criteria) else "fail",
                "evidence": [{"source": "cursor_summary", "text": "Склады добавлены"}],
                "note": "",
            }
        )
    return {
        "verdict": verdict,
        "confidence": confidence,
        "evidence": [{"source": "cursor_summary", "text": "Склады добавлены"}],
        "missing": [],
        "reasoning": "checked",
        "criteria": rows,
        "fix_request": fix,
        "customer_summary": "Склады готовы, остатки считаются." if verdict == "accept" else "",
    }


async def make_case(db, *, structured_pass: bool, summary: str = "Модуль складов уже реализован и покрывает все требования."):
    agent = Agent(name="pm-qa")
    db.add(agent)
    await db.flush()
    item = WorkItem(
        agent_id=agent.id,
        project_id="mysell",
        title="Склады",
        goal="Учёт складов",
        status="in_progress",
        wait_owner="self",
        pm_phase="QA",
        acceptance_criteria=list(CRITERIA),
        metadata_json={"cursor_in_flight": False},
    )
    db.add(item)
    await db.flush()
    rows = [
        {"criterion": c, "passed": structured_pass, "evidence": "pytest" if structured_pass else ""}
        for c in CRITERIA
    ]
    run = CursorRun(
        work_item_id=item.id,
        project_id="mysell",
        attempt=1,
        idempotency_key=f"qa-{item.id}",
        status="completed",
        result_json={
            "task_id": str(item.id),
            "status": "completed",
            "implementation": {"summary": summary, "files_changed": ["a.py"], "tests": []},
            "verification": {
                "tests_passed": structured_pass,
                "lint_passed": structured_pass,
                "acceptance_criteria": rows,
            },
        },
    )
    db.add(run)
    await db.flush()
    item.active_cursor_run_id = run.id
    await db.commit()
    return item, run


def service_with(mode: str) -> JudgmentService:
    service = JudgmentService(settings=None)
    service.bind_runtime_settings(RuntimeSettings(id=1, judge_modes={"qa_verifier": mode}))
    return service


def test_qa_payload_exposes_only_report_and_task() -> None:
    item = WorkItem(id=5, agent_id=1, title="t", goal="g", acceptance_criteria=["a"])
    run = CursorRun(
        id=9,
        work_item_id=5,
        project_id="p",
        attempt=1,
        idempotency_key="k",
        status="completed",
        result_json={"implementation": {"summary": "done", "files_changed": ["x"]}, "native_summary": True},
    )
    payload = qa_payload(item, run)
    assert payload["task"]["acceptance_criteria"] == ["a"]
    assert payload["executor_report"]["native_summary_only"] is True
    assert payload["executor_report"]["files_changed"] == ["x"]


@pytest.mark.asyncio
async def test_prose_without_judge_does_not_auto_close(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "qa-prose.db")
    async with sessions() as db:
        item, run = await make_case(db, structured_pass=False)
        result = await _auto_accept_pm_qa(db, item)
        await db.commit()
        assert result is not None and result["fix_requested"] is True
        assert item.pm_phase == "CHANGES_REQUESTED"
        assert item.metadata_json.get("qa_fix_request") == DEFAULT_EVIDENCE_FIX_REQUEST
    await engine.dispose()


@pytest.mark.asyncio
async def test_shadow_mode_records_judge_but_legacy_decides(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "qa-shadow.db")
    service = service_with("shadow")
    client = FakeStructuredClient([qa_reply("fix_required", fix="Добавь тест на остатки", criteria=[CRITERIA[0]])])
    async with sessions() as db:
        item, run = await make_case(db, structured_pass=True)
        decision = await evaluate_qa(db, item, run, judgment=service, client=client)
        await db.commit()
        assert decision.accept is True
        assert decision.legacy is True
        assert decision.judgment is not None and decision.judgment.agreed is False
        rows = list(await db.scalars(select(AgentJudgment)))
        assert len(rows) == 1 and rows[0].kind == "qa_verifier" and rows[0].mode == "shadow"
        assert rows[0].legacy_json == {"verdict": "accept"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_enforce_accept_closes_case_with_customer_summary(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "qa-enforce.db")
    service = service_with("enforce")
    client = FakeStructuredClient([qa_reply("accept")])
    async with sessions() as db:
        item, run = await make_case(db, structured_pass=False)
        result = await _auto_accept_pm_qa(db, item, judgment=service, client=client)
        await db.commit()
        assert result is not None and result["deliver_origin"] is True
        assert result["result"] == "Склады готовы, остатки считаются."
        assert item.pm_phase == "DONE"
        events = list(await db.scalars(select(WorkItemEvent).where(WorkItemEvent.kind == "completed")))
        assert events and events[0].payload["qa"]["verdict"] == "accept"
    await engine.dispose()


@pytest.mark.asyncio
async def test_enforce_fix_required_requests_fix_with_judge_text(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "qa-fix.db")
    service = service_with("enforce")
    client = FakeStructuredClient(
        [qa_reply("fix_required", fix="Остатки не пересчитываются после прихода — добавь и докажи тестом", criteria=[CRITERIA[0]])]
    )
    async with sessions() as db:
        # Structured rows claim pass, but the judge sees the report does not prove criterion 2.
        item, run = await make_case(db, structured_pass=True)
        result = await _auto_accept_pm_qa(db, item, judgment=service, client=client)
        await db.commit()
        assert result is not None and result["fix_requested"] is True
        assert item.pm_phase == "CHANGES_REQUESTED"
        assert "Остатки не пересчитываются" in item.metadata_json["qa_fix_request"]
        assert "Остатки не пересчитываются" in item.next_action
    await engine.dispose()


@pytest.mark.asyncio
async def test_enforce_insufficient_evidence_requests_verification(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "qa-insufficient.db")
    service = service_with("enforce")
    client = FakeStructuredClient([qa_reply("insufficient_evidence", confidence=0.9, fix="")])
    async with sessions() as db:
        item, run = await make_case(db, structured_pass=True)
        decision = await evaluate_qa(db, item, run, judgment=service, client=client)
        assert decision.accept is False
        assert decision.should_request_fix is True
        assert decision.fix_request == DEFAULT_EVIDENCE_FIX_REQUEST
    await engine.dispose()


@pytest.mark.asyncio
async def test_enforce_low_confidence_holds_without_closing(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "qa-lowconf.db")
    service = service_with("enforce")
    client = FakeStructuredClient([qa_reply("accept", confidence=0.5)])
    async with sessions() as db:
        item, run = await make_case(db, structured_pass=True)
        result = await _auto_accept_pm_qa(db, item, judgment=service, client=client)
        await db.commit()
        assert result is None
        assert item.pm_phase == "QA"
        assert item.next_action.startswith("QA на паузе")
        assert item.metadata_json["qa_hold_marker"] == f"{run.id}:low_confidence"
        # second pass with the same verdict does not spam events
        client.replies = [qa_reply("accept", confidence=0.5)]
        await _auto_accept_pm_qa(db, item, judgment=service, client=client)
        events = list(await db.scalars(select(WorkItemEvent).where(WorkItemEvent.title == "QA: требуется проверка")))
        assert len(events) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_enforce_degraded_judge_fails_closed(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "qa-degraded.db")
    service = service_with("enforce")

    class Broken:
        model = "broken"

        async def structured(self, **kwargs):
            raise RuntimeError("timeout")

    async with sessions() as db:
        item, run = await make_case(db, structured_pass=True)
        result = await _auto_accept_pm_qa(db, item, judgment=service, client=Broken())
        await db.commit()
        assert result is None
        assert item.pm_phase == "QA"
        assert "QA judge unavailable" in item.next_action
    await engine.dispose()
