from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.db import Agent, Base, WorkItem
from app.integrations import McpManager
from app.runtime import AgentRuntime


class FakeSearch:
    async def search(self, query: str) -> list[dict[str, str]]:
        return []


class FakeEvents:
    async def publish(self, event: str, payload: dict) -> None:
        return None


class FakeCursorSession:
    calls = 0

    async def list_tools(self):
        definition = SimpleNamespace(
            name="send_prompt",
            description="Raw Cursor prompt",
            inputSchema={
                "type": "object",
                "properties": {"prompt": {"type": "string"}},
                "required": ["prompt"],
            },
        )
        return SimpleNamespace(tools=[definition])

    async def call_tool(self, name: str, arguments: dict):
        self.calls += 1
        return SimpleNamespace(content=[])


def stub_idle_composer(monkeypatch) -> None:
    async def fake_check(*args, **kwargs):
        return {"done": True, "status": "idle"}

    async def fake_peek(*args, **kwargs):
        return {"ok": True, "busy": False, "agentStatus": "idle"}

    monkeypatch.setattr("app.cursorremote_drive.check_and_drive", fake_check)
    monkeypatch.setattr("app.cursorremote_drive.peek_composer", fake_peek)


@pytest.mark.asyncio
async def test_pm_registry_hides_raw_cursor_and_blocks_incomplete_task(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'pm-runtime.db').as_posix()}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    cursor = FakeCursorSession()
    mcp = McpManager()
    mcp.sessions = {"cursorremote": cursor}
    runtime = AgentRuntime(
        Settings(mem0_enabled=False),
        SimpleNamespace(),
        FakeSearch(),
        FakeEvents(),
        mcp=mcp,
    )

    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.flush()
        item = WorkItem(
            agent_id=agent.id,
            title="Unclear requirement",
            goal="Add cancellation",
            project_id="orders",
            task_type="feature",
            requirements=[],
            acceptance_criteria=[],
            priority="normal",
            pm_phase="CLARIFICATION",
        )
        db.add(item)
        await db.commit()
        runtime_context = {
            "_pm_mode": True,
            "work_item_id": item.id,
            "source": "telegram",
            "chat_id": "customer-1",
            "client_id": "customer-1",
            "message_id": "message-1",
        }
        registry = await runtime.registry(
            agent,
            mcp_server_names={"cursorremote"},
            memory_enabled=False,
            db=db,
            context=runtime_context,
        )
        assert "mcp_cursorremote_run" not in registry.tools
        assert "cursorremote_do" not in registry.tools
        assert {
            "pm_structure_task",
            "submit_development_task",
            "get_development_status",
            "get_development_result",
            "request_development_fix",
            "pm_accept_task",
        } <= set(registry.tools)
        with pytest.raises(ValueError, match="incomplete"):
            await registry.call("submit_development_task", {})
        assert cursor.calls == 0

        await registry.call(
            "pm_structure_task",
            {
                "project_id": "orders",
                "task_type": "feature",
                "title": "Clarify cancellation",
                "requirements": ["Cancel an open order"],
                "acceptance_criteria": ["Cancellation is persisted"],
            },
        )
        confirmed = await registry.call(
            "pm_transition_task",
            {"to_phase": "CLIENT_CONFIRMED", "detail": "Customer approved"},
        )
        assert confirmed["task"]["pm_phase"] == "CLIENT_CONFIRMED"
        runtime_context["message_id"] = "message-2"
        created = await registry.call(
            "pm_structure_task",
            {
                "project_id": "orders",
                "task_type": "feature",
                "title": "Add SMS notification",
                "requirements": ["Send an SMS after cancellation"],
                "acceptance_criteria": ["SMS fake records one delivery"],
                "create_new_task": True,
            },
        )
        assert created["duplicate"] is False
        assert created["task"]["id"] != item.id
        assert (
            await db.scalar(select(func.count()).select_from(WorkItem))
        ) == 2

        other_agent = Agent(name="other-pm")
        db.add(other_agent)
        await db.flush()
        other_item = WorkItem(
            agent_id=other_agent.id,
            title="Other task",
            project_id="orders",
        )
        db.add(other_item)
        await db.commit()
        with pytest.raises(PermissionError, match="same project"):
            await registry.call(
                "pm_record_decision",
                {
                    "project_id": "orders",
                    "topic": "Isolation",
                    "decision": "Must not cross agents",
                    "work_item_id": other_item.id,
                },
            )

    await engine.dispose()


@pytest.mark.asyncio
async def test_customer_decision_unlocks_development_without_manager(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def fake_send(*args, **kwargs):
        return {"done": False, "prompt_sent": True, "status": "working"}

    monkeypatch.setattr("app.cursorremote_drive.send_prompt_and_drive", fake_send)
    stub_idle_composer(monkeypatch)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'pm-customer-confirm.db').as_posix()}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    mcp = McpManager()
    mcp.sessions = {"cursorremote": FakeCursorSession()}
    runtime = AgentRuntime(
        Settings(mem0_enabled=False),
        SimpleNamespace(),
        FakeSearch(),
        FakeEvents(),
        mcp=mcp,
    )
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.flush()
        item = WorkItem(
            agent_id=agent.id,
            title="Carousel",
            goal="Fix carousel",
            project_id="lavve",
            task_type="feature",
            requirements=["Carousel scrolls"],
            acceptance_criteria=["Slides change on swipe"],
            pm_phase="DISCUSSION",
        )
        db.add(item)
        await db.commit()
        runtime_context = {
            "_pm_mode": True,
            "work_item_id": item.id,
            "source": "telegram",
            "chat_id": "customer-1",
            "client_id": "customer-1",
            "message_id": "m-1",
        }
        registry = await runtime.registry(
            agent,
            mcp_server_names={"cursorremote"},
            memory_enabled=False,
            db=db,
            context=runtime_context,
        )
        stored = await registry.call(
            "pm_structure_task",
            {
                "project_id": "lavve",
                "task_type": "feature",
                "title": "Fix carousel",
                "requirements": ["Carousel scrolls"],
                "acceptance_criteria": ["Slides change on swipe"],
            },
        )
        assert stored["task"]["pm_phase"] == "REQUIREMENTS_READY"
        runtime_context["source"] = "scheduled"
        runtime_context.pop("message_id", None)
        runtime_context.pop("client_id", None)
        with pytest.raises(PermissionError, match="customer confirmation"):
            await registry.call("submit_development_task", {})
        with pytest.raises(PermissionError, match="customer's confirmation"):
            await registry.call(
                "pm_transition_task",
                {"to_phase": "CLIENT_CONFIRMED", "detail": "tick"},
            )
        runtime_context["source"] = "telegram"
        runtime_context["client_id"] = "customer-1"
        runtime_context["message_id"] = "m-confirm"
        recorded = await registry.call(
            "pm_record_decision",
            {
                "project_id": "lavve",
                "topic": "Start development",
                "decision": "Customer asked to ship the carousel fix",
                "confirmed_by": "заказчик",
            },
        )
        assert recorded["task"]["pm_phase"] == "CLIENT_CONFIRMED"
        runtime_context["source"] = "scheduled"
        submitted = await registry.call("submit_development_task", {})
        assert submitted["status"] in {"in_progress", "running"} or submitted.get("done") is False
        await db.refresh(item)
        assert item.pm_phase == "IN_DEVELOPMENT"
    await engine.dispose()


@pytest.mark.asyncio
async def test_structured_cursor_result_requires_and_then_passes_qa(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def fake_send(*args, **kwargs):
        return {
            "done": True,
            "result": {
                "task_id": "1",
                "status": "completed",
                "implementation": {
                    "summary": "Implemented cancellation",
                    "files_changed": ["orders.py"],
                    "tests": ["test_cancel"],
                },
                "verification": {
                    "tests_passed": True,
                    "lint_passed": True,
                    "acceptance_criteria": [
                        {
                            "criterion": "Cancellation tests pass",
                            "passed": True,
                            "evidence": "test_cancel passed",
                        }
                    ],
                },
                "questions": [],
                "risks": [],
                "limitations": [],
            },
        }

    monkeypatch.setattr("app.cursorremote_drive.send_prompt_and_drive", fake_send)
    stub_idle_composer(monkeypatch)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'pm-cursor.db').as_posix()}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    mcp = McpManager()
    mcp.sessions = {"cursorremote": FakeCursorSession()}
    runtime = AgentRuntime(
        Settings(mem0_enabled=False),
        SimpleNamespace(),
        FakeSearch(),
        FakeEvents(),
        mcp=mcp,
    )

    async with sessions() as db:
        agent = Agent(name="pm-cursor")
        db.add(agent)
        await db.flush()
        item = WorkItem(
            agent_id=agent.id,
            title="Fix order cancellation",
            goal="Allow an order to be cancelled",
            project_id="orders",
            task_type="bug",
            requirements=["Cancel an open order"],
            acceptance_criteria=["Cancellation tests pass"],
            priority="normal",
            pm_phase="REQUIREMENTS_READY",
            context_json={"inside_agreed_scope": True, "small_fix": True},
        )
        db.add(item)
        await db.commit()
        registry = await runtime.registry(
            agent,
            mcp_server_names={"cursorremote"},
            memory_enabled=False,
            db=db,
            context={"_pm_mode": True, "work_item_id": item.id},
        )
        result = await registry.call("submit_development_task", {})
        assert result["status"] == "completed"
        assert result["qa_required"] is True
        assert item.pm_phase == "QA"
        assert item.status != "done"
        with pytest.raises(ValueError, match="controlled"):
            await registry.call("pm_transition_task", {"to_phase": "DONE"})

        accepted = await registry.call("pm_accept_task", {})
        assert accepted["ok"] is True
        assert item.pm_phase == "DONE"
        assert item.status == "done"

    await engine.dispose()


@pytest.mark.asyncio
async def test_new_tracker_card_is_not_duplicate_of_closed_case(tmp_path: Path) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'pm-tracker-dedupe.db').as_posix()}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    mcp = McpManager()
    runtime = AgentRuntime(
        Settings(mem0_enabled=False),
        SimpleNamespace(),
        FakeSearch(),
        FakeEvents(),
        mcp=mcp,
    )
    closed_card = "a75de1b6-c1af-4c06-a141-9b56505ff6a4"
    new_card = "7f157a72-3408-474e-a183-d1777638fc4c"
    project_uuid = "d82c8c0d-c1af-4c06-a141-9b56505ff6a4"
    async with sessions() as db:
        agent = Agent(name="max")
        db.add(agent)
        await db.flush()
        closed = WorkItem(
            agent_id=agent.id,
            title="мобильная версия — кривая вёрстка",
            goal="mobile layout",
            project_id="uraltrade",
            status="done",
            pm_phase="DONE",
            source="employee_tick",
            chat_id="customer-1",
            source_message_id="old-tg-msg",
            context_json={
                "tracker_task_id": closed_card,
                "tracker_project_id": project_uuid,
            },
            metadata_json={"pm": {"tracker_task_id": closed_card}},
        )
        db.add(closed)
        await db.commit()
        runtime_context = {
            "_pm_mode": True,
            "work_item_id": closed.id,
            "source": "employee_tick",
            "chat_id": "customer-1",
            "message_id": "old-tg-msg",
        }
        registry = await runtime.registry(
            agent,
            mcp_server_names=set(),
            memory_enabled=False,
            db=db,
            context=runtime_context,
        )
        created = await registry.call(
            "pm_structure_task",
            {
                "project_id": "uraltrade",
                "task_type": "bug",
                "title": "в верхнем меню перестал раскрывать каталог",
                "requirements": ["Каталог в шапке раскрывается"],
                "acceptance_criteria": ["Подменю каталога открывается"],
                "create_new_task": True,
                "context_json": {
                    "tracker_task_id": new_card,
                    "tracker_project_id": project_uuid,
                },
            },
        )
        assert created["duplicate"] is False
        assert created["task"]["id"] != closed.id
        assert created["task"]["context"]["tracker_task_id"] == new_card
        assert (
            await db.scalar(select(func.count()).select_from(WorkItem))
        ) == 2

    await engine.dispose()


@pytest.mark.asyncio
async def test_submit_sends_when_composer_is_idle_with_old_task_json(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sent = {"called": False}

    async def fake_check(*args, **kwargs):
        return {
            "done": True,
            "status": "idle",
            "seen_busy": True,
            "started": True,
            "result": {
                "task_id": "31",
                "status": "completed",
                "implementation": {"summary": "old"},
                "verification": {
                    "tests_passed": True,
                    "lint_passed": True,
                    "acceptance_criteria": [],
                },
            },
        }

    async def fake_send(*args, **kwargs):
        sent["called"] = True
        return {
            "done": False,
            "prompt_sent": True,
            "status": "working",
            "seen_busy": True,
        }

    async def fake_peek(*args, **kwargs):
        return {"ok": True, "busy": False, "agentStatus": "idle"}

    monkeypatch.setattr("app.cursorremote_drive.check_and_drive", fake_check)
    monkeypatch.setattr("app.cursorremote_drive.peek_composer", fake_peek)
    monkeypatch.setattr("app.cursorremote_drive.send_prompt_and_drive", fake_send)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'pm-idle-submit.db').as_posix()}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    mcp = McpManager()
    mcp.sessions = {"cursorremote": FakeCursorSession()}
    runtime = AgentRuntime(
        Settings(mem0_enabled=False),
        SimpleNamespace(),
        FakeSearch(),
        FakeEvents(),
        mcp=mcp,
    )
    async with sessions() as db:
        agent = Agent(name="pm")
        db.add(agent)
        await db.flush()
        item = WorkItem(
            agent_id=agent.id,
            title="Catalog menu",
            goal="Fix catalog dropdown",
            project_id="uraltrade",
            task_type="bug",
            requirements=["Open catalog in header"],
            acceptance_criteria=["Catalog expands on hover/tap"],
            priority="high",
            pm_phase="READY_FOR_DEV",
            context_json={"inside_agreed_scope": True, "small_fix": True},
        )
        db.add(item)
        await db.commit()
        registry = await runtime.registry(
            agent,
            mcp_server_names={"cursorremote"},
            memory_enabled=False,
            db=db,
            context={"_pm_mode": True, "work_item_id": item.id},
        )
        result = await registry.call("submit_development_task", {})
        assert sent["called"] is True
        assert result.get("status") != "cursor_busy"
        assert result.get("leftover") is not True
    await engine.dispose()
