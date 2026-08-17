import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .db import CronJob
from .timezones import normalize_timezone

logger = logging.getLogger(__name__)


class CronManager:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], callback: Callable[[int, dict[str, Any]], Awaitable[None]]) -> None:
        self.sessions = sessions
        self.callback = callback
        self.scheduler = AsyncIOScheduler(timezone="UTC")

    async def load(self) -> None:
        async with self.sessions() as db:
            jobs = (await db.scalars(select(CronJob).where(CronJob.enabled.is_(True)))).all()
        for job in jobs:
            try:
                self.upsert(job)
            except Exception as exc:
                logger.exception(
                    "Failed to schedule cron job id=%s name=%r cron=%r: %s",
                    job.id,
                    job.name,
                    job.cron,
                    exc,
                )

    def upsert(self, job: CronJob) -> None:
        payload = job.payload or {}
        run_once_at = payload.get("run_once_at")
        trigger = (
            DateTrigger(run_date=datetime.fromisoformat(str(run_once_at).replace("Z", "+00:00")))
            if run_once_at
            else CronTrigger.from_crontab(
                job.cron,
                timezone=normalize_timezone(payload.get("timezone"), default="UTC"),
            )
        )
        self.scheduler.add_job(
            self._run,
            trigger,
            id=f"cron-{job.id}",
            args=[job.id, job.agent_id, payload, bool(run_once_at)],
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=None if run_once_at else 300,
        )

    async def _run(
        self,
        job_id: int,
        agent_id: int,
        payload: dict[str, Any],
        run_once: bool,
    ) -> None:
        try:
            await self.callback(agent_id, payload)
        finally:
            values: dict[str, Any] = {"last_run_at": datetime.now(timezone.utc)}
            if run_once:
                values["enabled"] = False
            async with self.sessions() as db:
                await db.execute(update(CronJob).where(CronJob.id == job_id).values(**values))
                await db.commit()

    def remove(self, job_id: int) -> None:
        if self.scheduler.get_job(f"cron-{job_id}"):
            self.scheduler.remove_job(f"cron-{job_id}")

    def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
