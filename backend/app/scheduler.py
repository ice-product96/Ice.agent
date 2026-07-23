from typing import Any, Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .db import CronJob


class CronManager:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], callback: Callable[[int, dict[str, Any]], Awaitable[None]]) -> None:
        self.sessions = sessions
        self.callback = callback
        self.scheduler = AsyncIOScheduler(timezone="UTC")

    async def load(self) -> None:
        async with self.sessions() as db:
            jobs = (await db.scalars(select(CronJob).where(CronJob.enabled.is_(True)))).all()
        for job in jobs:
            self.upsert(job)

    def upsert(self, job: CronJob) -> None:
        self.scheduler.add_job(
            self.callback,
            CronTrigger.from_crontab(job.cron, timezone=(job.payload or {}).get("timezone", "UTC")),
            id=f"cron-{job.id}",
            args=[job.agent_id, job.payload],
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
        )

    def remove(self, job_id: int) -> None:
        if self.scheduler.get_job(f"cron-{job_id}"):
            self.scheduler.remove_job(f"cron-{job_id}")

    def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
