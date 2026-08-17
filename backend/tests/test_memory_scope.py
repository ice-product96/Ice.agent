import pytest

from app.db import Agent, ConversationState
from app.integrations import MemoryStore
from app.memory_scope import (
    MemoryScope,
    bind_conversation_from_config,
    build_memory_metadata,
    prefetch_memories,
    resolve_memory_scope,
)
from app.config import Settings


@pytest.mark.asyncio
async def test_search_scoped_prefers_project_and_includes_global() -> None:
    memory = MemoryStore(Settings(mem0_enabled=False))
    await memory.add(
        "Project Alpha deadline is Friday",
        "user-1",
        "1",
        {"project_id": "alpha", "category": "project"},
    )
    await memory.add(
        "Client prefers email updates",
        "user-1",
        "1",
        {"project_id": "beta", "category": "preference"},
    )
    await memory.add(
        "Always reply in Russian",
        "user-1",
        "1",
        {"category": "preference"},
    )

    scoped = await memory.search_scoped(
        "reply Russian",
        user_id="user-1",
        agent_id="1",
        project_id="alpha",
        include_global=True,
        limit=8,
    )
    texts = {item["memory"] for item in scoped}
    assert "Always reply in Russian" in texts
    assert "Client prefers email updates" not in texts

    project_hits = await memory.search_scoped(
        "deadline",
        user_id="user-1",
        agent_id="1",
        project_id="alpha",
        include_global=False,
        limit=8,
    )
    assert {item["memory"] for item in project_hits} == {"Project Alpha deadline is Friday"}


@pytest.mark.asyncio
async def test_search_scoped_without_global_is_project_only() -> None:
    memory = MemoryStore(Settings(mem0_enabled=False))
    await memory.add("Alpha fact", "user-1", "1", {"project_id": "alpha"})
    await memory.add("Global fact", "user-1", "1", {})

    scoped = await memory.search_scoped(
        "fact",
        user_id="user-1",
        agent_id="1",
        project_id="alpha",
        include_global=False,
        limit=8,
    )
    assert len(scoped) == 1
    assert scoped[0]["memory"] == "Alpha fact"


def test_bind_conversation_from_thread_and_chat_mappings() -> None:
    state = ConversationState(
        agent_id=1,
        account_id=1,
        chat_id="-1001",
        user_id="42",
        thread_id="77",
    )
    bind_conversation_from_config(
        state,
        {
            "memory": {
                "thread_projects": {"77": "project-topic"},
                "chat_projects": {"-1001": "project-chat"},
            }
        },
        {"chat_id": "-1001", "thread_id": "77"},
    )
    assert state.project_id == "project-topic"

    other = ConversationState(
        agent_id=1,
        account_id=1,
        chat_id="-1002",
        user_id="42",
    )
    bind_conversation_from_config(
        other,
        {"memory": {"chat_projects": {"-1002": "project-chat"}}},
        {"chat_id": "-1002"},
    )
    assert other.project_id == "project-chat"


def test_resolve_memory_scope_uses_conversation_state() -> None:
    agent = Agent(id=3, name="pm", config={})
    state = ConversationState(
        agent_id=3,
        account_id=1,
        chat_id="chat",
        user_id="user",
        thread_id="12",
        project_id="acme-site",
        customer_id="acme",
    )
    scope = resolve_memory_scope(
        {"source": "telegram", "chat_id": "chat", "sender_id": "user", "thread_id": "12"},
        agent,
        state=state,
    )
    assert scope == MemoryScope(
        user_id="user",
        agent_id="3",
        project_id="acme-site",
        customer_id="acme",
        chat_id="chat",
        thread_id="12",
    )


def test_build_memory_metadata_respects_global_scope() -> None:
    scope = MemoryScope(user_id="u", agent_id="1", project_id="alpha", customer_id="cust")
    project_meta = build_memory_metadata(scope, category="decision")
    global_meta = build_memory_metadata(scope, category="preference", global_scope=True)
    assert project_meta["project_id"] == "alpha"
    assert project_meta["category"] == "decision"
    assert "project_id" not in global_meta


@pytest.mark.asyncio
async def test_prefetch_memories_uses_scoped_search() -> None:
    memory = MemoryStore(Settings(mem0_enabled=False))
    await memory.add("Scoped note", "user-9", "9", {"project_id": "p1"})
    await memory.add("Other project", "user-9", "9", {"project_id": "p2"})
    scope = MemoryScope(user_id="user-9", agent_id="9", project_id="p1")
    hits = await prefetch_memories(memory, "note", scope, limit=5)
    assert len(hits) == 1
    assert hits[0]["memory"] == "Scoped note"
