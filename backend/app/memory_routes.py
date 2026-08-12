"""API routes for clearing agent memory and context."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .contract import auth, one
from .db import Agent, ConversationState, MessageLog, get_db
from .integrations import exception_text
from .memory_clear import clear_agent_memory as do_clear

router = APIRouter(prefix="/api/v1")


class AgentMemoryClearBody(BaseModel):
    include_conversations: bool = False


@router.post("/agents/{agent_id}/memory/clear", dependencies=auth)
async def clear_agent_memory_endpoint(
    agent_id: str,
    request: Request,
    payload: AgentMemoryClearBody = AgentMemoryClearBody(),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    agent = await one(db, Agent, agent_id)
    memory = request.app.state.memory
    try:
        result = await do_clear(memory, str(agent.id))
    except Exception as exc:
        detail = exception_text(exc)
        memory.last_error = detail
        raise HTTPException(
            status_code=502,
            detail=f"Memory backend request failed: {detail}",
        ) from exc

    conversations_cleared = 0
    if payload.include_conversations:
        conversations_cleared = int(
            await db.scalar(
                select(func.count()).select_from(ConversationState).where(
                    ConversationState.agent_id == agent.id
                )
            )
            or 0
        )
        await db.execute(delete(MessageLog).where(MessageLog.agent_id == agent.id))
        await db.execute(delete(ConversationState).where(ConversationState.agent_id == agent.id))
        await db.commit()

    return {
        "ok": True,
        **result,
        "conversations_cleared": conversations_cleared,
    }
