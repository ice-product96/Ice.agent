"""Agent memory clearing helpers (separate module for MemoryStore extensions)."""

from __future__ import annotations

import asyncio
from typing import Any


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
