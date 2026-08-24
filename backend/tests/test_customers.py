from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.customers import (
    allowed_workspaces_from_env,
    customer_prompt_block,
    extract_cursor_projects,
    slugify,
)
from app.db import Agent, Base, Customer
from app.employee import assemble_system_prompt, ensure_prompt_sections


def test_slugify_transliterates_russian() -> None:
    assert slugify("Типография LAVVE") == "tipografiya-lavve"
    assert slugify("  Acme Site  ") == "acme-site"


def test_allowed_workspaces_from_env_splits_paths() -> None:
    paths = allowed_workspaces_from_env(
        {"MCP_ALLOWED_WORKSPACES": r"D:\projects\ice.agent;D:\projects\DigitalWorker\LAVVE"}
    )
    assert paths == [r"D:\projects\ice.agent", r"D:\projects\DigitalWorker\LAVVE"]


def test_extract_cursor_projects_from_status_windows() -> None:
    items = extract_cursor_projects(
        {
            "workspacePath": r"D:\projects\ice.agent",
            "windows": [
                {
                    "id": "win-1",
                    "title": "LAVVE",
                    "workspacePath": r"D:\projects\DigitalWorker\LAVVE",
                }
            ],
        }
    )
    by_label = {item["label"]: item for item in items}
    assert "LAVVE" in by_label
    assert by_label["LAVVE"]["project_id"] == "lavve"
    assert by_label["LAVVE"]["window_id"] == "win-1"


def test_customer_prompt_block_presents_customer_and_project() -> None:
    customer = Customer(
        id="lavve",
        name="Типография LAVVE",
        project_id="lavve",
        cursor_workspace=r"D:\projects\DigitalWorker\LAVVE",
    )
    block = customer_prompt_block(customer)
    assert "Типография LAVVE" in block
    assert "id=lavve" in block
    assert "Проект разработки: lavve" in block
    assert r"D:\projects\DigitalWorker\LAVVE" in block
    assert "Представляйся" in block


@pytest.mark.asyncio
async def test_assemble_system_prompt_injects_customer_assignment(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'customers.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        agent = Agent(name="pm-agent", prompt="")
        db.add(agent)
        await db.flush()
        customer = Customer(
            id="lavve",
            name="LAVVE",
            agent_id=agent.id,
            project_id="lavve",
            cursor_workspace=r"D:\projects\DigitalWorker\LAVVE",
            is_default=True,
        )
        db.add(customer)
        await ensure_prompt_sections(db, agent)
        await db.commit()
        prompt = await assemble_system_prompt(db, agent)
        assert "## Заказчик и проект" in prompt
        assert "LAVVE" in prompt
        assert "Проект разработки: lavve" in prompt
    await engine.dispose()
