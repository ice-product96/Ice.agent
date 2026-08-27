"""Quick WI status check."""
import asyncio
from sqlalchemy import select

from app.db import CursorRun, WorkItem, async_session_factory


async def main() -> None:
    async with async_session_factory() as db:
        for wid in (24, 25):
            item = await db.get(WorkItem, wid)
            if not item:
                print(f"WI {wid}: missing")
                continue
            print(
                f"WI {wid}: status={item.status} pm={item.pm_phase} "
                f"wait={item.wait_owner} next={item.next_action!r} "
                f"err={item.last_error!r} active_run={item.active_cursor_run_id}"
            )
            runs = (
                await db.scalars(
                    select(CursorRun)
                    .where(CursorRun.work_item_id == wid)
                    .order_by(CursorRun.attempt)
                )
            ).all()
            for r in runs[-5:]:
                print(f"  run #{r.attempt} {r.status} err={r.error!r}")


if __name__ == "__main__":
    asyncio.run(main())
