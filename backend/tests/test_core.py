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


def test_admin_action_report_only_for_side_effects() -> None:
    from app.action_reports import format_admin_action_report, is_side_effect_tool

    assert is_side_effect_tool("web_search") is False
    assert is_side_effect_tool("telegram_get_history") is False
    assert is_side_effect_tool("telegram_get_chat_full") is False
    assert is_side_effect_tool("telegram_resolve_phone") is True
    assert is_side_effect_tool("memory_add") is False
    assert is_side_effect_tool("ice_tracker_move_card") is True
    assert (
        format_admin_action_report(
            agent_name="Sales",
            audit=[{"tool": "web_search", "status": "success", "result": []}],
        )
        is None
    )
    report = format_admin_action_report(
        agent_name="Sales",
        audit=[
            {"tool": "web_search", "status": "success", "result": []},
            {
                "tool": "ice_tracker_move_card",
                "status": "success",
                "arguments": {"card_id": 1, "column": "done"},
                "result": {"ok": True},
            },
        ],
        user_message="передвинь карточку",
        chat_id=42,
        sender_id=7,
        sender_username="client",
    )
    assert report is not None
    assert "Sales" in report
    assert "ice_tracker_move_card" in report
    assert "web_search" not in report
    assert "@client" in report


def test_normalize_contact_phone_and_tool_registration() -> None:
    from app.config import Settings
    from app.telegram import TelegramGateway, normalize_contact_phone

    assert normalize_contact_phone("+7 (900) 111-22-33") == "+79001112233"
    assert normalize_contact_phone("89001112233") == "+79001112233"
    assert normalize_contact_phone("0079001112233") == "+79001112233"
    with pytest.raises(ValueError):
        normalize_contact_phone("abc")

    gateway = TelegramGateway(Settings())
    names = set(gateway.tool_registry("+100").tools)
    assert "telegram_get_chat_full" in names
    assert "telegram_resolve_phone" in names



@pytest.mark.asyncio
async def test_web_search_tavily(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations import WebSearch

    search = WebSearch()
    search.configure(
        "tavily",
        tavily_api_key="tvly-test",
        tavily_http_proxy="http://proxy.example:8080",
    )

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "results": [
                    {
                        "title": "Ice School",
                        "url": "https://example.com/school",
                        "content": "Частная школа в Екатеринбурге",
                    }
                ]
            }

    class FakeClient:
        last_kwargs: dict = {}

        def __init__(self, *args: object, **kwargs: object) -> None:
            FakeClient.last_kwargs = kwargs

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, json: dict) -> FakeResponse:
            assert url == "https://api.tavily.com/search"
            assert json["api_key"] == "tvly-test"
            assert json["query"] == "школа екатеринбург"
            return FakeResponse()

    monkeypatch.setattr("app.integrations.httpx.AsyncClient", FakeClient)
    results = await search.search("школа екатеринбург", limit=3)
    assert FakeClient.last_kwargs.get("proxy") == "http://proxy.example:8080"
    assert results == [
        {
            "title": "Ice School",
            "url": "https://example.com/school",
            "content": "Частная школа в Екатеринбурге",
        }
    ]

    bare = WebSearch()
    bare.configure("tavily")
    with pytest.raises(RuntimeError, match="Tavily API key"):
        await bare.search("query")


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


@pytest.mark.asyncio
async def test_gpt56_chat_tools_set_reasoning_effort_none() -> None:
    from types import SimpleNamespace

    from app.integrations import LLMClient

    captured: dict[str, object] = {}

    class FakeCompletions:
        async def create(self, **kwargs: object) -> object:
            captured.update(kwargs)
            message = SimpleNamespace(
                content="ok",
                tool_calls=None,
                model_dump=lambda exclude_none=True: {"role": "assistant", "content": "ok"},
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = LLMClient(api_key="k", base_url=None, model="gpt-5.6-luna", max_rounds=1)
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    registry = ToolRegistry()
    registry.register(lambda q: "hit", "web_search")
    result = await client.complete([{"role": "user", "content": "hi"}], registry, set())
    assert result == "ok"
    assert captured["reasoning_effort"] == "none"
    assert captured["tools"]
