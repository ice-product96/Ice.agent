import pytest
from fastapi.testclient import TestClient

from app.tools import DangerousActionError, ToolPolicy, ToolRegistry, resolve_tool_permissions


def test_auth_and_agent_crud(client: TestClient, headers: dict[str, str]) -> None:
    denied = client.get("/api/agents")
    assert denied.status_code == 401

    created = client.post("/api/agents", headers=headers, json={"name": "planner", "prompt": "Plan tasks"})
    assert created.status_code == 201
    agent_id = created.json()["id"]

    listed = client.get("/api/agents", headers=headers)
    assert listed.status_code == 200
    assert any(item["name"] == "planner" for item in listed.json())

    updated = client.patch(f"/api/agents/{agent_id}", headers=headers, json={"enabled": False})
    assert updated.json()["enabled"] is False

    assert client.delete(f"/api/agents/{agent_id}", headers=headers).status_code == 204


def test_agent_link_enforces_delegation(client: TestClient, headers: dict[str, str]) -> None:
    first = client.post("/api/agents", headers=headers, json={"name": "source"}).json()
    second = client.post("/api/agents", headers=headers, json={"name": "target"}).json()

    denied = client.post(
        "/api/tasks/delegate",
        headers=headers,
        json={"source_agent_id": first["id"], "target_agent_id": second["id"], "input": {"work": "x"}},
    )
    assert denied.status_code == 403

    link = client.post(
        "/api/agent-links",
        headers=headers,
        json={"source_agent_id": first["id"], "target_agent_id": second["id"], "can_delegate": True},
    )
    assert link.status_code == 201
    allowed = client.post(
        "/api/tasks/delegate",
        headers=headers,
        json={"source_agent_id": first["id"], "target_agent_id": second["id"], "input": {"work": "x"}},
    )
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "queued"


@pytest.mark.asyncio
async def test_tool_policy_blocks_dangerous_calls() -> None:
    registry = ToolRegistry(ToolPolicy())
    registry.register(lambda chat, text: "sent", "send_message")
    with pytest.raises(DangerousActionError):
        await registry.call("send_message", {"chat": "1", "text": "hello"})
    assert await registry.call(
        "send_message",
        {"chat": "1", "text": "hello"},
        {"send_message"},
    ) == "sent"


def test_resolve_tool_permissions_grants_telegram_ops() -> None:
    granted = resolve_tool_permissions({"tools": ["telegram"], "tool_permissions": []})
    assert "telegram_join_channel" in granted
    assert "telegram_send_message" in granted
    assert "telegram_delete_dialog" not in granted
    with_explicit = resolve_tool_permissions(
        {"tools": ["telegram"], "tool_permissions": ["telegram_leave_channel"]}
    )
    assert "telegram_leave_channel" in with_explicit
    assert "telegram_join_channel" in with_explicit
    assert resolve_tool_permissions({"tools": ["memory"], "tool_permissions": []}) == set()


def test_reflective_tool_schema() -> None:
    def add(a: int, b: int = 1) -> int:
        """Add two integers."""
        return a + b

    registry = ToolRegistry()
    registry.register(add)
    schema = registry.schemas()[0]["function"]
    assert schema["name"] == "add"
    assert schema["parameters"]["properties"]["a"]["type"] == "integer"
    assert schema["parameters"]["required"] == ["a"]
