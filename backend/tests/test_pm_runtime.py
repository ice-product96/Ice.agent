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
