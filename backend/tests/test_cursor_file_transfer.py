import pytest

from app.cursor_assets import asset_access_token, asset_download_url, verify_asset_access_token
from app.cursor_file_transfer import build_customer_files_prompt, deliver_customer_files_to_cursor


class FakeSession:
    def __init__(self, tools: list[str] | None = None) -> None:
        self.tools = tools or []
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self):
        class _Tool:
            def __init__(self, name: str) -> None:
                self.name = name

        class _Result:
            tools = [_Tool(name) for name in self.tools]

        return _Result()

    async def call_tool(self, tool: str, arguments: dict | None = None):
        self.calls.append((tool, dict(arguments or {})))
        class _Item:
            def model_dump(self):
                return {"type": "text", "text": '{"ok": true}'}

        class _Resp:
            content = [_Item()]
            isError = False

        return _Resp()


@pytest.mark.asyncio
async def test_deliver_uses_mcp_write_when_available() -> None:
    session = FakeSession(["write_workspace_file"])
    png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/"
        "x8AAwMCAO+ip1sAAAAASUVORK5CYII="
    )
    delivery = await deliver_customer_files_to_cursor(
        session,
        [{"kind": "image", "filename": "hero.png", "mime_type": "image/png", "data_b64": png}],
        work_item_id=7,
        public_base_url="http://agent.local:8000",
        secret_key="secret",
    )
    assert delivery["method"] == "mcp_write"
    assert delivery["paths"]
    assert session.calls[0][0] == "write_workspace_file"


@pytest.mark.asyncio
async def test_deliver_falls_back_to_download_url_without_write_tool() -> None:
    session = FakeSession([])
    png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/"
        "x8AAwMCAO+ip1sAAAAASUVORK5CYII="
    )
    delivery = await deliver_customer_files_to_cursor(
        session,
        [{"kind": "image", "filename": "hero.png", "mime_type": "image/png", "data_b64": png, "digest": "abc123"}],
        work_item_id=7,
        public_base_url="http://192.168.1.10:8000",
        secret_key="secret",
    )
    assert delivery["download_steps"]
    assert "192.168.1.10:8000" in delivery["download_steps"][0]
    prompt = build_customer_files_prompt("task", workspace_paths=delivery["paths"], download_steps=delivery["download_steps"])
    assert "curl" in prompt


def test_asset_token_roundtrip() -> None:
    token = asset_access_token(12, "deadbeef", "secret")
    assert verify_asset_access_token(12, "deadbeef", token, "secret")
    assert not verify_asset_access_token(12, "deadbeef", token, "wrong")


def test_asset_download_url_builds_signed_link() -> None:
    url = asset_download_url(
        12,
        "deadbeef",
        {"filename": "photo.jpg"},
        public_base_url="http://agent:8000",
        secret_key="secret",
    )
    assert url is not None
    assert "/api/v1/work-assets/12/deadbeef/photo.jpg" in url
    assert "token=" in url
