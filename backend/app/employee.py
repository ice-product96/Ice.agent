"""Autonomous employee runtime: heartbeat, scheduler, needs, consultations, prompt sections."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .timezones import normalize_timezone, zoneinfo as resolve_zoneinfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .db import (
    Agent,
    Consultation,
    CronJob,
    EmployeeNeed,
    EmployeePlan,
    EmployeeProfile,
    MessageLog,
    PromptSection,
    utcnow,
)

logger = logging.getLogger(__name__)

PROMPT_SECTION_KEYS = ("identity", "role", "rules", "skills", "tone", "self_notes")
AGENT_EDITABLE_SECTIONS = frozenset({"self_notes", "skills", "tone"})
MANAGER_ONLY_SECTIONS = frozenset({"identity", "role", "rules"})
HORIZONS = ("hour", "day", "week", "month")
NEED_KINDS = ("info", "access", "decision", "resource", "rest")

HEARTBEAT_JOB_PREFIX = "employee-heartbeat-"
ORIGIN_PAYLOAD_KEYS = (
    "reply_to_chat",
    "reply_chat_id",
    "reply_phone",
    "chat_id",
    "phone",
    "sender_id",
    "sender_username",
    "is_admin",
    "message_id",
    "work_item_id",
)

from .employee_policy import (
    build_employee_tick_instruction,
    employee_policy,
    normalize_action_name,
    action_matches_tool,
)

CONSULT_CMD_RE = re.compile(
    r"^/(answer|approve|reject)\s+(\d+)\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)


def _tz(name: str) -> ZoneInfo:
    return resolve_zoneinfo(name)


def _parse_hhmm(value: str) -> tuple[int, int]:
    parts = (value or "09:00").strip().split(":")
    try:
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (TypeError, ValueError):
        return 9, 0


def period_bounds(horizon: str, now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    local = now.astimezone(tz)
    if horizon == "hour":
        start = local.replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)
    elif horizon == "day":
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif horizon == "week":
        start = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=local.weekday())
        end = start + timedelta(days=7)
    elif horizon == "month":
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
    else:
        raise ValueError(f"Unknown horizon: {horizon}")
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def heartbeat_cron(minutes: int) -> str:
    m = max(1, min(int(minutes or 15), 120))
    if 60 % m == 0:
        return f"*/{m} * * * *"
    # Fallback: every hour at :00 and rely on budget; prefer divisors of 60.
    for candidate in (15, 10, 20, 30, 5, 60):
        if candidate >= m and 60 % candidate == 0:
            return f"*/{candidate} * * * *"
    return "*/15 * * * *"


def profile_json(profile: EmployeeProfile) -> dict[str, Any]:
    return {
        "id": profile.id,
        "agent_id": profile.agent_id,
        "autonomy_enabled": profile.autonomy_enabled,
        "paused": profile.paused,
        "heartbeat_minutes": profile.heartbeat_minutes,
        "workday_start": profile.workday_start,
        "workday_end": profile.workday_end,
        "timezone": profile.timezone,
        "budget_ticks_per_day": profile.budget_ticks_per_day,
        "ticks_used_today": profile.ticks_used_today,
        "ticks_day": profile.ticks_day,
        "last_tick_at": profile.last_tick_at.isoformat() if profile.last_tick_at else None,
        "last_digest_at": profile.last_digest_at.isoformat() if profile.last_digest_at else None,
        "role_title": profile.role_title,
        "mission": profile.mission,
        "policy": employee_policy(profile),
        "created_at": profile.created_at.isoformat() if profile.created_at else None,
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
    }


def plan_json(plan: EmployeePlan) -> dict[str, Any]:
    return {
        "id": plan.id,
        "agent_id": plan.agent_id,
        "horizon": plan.horizon,
        "period_start": plan.period_start.isoformat() if plan.period_start else None,
        "period_end": plan.period_end.isoformat() if plan.period_end else None,
        "title": plan.title,
        "body": plan.body or {},
        "status": plan.status,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
    }


def need_json(need: EmployeeNeed) -> dict[str, Any]:
    return {
        "id": need.id,
        "agent_id": need.agent_id,
        "kind": need.kind,
        "title": need.title,
        "detail": need.detail,
        "priority": need.priority,
        "status": need.status,
        "consultation_id": need.consultation_id,
        "created_at": need.created_at.isoformat() if need.created_at else None,
        "updated_at": need.updated_at.isoformat() if need.updated_at else None,
    }


def consultation_json(item: Consultation) -> dict[str, Any]:
    return {
        "id": item.id,
        "agent_id": item.agent_id,
        "work_item_id": item.work_item_id,
        "question": item.question,
        "context": item.context,
        "status": item.status,
        "requires_approval": item.requires_approval,
        "action_name": item.action_name,
        "telegram_message_ids": item.telegram_message_ids or [],
        "answer_text": item.answer_text,
        "answered_by": item.answered_by,
        "answered_at": item.answered_at.isoformat() if item.answered_at else None,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


async def get_or_create_profile(db: AsyncSession, agent_id: int) -> EmployeeProfile:
    profile = await db.scalar(select(EmployeeProfile).where(EmployeeProfile.agent_id == agent_id))
    if profile is not None:
        return profile
    profile = EmployeeProfile(agent_id=agent_id)
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    return profile


async def ensure_prompt_sections(db: AsyncSession, agent: Agent) -> dict[str, str]:
    rows = (
        await db.scalars(select(PromptSection).where(PromptSection.agent_id == agent.id))
    ).all()
    by_key = {row.key: row for row in rows}
    if "identity" not in by_key and (agent.prompt or "").strip():
        section = PromptSection(agent_id=agent.id, key="identity", content=agent.prompt)
        db.add(section)
        by_key["identity"] = section
    for key in PROMPT_SECTION_KEYS:
        if key not in by_key:
            section = PromptSection(agent_id=agent.id, key=key, content="")
            db.add(section)
            by_key[key] = section
    await db.commit()
    return {key: (by_key[key].content if key in by_key else "") for key in PROMPT_SECTION_KEYS}


async def assemble_system_prompt(
    db: AsyncSession,
    agent: Agent,
    *,
    customer: Any | None = None,
    customer_id: str | None = None,
    project_id: str | None = None,
) -> str:
    from .customers import customer_prompt_block, resolve_customer

    sections = await ensure_prompt_sections(db, agent)
    if customer is None:
        customer = await resolve_customer(
            db,
            agent,
            customer_id=customer_id,
            project_id=project_id,
        )
    assignment = customer_prompt_block(customer)
    parts: list[str] = []
    labels = {
        "identity": "Личность",
        "role": "Роль",
        "rules": "Правила (задаёт руководитель)",
        "skills": "Навыки",
        "tone": "Тон общения",
        "self_notes": "Заметки сотрудника",
    }
    inserted = False
    for key in PROMPT_SECTION_KEYS:
        text = (sections.get(key) or "").strip()
        if text:
            parts.append(f"## {labels.get(key, key)}\n{text}")
        if key == "identity" and assignment:
            parts.append(assignment)
            inserted = True
    if assignment and not inserted:
        parts.insert(0, assignment)
    if not parts and (agent.prompt or "").strip():
        body = agent.prompt
        return f"{assignment}\n\n{body}" if assignment else body
    return "\n\n".join(parts) if parts else (agent.prompt or "You are a professional employee agent.")


def is_within_workday(profile: EmployeeProfile, now: datetime | None = None) -> bool:
    now = now or utcnow()
    tz = _tz(profile.timezone)
    local = now.astimezone(tz)
    sh, sm = _parse_hhmm(profile.workday_start)
    eh, em = _parse_hhmm(profile.workday_end)
    start_m = sh * 60 + sm
    end_m = eh * 60 + em
    cur = local.hour * 60 + local.minute
    if start_m == end_m:
        return True
    if start_m < end_m:
        return start_m <= cur < end_m
    return cur >= start_m or cur < end_m


async def list_active_plans(db: AsyncSession, agent_id: int) -> list[EmployeePlan]:
    return list(
        await db.scalars(
            select(EmployeePlan)
            .where(EmployeePlan.agent_id == agent_id, EmployeePlan.status == "active")
            .order_by(EmployeePlan.horizon, EmployeePlan.id.desc())
        )
    )


async def list_open_needs(db: AsyncSession, agent_id: int) -> list[EmployeeNeed]:
    return list(
        await db.scalars(
            select(EmployeeNeed)
            .where(
                EmployeeNeed.agent_id == agent_id,
                EmployeeNeed.status.in_(("open", "waiting")),
            )
            .order_by(EmployeeNeed.priority.asc(), EmployeeNeed.id.asc())
        )
    )


async def list_open_consultations(db: AsyncSession, agent_id: int) -> list[Consultation]:
    return list(
        await db.scalars(
            select(Consultation)
            .where(Consultation.agent_id == agent_id, Consultation.status == "open")
            .order_by(Consultation.id.desc())
        )
    )


def clip_cron_name(name: str) -> str:
    return " ".join((name or "").split())[:100]


def unique_cron_name(base: str, agent_id: int) -> str:
    prefix = clip_cron_name(base) or f"once-{agent_id}"
    return f"{prefix}-{uuid.uuid4().hex[:8]}"[:120]


def _merge_origin_payload(payload: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    merged = dict(payload)
    for key in ORIGIN_PAYLOAD_KEYS:
        if merged.get(key) in (None, "") and previous.get(key) not in (None, ""):
            merged[key] = previous[key]
    return merged


async def save_once_job(
    db: AsyncSession,
    scheduler: Any,
    *,
    agent_id: int,
    name: str,
    payload: dict[str, Any],
    current_job_id: int | None = None,
) -> CronJob:
    """Create or reschedule a one-shot cron job without colliding on unique names."""
    requested = clip_cron_name(name)
    current_id: int | None
    try:
        current_id = int(current_job_id) if current_job_id is not None else None
    except (TypeError, ValueError):
        current_id = None
    existing = None
    if requested:
        existing = await db.scalar(select(CronJob).where(CronJob.name == requested))
    reuse = (
        existing is not None
        and existing.agent_id == agent_id
        and existing.enabled
        and existing.id != current_id
    )
    if reuse:
        existing.payload = _merge_origin_payload(payload, dict(existing.payload or {}))
        existing.cron = "@once"
        existing.enabled = True
        job = existing
    else:
        job_name = requested if existing is None and requested else unique_cron_name(requested, agent_id)
        job = CronJob(
            name=job_name,
            agent_id=agent_id,
            cron="@once",
            payload=payload,
            enabled=True,
        )
        db.add(job)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        job = CronJob(
            name=unique_cron_name(requested, agent_id),
            agent_id=agent_id,
            cron="@once",
            payload=payload,
            enabled=True,
        )
        db.add(job)
        await db.commit()
    await db.refresh(job)
    if scheduler is not None:
        scheduler.upsert(job)
    return job


async def list_agent_jobs(
    db: AsyncSession,
    agent_id: int,
    *,
    enabled_only: bool = True,
) -> list[CronJob]:
    filters = [CronJob.agent_id == agent_id]
    if enabled_only:
        filters.append(CronJob.enabled.is_(True))
    return list(
        await db.scalars(select(CronJob).where(*filters).order_by(CronJob.id.desc()))
    )


def _job_when(job: CronJob) -> str:
    payload = job.payload or {}
    run_once = str(payload.get("run_once_at") or "").strip()
    if run_once:
        return f"once {run_once}"
    return f"cron {job.cron}"


def _job_message(job: CronJob) -> str:
    payload = job.payload or {}
    text = str(payload.get("message") or payload.get("prompt") or "").strip()
    return text[:180]


def _job_result_text(job: CronJob) -> str:
    payload = job.payload or {}
    result = payload.get("last_result") or {}
    if not isinstance(result, dict):
        return ""
    summary = str(result.get("summary") or "").strip()
    title = str(result.get("title") or "").strip()
    if summary:
        return summary[:180]
    return title[:80]


def build_employee_context_block(
    profile: EmployeeProfile,
    jobs: list[CronJob],
    needs: list[EmployeeNeed],
    consultations: list[Consultation],
    work_items: list[Any] | None = None,
) -> str:
    from .work_items import work_items_context_lines

    lines = [
        "Состояние сотрудника (служебно — не зачитывать клиентам):",
        f"Должность: {profile.role_title or '(не задана)'}",
        f"Миссия: {profile.mission or '(не задана)'}",
        f"Автономия: включена={profile.autonomy_enabled} пауза={profile.paused}",
        f"Рабочий день: {profile.workday_start}-{profile.workday_end} {profile.timezone}",
        f"Тиков сегодня: {profile.ticks_used_today}/{profile.budget_ticks_per_day}",
    ]
    lines.extend(work_items_context_lines(work_items or []))
    staff_jobs = [
        job
        for job in jobs
        if not str(job.name or "").startswith(HEARTBEAT_JOB_PREFIX)
        and not str(job.name or "").startswith("employee-tick-once-")
        and not (job.payload or {}).get("work_item_id")
    ]
    if staff_jobs:
        lines.append("Штатное расписание (не кейсы Cursor/follow-up):")
        for job in staff_jobs[:8]:
            lines.append(f"- #{job.id} {job.name} ({_job_when(job)})")
    if needs:
        lines.append("Открытые потребности:")
        for need in needs[:10]:
            lines.append(f"- #{need.id} [{need.kind}/{need.status} p={need.priority}] {need.title}")
    if consultations:
        lines.append("Ожидают ответа руководителя:")
        for item in consultations[:10]:
            kind = "одобрение" if item.requires_approval else "вопрос"
            extra = f" кейс #{item.work_item_id}" if getattr(item, "work_item_id", None) else ""
            lines.append(f"- #{item.id} ({kind}{extra}) {item.question[:200]}")
    return "\n".join(lines)


async def sync_heartbeat_job(
    db: AsyncSession,
    scheduler: Any,
    profile: EmployeeProfile,
) -> CronJob | None:
    """Create/update/disable the recurring heartbeat CronJob for this agent."""
    name = f"{HEARTBEAT_JOB_PREFIX}{profile.agent_id}"
    job = await db.scalar(select(CronJob).where(CronJob.name == name))
    want_enabled = bool(profile.autonomy_enabled and not profile.paused)
    cron = heartbeat_cron(profile.heartbeat_minutes)
    payload = {
        "kind": "employee_tick",
        "message": build_employee_tick_instruction(profile),
        "source": "employee_heartbeat",
        "timezone": normalize_timezone(profile.timezone or "UTC"),
    }
    if job is None:
        if not want_enabled:
            return None
        job = CronJob(
            name=name,
            agent_id=profile.agent_id,
            cron=cron,
            payload=payload,
            enabled=True,
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
        if scheduler is not None:
            scheduler.upsert(job)
        return job
    job.cron = cron
    job.payload = payload
    job.enabled = want_enabled
    await db.commit()
    await db.refresh(job)
    if scheduler is not None:
        if want_enabled:
            scheduler.upsert(job)
        else:
            scheduler.remove(job.id)
    return job


async def schedule_immediate_tick(
    db: AsyncSession,
    scheduler: Any,
    agent_id: int,
    *,
    reason: str = "consult_resolved",
    work_item_id: int | None = None,
    manager_answer: str = "",
    consultation_id: int | None = None,
    consultation_status: str = "",
    approved_by: str = "",
    force: bool = True,
) -> CronJob:
    profile = await get_or_create_profile(db, agent_id)
    run_at = utcnow() + timedelta(seconds=3)
    payload: dict[str, Any] = {
        "kind": "employee_tick",
        "message": build_employee_tick_instruction(profile),
        "source": reason,
        "run_once_at": run_at.isoformat(),
        "timezone": "UTC",
        "force": force,
    }
    prefix_parts: list[str] = []
    if consultation_id is not None:
        payload["consultation_id"] = consultation_id
        payload["consultation_status"] = consultation_status
        payload["approved_by"] = approved_by
        prefix_parts.append(f"Консультация #{consultation_id} закрыта руководителем.")
    answer = (manager_answer or "").strip()
    if answer:
        prefix_parts.append(f"Ответ руководителя: {answer}")
    if work_item_id:
        payload["work_item_id"] = work_item_id
        prefix_parts.insert(0, f"Продолжи кейс #{work_item_id}. Не создавай новый кейс.")
    if prefix_parts:
        payload["message"] = " ".join(prefix_parts) + "\n\n" + payload["message"]
    job = CronJob(
        name=f"employee-tick-once-{agent_id}-{uuid.uuid4().hex[:10]}",
        agent_id=agent_id,
        cron="@once",
        payload=payload,
        enabled=True,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    if scheduler is not None:
        scheduler.upsert(job)
    return job


class EmployeeService:
    """Helpers bound to telegram + scheduler for tools and tick."""

    def __init__(self, telegram: Any = None, scheduler: Any = None, events: Any = None) -> None:
        self.telegram = telegram
        self.scheduler = scheduler
        self.events = events

    async def reset_budget_if_needed(self, profile: EmployeeProfile) -> None:
        tz = _tz(profile.timezone)
        today = utcnow().astimezone(tz).date().isoformat()
        if profile.ticks_day != today:
            profile.ticks_day = today
            profile.ticks_used_today = 0

    async def can_tick(
        self,
        profile: EmployeeProfile,
        *,
        force: bool = False,
    ) -> tuple[bool, str]:
        if not profile.autonomy_enabled and not force:
            return False, "autonomy_disabled"
        if profile.paused and not force:
            return False, "paused"
        await self.reset_budget_if_needed(profile)
        if not force and profile.ticks_used_today >= profile.budget_ticks_per_day:
            return False, "budget_exhausted"
        if not force and not is_within_workday(profile):
            # Outside workday: only allow if there are urgent waiting needs — checked by caller
            return True, "off_hours"
        return True, "ok"

    async def mark_tick(self, db: AsyncSession, profile: EmployeeProfile) -> None:
        await self.reset_budget_if_needed(profile)
        profile.ticks_used_today = int(profile.ticks_used_today or 0) + 1
        profile.last_tick_at = utcnow()
        await db.commit()

    async def create_consultation(
        self,
        db: AsyncSession,
        agent: Agent,
        *,
        question: str,
        context: str = "",
        requires_approval: bool = False,
        action_name: str | None = None,
        need_kind: str = "decision",
        work_item_id: int | None = None,
    ) -> dict[str, Any]:
        cleaned_question = question.strip()
        existing = await db.scalar(
            select(Consultation).where(
                Consultation.agent_id == agent.id,
                Consultation.status == "open",
                Consultation.question == cleaned_question,
            )
        )
        if existing is not None:
            need = await db.scalar(
                select(EmployeeNeed).where(EmployeeNeed.consultation_id == existing.id)
            )
            return {
                "consultation": consultation_json(existing),
                "need": need_json(need) if need is not None else None,
                "duplicate": True,
            }
        item = Consultation(
            agent_id=agent.id,
            work_item_id=work_item_id,
            question=question.strip(),
            context=(context or "").strip(),
            status="open",
            requires_approval=requires_approval,
            action_name=(normalize_action_name(action_name) if action_name else None) or None,
            telegram_message_ids=[],
        )
        db.add(item)
        await db.flush()
        need = EmployeeNeed(
            agent_id=agent.id,
            kind=need_kind if need_kind in NEED_KINDS else "decision",
            title=("Approval: " if requires_approval else "Consult: ") + question.strip()[:180],
            detail=context or question,
            priority=2 if requires_approval else 4,
            status="waiting",
            consultation_id=item.id,
        )
        db.add(need)
        await db.commit()
        await db.refresh(item)
        await db.refresh(need)

        message_ids: list[Any] = []
        if self.telegram and agent.telegram_account_id is not None:
            from .db import TelegramAccount

            account = await db.get(TelegramAccount, agent.telegram_account_id)
            if account is not None:
                kind = "APPROVAL" if requires_approval else "CONSULT"
                text = (
                    f"[{kind} #{item.id}] агент «{agent.name}»\n"
                    f"{question.strip()}\n"
                )
                if context:
                    text += f"\nКонтекст:\n{context.strip()[:1500]}\n"
                if requires_approval:
                    text += (
                        f"\nОтветьте:\n/approve {item.id}\n/reject {item.id} причина\n"
                        f"или /answer {item.id} текст"
                    )
                else:
                    text += f"\nОтветьте: /answer {item.id} ваш ответ"
                try:
                    sent = await self.telegram.notify_admins(account.phone, text)
                    for entry in sent:
                        if isinstance(entry, dict) and entry.get("id") is not None:
                            message_ids.append(entry.get("id"))
                        elif isinstance(entry, dict) and entry.get("message_id") is not None:
                            message_ids.append(entry.get("message_id"))
                except Exception:
                    logger.exception("Failed to notify admins for consultation %s", item.id)
        if message_ids:
            item.telegram_message_ids = message_ids
            await db.commit()
            await db.refresh(item)
        if self.events:
            await self.events.publish(
                "employee.consultation.created",
                {"agent_id": agent.id, "consultation_id": item.id, "requires_approval": requires_approval},
            )
        return {"consultation": consultation_json(item), "need": need_json(need)}

    async def resolve_consultation(
        self,
        db: AsyncSession,
        consultation_id: int,
        *,
        status: str,
        answer_text: str = "",
        answered_by: str = "",
        schedule_tick: bool = True,
    ) -> Consultation:
        item = await db.get(Consultation, consultation_id)
        if item is None:
            raise KeyError(consultation_id)
        if item.status != "open":
            return item
        if status not in {"answered", "approved", "rejected"}:
            raise ValueError("Invalid consultation status")
        item.status = status
        item.answer_text = (answer_text or "").strip() or None
        item.answered_by = (answered_by or "").strip() or None
        item.answered_at = utcnow()
        needs = (
            await db.scalars(
                select(EmployeeNeed).where(EmployeeNeed.consultation_id == item.id)
            )
        ).all()
        for need in needs:
            if status == "rejected":
                need.status = "dropped"
            else:
                need.status = "satisfied"
                if answer_text:
                    need.detail = (need.detail or "") + f"\nManager: {answer_text.strip()}"
        await db.commit()
        await db.refresh(item)
        if item.work_item_id:
            from .work_items import get_work_item, resume_work_item

            work = await get_work_item(db, item.work_item_id)
            if work is not None:
                note = item.answer_text or status
                await resume_work_item(db, work, note=f"Руководитель ({status}): {note}")
        if schedule_tick and self.scheduler is not None:
            await schedule_immediate_tick(
                db,
                self.scheduler,
                item.agent_id,
                reason="consult_resolved",
                work_item_id=item.work_item_id,
                manager_answer=item.answer_text or "",
                consultation_id=item.id,
                consultation_status=item.status,
                approved_by=item.answered_by or "",
                force=True,
            )
        if self.events:
            await self.events.publish(
                "employee.consultation.resolved",
                {
                    "agent_id": item.agent_id,
                    "consultation_id": item.id,
                    "status": status,
                },
            )
        return item

    async def dismiss_consultation(
        self,
        db: AsyncSession,
        consultation_id: int,
        *,
        reason: str = "",
        answered_by: str = "",
    ) -> Consultation:
        """Close a stale consultation without resuming the agent."""
        item = await db.get(Consultation, consultation_id)
        if item is None:
            raise KeyError(consultation_id)
        if item.status != "open":
            return item
        note = (reason or "Снято с очереди руководителем").strip()
        item.status = "dismissed"
        item.answer_text = note
        item.answered_by = (answered_by or "").strip() or None
        item.answered_at = utcnow()
        needs = (
            await db.scalars(
                select(EmployeeNeed).where(EmployeeNeed.consultation_id == item.id)
            )
        ).all()
        for need in needs:
            need.status = "dropped"
            need.detail = (need.detail or "") + f"\nDismissed: {note}"
        if item.work_item_id:
            from .work_items import get_work_item, set_status

            work = await get_work_item(db, item.work_item_id)
            if work is not None and work.status == "waiting_manager":
                await set_status(
                    db,
                    work,
                    "in_progress",
                    next_action="Консультация снята — продолжай без ожидания",
                    wait_owner="self",
                    event_title="Консультация снята",
                    event_detail=note,
                )
        await db.commit()
        await db.refresh(item)
        if self.events:
            await self.events.publish(
                "employee.consultation.dismissed",
                {
                    "agent_id": item.agent_id,
                    "consultation_id": item.id,
                },
            )
        return item

    async def has_approval(
        self,
        db: AsyncSession,
        agent_id: int,
        action_name: str,
        *,
        max_age_hours: int = 24,
    ) -> bool:
        cutoff = utcnow() - timedelta(hours=max_age_hours)
        rows = (
            await db.scalars(
                select(Consultation).where(
                    Consultation.agent_id == agent_id,
                    Consultation.requires_approval.is_(True),
                    Consultation.status == "approved",
                    Consultation.answered_at.is_not(None),
                    Consultation.answered_at >= cutoff,
                )
            )
        ).all()
        return any(action_matches_tool(row.action_name, tool_name) for row in rows)

    async def ensure_period_plans(
        self,
        db: AsyncSession,
        agent: Agent,
        profile: EmployeeProfile,
    ) -> list[EmployeePlan]:
        """Create stub active plans for missing horizons in the current period."""
        tz = _tz(profile.timezone)
        now = utcnow()
        created: list[EmployeePlan] = []
        existing = await list_active_plans(db, agent.id)
        by_horizon = {p.horizon: p for p in existing}
        for horizon in HORIZONS:
            start, end = period_bounds(horizon, now, tz)
            current = by_horizon.get(horizon)
            if current is not None and current.period_start <= now < current.period_end:
                continue
            if current is not None and now >= current.period_end:
                current.status = "done"
            title = {
                "hour": "План на час",
                "day": "План на день",
                "week": "План на неделю",
                "month": "План на месяц",
            }[horizon]
            plan = EmployeePlan(
                agent_id=agent.id,
                horizon=horizon,
                period_start=start,
                period_end=end,
                title=f"{title}: {profile.mission[:80] or agent.name}",
                body={
                    "steps": [
                        {
                            "id": "1",
                            "title": "Уточнить приоритеты и следующий конкретный шаг",
                            "status": "todo",
                        }
                    ]
                },
                status="active",
            )
            db.add(plan)
            created.append(plan)
        if created:
            await db.commit()
            for plan in created:
                await db.refresh(plan)
        return await list_active_plans(db, agent.id)

    async def maybe_send_daily_digest(
        self,
        db: AsyncSession,
        agent: Agent,
        profile: EmployeeProfile,
    ) -> None:
        tz = _tz(profile.timezone)
        local = utcnow().astimezone(tz)
        if local.hour < 17:
            return
        today = local.date().isoformat()
        if profile.last_digest_at and profile.last_digest_at.astimezone(tz).date().isoformat() == today:
            return
        jobs = await list_agent_jobs(db, agent.id)
        needs = await list_open_needs(db, agent.id)
        consults = await list_open_consultations(db, agent.id)
        lines = [
            f"[DIGEST] агент «{agent.name}» · {today}",
            f"Миссия: {profile.mission or '—'}",
            f"Тиков сегодня: {profile.ticks_used_today}/{profile.budget_ticks_per_day}",
            f"Задач в расписании: {len(jobs)} · needs: {len(needs)} · consults: {len(consults)}",
        ]
        for job in jobs[:8]:
            lines.append(f"- {_job_when(job)} {job.name}")
        for need in needs[:5]:
            lines.append(f"! need #{need.id} {need.title}")
        text = "\n".join(lines)
        if self.telegram and agent.telegram_account_id is not None:
            from .db import TelegramAccount

            account = await db.get(TelegramAccount, agent.telegram_account_id)
            if account is not None:
                try:
                    await self.telegram.notify_admins(account.phone, text)
                except Exception:
                    logger.exception("Daily digest failed for agent %s", agent.id)
        profile.last_digest_at = utcnow()
        await db.commit()

    async def prepare_tick_context(
        self,
        db: AsyncSession,
        agent: Agent,
        profile: EmployeeProfile,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        ok, reason = await self.can_tick(profile, force=force)
        jobs = await list_agent_jobs(db, agent.id)
        needs = await list_open_needs(db, agent.id)
        consultations = await list_open_consultations(db, agent.id)
        from .work_items import list_open_work_items

        work_items = await list_open_work_items(db, agent.id)
        urgent = any(n.priority <= 2 and n.status in {"open", "waiting"} for n in needs)
        if reason == "off_hours" and not force and not urgent:
            return {"skip": True, "reason": "off_hours", "jobs": jobs, "needs": needs}
        if not ok and not force:
            return {"skip": True, "reason": reason, "jobs": jobs, "needs": needs}
        prompt = await assemble_system_prompt(db, agent)
        block = build_employee_context_block(profile, jobs, needs, consultations, work_items)
        return {
            "skip": False,
            "reason": reason,
            "system_prompt": prompt,
            "context_block": block,
            "jobs": jobs,
            "needs": needs,
            "consultations": consultations,
        }
