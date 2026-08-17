import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Agent, AgentLink, AgentTask, Base, TelegramAccount
from app.integrations import McpManager
from app.routing import ADMIN_ACK_TEXT, TelegramEventRouter
from app.runtime import TaskBus
from app.tools import ToolRegistry


class RecordingEvents:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, event: str, payload: dict[str, Any]) -> None:
        self.items.append((event, payload))


class WorkerRuntime:
    async def run(
        self,
        db: Any,
        agent: Agent,
        message: str,
        context: dict[str, Any],
    ) -> str:
        if message == "fail":
            raise RuntimeError("planned failure")
        return f"done:{message}"


async def sessions_for(path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_task_bus_processes_success_and_failure(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "tasks.db")
    events = RecordingEvents()
    async with sessions() as db:
        source = Agent(name="worker-source")
        target = Agent(name="worker-target")
        db.add_all([source, target])
        await db.flush()
        db.add(AgentLink(
            source_agent_id=source.id,
            target_agent_id=target.id,
            can_delegate=True,
            can_message=True,
        ))
        restored = AgentTask(
            source_agent_id=source.id,
            target_agent_id=target.id,
            status="running",
            input={"message": "restored"},
        )
        db.add(restored)
        await db.commit()
        source_id, target_id, restored_id = source.id, target.id, restored.id

    bus = TaskBus(sessions, events)
    bus.bind_runtime(WorkerRuntime())
    await bus.start()
    completed = await bus.delegate(source_id, target_id, {"message": "work"})
    failed = await bus.delegate(source_id, target_id, {"message": "fail"})
    await asyncio.wait_for(bus.queue.join(), timeout=2)

    async with sessions() as db:
        completed_row = await db.get(AgentTask, completed.id)
        failed_row = await db.get(AgentTask, failed.id)
        restored_row = await db.get(AgentTask, restored_id)
        assert completed_row.status == "completed"
        assert completed_row.output == {"response": "done:work"}
        assert failed_row.status == "failed"
        assert failed_row.error == "planned failure"
        assert restored_row.status == "completed"
        assert restored_row.output == {"response": "done:restored"}
    names = [name for name, _ in events.items]
    assert "task.running" in names
    assert "task.completed" in names
    assert "task.failed" in names
    await bus.stop()
    await engine.dispose()


class RoutingRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, dict[str, Any]]] = []
        self.suppress_reply = False

    async def run(
        self,
        db: Any,
        agent: Agent,
        message: str,
        context: dict[str, Any],
    ) -> str:
        self.calls.append((agent.id, message, context))
        if self.suppress_reply:
            context["_suppress_telegram_reply"] = True
            context["_suppress_telegram_reason"] = "test"
        return "routed reply"

    async def update_telegram_outbound(self, db: Any, context: dict[str, Any], sent: Any) -> None:
        return None


class RoutingTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[Any, ...]] = []

    async def send_message(
        self,
        phone: str,
        entity: str | int,
        text: str,
        reply_to: int | None = None,
        *,
        humanize: bool = True,
    ) -> None:
        self.sent.append((phone, entity, text, reply_to, humanize))


@pytest.mark.asyncio
async def test_telegram_router_routes_and_prevents_loops(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "routing.db")
    async with sessions() as db:
        account = TelegramAccount(
            phone="+100000",
            name="routing",
            session_path="routing.session",
            authorized=True,
        )
        db.add(account)
        await db.flush()
        agent = Agent(name="routing-agent", telegram_account_id=account.id)
        db.add(agent)
        await db.commit()

    runtime = RoutingRuntime()
    telegram = RoutingTelegram()
    events = RecordingEvents()
    router = TelegramEventRouter(sessions, runtime, telegram, events)
    payload = {
        "phone": "+100000",
        "sender_id": 10,
        "chat_id": 20,
        "message_id": 30,
        "text": "hello",
        "outgoing": False,
        "service": False,
        "is_admin": False,
    }
    await router.new_message(payload)
    await router.new_message(payload)
    assert len(runtime.calls) == 1
    assert runtime.calls[0][2]["sender_id"] == 10
    assert telegram.sent == [("+100000", 20, "routed reply", 30, True)]

    await router.new_message({**payload, "message_id": 31, "text": "/system reboot"})
    assert len(runtime.calls) == 1
    await router.new_message({
        **payload,
        "message_id": 32,
        "text": "/admin status",
        "is_admin": True,
    })
    assert len(runtime.calls) == 2
    assert runtime.calls[-1][2]["admin_command"] is True
    assert telegram.sent[-2] == ("+100000", 20, ADMIN_ACK_TEXT, 32, False)
    assert telegram.sent[-1] == ("+100000", 20, "routed reply", 32, True)

    await router.new_message({**payload, "message_id": 33, "outgoing": True})
    assert len(runtime.calls) == 2
    await router.new_message({
        **payload,
        "message_id": 34,
        "sender_is_bot": True,
    })
    assert len(runtime.calls) == 2
    assert len(telegram.sent) == 3

    runtime.suppress_reply = True
    await router.new_message({**payload, "message_id": 35})
    assert len(runtime.calls) == 3
    assert len(telegram.sent) == 3
    assert "telegram.reply_suppressed" in [name for name, _ in events.items]
    await engine.dispose()


@pytest.mark.asyncio
async def test_mcp_registration_filters_attached_servers() -> None:
    class Session:
        async def list_tools(self) -> Any:
            definition = SimpleNamespace(
                name="lookup",
                description="Lookup data",
                inputSchema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            )
            return SimpleNamespace(tools=[definition])

    manager = McpManager()
    manager.sessions = {"attached": Session(), "detached": Session()}
    registry = ToolRegistry()
    await manager.register_tools(registry, {"attached"})
    assert set(registry.tools) == {"mcp_attached_tools", "mcp_attached_run"}
    schema = registry.tools["mcp_attached_run"].schema()["function"]
    assert schema["parameters"]["required"] == ["tool"]


@pytest.mark.asyncio
async def test_mcp_registration_uses_gateway_for_large_servers() -> None:
    class Session:
        async def list_tools(self) -> Any:
            tools = [
                SimpleNamespace(
                    name=f"tool_{index}",
                    description=f"Tool {index}",
                    inputSchema={"type": "object", "properties": {}},
                )
                for index in range(9)
            ]
            return SimpleNamespace(tools=tools)

    manager = McpManager()
    manager.sessions = {"heavy": Session()}
    registry = ToolRegistry()
    await manager.register_tools(registry, {"heavy"})
    assert set(registry.tools) == {"mcp_heavy_tools", "mcp_heavy_run"}
