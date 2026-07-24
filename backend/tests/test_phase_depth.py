from typing import Any
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.db import Agent, LlmProfile, RuntimeSettings
from app.integrations import MemoryStore
from app.runtime import AgentRuntime
from app.telegram import TelegramToolAdapter
from app.tools import DangerousActionError


class FakeTelegramClient:
    async def get_dialogs(self, limit: int = 10) -> list[Any]:
        return []

    async def send_message(self, entity: str, message: str) -> None:
        return None

    async def log_out(self) -> None:
        return None

    async def _private(self) -> None:
        return None


@pytest.mark.asyncio
async def test_reflective_telegram_filtering_and_danger_policy() -> None:
    schemas = TelegramToolAdapter.reflect_client(FakeTelegramClient)
    names = {schema["function"]["name"] for schema in schemas}
    assert "telegram_get_dialogs" in names
    assert "telegram_send_message" in names
    assert "telegram_log_out" not in names
    assert "telegram__private" not in names
    send_schema = next(schema for schema in schemas if schema["function"]["name"] == "telegram_send_message")
    assert send_schema["x-ice-classification"] == "danger"

    with pytest.raises(DangerousActionError):
        await TelegramToolAdapter.authorize("telegram_send_message")
    await TelegramToolAdapter.authorize("telegram_send_message", admin_confirmed=True)


@pytest.mark.asyncio
async def test_memory_fallback_namespace_and_lifecycle() -> None:
    memory = MemoryStore(Settings(mem0_enabled=False))
    first = await memory.add("alpha fact", "user-1", "agent-1", {"topic": "a"})
    await memory.add("alpha other agent", "user-1", "agent-2")
    await memory.add("alpha other user", "user-2", "agent-1")

    results = await memory.search("alpha", "user-1", "agent-1")
    assert [item["id"] for item in results] == [first["id"]]
    assert (await memory.get(first["id"]))["memory"] == "alpha fact"

    await memory.update(first["id"], "beta fact")
    assert (await memory.get(first["id"]))["memory"] == "beta fact"
    assert [event["event"] for event in await memory.history(first["id"])] == ["ADD", "UPDATE"]
    assert len(await memory.get_all("user-1")) == 2

    await memory.delete_all("user-1", "agent-1")
    assert await memory.get(first["id"]) is None
    assert len(await memory.get_all()) == 2
    await memory.reset()
    assert await memory.get_all() == []


def test_local_memory_uses_multilingual_fastembed() -> None:
    config = MemoryStore._local_mem0_config(
        SimpleNamespace(qdrant_url="http://qdrant:6333"),
        {
            "api_key": "secret",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
        },
    )

    assert config["embedder"]["provider"] == "fastembed"
    assert config["embedder"]["config"]["embedding_dims"] == 384
    assert config["vector_store"]["config"]["host"] == "qdrant"
    assert config["vector_store"]["config"]["embedding_model_dims"] == 384
    assert config["llm"]["config"]["model"] == "deepseek-v4-flash"


class FakeDB:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        return None

    async def get(self, model: type[Any], item_id: int) -> Any:
        if model is RuntimeSettings:
            return RuntimeSettings(id=1, memory_enabled=True, max_tool_rounds=8)
        if model is LlmProfile:
            from app.secrets import SecretStore

            return LlmProfile(
                id=item_id,
                name="test",
                provider="openai",
                default_model="profile-default",
                enabled=True,
                api_key_ciphertext=SecretStore.from_settings(Settings()).encrypt("test-key"),
            )
        return None

    async def scalars(self, statement: Any) -> list[Any]:
        return []


class FakeEvents:
    async def publish(self, event: str, payload: dict[str, Any]) -> None:
        return None


class FakeSearch:
    async def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return []


@pytest.mark.asyncio
async def test_runtime_injects_and_records_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class FakeLLM:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["llm_kwargs"] = kwargs

        async def complete(self, messages: list[dict[str, Any]], tools: Any, permissions: set[str]) -> str:
            captured["messages"] = messages
            return "remembered response"

    monkeypatch.setattr("app.runtime.LLMClient", FakeLLM)
    memory = MemoryStore(Settings(mem0_enabled=False))
    await memory.add("prefers concise replies", "user-7", "7")
    runtime = AgentRuntime(Settings(), memory, FakeSearch(), FakeEvents())
    agent = Agent(
        id=7,
        name="memory-agent",
        prompt="Be useful",
        model_provider="openai",
        model_name="test",
        llm_profile_id=1,
        enabled=True,
        config={},
    )

    result = await runtime.run(FakeDB(), agent, "Please give a concise answer", {"user_id": "user-7"})
    assert result == "remembered response"
    assert captured["llm_kwargs"]["model"] == "test"
    assert "prefers concise replies" in captured["messages"][1]["content"]
    exchanges = await memory.search("remembered response", "user-7", "7")
    assert exchanges[0]["metadata"]["kind"] == "exchange"
