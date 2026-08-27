from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Agent, Base, CursorRun, DecisionRecord, WorkItem, WorkItemEvent
from app.pm_state import (
    InvalidPhaseTransition,
    autonomy_gate,
    development_is_client_confirmed,
    get_or_create_cursor_run,
    get_or_create_project_state,
    is_client_confirmer,
    is_task_ready,
    item_has_client_confirmation,
    parse_cursor_result,
    readiness_issues,
    record_decision,
    record_scope_change,
    render_task_brief,
    submission_requires_approval,
    transition_pm_phase,
)


async def sessions_for(path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def make_item(db, **overrides) -> WorkItem:
    agent = Agent(name=f"pm-{overrides.get('project_id', 'default')}")
    db.add(agent)
    await db.flush()
    values = {
        "agent_id": agent.id,
        "title": "Persist PM state",
        "goal": "Store deterministic PM state",
        "project_id": "ice",
        "task_type": "technical",
        "requirements": ["Save phase and execution state."],
        "acceptance_criteria": ["State survives a new database session."],
    }
    values.update(overrides)
    item = WorkItem(**values)
    db.add(item)
    await db.flush()
    return item


@pytest.mark.asyncio
async def test_transition_writes_event_without_changing_operational_status(
    tmp_path: Path,
) -> None:
    engine, sessions = await sessions_for(tmp_path / "transition.db")
    async with sessions() as db:
        item = await make_item(db, status="waiting_external")
        event = await transition_pm_phase(
            db, item, "REQUIREMENTS_READY", detail="Requirements known"
        )
        await db.commit()

        assert item.pm_phase == "REQUIREMENTS_READY"
        assert item.status == "waiting_external"
        assert event.kind == "pm_phase"
        assert event.payload == {
            "from_phase": "DISCUSSION",
            "to_phase": "REQUIREMENTS_READY",
        }
        assert await db.scalar(select(func.count()).select_from(WorkItemEvent)) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_invalid_transition_does_not_write_event(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "invalid.db")
    async with sessions() as db:
        item = await make_item(db)
        with pytest.raises(InvalidPhaseTransition):
            await transition_pm_phase(db, item, "DONE")
        assert item.pm_phase == "DISCUSSION"
        assert await db.scalar(select(func.count()).select_from(WorkItemEvent)) == 0
    await engine.dispose()


def test_task_readiness_requires_brief_fields() -> None:
    item = WorkItem(
        agent_id=1,
        goal="",
        task_type="technical",
        priority="normal",
        requirements=[],
        acceptance_criteria=["Done when tests pass."],
    )
    assert not is_task_ready(item)
    assert readiness_issues(item) == ["Missing goal", "Missing requirements"]

    item.goal = "Implement persistence"
    item.requirements = ["Persist all PM records."]
    assert is_task_ready(item)


def test_project_autonomy_submission_rules() -> None:
    common = {
        "task_type": "bug",
        "client_confirmed": False,
        "inside_agreed_scope": True,
        "small_fix": True,
    }
    assert submission_requires_approval("LEVEL_0", **common)
    assert not submission_requires_approval("LEVEL_1", **common)
    assert submission_requires_approval(
        "LEVEL_1",
        task_type="feature",
        client_confirmed=False,
        inside_agreed_scope=True,
        small_fix=False,
    )
    assert not submission_requires_approval(
        "LEVEL_2",
        task_type="feature",
        client_confirmed=False,
        inside_agreed_scope=True,
        small_fix=False,
    )
    assert submission_requires_approval("LEVEL_3", high_risk=True, **common)


def test_client_confirmation_is_not_manager_approval() -> None:
    assert is_client_confirmer("заказчик")
    assert is_client_confirmer("7868511513")
    assert is_client_confirmer("", source_message_id="tg-88")
    assert not is_client_confirmer("")
    assert not is_client_confirmer("manager")
    assert not is_client_confirmer("руководитель")
    item = WorkItem(agent_id=1, title="x", pm_phase="REQUIREMENTS_READY")
    assert not development_is_client_confirmed(item)
    assert development_is_client_confirmed(item, has_client_decision=True)
    item.pm_phase = "CLIENT_CONFIRMED"
    assert development_is_client_confirmed(item)


@pytest.mark.asyncio
async def test_stored_customer_decision_counts_as_confirmation(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "client-confirm.db")
    async with sessions() as db:
        item = await make_item(
            db,
            project_id="lavve",
            task_type="feature",
            pm_phase="REQUIREMENTS_READY",
            goal="Fix carousel",
            requirements=["Carousel scrolls"],
            acceptance_criteria=["Slides change on swipe"],
        )
        assert not await item_has_client_confirmation(db, item)
        await record_decision(
            db,
            project_id="lavve",
            topic="Start the carousel fix",
            decision="Customer asked to ship it",
            confirmed_by="заказчик",
            source_message_id="m-1",
            work_item_id=item.id,
        )
        await db.commit()
        assert await item_has_client_confirmation(db, item)
    await engine.dispose()


@pytest.mark.asyncio
async def test_project_state_defaults_to_level_one(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "project.db")
    async with sessions() as db:
        state = await get_or_create_project_state(db, "ice")
        same_state = await get_or_create_project_state(db, "ice", autonomy_level="LEVEL_3")
        assert state.project_id == "ice"
        assert state is same_state
        assert state.autonomy_level == "LEVEL_1"
        assert state.config == {}
        assert autonomy_gate(state.autonomy_level, "plan")
        assert autonomy_gate(state.autonomy_level, "small_bug_fix")
        assert not autonomy_gate(state.autonomy_level, "agreed_scope")
    await engine.dispose()


@pytest.mark.asyncio
async def test_decision_record_is_deterministic_and_persistent(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "decision.db")
    async with sessions() as db:
        item = await make_item(db)
        first = await record_decision(
            db,
            project_id="ice",
            topic="Architecture",
            work_item_id=item.id,
            decision="Use WorkItemEvent for transitions",
            rationale="It is the existing audit timeline.",
            context={"phase": "REQUIREMENTS_READY"},
        )
        second = await record_decision(
            db,
            project_id="ice",
            topic="Architecture",
            work_item_id=item.id,
            decision="Use WorkItemEvent for transitions",
            rationale="It is the existing audit timeline.",
            context={"phase": "REQUIREMENTS_READY"},
        )
        await db.commit()
        assert first.id == second.id
        assert len(first.decision_key) == 64
        assert await db.scalar(select(func.count()).select_from(DecisionRecord)) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_task_and_decision_survive_session_reopen(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "reopen.db")
    async with sessions() as db:
        item = await make_item(db, pm_phase="REQUIREMENTS_READY")
        decision = await record_decision(
            db,
            project_id="ice",
            topic="Scope",
            decision="Cancellation applies only to open orders",
            confirmed_by="client",
            work_item_id=item.id,
        )
        await db.commit()
        item_id, decision_id = item.id, decision.id
    async with sessions() as reopened:
        stored_item = await reopened.get(WorkItem, item_id)
        stored_decision = await reopened.get(DecisionRecord, decision_id)
        assert stored_item is not None
        assert stored_item.pm_phase == "REQUIREMENTS_READY"
        assert stored_decision is not None
        assert stored_decision.confirmed_by == "client"
    await engine.dispose()


@pytest.mark.asyncio
async def test_cursor_run_idempotency(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "cursor.db")
    async with sessions() as db:
        item = await make_item(db)
        first, first_created = await get_or_create_cursor_run(
            db, item, attempt=1, request={"brief": "build"}
        )
        second, second_created = await get_or_create_cursor_run(
            db, item, attempt=1, request={"brief": "build"}
        )
        await db.commit()

        assert first_created is True
        assert second_created is False
        assert first.id == second.id == item.active_cursor_run_id
        assert await db.scalar(select(func.count()).select_from(CursorRun)) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_source_message_cannot_create_two_tasks(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "message-id.db")
    async with sessions() as db:
        first = await make_item(
            db,
            source="telegram",
            chat_id="customer-1",
            source_message_id="message-1",
        )
        await db.commit()
        duplicate = WorkItem(
            agent_id=first.agent_id,
            title="Duplicate",
            source="telegram",
            chat_id="customer-1",
            source_message_id="message-1",
        )
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_new_scope_is_recorded_separately(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "scope.db")
    async with sessions() as db:
        item = await make_item(db, pm_phase="CLIENT_CONFIRMED")
        event = await record_scope_change(
            db,
            item,
            detail="А ещё добавьте SMS",
            source_message_id="message-sms",
        )
        await transition_pm_phase(db, item, "CHANGES_REQUESTED", detail=event.detail)
        await db.commit()
        assert item.pm_phase == "CHANGES_REQUESTED"
        assert event.kind == "scope_change"
        assert event.payload["source_message_id"] == "message-sms"
    await engine.dispose()


def test_brief_and_result_parsing_are_canonical() -> None:
    item = WorkItem(
        id=7,
        agent_id=1,
        title="PM persistence",
        goal="Persist state",
        project_id="ice",
        task_type="technical",
        priority="normal",
        requirements=["Reuse WorkItem."],
        acceptance_criteria=["Focused tests pass."],
        constraints=["Do not select an LLM."],
        edge_cases=["Retry the same Cursor request."],
        context_json={"branch": "main"},
    )
    brief = render_task_brief(item)
    assert "# Task: PM persistence" in brief
    assert "## Acceptance criteria" in brief
    assert '"branch": "main"' in brief
    assert "task_id:** 7" in brief
    assert "ice_tracker separately" in brief

    trackerish = WorkItem(
        id=24,
        agent_id=1,
        title="UI fix",
        goal="Align blocks",
        project_id="d82c8c0d-c1af-4c06-a141-9b56505ff6a4",
        task_type="change",
        priority="normal",
        requirements=["Align homepage blocks."],
        acceptance_criteria=["Blocks aligned."],
        context_json={
            "branch": "main",
            "tracker_project_id": "d82c8c0d-c1af-4c06-a141-9b56505ff6a4",
            "board_id": "board-1",
        },
    )
    tracker_brief = render_task_brief(trackerish)
    assert "**Project:** unspecified" in tracker_brief
    assert "tracker_project_id" not in tracker_brief
    assert "board_id" not in tracker_brief
    assert '"branch": "main"' in tracker_brief

    parsed = parse_cursor_result(
        """```json
        {
          "status": "completed",
          "implementation": {
            "summary": "done",
            "files_changed": ["app/db.py"],
            "tests": ["pytest"]
          },
          "verification": {
            "tests_passed": true,
            "lint_passed": true,
            "acceptance_criteria": [
              {
                "criterion": "Focused tests pass.",
                "passed": true,
                "evidence": "pytest passed"
              }
            ]
          }
        }
        ```"""
    )
    assert parsed["status"] == "completed"
    assert parsed["implementation"]["summary"] == "done"
    assert parsed["verification"]["tests_passed"] is True
    with pytest.raises(ValueError):
        parse_cursor_result("plain summary")

    embedded = parse_cursor_result(
        'Готово.\n{"status":"blocked","implementation":{"summary":"need ssh"},'
        '"verification":{},"questions":["give key"],"risks":[],"limitations":[]}\n'
    )
    assert embedded["status"] == "blocked"
    assert embedded["implementation"]["summary"] == "need ssh"

    from app.pm_state import is_leftover_cursor_idle, recover_truncated_cursor_result

    assert is_leftover_cursor_idle({"done": True, "skipped_prompt": True})
    assert is_leftover_cursor_idle({"done": True})
    assert not is_leftover_cursor_idle({"done": True, "prompt_sent": True})
    assert not is_leftover_cursor_idle({"done": True, "seen_busy": True})
    assert not is_leftover_cursor_idle({"done": True, "started": True})
    assert not is_leftover_cursor_idle(
        {"done": True, "skipped_prompt": True, "started": True}
    )
    assert not is_leftover_cursor_idle({"done": False})

    truncated = (
        '{  "task_id": 30,  "status": "completed",  "implementation": {    '
        '"summary": "Семь правок UI уже в master",    "files_changed": ["a.tsx"], '
        '"tests": ["tsc"]  },  "verification": {    "tests_passed": true, '
        '"lint_passed": true,    "acceptance_criteria": [      {        '
        '"criterion": "Название перенесено",        "passed": true,        '
        '"evidence": "product-card'
    )
    recovered = recover_truncated_cursor_result(
        truncated,
        expected_task_id="30",
        acceptance_criteria=["Название перенесено"],
    )
    assert recovered is not None
    assert recovered["status"] == "completed"
    assert recovered["task_id"] == "30"
    assert "master" in recovered["implementation"]["summary"]
    assert recovered["verification"]["acceptance_criteria"][0]["passed"] is True
    assert recover_truncated_cursor_result(truncated, expected_task_id="99") is None
