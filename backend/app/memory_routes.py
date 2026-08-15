"""Compatibility routes — journals clear also lives on the main contract router."""

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from .contract import JournalsClearBody, _journals_clear, auth, one
from .db import Agent, get_db

router = APIRouter(prefix="/api/v1")


@router.post("/agents/{agent_id}/memory/clear", dependencies=auth)
async def clear_agent_memory_endpoint(
    agent_id: str,
    request: Request,
    payload: JournalsClearBody = JournalsClearBody(),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    agent = await one(db, Agent, agent_id)
    return await _journals_clear(request, db, agent_id=agent.id, payload=payload)
