from datetime import datetime, timezone
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.conversation import ConversationContextService
from app.db import (
    Agent,
    Base,
    MessageLog,
    RuntimeSettings,
    TelegramAccount,
)
from app.schemas import RuntimeSettingsBody
from app.telegram import normalized_message
from conftest import DB_PATH


@pytest.fixture()
async def conversation_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        account = TelegramAccount(
            phone="+10000000000", name="test", session_path="test.session"
        )
        first = Agent(name="first")
        second = Agent(name="second")
        db.add_all([account, first, second])
        await db.commit()
        await db.refresh(account)
        await db.refresh(first)
        await db.refresh(second)
        yield db, account, first, second
    await engine.dispose()


def runtime_settings(**overrides: Any) -> RuntimeSettings:
    values = {
        "id": 1,
        "timezone": "America/New_York",
        "telegram_history_limit": 100,
        "recent_context_messages": 30,
        "context_max_chars": 30000,
        "summarization_enabled": True,
        "summarize_after_messages": 80,
    }
    return RuntimeSettings(**{**values, **overrides})


@pytest.mark.asyncio
async def test_conversations_are_namespaced_ordered_and_deduplicated(
    conversation_db: Any,
) -> None:
    db, account, first, second = conversation_db
    service = ConversationContextService()
    settings = runtime_settings()
    base = {
        "source": "telegram",
        "chat_id": "chat-a",
        "sender_id": "user-1",
        "message_id": "3",
        "message_at": "2026-07-23T12:00:00Z",
        "telegram_history": [
            {
                "id": 2,
                "date": "2026-07-23T11:00:00Z",
                "sender_id": "agent",
                "outgoing": True,
                "text": "second",
            },
            {
                "id": 1,
                "date": "2026-07-23T10:00:00Z",
                "sender_id": "user-1",
                "outgoing": False,
                "text": "first",
            },
            {
                "id": 3,
                "date": "2026-07-23T12:00:00Z",
                "sender_id": "user-1",
                "outgoing": False,
                "text": "current",
            },
        ],
    }
    prompt, state, _ = await service.prepare(
        db,
        agent_id=first.id,
        account_id=account.id,
        message="current",
        context=base,
        settings=settings,
        now=datetime(2026, 7, 23, 12, 5, tzinfo=timezone.utc),
    )
    await service.prepare(
        db,
        agent_id=first.id,
        account_id=account.id,
        message="current",
        context=base,
        settings=settings,
    )
    assert prompt.index("first") < prompt.index("second")
    assert "America/New_York" in prompt
    assert "Elapsed since previous message: 1h" in prompt
    assert "User sender=user-1: current" not in prompt
    assert (
        await db.scalar(
            select(func.count())
            .select_from(MessageLog)
            .where(MessageLog.message_id == "3")
        )
        == 1
    )

    other_user = {**base, "chat_id": "chat-b", "sender_id": "user-2", "message_id": "4"}
    _, other_user_state, _ = await service.prepare(
        db,
        agent_id=first.id,
        account_id=account.id,
        message="isolated",
        context=other_user,
        settings=settings,
    )
    _, other_agent_state, _ = await service.prepare(
        db,
        agent_id=second.id,
        account_id=account.id,
        message="isolated",
        context={**base, "message_id": "5"},
        settings=settings,
    )
    assert len({state.id, other_user_state.id, other_agent_state.id}) == 3


@pytest.mark.asyncio
async def test_long_history_is_summarized_and_bounded(conversation_db: Any) -> None:
    db, account, agent, _ = conversation_db
    service = ConversationContextService()
    calls: list[str] = []

    async def summarize(value: str) -> str:
        calls.append(value)
        return "Summary preserves Alice, delivery commitment, and 2026-07-23."

    history = [
        {
            "id": index,
            "date": f"2026-07-23T{index:02d}:00:00Z",
            "sender_id": "alice",
            "outgoing": index % 2 == 0,
            "text": f"message {index} " + ("x" * 250),
        }
        for index in range(1, 10)
    ]
    prompt, state, _ = await service.prepare(
        db,
        agent_id=agent.id,
        account_id=account.id,
        message="latest",
        context={
            "source": "telegram",
            "chat_id": "long",
            "sender_id": "alice",
            "message_id": "10",
            "message_at": "2026-07-23T10:00:00Z",
            "telegram_history": history,
        },
        settings=runtime_settings(
            context_max_chars=1200,
            recent_context_messages=2,
            summarize_after_messages=3,
        ),
        summarizer=summarize,
    )
    assert calls
    assert state.rolling_summary.startswith("Summary preserves Alice")
    assert state.summary_through_message_id is not None
    assert len(prompt) <= 1200


def test_telegram_history_normalization_and_timezone_validation() -> None:
    message = SimpleNamespace(
        id=7,
        date=datetime(2026, 7, 23, 9, 30, tzinfo=timezone.utc),
        sender_id=42,
        out=False,
        message="hello",
    )
    assert normalized_message(message) == {
        "id": 7,
        "date": "2026-07-23T09:30:00Z",
        "sender_id": 42,
        "outgoing": False,
        "text": "hello",
        "media": None,
    }
    with pytest.raises(ValueError):
        RuntimeSettingsBody(timezone="Not/A_Real_Zone")


def test_conversation_list_and_detail_api(
    client: TestClient, headers: dict[str, str]
) -> None:
    account = client.post(
        "/api/telegram-accounts",
        headers=headers,
        json={"phone": "+18880000000", "name": "conversation-api"},
    ).json()
    agent = client.post(
        "/api/agents",
        headers=headers,
        json={"name": "conversation-api-agent"},
    ).json()
    now = "2026-07-23 09:00:00+00:00"
    with sqlite3.connect(DB_PATH) as connection:
        cursor = connection.execute(
            """
            INSERT INTO conversation_states (
                agent_id, account_id, chat_id, user_id, rolling_summary,
                message_count, metadata_json, created_at, updated_at, last_message_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (agent["id"], account["id"], "api-chat", "api-user", "", 1, "{}", now, now, now),
        )
        conversation_id = cursor.lastrowid
        connection.execute(
            """
            INSERT INTO message_logs (
                created_at, agent_id, account_id, direction, chat_id, user_id,
                sender_id, message_id, message_at, text, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                agent["id"],
                account["id"],
                "in",
                "api-chat",
                "api-user",
                "api-user",
                "99",
                now,
                "timestamped message",
                "{}",
            ),
        )
    listing = client.get(
        f"/api/v1/conversations?agent_id={agent['id']}&search=api-user",
        headers=headers,
    )
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    detail = client.get(
        f"/api/v1/conversations/{conversation_id}", headers=headers
    )
    assert detail.status_code == 200
    assert detail.json()["messages"][0]["message_at"] is not None
