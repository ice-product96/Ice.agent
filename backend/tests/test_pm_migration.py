from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app import migrate
from app.config import get_settings


def test_pm_migration_upgrade_and_downgrade(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "pm-migration.db"
    monkeypatch.setenv(
        "ICE_DATABASE_URL",
        f"sqlite+aiosqlite:///{database.as_posix()}",
    )
    get_settings.cache_clear()
    backend = Path(__file__).parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))

    command.upgrade(config, "head")
    sync_engine = create_engine(f"sqlite:///{database.as_posix()}")
    with sync_engine.connect() as connection:
        inspector = inspect(connection)
        assert {"project_states", "decision_records", "cursor_runs"} <= set(
            inspector.get_table_names()
        )
        columns = {column["name"] for column in inspector.get_columns("work_items")}
        assert {
            "task_type",
            "requirements",
            "acceptance_criteria",
            "pm_phase",
            "source_message_id",
            "active_cursor_run_id",
        } <= columns

    command.downgrade(config, "c9d0e1f2a3b4")
    with sync_engine.connect() as connection:
        inspector = inspect(connection)
        assert "cursor_runs" not in inspector.get_table_names()
        columns = {column["name"] for column in inspector.get_columns("work_items")}
        assert "pm_phase" not in columns
    sync_engine.dispose()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_legacy_bootstrap_runs_real_pm_migration(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    async def table_exists(name: str) -> bool:
        return name in {"admin_settings", "work_items"}

    monkeypatch.setattr(migrate, "_table_exists", table_exists)
    monkeypatch.setattr(migrate, "_run_alembic", lambda *args: calls.append(args))
    await migrate.bootstrap()
    assert calls == [
        ("stamp", "c9d0e1f2a3b4"),
        ("upgrade", "head"),
    ]
