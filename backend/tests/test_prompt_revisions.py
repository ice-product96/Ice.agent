"""Prompt section revision history."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Agent, Base, PromptSection, PromptSectionRevision
from app.employee import (
    list_prompt_revisions,
    restore_prompt_revision,
    save_prompt_section,
)


async def sessions_for(path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_save_prompt_section_keeps_revisions(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "rev.db")
    async with sessions() as db:
        agent = Agent(name="max", prompt="v0")
        db.add(agent)
        await db.commit()
        await db.refresh(agent)

        await save_prompt_section(db, agent, "rules", "rules-v1", source="manager", commit=True)
        await save_prompt_section(db, agent, "rules", "rules-v2", source="manager", commit=True)
        await save_prompt_section(db, agent, "rules", "rules-v3", source="manager", commit=True)

        current = await db.scalar(
            select(PromptSection).where(
                PromptSection.agent_id == agent.id,
                PromptSection.key == "rules",
            )
        )
        assert current is not None
        assert current.content == "rules-v3"

        revisions = await list_prompt_revisions(db, agent.id, key="rules")
        assert len(revisions) == 2
        assert revisions[0].content == "rules-v2"
        assert revisions[1].content == "rules-v1"

        await restore_prompt_revision(db, agent, revisions[1].id, note="rollback")
        current = await db.scalar(
            select(PromptSection).where(
                PromptSection.agent_id == agent.id,
                PromptSection.key == "rules",
            )
        )
        assert current is not None
        assert current.content == "rules-v1"
        after = await list_prompt_revisions(db, agent.id, key="rules")
        assert after[0].content == "rules-v3"
        assert after[0].source == "restore"
    await engine.dispose()


@pytest.mark.asyncio
async def test_identical_save_does_not_create_revision(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "same.db")
    async with sessions() as db:
        agent = Agent(name="max", prompt="")
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        await save_prompt_section(db, agent, "tone", "calm", source="manager", commit=True)
        await save_prompt_section(db, agent, "tone", "calm", source="manager", commit=True)
        count = len(
            (
                await db.scalars(
                    select(PromptSectionRevision).where(
                        PromptSectionRevision.agent_id == agent.id
                    )
                )
            ).all()
        )
        assert count == 0
    await engine.dispose()
