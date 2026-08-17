"""Bootstrap Alembic on databases that predate migration tracking."""

from __future__ import annotations

import asyncio
import subprocess
import sys

from sqlalchemy import inspect

from app.db import create_schema, engine

HEAD_REVISION = "b8c9d0e1f2a3"


async def _table_exists(table: str) -> bool:
    async with engine.connect() as conn:
        return await conn.run_sync(lambda sync_conn: inspect(sync_conn).has_table(table))


async def bootstrap() -> None:
    has_alembic = await _table_exists("alembic_version")
    if has_alembic:
        _run_alembic("upgrade", "head")
        return

    if await _table_exists("admin_settings"):
        # Legacy DB: tables were created by create_schema(), not Alembic.
        await create_schema()
        _run_alembic("stamp", HEAD_REVISION)
        return

    _run_alembic("upgrade", "head")


def _run_alembic(*args: str) -> None:
    result = subprocess.run(["alembic", *args], check=False)
    if result.returncode != 0:
        sys.exit(result.returncode)


def main() -> None:
    asyncio.run(bootstrap())


if __name__ == "__main__":
    main()
