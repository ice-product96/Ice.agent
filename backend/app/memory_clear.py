"""Clear semantic memory, SIP call journal, and conversation transcripts."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import ConversationState, MessageLog, SipCall

ACTIVE_SIP_STATUSES = {"initiated", "dialing", "ringing", "answered", "early"}


async def clear_agent_memory(memory: Any, agent_id: str) -> dict[str, int]:
    """Remove all memory records scoped to one agent across every user."""
    agent_id = str(agent_id)
    scope = memory._scope(None, agent_id)
    before = len(await memory.get_all(agent_id=agent_id))

    client = memory._client
    if client:
        try:
            await asyncio.to_thread(client.delete_all, **scope)
        except Exception:
            for item in await memory.get_all(agent_id=agent_id):
                memory_id = item.get("id")
                if memory_id:
                    try:
                        await asyncio.to_thread(client.delete, str(memory_id))
                    except Exception:
                        pass

    for memory_id, item in list(memory._fallback.items()):
        if memory._matches(item, scope):
            memory._fallback.pop(memory_id, None)
            memory._history.pop(memory_id, None)

    remaining = len(await memory.get_all(agent_id=agent_id))
    return {
        "memory_deleted": max(0, before - remaining),
        "memory_remaining": remaining,
    }


async def clear_all_memory(memory: Any) -> dict[str, int]:
    before = 0
    try:
        before = len(await memory.get_all())
    except Exception:
        before = len(getattr(memory, "_fallback", {}) or {})
    try:
        await memory.reset()
    except Exception:
        client = getattr(memory, "_client", None)
        if client:
            try:
                await asyncio.to_thread(client.reset)
            except Exception:
                try:
                    await asyncio.to_thread(client.delete_all)
                except Exception:
                    pass
        fallback = getattr(memory, "_fallback", None)
        history = getattr(memory, "_history", None)
        if isinstance(fallback, dict):
            fallback.clear()
        if isinstance(history, dict):
            history.clear()
    remaining = 0
    try:
        remaining = len(await memory.get_all())
    except Exception:
        remaining = len(getattr(memory, "_fallback", {}) or {})
    return {
        "memory_deleted": max(0, before - remaining),
        "memory_remaining": remaining,
    }


async def _count(db: AsyncSession, stmt: Any) -> int:
    return int(await db.scalar(stmt) or 0)


async def clear_journals(
    *,
    db: AsyncSession,
    memory: Any,
    agent_id: int | str | None = None,
    include_memory: bool = True,
    include_calls: bool = True,
    include_conversations: bool = True,
) -> dict[str, Any]:
    """Wipe memory + SIP history + Telegram dialogs (optionally for one agent)."""
    aid = int(agent_id) if agent_id is not None and str(agent_id).strip() else None
    result: dict[str, Any] = {
        "ok": True,
        "memory_deleted": 0,
        "memory_remaining": 0,
        "calls_deleted": 0,
        "conversations_cleared": 0,
        "messages_deleted": 0,
    }

    if include_memory and memory is not None:
        if aid is not None:
            mem = await clear_agent_memory(memory, str(aid))
        else:
            mem = await clear_all_memory(memory)
        result.update(mem)

    if include_calls:
        call_filters = [SipCall.status.notin_(ACTIVE_SIP_STATUSES)]
        if aid is not None:
            call_filters.append(SipCall.agent_id == aid)
        result["calls_deleted"] = await _count(
            db, select(func.count()).select_from(SipCall).where(*call_filters)
        )
        await db.execute(delete(SipCall).where(*call_filters))

    if include_conversations:
        if aid is not None:
            result["conversations_cleared"] = await _count(
                db,
                select(func.count()).select_from(ConversationState).where(
                    ConversationState.agent_id == aid
                ),
            )
            result["messages_deleted"] = await _count(
                db,
                select(func.count()).select_from(MessageLog).where(MessageLog.agent_id == aid),
            )
            await db.execute(delete(MessageLog).where(MessageLog.agent_id == aid))
            await db.execute(delete(ConversationState).where(ConversationState.agent_id == aid))
        else:
            result["conversations_cleared"] = await _count(
                db, select(func.count()).select_from(ConversationState)
            )
            result["messages_deleted"] = await _count(
                db, select(func.count()).select_from(MessageLog)
            )
            await db.execute(delete(MessageLog))
            await db.execute(delete(ConversationState))

    await db.commit()
    return result
