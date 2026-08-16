import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import Settings
from .conversation import ConversationContextService, as_utc, iso_utc
from .db import (
    Agent,
    AgentLink,
    AgentTask,
    CronJob,
    EmployeeNeed,
    EmployeePlan,
    LlmProfile,
    McpServer,
    MessageLog,
    PromptSection,
    RuntimeSettings,
    TelegramAccount,
    agent_mcp_servers,
    utcnow,
)
from .action_reports import format_admin_action_report
from .employee import (
    AGENT_EDITABLE_SECTIONS,
    EMPLOYEE_TICK_INSTRUCTION,
    HORIZONS,
    NEED_KINDS,
    PROMPT_SECTION_KEYS,
    EmployeeService,
    assemble_system_prompt,
    consultation_json,
    get_or_create_profile,
    list_active_plans,
    list_open_consultations,
    list_open_needs,
    need_json,
    period_bounds,
    plan_json,
)
from .events import EventHub
from .integrations import LLMClient, McpManager, MemoryStore, WebSearch
from .tools import (
    APPROVAL_REQUIRED_TOOLS,
    ToolRegistry,
    common_registry,
    resolve_tool_permissions,
)
from .telegram import TelegramGateway
from .secrets import SecretStore


NO_TELEGRAM_REPLY = "[[NO_TELEGRAM_REPLY]]"


class PermissionDenied(PermissionError):
    pass


class TaskBus:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], events: EventHub) -> None:
        self.sessions = sessions
        self.events = events
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self.runtime: AgentRuntime | None = None
        self._workers: list[asyncio.Task[None]] = []
        self._scheduled: set[int] = set()

    def bind_runtime(self, runtime: "AgentRuntime") -> None:
        self.runtime = runtime

    async def start(self, workers: int = 1) -> None:
        if self._workers:
            return
        async with self.sessions() as db:
            running = (await db.scalars(
                select(AgentTask).where(AgentTask.status.in_(("queued", "running")))
            )).all()
            for task in running:
                task.status = "queued"
            await db.commit()
        for task in running:
            await self._enqueue(task.id)
        self._workers = [
            asyncio.create_task(self._worker(), name=f"ice-task-worker-{index}")
            for index in range(max(1, workers))
        ]

    async def stop(self) -> None:
        workers, self._workers = self._workers, []
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    async def _enqueue(self, task_id: int) -> None:
        if task_id in self._scheduled:
            return
        self._scheduled.add(task_id)
        await self.queue.put(task_id)

    async def _create(
        self,
        source_agent_id: int,
        target_agent_id: int,
        payload: dict[str, Any],
        permission: str,
    ) -> AgentTask:
        permission_column = (
            AgentLink.can_delegate if permission == "delegate" else AgentLink.can_message
        )
        async with self.sessions() as db:
            link = await db.scalar(select(AgentLink).where(
                AgentLink.source_agent_id == source_agent_id,
                AgentLink.target_agent_id == target_agent_id,
                permission_column.is_(True),
            ))
            if link is None:
                raise PermissionDenied(f"AgentLink does not permit {permission}")
            task = AgentTask(
                source_agent_id=source_agent_id,
                target_agent_id=target_agent_id,
                input=payload,
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
        await self._enqueue(task.id)
        await self.events.publish("task.queued", {"task_id": task.id})
        return task

    async def delegate(self, source_agent_id: int, target_agent_id: int, payload: dict[str, Any]) -> AgentTask:
        return await self._create(source_agent_id, target_agent_id, payload, "delegate")

    async def notify(self, source_agent_id: int, target_agent_id: int, message: str) -> AgentTask:
        return await self._create(
            source_agent_id,
            target_agent_id,
            {"message": message, "kind": "notification"},
            "message",
        )

    async def _worker(self) -> None:
        while True:
            task_id = await self.queue.get()
            try:
                try:
                    await self._process(task_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self.events.publish(
                        "task.worker_error",
                        {"task_id": task_id, "error": str(exc)},
                    )
            finally:
                self._scheduled.discard(task_id)
                self.queue.task_done()

    async def _process(self, task_id: int) -> None:
        async with self.sessions() as db:
            claimed = await db.execute(
                update(AgentTask)
                .where(AgentTask.id == task_id, AgentTask.status == "queued")
                .values(status="running", error=None)
            )
            await db.commit()
            if not claimed.rowcount:
                return
            task = await db.get(AgentTask, task_id)
            agent = await db.get(Agent, task.target_agent_id) if task else None
            if task is None or agent is None or not agent.enabled:
                await db.execute(
                    update(AgentTask)
                    .where(AgentTask.id == task_id, AgentTask.status == "running")
                    .values(status="failed", error="Target agent is missing or disabled")
                )
                await db.commit()
                await self.events.publish(
                    "task.failed",
                    {"task_id": task_id, "error": "Target agent is missing or disabled"},
                )
                return
            payload = task.input or {}
            source_agent_id = task.source_agent_id
        await self.events.publish("task.running", {"task_id": task_id})
        try:
            if self.runtime is None:
                raise RuntimeError("TaskBus runtime is not bound")
            message = str(
                payload.get("message")
                or payload.get("prompt")
                or json.dumps(payload, ensure_ascii=False)
            )
            async with self.sessions() as db:
                agent = await db.get(Agent, task.target_agent_id)
                result = await self.runtime.run(
                    db,
                    agent,
                    message,
                    {
                        **payload,
                        "task_id": task_id,
                        "source_agent_id": source_agent_id,
                    },
                )
                await db.execute(
                    update(AgentTask)
                    .where(AgentTask.id == task_id, AgentTask.status == "running")
                    .values(status="completed", output={"response": result}, error=None)
                )
                await db.commit()
            await self.events.publish(
                "task.completed",
                {"task_id": task_id, "output": {"response": result}},
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self.sessions() as db:
                await db.execute(
                    update(AgentTask)
                    .where(AgentTask.id == task_id, AgentTask.status == "running")
                    .values(status="failed", error=str(exc))
                )
                await db.commit()
            await self.events.publish("task.failed", {"task_id": task_id, "error": str(exc)})


class AgentRuntime:
    def __init__(
        self,
        settings: Settings,
        memory: MemoryStore,
        search: WebSearch,
        events: EventHub,
        telegram: TelegramGateway | None = None,
        mcp: McpManager | None = None,
        sip: Any | None = None,
    ) -> None:
        self.settings = settings
        self.memory = memory
        self.search = search
        self.events = events
        self.telegram = telegram
        self.mcp = mcp
        self.sip = sip
        self.task_bus: TaskBus | None = None
        self.scheduler: Any = None
        self.conversations = ConversationContextService()
        self.employee = EmployeeService(telegram=telegram, scheduler=None, events=events)

    def bind_task_bus(self, task_bus: TaskBus) -> None:
        self.task_bus = task_bus

    def bind_scheduler(self, scheduler: Any) -> None:
        self.scheduler = scheduler
        self.employee.scheduler = scheduler

    async def registry(
        self,
        agent: Agent,
        phone: str | None = None,
        mcp_server_names: set[str] | None = None,
        memory_enabled: bool = True,
        db: AsyncSession | None = None,
        runtime_settings: RuntimeSettings | None = None,
        context: dict[str, Any] | None = None,
    ) -> ToolRegistry:
        registry = common_registry()
        registry.register(
            self.search.search,
            "web_search",
            (
                "Search the public web for titles, URLs and snippets. "
                "To open or read a specific page, use an MCP browser tool when available."
            ),
        )
        if memory_enabled:
            registry.register(self.memory.search, "memory_search", "Search long-term memory")
            registry.register(self.memory.add, "memory_add", "Store long-term memory")
        if phone and self.telegram:
            telegram_tools = self.telegram.tool_registry(phone)
            registry.tools.update(telegram_tools.tools)
            if context is not None and context.get("source") == "telegram":
                async def telegram_suppress_reply(reason: str = "") -> dict[str, Any]:
                    """Suppress the automatic Telegram reply for the current incoming message."""
                    context["_suppress_telegram_reply"] = True
                    context["_suppress_telegram_reason"] = (
                        reason.strip() or "Agent chose not to reply"
                    )
                    return {"ok": True, "reply_suppressed": True}

                registry.register(
                    telegram_suppress_reply,
                    "telegram_suppress_reply",
                    (
                        "Do not send any Telegram response to the current incoming message. "
                        "Use this instead of writing 'silence', an emoji, or an explanation."
                    ),
                )
        tools_enabled = set((agent.config or {}).get("tools") or [])
        if self.sip and agent.sip_account_id is not None and "sip" in tools_enabled:
            async def sip_dial(number: str) -> dict[str, Any]:
                """Place an outbound phone call via the agent's SIP account and talk with OpenAI Realtime."""
                from .db import SipAccount

                if db is None:
                    raise RuntimeError("Database session is required for sip_dial")
                account = await db.get(SipAccount, agent.sip_account_id)
                if account is None:
                    raise RuntimeError("SIP account not found")
                return await self.sip.dial(account=account, agent=agent, number=number)

            async def sip_hangup(call_id: str = "") -> dict[str, Any]:
                """Hang up an active SIP call. Pass db call id or SIP Call-ID; empty hangs up nothing."""
                if not call_id:
                    active = [
                        item
                        for item in self.sip.list_active_calls()
                        if item.get("sip_account_id") == agent.sip_account_id
                    ]
                    if not active:
                        return {"ok": False, "error": "no active calls"}
                    call_id = str(active[0].get("db_id") or active[0].get("sip_call_id"))
                if str(call_id).isdigit():
                    await self.sip.hangup(db_id=int(call_id))
                else:
                    await self.sip.hangup(sip_call_id=str(call_id))
                return {"ok": True, "call_id": call_id}

            async def sip_status() -> dict[str, Any]:
                """Return SIP registration and active calls for this agent's account."""
                return await self.sip.status(agent.sip_account_id)

            registry.register(sip_dial, "sip_dial", "Dial a phone number through the agent's SIP account")
            registry.register(sip_hangup, "sip_hangup", "Hang up an active SIP call")
            registry.register(sip_status, "sip_status", "SIP registration and active call status")
        if self.mcp and mcp_server_names:
            await self.mcp.register_tools(registry, mcp_server_names)
        if self.task_bus:
            async def agent_create_task(target_agent_id: int, message: str) -> dict[str, Any]:
                task = await self.task_bus.delegate(
                    agent.id,
                    target_agent_id,
                    {"message": message},
                )
                return {"task_id": task.id, "status": task.status}

            async def agent_notify(target_agent_id: int, message: str) -> dict[str, Any]:
                task = await self.task_bus.notify(agent.id, target_agent_id, message)
                return {"task_id": task.id, "status": task.status}

            registry.register(agent_create_task, "agent_create_task", "Delegate a task to a linked agent")
            registry.register(agent_notify, "agent_notify", "Notify a linked agent")
        if self.scheduler and db is not None and runtime_settings is not None:
            async def schedule_self(run_at: str, message: str, name: str = "") -> dict[str, Any]:
                """Schedule this agent to execute a one-time task at an ISO date and time."""
                try:
                    target = datetime.fromisoformat(run_at.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("run_at must be an ISO date and time") from exc
                if target.tzinfo is None:
                    target = target.replace(tzinfo=ZoneInfo(runtime_settings.timezone))
                target = target.astimezone(timezone.utc)
                if target <= datetime.now(timezone.utc):
                    raise ValueError("run_at must be in the future")
                source = context or {}
                payload = {
                    "message": message,
                    "run_once_at": target.isoformat(),
                    "timezone": runtime_settings.timezone,
                    "source": "scheduled",
                    "reply_phone": phone,
                    "reply_chat_id": source.get("chat_id") or source.get("entity"),
                }
                job = CronJob(
                    name=name.strip() or f"once-{agent.id}-{uuid4().hex[:12]}",
                    agent_id=agent.id,
                    cron="@once",
                    payload=payload,
                    enabled=True,
                )
                db.add(job)
                await db.commit()
                await db.refresh(job)
                self.scheduler.upsert(job)
                return {
                    "ok": True,
                    "job_id": job.id,
                    "run_at": target.isoformat(),
                    "message": message,
                }

            registry.register(
                schedule_self,
                "schedule_self",
                "Schedule this agent to execute a one-time task at an ISO date and time",
            )

            async def schedule_self_list() -> dict[str, Any]:
                """List enabled cron/one-shot jobs owned by this agent."""
                jobs = (
                    await db.scalars(
                        select(CronJob).where(CronJob.agent_id == agent.id).order_by(CronJob.id.desc())
                    )
                ).all()
                return {
                    "jobs": [
                        {
                            "id": job.id,
                            "name": job.name,
                            "cron": job.cron,
                            "enabled": job.enabled,
                            "payload": job.payload or {},
                            "last_run_at": job.last_run_at.isoformat() if job.last_run_at else None,
                        }
                        for job in jobs[:50]
                    ]
                }

            async def schedule_self_cancel(job_id: int) -> dict[str, Any]:
                """Disable and remove one of this agent's scheduled jobs."""
                job = await db.get(CronJob, int(job_id))
                if job is None or job.agent_id != agent.id:
                    raise ValueError("Job not found for this agent")
                job.enabled = False
                await db.commit()
                if self.scheduler is not None:
                    self.scheduler.remove(job.id)
                return {"ok": True, "job_id": job.id}

            registry.register(schedule_self_list, "schedule_self_list", "List this agent's scheduled jobs")
            registry.register(
                schedule_self_cancel,
                "schedule_self_cancel",
                "Cancel one of this agent's scheduled jobs by id",
            )

        if db is not None:
            profile = await get_or_create_profile(db, agent.id)
            tools_set = set((agent.config or {}).get("tools") or [])
            if profile.autonomy_enabled or "employee" in tools_set or "autonomy" in tools_set:
                await self._register_employee_tools(registry, db, agent, profile)

        return registry

    async def _register_employee_tools(
        self,
        registry: ToolRegistry,
        db: AsyncSession,
        agent: Agent,
        profile: Any,
    ) -> None:
        from zoneinfo import ZoneInfo

        async def plan_get(horizon: str = "", plan_id: int = 0) -> dict[str, Any]:
            """Get active plans; optionally filter by horizon or plan id."""
            if plan_id:
                plan = await db.get(EmployeePlan, int(plan_id))
                if plan is None or plan.agent_id != agent.id:
                    return {"error": "plan not found"}
                return {"plan": plan_json(plan)}
            plans = await list_active_plans(db, agent.id)
            if horizon:
                plans = [p for p in plans if p.horizon == horizon]
            return {"plans": [plan_json(p) for p in plans]}

        async def plan_upsert(
            horizon: str,
            title: str,
            steps_json: str = "[]",
            plan_id: int = 0,
            status: str = "active",
        ) -> dict[str, Any]:
            """Create or update a plan for horizon hour|day|week|month. steps_json is JSON array of {id,title,status}."""
            if horizon not in HORIZONS:
                raise ValueError(f"horizon must be one of {HORIZONS}")
            try:
                steps = json.loads(steps_json) if steps_json else []
            except json.JSONDecodeError as exc:
                raise ValueError("steps_json must be valid JSON") from exc
            if not isinstance(steps, list):
                raise ValueError("steps_json must be a JSON array")
            normalized = []
            for idx, step in enumerate(steps[:40]):
                if not isinstance(step, dict):
                    continue
                normalized.append(
                    {
                        "id": str(step.get("id") or idx + 1),
                        "title": str(step.get("title") or "")[:300],
                        "status": str(step.get("status") or "todo"),
                        "result": str(step.get("result") or "")[:1000],
                    }
                )
            tz = ZoneInfo(profile.timezone or "UTC")
            start, end = period_bounds(horizon, utcnow(), tz)
            plan = await db.get(EmployeePlan, int(plan_id)) if plan_id else None
            if plan is None or plan.agent_id != agent.id:
                plan = EmployeePlan(
                    agent_id=agent.id,
                    horizon=horizon,
                    period_start=start,
                    period_end=end,
                    title=title.strip()[:300],
                    body={"steps": normalized},
                    status=status if status in {"draft", "active", "done", "cancelled"} else "active",
                )
                db.add(plan)
            else:
                plan.title = title.strip()[:300] or plan.title
                plan.body = {"steps": normalized}
                plan.status = status if status in {"draft", "active", "done", "cancelled"} else plan.status
                plan.horizon = horizon
            await db.commit()
            await db.refresh(plan)
            return {"plan": plan_json(plan)}

        async def plan_complete_step(plan_id: int, step_id: str, result: str = "") -> dict[str, Any]:
            """Mark a plan step as done."""
            plan = await db.get(EmployeePlan, int(plan_id))
            if plan is None or plan.agent_id != agent.id:
                raise ValueError("plan not found")
            body = dict(plan.body or {})
            steps = list(body.get("steps") or [])
            found = False
            for step in steps:
                if isinstance(step, dict) and str(step.get("id")) == str(step_id):
                    step["status"] = "done"
                    if result:
                        step["result"] = result[:1000]
                    found = True
                    break
            if not found:
                raise ValueError("step not found")
            body["steps"] = steps
            plan.body = body
            if steps and all(isinstance(s, dict) and s.get("status") == "done" for s in steps):
                plan.status = "done"
            await db.commit()
            await db.refresh(plan)
            return {"plan": plan_json(plan)}

        async def need_upsert(
            title: str,
            kind: str = "info",
            detail: str = "",
            priority: int = 5,
            need_id: int = 0,
            status: str = "open",
        ) -> dict[str, Any]:
            """Create or update an employee need/desire."""
            if kind not in NEED_KINDS:
                kind = "info"
            need = await db.get(EmployeeNeed, int(need_id)) if need_id else None
            if need is None or need.agent_id != agent.id:
                need = EmployeeNeed(
                    agent_id=agent.id,
                    kind=kind,
                    title=title.strip()[:300],
                    detail=detail.strip(),
                    priority=max(1, min(int(priority or 5), 10)),
                    status=status if status in {"open", "waiting", "satisfied", "dropped"} else "open",
                )
                db.add(need)
            else:
                need.title = title.strip()[:300] or need.title
                need.kind = kind
                need.detail = detail.strip() or need.detail
                need.priority = max(1, min(int(priority or need.priority), 10))
                if status in {"open", "waiting", "satisfied", "dropped"}:
                    need.status = status
            await db.commit()
            await db.refresh(need)
            return {"need": need_json(need)}

        async def need_satisfy(need_id: int, note: str = "") -> dict[str, Any]:
            """Mark a need as satisfied."""
            need = await db.get(EmployeeNeed, int(need_id))
            if need is None or need.agent_id != agent.id:
                raise ValueError("need not found")
            need.status = "satisfied"
            if note:
                need.detail = (need.detail or "") + f"\nResolved: {note.strip()}"
            await db.commit()
            await db.refresh(need)
            return {"need": need_json(need)}

        async def consult_manager(question: str, context: str = "", kind: str = "decision") -> dict[str, Any]:
            """Ask the manager a question via Telegram; wait for /answer on next ticks."""
            return await self.employee.create_consultation(
                db,
                agent,
                question=question,
                context=context,
                requires_approval=False,
                need_kind=kind if kind in NEED_KINDS else "decision",
            )

        async def request_approval(action_name: str, reason: str, context: str = "") -> dict[str, Any]:
            """Request manager approval before a dangerous action (telegram deletes, sip_dial, etc.)."""
            return await self.employee.create_consultation(
                db,
                agent,
                question=f"Approve action `{action_name}`: {reason}",
                context=context,
                requires_approval=True,
                action_name=action_name.strip(),
                need_kind="decision",
            )

        async def self_configure(section_key: str, content: str) -> dict[str, Any]:
            """Update an editable prompt section: self_notes, skills, or tone."""
            key = section_key.strip().lower()
            if key not in AGENT_EDITABLE_SECTIONS:
                raise PermissionError(
                    f"Employee can only edit {sorted(AGENT_EDITABLE_SECTIONS)}; "
                    f"{key} is manager-owned"
                )
            if key not in PROMPT_SECTION_KEYS:
                raise ValueError("Unknown section")
            row = await db.scalar(
                select(PromptSection).where(
                    PromptSection.agent_id == agent.id,
                    PromptSection.key == key,
                )
            )
            if row is None:
                row = PromptSection(agent_id=agent.id, key=key, content=content[:8000])
                db.add(row)
            else:
                row.content = content[:8000]
            await db.commit()
            return {"ok": True, "key": key, "chars": len(row.content)}

        async def employee_status() -> dict[str, Any]:
            """Return current mission, plans summary, needs and open consultations."""
            plans = await list_active_plans(db, agent.id)
            needs = await list_open_needs(db, agent.id)
            consults = await list_open_consultations(db, agent.id)
            return {
                "mission": profile.mission,
                "role_title": profile.role_title,
                "paused": profile.paused,
                "autonomy_enabled": profile.autonomy_enabled,
                "ticks_used_today": profile.ticks_used_today,
                "budget_ticks_per_day": profile.budget_ticks_per_day,
                "plans": [plan_json(p) for p in plans],
                "needs": [need_json(n) for n in needs],
                "consultations": [consultation_json(c) for c in consults],
            }

        registry.register(plan_get, "plan_get", "Get employee plans by horizon or id")
        registry.register(plan_upsert, "plan_upsert", "Create or update hour/day/week/month plan")
        registry.register(plan_complete_step, "plan_complete_step", "Complete a step in a plan")
        registry.register(need_upsert, "need_upsert", "Create or update a need/desire")
        registry.register(need_satisfy, "need_satisfy", "Mark a need as satisfied")
        registry.register(consult_manager, "consult_manager", "Ask the manager a question")
        registry.register(
            request_approval,
            "request_approval",
            "Request manager approval for a dangerous action",
        )
        registry.register(self_configure, "self_configure", "Update editable prompt sections")
        registry.register(employee_status, "employee_status", "Current employee focus and state")

    async def tick(
        self,
        db: AsyncSession,
        agent: Agent,
        *,
        force: bool = False,
        reason: str = "heartbeat",
    ) -> dict[str, Any]:
        if not agent.enabled:
            return {"ok": False, "skipped": True, "reason": "agent_disabled"}
        profile = await get_or_create_profile(db, agent.id)
        prepared = await self.employee.prepare_tick_context(db, agent, profile, force=force)
        if prepared.get("skip"):
            return {"ok": True, "skipped": True, "reason": prepared.get("reason")}
        await self.employee.mark_tick(db, profile)
        context = {
            "source": "employee_tick",
            "employee_tick": True,
            "force_tick": force,
            "tick_reason": reason,
            "user_id": f"employee:{agent.id}",
        }
        message = EMPLOYEE_TICK_INSTRUCTION
        result = await self.run(db, agent, message, context)
        try:
            await self.employee.maybe_send_daily_digest(db, agent, profile)
        except Exception:
            pass
        await self.events.publish(
            "employee.tick",
            {"agent_id": agent.id, "reason": reason, "force": force},
        )
        return {"ok": True, "skipped": False, "result": result}

    async def run(
        self,
        db: AsyncSession,
        agent: Agent,
        message: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        if not agent.enabled:
            raise RuntimeError("Agent is disabled")
        runtime_settings = await db.get(RuntimeSettings, 1)
        if runtime_settings is None:
            raise RuntimeError("Runtime settings are not initialized")
        if agent.llm_profile_id is None:
            raise RuntimeError("Agent has no LLM profile assigned")
        profile = await db.get(LlmProfile, agent.llm_profile_id)
        if profile is None:
            raise RuntimeError("Assigned LLM profile does not exist")
        if not profile.enabled:
            raise RuntimeError("Assigned LLM profile is disabled")
        api_key = SecretStore.from_settings(self.settings).decrypt(
            profile.api_key_ciphertext
        )
        if not api_key:
            raise RuntimeError("Assigned LLM profile has no API key")
        context = context or {}
        await self.events.publish("agent.started", {"agent_id": agent.id})
        user_id = str(context.get("user_id") or context.get("sender_id") or context.get("chat_id") or "global")
        if runtime_settings.memory_enabled:
            try:
                memories = await self.memory.search(
                    message, user_id=user_id, agent_id=str(agent.id), limit=8
                )
            except Exception:
                memories = []
        else:
            memories = []
        memory_context = "\n".join(
            f"- {item.get('memory', item.get('text', ''))}" for item in memories
        )
        account = (
            await db.get(TelegramAccount, agent.telegram_account_id)
            if agent.telegram_account_id is not None
            else None
        )
        mcp_server_names = set(await db.scalars(
            select(McpServer.name)
            .join(agent_mcp_servers, agent_mcp_servers.c.mcp_server_id == McpServer.id)
            .where(
                agent_mcp_servers.c.agent_id == agent.id,
                McpServer.enabled.is_(True),
            )
        ))
        # If no explicit attach rows, enable all connected MCP servers when agent has the mcp tool
        tools = set((agent.config or {}).get("tools") or [])
        if not mcp_server_names and "mcp" in tools:
            mcp_server_names = {
                name
                for name in (self.mcp.sessions if self.mcp else {})
            }
        client_options: dict[str, Any] = dict(
            api_key=api_key,
            base_url=profile.base_url,
            model=agent.model_name or profile.default_model,
            max_rounds=runtime_settings.max_tool_rounds,
        )
        if profile.http_proxy:
            client_options["http_proxy"] = profile.http_proxy
        client = LLMClient(**client_options)
        try:
            is_telegram = context.get("source") == "telegram" and account is not None

            async def summarize(prompt: str) -> str:
                return await client.complete(
                    [
                        {
                            "role": "system",
                            "content": (
                                "You summarize conversation history for future context. "
                                "Do not invent facts and retain temporal details."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    ToolRegistry(),
                    set(),
                )

            state = None
            inbound_at: datetime
            if is_telegram:
                conversation_context, state, inbound = await self.conversations.prepare(
                    db,
                    agent_id=agent.id,
                    account_id=account.id,
                    message=message,
                    context=context,
                    settings=runtime_settings,
                    summarizer=summarize,
                )
                inbound_at = as_utc(inbound.message_at or inbound.created_at)
            else:
                inbound_at = as_utc(context.get("message_at") or context.get("date"))
                db.add(
                    MessageLog(
                        agent_id=agent.id,
                        direction="in",
                        message_at=inbound_at,
                        text=message,
                        metadata_json=context,
                    )
                )
                await db.commit()
                conversation_context = self.conversations.temporal_context(runtime_settings)
            employee_profile = await get_or_create_profile(db, agent.id)
            system_prompt = await assemble_system_prompt(db, agent)
            employee_block = ""
            if employee_profile.autonomy_enabled or context.get("employee_tick"):
                from .employee import build_employee_context_block

                employee_block = build_employee_context_block(
                    employee_profile,
                    await list_active_plans(db, agent.id),
                    await list_open_needs(db, agent.id),
                    await list_open_consultations(db, agent.id),
                )
            is_employee_tick = bool(context.get("employee_tick") or context.get("source") == "employee_tick")
            messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "system",
                    "content": (
                        "Relevant long-term memories:\n" + memory_context
                        if memory_context
                        else "No relevant long-term memories were found."
                    ),
                },
                {
                    "role": "system",
                    "content": (
                        EMPLOYEE_TICK_INSTRUCTION
                        if is_employee_tick
                        else (
                            (
                                "Your final assistant message is delivered ONLY to the Telegram interlocutor. "
                                "Write a natural conversational reply for them. "
                                "Do not narrate internal tool calls, MCP/tracker operations, schedules, "
                                "channel joins, deletions, or other system actions to the interlocutor. "
                                "Never say things like 'я передвинул карточку', 'я выполнил действие', "
                                "or dump tool JSON into the chat. "
                                "The platform automatically reports mutating tool outcomes to administrators. "
                                "If a tool failure affects the answer, say briefly that you could not complete "
                                "the request — without technical internals. "
                                "Never claim an external action succeeded unless its tool call returned successfully. "
                                "When no Telegram reply should be sent, call telegram_suppress_reply. "
                                "Never describe silence in a message. "
                                f"If that tool is unavailable, return exactly {NO_TELEGRAM_REPLY}."
                            )
                            if is_telegram
                            else (
                                "Never claim that an external action succeeded unless its tool call "
                                "returned successfully. Report tool errors truthfully and explicitly."
                            )
                        )
                    ),
                },
                {"role": "system", "content": conversation_context},
            ]
            if employee_block:
                messages.append({"role": "system", "content": employee_block})
            messages.append({"role": "user", "content": message})
            permissions = resolve_tool_permissions(
                agent.config,
                employee_autonomy=bool(employee_profile.autonomy_enabled),
            )
            registry = await self.registry(
                agent,
                account.phone if account else None,
                mcp_server_names,
                runtime_settings.memory_enabled,
                db,
                runtime_settings,
                context,
            )
            if employee_profile.autonomy_enabled:
                async def _approval_gate(tool_name: str, arguments: dict[str, Any]) -> None:
                    if tool_name not in APPROVAL_REQUIRED_TOOLS:
                        return
                    ok = await self.employee.has_approval(db, agent.id, tool_name)
                    if not ok:
                        raise PermissionError(
                            f"Tool '{tool_name}' requires manager approval. "
                            f"Call request_approval(action_name='{tool_name}', reason=...) first."
                        )

                registry.before_call = _approval_gate
            result = await client.complete(
                messages,
                registry,
                permissions,
            )
            for call in registry.audit:
                safe_call = json.loads(json.dumps(call, ensure_ascii=False, default=str))
                db.add(
                    MessageLog(
                        agent_id=agent.id,
                        account_id=account.id if account else None,
                        direction="tool",
                        chat_id=str(context.get("chat_id") or "") or None,
                        message_at=as_utc(None),
                        text=f"{call['tool']}: {call['status']}",
                        metadata_json=safe_call,
                    )
                )
                await self.events.publish(
                    "tool.called",
                    {"agent_id": agent.id, **safe_call},
                )
            if registry.audit:
                await db.commit()
            admin_report = format_admin_action_report(
                agent_name=agent.name,
                audit=registry.audit,
                user_message=message,
                chat_id=context.get("chat_id"),
                sender_id=context.get("sender_id"),
                sender_username=(
                    str(context["sender_username"])
                    if context.get("sender_username")
                    else None
                ),
            )
            if admin_report:
                context["_admin_action_report"] = admin_report
                phone = (
                    (account.phone if account else None)
                    or context.get("reply_phone")
                    or context.get("phone")
                )
                if phone and self.telegram and self.telegram.admin_ids:
                    try:
                        exclude: set[int] = set()
                        sender = context.get("sender_id")
                        if sender is not None and str(sender).lstrip("-").isdigit():
                            exclude.add(int(sender))
                        sent_reports = await self.telegram.notify_admins(
                            str(phone),
                            admin_report,
                            exclude_ids=exclude,
                        )
                        context["_admin_action_report_sent"] = len(sent_reports)
                        db.add(
                            MessageLog(
                                agent_id=agent.id,
                                account_id=account.id if account else None,
                                direction="admin_report",
                                chat_id=str(context.get("chat_id") or "") or None,
                                user_id=str(context.get("sender_id") or "") or None,
                                message_at=as_utc(None),
                                text=admin_report,
                                metadata_json={
                                    "recipients": len(sent_reports),
                                    "excluded_sender": bool(exclude),
                                },
                            )
                        )
                        await db.commit()
                        await self.events.publish(
                            "telegram.admin_action_report",
                            {
                                "agent_id": agent.id,
                                "recipients": len(sent_reports),
                            },
                        )
                    except Exception as exc:
                        await self.events.publish(
                            "telegram.admin_action_report_failed",
                            {"agent_id": agent.id, "error": str(exc)},
                        )
            outbound_at = as_utc(None)
            suppressed = bool(context.get("_suppress_telegram_reply")) or (
                result.strip() == NO_TELEGRAM_REPLY
            )
            if suppressed:
                context["_suppress_telegram_reply"] = True
                context.setdefault(
                    "_suppress_telegram_reason",
                    "Agent returned the no-reply marker",
                )
            if runtime_settings.memory_enabled:
                try:
                    await self.memory.add(
                        (
                            f"User: {message}\nAssistant: [no reply sent]"
                            if suppressed
                            else f"User: {message}\nAssistant: {result}"
                        ),
                        user_id=user_id,
                        agent_id=str(agent.id),
                        metadata={
                            "kind": "exchange",
                            "agent_name": agent.name,
                            "chat_id": str(context.get("chat_id") or ""),
                            "inbound_at": iso_utc(inbound_at),
                            "outbound_at": iso_utc(outbound_at),
                            "last_message_at": iso_utc(outbound_at),
                        },
                    )
                except Exception:
                    pass
            if suppressed:
                db.add(
                    MessageLog(
                        agent_id=agent.id,
                        account_id=account.id if account else None,
                        direction="suppressed",
                        chat_id=str(context.get("chat_id") or "") or None,
                        user_id=str(context.get("sender_id") or "") or None,
                        message_id=str(context.get("message_id") or "") or None,
                        message_at=outbound_at,
                        text=str(context.get("_suppress_telegram_reason") or ""),
                        metadata_json={"source": "telegram", "suppressed": True},
                    )
                )
                await db.commit()
            elif state is not None:
                await self.conversations.record_outbound(
                    db, state, result, context, at=outbound_at
                )
            else:
                db.add(
                    MessageLog(
                        agent_id=agent.id,
                        direction="out",
                        message_at=outbound_at,
                        text=result,
                        metadata_json={"source": context.get("source", "runtime")},
                    )
                )
                await db.commit()
            await self.events.publish("agent.completed", {"agent_id": agent.id, "text": result})
            return result
        finally:
            close = getattr(client, "aclose", None)
            if close:
                await close()

    async def update_telegram_outbound(
        self,
        db: AsyncSession,
        context: dict[str, Any],
        sent: dict[str, Any] | list[dict[str, Any]],
    ) -> None:
        log_id = context.get("_outbound_log_id")
        if not log_id:
            return
        item = sent[-1] if isinstance(sent, list) and sent else sent
        if isinstance(item, dict):
            await self.conversations.update_outbound_delivery(db, int(log_id), item)
