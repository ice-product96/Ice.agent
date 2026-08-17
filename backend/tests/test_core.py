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


def test_resolve_tool_permissions_grants_cursorremote() -> None:
    assert "mcp_cursorremote_send_prompt" not in resolve_tool_permissions(
        {"tools": ["mcp"], "tool_permissions": []}
    )
    by_flag = resolve_tool_permissions(
        {"tools": ["mcp"], "tool_permissions": ["cursorremote"]}
    )
    assert "mcp_cursorremote_send_prompt" in by_flag
    assert "mcp_cursorremote_approve" in by_flag
    by_attach = resolve_tool_permissions(
        {"tools": ["mcp"], "tool_permissions": []},
        cursorremote_attached=True,
    )
    assert "mcp_cursorremote_click_action" in by_attach
    policy = ToolPolicy()
    with pytest.raises(DangerousActionError):
        policy.check("mcp_cursorremote_send_prompt", {}, set())
    policy.check("mcp_cursorremote_send_prompt", {}, by_attach)
    policy.check("mcp_cursorremote_get_status", {}, set())


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
    assert format_admin_action_report(
        agent_name="Max",
        audit=[
            {
                "tool": "cursorremote_check",
                "status": "success",
                "result": {"ok": True, "done": True},
            },
            {
                "tool": "schedule_self",
                "status": "success",
                "arguments": {"run_at": "2026-08-17T14:13:30Z"},
                "result": {"ok": True},
            },
        ],
        source="scheduled",
        user_message="Повторно проверить CursorRemote",
    ) is None
    from app.action_reports import cursor_finished_in_audit

    assert cursor_finished_in_audit(
        [{"tool": "cursorremote_check", "status": "success", "result": {"done": True}}]
    )
    assert not cursor_finished_in_audit(
        [{"tool": "cursorremote_check", "status": "success", "result": {"done": False}}]
    )


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


def test_effective_tool_name_maps_mcp_gateway_calls() -> None:
    from app.tools import effective_tool_name

    assert effective_tool_name(
        "mcp_cursorremote_run",
        {"tool": "send_prompt", "arguments": {"text": "hi"}},
    ) == "mcp_cursorremote_send_prompt"
    assert effective_tool_name("telegram_send_message", {}) == "telegram_send_message"


def test_schemas_for_llm_respects_openai_limit() -> None:
    from app.tool_plane import attach_tool_plane, schemas_for_tool_plane

    registry = ToolRegistry()
    for index in range(140):
        registry.register(lambda i=index: i, f"tool_{index:03d}")
    attach_tool_plane(registry)
    schemas = schemas_for_tool_plane(registry, set(), limit=128)
    assert len(schemas) <= 128


def test_tool_plane_search_finds_catalog_tools() -> None:
    from app.tool_plane import attach_tool_plane, search_catalog, build_catalog

    registry = ToolRegistry()
    registry.register(lambda: None, "telegram_delete_messages", "Delete Telegram messages")
    registry.register(lambda: None, "telegram_send_message", "Send Telegram message")
    attach_tool_plane(registry)
    hits = search_catalog(build_catalog(registry), "delete telegram")
    names = {item["name"] for item in hits}
    assert "telegram_delete_messages" in names
    assert "telegram_send_message" not in names or "telegram_delete_messages" in names


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


def test_classify_telegram_photo_and_voice() -> None:
    from types import SimpleNamespace

    from app.telegram import attachment_label, classify_telegram_media, public_attachment

    photo = SimpleNamespace(
        media=object(),
        photo=object(),
        voice=None,
        audio=None,
        video=None,
        sticker=None,
        document=None,
        file=SimpleNamespace(mime_type="image/jpeg", name="p.jpg", ext=".jpg", size=12),
    )
    voice = SimpleNamespace(
        media=object(),
        photo=None,
        voice=object(),
        audio=None,
        video=None,
        sticker=None,
        document=object(),
        file=SimpleNamespace(mime_type="audio/ogg", name="voice.ogg", ext=".ogg", size=80),
    )
    assert classify_telegram_media(photo)["kind"] == "image"
    assert classify_telegram_media(voice)["kind"] == "voice"
    assert attachment_label([{"kind": "image"}, {"kind": "voice"}]) == (
        "[Вложение: изображение, голосовое сообщение]"
    )
    assert public_attachment({"kind": "image", "data_b64": "xx", "size": 2}) == {
        "kind": "image",
        "size": 2,
    }


def test_llm_user_content_attaches_image_and_wav() -> None:
    from app.integrations import llm_user_content

    content = llm_user_content(
        "смотри",
        [
            {
                "kind": "image",
                "mime_type": "image/jpeg",
                "data_b64": "abc",
                "filename": "a.jpg",
            },
            {
                "kind": "voice",
                "mime_type": "audio/ogg",
                "data_b64": "def",
                "filename": "voice.ogg",
            },
            {
                "kind": "audio",
                "mime_type": "audio/mpeg",
                "data_b64": "ghi",
                "filename": "clip.mp3",
            },
        ],
    )
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "смотри"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,abc")
    assert content[2] == {
        "type": "input_audio",
        "input_audio": {"data": "ghi", "format": "mp3"},
    }


@pytest.mark.asyncio
async def test_ingest_transcribes_voice() -> None:
    from app.integrations import ingest_attachments_for_llm

    class FakeClient:
        async def transcribe_audio(self, data: bytes, *, filename: str = "voice.ogg") -> str:
            assert filename == "voice.ogg"
            assert data == b"ogg"
            return "привет из голосового"

    text, attachments = await ingest_attachments_for_llm(
        FakeClient(),  # type: ignore[arg-type]
        "",
        [
            {
                "kind": "voice",
                "filename": "voice.ogg",
                "data_b64": "b2dn",
            }
        ],
    )
    assert "привет из голосового" in text
    assert attachments[0]["transcript"] == "привет из голосового"


@pytest.mark.asyncio
async def test_collect_telegram_attachments_downloads_bytes() -> None:
    from types import SimpleNamespace

    from app.telegram import collect_telegram_attachments

    class FakeMessage:
        def __init__(self) -> None:
            self.media = object()
            self.photo = object()
            self.voice = None
            self.audio = None
            self.video = None
            self.sticker = None
            self.document = None
            self.file = SimpleNamespace(
                mime_type="image/jpeg", name="p.jpg", ext=".jpg", size=4
            )

    class FakeEvent:
        async def download_media(self, message: object, file: object = None) -> bytes:
            return b"jpeg"

        client = None

    event = FakeEvent()
    event.client = event
    attachments = await collect_telegram_attachments(event, [FakeMessage()])
    assert attachments[0]["kind"] == "image"
    assert attachments[0]["data_b64"]
