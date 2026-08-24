import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import Settings
from .conversation import ConversationContextService, as_utc, iso_utc
from .db import (
    Agent,
    AgentLink,
    AgentTask,
    CronJob,
    EmployeeNeed,
    LlmProfile,
    McpServer,
    MessageLog,
    PromptSection,
    RuntimeSettings,
    TelegramAccount,
    agent_mcp_servers,
    utcnow,
)
from .action_reports import (
    cursor_result_ready_for_customer,
    format_admin_action_report,
    format_manager_status,
    is_internal_execution,
    should_redirect_customer_outbound,
)
from .job_result import (
    build_followup_payload,
    collect_origin_from_jobs,
    notes_from_audit,
    origin_chat_id,
    telegram_already_sent,
)
from .work_items import (
    after_agent_run,
    begin_customer_intake,
    bind_work_item,
    build_watchdog_instruction,
    compile_intake_brief,
    get_work_item,
    list_open_work_items,
    mark_intake_executing,
    sync_cursor_work_items,
    should_collect_customer_intake,
    watchdog_items,
)
from .employee import (
    AGENT_EDITABLE_SECTIONS,
    NEED_KINDS,
    PROMPT_SECTION_KEYS,
    EmployeeService,
    assemble_system_prompt,
    consultation_json,
    get_or_create_profile,
    list_agent_jobs,
    list_open_consultations,
    list_open_needs,
    need_json,
    save_once_job,
)
from .events import EventHub
from .integrations import (
    LLMClient,
    McpManager,
    MemoryStore,
    WebSearch,
    ingest_attachments_for_llm,
    llm_user_content,
)
from .employee_policy import (
    approval_required_for_tool,
    build_employee_tick_instruction,
    customer_intake_flush_instruction,
    customer_intake_instruction,
    customer_result_only_instruction,
    customer_telegram_instruction,
    intake_debounce_minutes,
    manager_telegram_instruction,
    normalize_action_name,
    pm_mode_enabled,
    pm_system_instruction,
)
from .memory_scope import (
    bind_conversation_from_config,
    build_memory_metadata,
    format_memory_hits,
    memory_scope_prompt,
    prefetch_memories,
    resolve_memory_scope,
)
from .sip_dial import (
    SipDialError,
    format_channel_context,
    sip_failure_admin_message,
    sip_failure_customer_message,
    validate_sip_dial_target,
)
from .tool_plane import attach_tool_plane
from .tools import (
    ToolRegistry,
    common_registry,
    effective_tool_name,
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
        self._agent_locks: dict[int, asyncio.Lock] = {}

    def _lock_for(self, agent_id: int) -> asyncio.Lock:
        lock = self._agent_locks.get(agent_id)
        if lock is None:
            lock = asyncio.Lock()
            self._agent_locks[agent_id] = lock
        return lock

    async def _cursorremote_session(
        self,
        db: AsyncSession,
        agent: Agent,
    ) -> Any | None:
        if self.mcp is None:
            return None
        mcp_server_names = set(
            await db.scalars(
                select(McpServer.name)
                .join(agent_mcp_servers, agent_mcp_servers.c.mcp_server_id == McpServer.id)
                .where(
                    agent_mcp_servers.c.agent_id == agent.id,
                    McpServer.enabled.is_(True),
                )
            )
        )
        tools = set((agent.config or {}).get("tools") or [])
        if not mcp_server_names and "mcp" in tools:
            mcp_server_names = {
                name
                for name in self.mcp.sessions
                if name != "cursorremote"
            }
        return next(
            (
                session
                for name, session in self.mcp.sessions.items()
                if name.lower() == "cursorremote"
                and (not mcp_server_names or name in mcp_server_names)
            ),
            None,
        )

    def bind_task_bus(self, task_bus: TaskBus) -> None:
        self.task_bus = task_bus

    def bind_scheduler(self, scheduler: Any) -> None:
        self.scheduler = scheduler
        self.employee.scheduler = scheduler

    def _guard_internal_customer_telegram(
        self,
        registry: ToolRegistry,
        agent: Agent,
        phone: str,
        context: dict[str, Any],
    ) -> None:
        """Redirect customer progress on internal runs to the manager."""
        import inspect

        admin_ids = set(self.telegram.admin_ids) if self.telegram else set()

        async def redirect_to_manager(text: str) -> dict[str, Any]:
            sent = await self._notify_manager_status(phone, agent, context, text)
            return {
                "ok": True,
                "redirected_to_manager": True,
                "customer_notified": False,
                "recipients": len(sent),
                "reason": (
                    "Customer receives only the finished result. "
                    "This progress note was sent to the manager."
                ),
            }

        send_tool = registry.tools.get("telegram_send_message")
        if send_tool is not None:
            inner_send = send_tool.function

            async def telegram_send_message(
                entity: Any,
                text: str,
                reply_to: int | None = None,
                *,
                humanize: bool = True,
            ) -> Any:
                if should_redirect_customer_outbound(
                    context, registry.audit, entity, admin_ids=admin_ids
                ):
                    return await redirect_to_manager(str(text or ""))
                result = inner_send(entity, text, reply_to=reply_to, humanize=humanize)
                return await result if inspect.isawaitable(result) else result

            registry.register(
                telegram_send_message,
                "telegram_send_message",
                send_tool.description,
                send_tool.parameters,
            )

        file_tool = registry.tools.get("telegram_send_file")
        if file_tool is not None:
            inner_file = file_tool.function

            async def telegram_send_file(entity: Any, file: str, caption: str = "") -> Any:
                if should_redirect_customer_outbound(
                    context, registry.audit, entity, admin_ids=admin_ids
                ):
                    note = caption.strip() or "Файл для заказчика (ещё не результат)."
                    return await redirect_to_manager(note)
                result = inner_file(entity, file, caption=caption)
                return await result if inspect.isawaitable(result) else result

            registry.register(
                telegram_send_file,
                "telegram_send_file",
                file_tool.description,
                file_tool.parameters,
            )

    async def _notify_manager_status(
        self,
        phone: str | None,
        agent: Agent,
        context: dict[str, Any],
        text: str,
    ) -> list[Any]:
        cleaned = (text or "").strip()
        if not cleaned or not phone or not self.telegram or not self.telegram.admin_ids:
            return []
        previous = str(context.get("_manager_status_text") or "").strip()
        if previous and previous == cleaned:
            return []
        body = format_manager_status(
            agent_name=agent.name,
            text=cleaned,
            work_item_id=context.get("work_item_id"),
            source=context.get("source"),
        )
        try:
            sent = await self.telegram.notify_admins(str(phone), body)
        except Exception:
            return []
        context["_manager_status_sent"] = True
        context["_manager_status_text"] = cleaned
        return list(sent)

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
        if memory_enabled and context is not None:
            async def memory_add(
                text: str,
                category: str = "note",
                project_id: str = "",
                customer_id: str = "",
                global_scope: bool = False,
            ) -> dict[str, Any]:
                """Store a structured long-term memory for the current user and project scope."""
                active = context.get("_memory_scope") or resolve_memory_scope(
                    context,
                    agent,
                    state=context.get("_conversation_state"),
                )
                metadata = build_memory_metadata(
                    active,
                    category=category,
                    global_scope=global_scope,
                )
                if project_id.strip():
                    metadata["project_id"] = project_id.strip()
                if customer_id.strip():
                    metadata["customer_id"] = customer_id.strip()
                if global_scope:
                    metadata.pop("project_id", None)
                item = await self.memory.add(
                    text,
                    user_id=active.user_id,
                    agent_id=active.agent_id,
                    metadata=metadata,
                )
                return {"ok": True, "id": item.get("id"), "metadata": metadata}

            async def memory_search(
                query: str,
                category: str = "",
                project_id: str = "",
                include_global: bool = True,
                limit: int = 10,
            ) -> list[dict[str, Any]]:
                """Search long-term memory for the current user, optionally filtered by project/category."""
                active = context.get("_memory_scope") or resolve_memory_scope(
                    context,
                    agent,
                    state=context.get("_conversation_state"),
                )
                filters: dict[str, Any] = {}
                if category.strip():
                    filters["category"] = category.strip().lower()
                target_project = project_id.strip() or active.project_id
                return await self.memory.search_scoped(
                    query,
                    user_id=active.user_id,
                    agent_id=active.agent_id,
                    project_id=target_project,
                    filters=filters or None,
                    include_global=include_global,
                    limit=limit,
                )

            async def memory_set_project(
                project_id: str,
                customer_id: str = "",
            ) -> dict[str, Any]:
                """Bind the current conversation to a project/customer for scoped memory."""
                state = context.get("_conversation_state")
                if state is None:
                    raise RuntimeError("memory_set_project requires an active conversation")
                if db is None:
                    raise RuntimeError("Database session is required for memory_set_project")
                normalized_project = project_id.strip()
                if not normalized_project:
                    raise ValueError("project_id must not be empty")
                state.project_id = normalized_project
                if customer_id.strip():
                    state.customer_id = customer_id.strip()
                await db.flush()
                context["project_id"] = normalized_project
                if customer_id.strip():
                    context["customer_id"] = customer_id.strip()
                context["_memory_scope"] = resolve_memory_scope(context, agent, state=state)
                return {
                    "ok": True,
                    "project_id": state.project_id,
                    "customer_id": state.customer_id,
                }

            registry.register(memory_add, "memory_add", "Store a structured long-term memory fact")
            registry.register(
                memory_search,
                "memory_search",
                "Search long-term memory scoped to the current project and user",
            )
            registry.register(
                memory_set_project,
                "memory_set_project",
                "Bind the current conversation to a project/customer for scoped memory",
            )
        elif memory_enabled:
            registry.register(self.memory.search, "memory_search", "Search long-term memory")
            registry.register(self.memory.add, "memory_add", "Store long-term memory")
        if phone and self.telegram:
            telegram_tools = self.telegram.tool_registry(phone)
            registry.tools.update(telegram_tools.tools)
            if context is not None:
                self._guard_internal_customer_telegram(registry, agent, phone, context)
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
            async def sip_dial(
                number: str = "",
                purpose: str = "",
                opening: str = "",
            ) -> dict[str, Any]:
                """Place an outbound phone call via the agent's SIP account and talk with OpenAI Realtime."""
                from .db import SipAccount

                if db is None:
                    raise RuntimeError("Database session is required for sip_dial")
                account = await db.get(SipAccount, agent.sip_account_id)
                if account is None:
                    raise RuntimeError("SIP account not found")
                target = str(number or "").strip()
                ctx = context or {}
                if not target and ctx.get("source") == "telegram" and phone and self.telegram:
                    sender = ctx.get("sender_id") or ctx.get("chat_id")
                    if sender is not None:
                        target = await self.telegram.get_user_phone(phone, sender) or ""
                interlocutor = str(ctx.get("sender_username") or "").strip()
                try:
                    normalized = validate_sip_dial_target(target, ctx)
                    user_message = str(
                        ctx.get("_user_message") or ctx.get("text") or ctx.get("message") or ""
                    )
                    channel_context = format_channel_context(
                        ctx.get("telegram_history") if isinstance(ctx.get("telegram_history"), list) else [],
                        user_message,
                    )
                    await self.sip.dial(
                        account=account,
                        agent=agent,
                        number=normalized,
                        purpose=purpose,
                        opening=opening,
                        interlocutor=interlocutor,
                        channel_context=channel_context,
                        current_message=user_message,
                    )
                    if ctx.get("source") == "telegram":
                        ctx["_suppress_telegram_reply"] = True
                        ctx["_suppress_telegram_reason"] = "Outbound SIP call connected"
                    return {"ok": True, "telegram_reply": None}
                except Exception as exc:
                    is_customer = (
                        ctx.get("source") == "telegram" and not ctx.get("is_admin")
                    )
                    payload: dict[str, Any] = {
                        "ok": False,
                        "customer_reply": sip_failure_customer_message(exc),
                    }
                    if is_customer:
                        payload["detail"] = payload["customer_reply"]
                    else:
                        payload["detail"] = sip_failure_admin_message(
                            exc,
                            number=target or number,
                        )
                    if isinstance(exc, SipDialError):
                        payload["error_code"] = "invalid_number"
                    elif "403" in str(exc):
                        payload["error_code"] = "operator_rejected"
                    else:
                        payload["error_code"] = "dial_failed"
                    return payload

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

            registry.register(
                sip_dial,
                "sip_dial",
                (
                    "Place an outbound phone call. Always pass purpose: who you call, why, "
                    "what to achieve, and facts from this chat. Pass opening: first spoken sentence. "
                    "Never pass Telegram sender_id as the number. "
                    "On success the platform talks on the phone — do not write anything to Telegram. "
                    "On failure, reply using customer_reply from the tool result."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "number": {
                            "type": "string",
                            "description": "Mobile number like 79001234567, not a Telegram id",
                        },
                        "purpose": {
                            "type": "string",
                            "description": (
                                "Call briefing for the voice agent: goal, who the person is, "
                                "what already discussed in Telegram, what to say/ask/close"
                            ),
                        },
                        "opening": {
                            "type": "string",
                            "description": "First sentence to speak after they pick up, in character",
                        },
                    },
                },
            )
            registry.register(sip_hangup, "sip_hangup", "Hang up an active SIP call")
            registry.register(sip_status, "sip_status", "SIP registration and active call status")
        if db is not None and (context or {}).get("_pm_mode"):
            from .db import Consultation, CursorRun, DecisionRecord, WorkItem
            from .pm_state import (
                TaskContract,
                apply_task_contract,
                can_transition,
                get_or_create_project_state,
                is_task_ready,
                readiness_issues,
                record_decision,
                record_scope_change,
                transition_pm_phase,
            )
            from .work_items import (
                add_event,
                create_work_item,
                get_work_item,
                list_events,
                work_item_json,
            )

            async def pm_structure_task(
                project_id: str,
                task_type: str,
                title: str,
                requirements: list[str],
                acceptance_criteria: list[str],
                context_json: dict[str, Any] | None = None,
                constraints: list[str] | None = None,
                edge_cases: list[str] | None = None,
                dependencies: list[str] | None = None,
                related_tasks: list[str] | None = None,
                priority: str = "normal",
                create_new_task: bool = False,
            ) -> dict[str, Any]:
                """Create or update the current deterministic PM task. Never call for an idea."""
                source_message_id = str((context or {}).get("message_id") or "") or None
                item = None
                if source_message_id:
                    item = await db.scalar(
                        select(WorkItem).where(
                            WorkItem.agent_id == agent.id,
                            WorkItem.source == str((context or {}).get("source") or ""),
                            WorkItem.chat_id == str((context or {}).get("chat_id") or ""),
                            WorkItem.source_message_id == source_message_id,
                        )
                    )
                    if item is not None:
                        if context is not None:
                            context["work_item_id"] = item.id
                        return {
                            "ok": True,
                            "duplicate": True,
                            "task": work_item_json(item),
                        }
                if not create_new_task:
                    item = await get_work_item(
                        db, (context or {}).get("work_item_id")
                    )
                if item is None:
                    try:
                        item = await create_work_item(
                            db,
                            agent,
                            title=title,
                            goal=str((context_json or {}).get("business_reason") or title),
                            context=context,
                            source=str((context or {}).get("source") or "manual"),
                            commit=False,
                        )
                    except IntegrityError:
                        await db.rollback()
                        item = await db.scalar(
                            select(WorkItem).where(
                                WorkItem.agent_id == agent.id,
                                WorkItem.source
                                == str((context or {}).get("source") or ""),
                                WorkItem.chat_id
                                == str((context or {}).get("chat_id") or ""),
                                WorkItem.source_message_id == source_message_id,
                            )
                        )
                        if item is None:
                            raise
                        return {
                            "ok": True,
                            "duplicate": True,
                            "task": work_item_json(item),
                        }
                    if context is not None:
                        context["work_item_id"] = item.id
                normalized_task_context = dict(context_json or {})
                approval = (
                    await db.get(
                        Consultation,
                        int((context or {}).get("consultation_id")),
                    )
                    if (context or {}).get("consultation_id")
                    else None
                )
                owner_approval_verified = bool(
                    approval is not None
                    and approval.status == "approved"
                    and approval.agent_id == agent.id
                    and approval.work_item_id == item.id
                    and approval.answered_by
                    and approval.answered_by == (context or {}).get("approved_by")
                )
                normalized_task_context["owner_approved"] = bool(
                    normalized_task_context.get("owner_approved")
                    and owner_approval_verified
                )
                if normalized_task_context["owner_approved"]:
                    normalized_task_context["owner_approval"] = {
                        "consultation_id": (context or {}).get("consultation_id"),
                        "approved_by": (context or {}).get("approved_by"),
                    }
                contract = TaskContract(
                    task_id=str(item.id),
                    project_id=project_id,
                    type=task_type,
                    title=title,
                    context=normalized_task_context,
                    requirements=requirements,
                    acceptance_criteria=acceptance_criteria,
                    constraints=constraints or [],
                    edge_cases=edge_cases or [],
                    dependencies=dependencies or [],
                    related_tasks=related_tasks or [],
                    priority=priority,
                    source={
                        "channel": (context or {}).get("source"),
                        "conversation_id": (context or {}).get("chat_id"),
                        "message_id": source_message_id,
                        "client_id": (context or {}).get("client_id")
                        or (context or {}).get("sender_id"),
                    },
                )
                stored_pm = (
                    (item.metadata_json or {}).get("pm")
                    if isinstance((item.metadata_json or {}).get("pm"), dict)
                    else {}
                )
                stored_source = (
                    stored_pm.get("source")
                    if isinstance(stored_pm.get("source"), dict)
                    else {}
                )
                if (
                    source_message_id
                    and str(stored_source.get("message_id") or "") == source_message_id
                ):
                    return {
                        "ok": True,
                        "duplicate": True,
                        "task": work_item_json(item),
                    }
                if item.pm_phase in {
                    "IN_DEVELOPMENT",
                    "DEV_COMPLETE",
                    "QA",
                    "CLIENT_REVIEW",
                    "DONE",
                    "CANCELLED",
                }:
                    event = await record_scope_change(
                        db,
                        item,
                        detail=title,
                        source_message_id=source_message_id,
                    )
                    await db.commit()
                    return {
                        "ok": False,
                        "change_request": True,
                        "event_id": event.id,
                        "reason": (
                            "The existing task is already in development or review. "
                            "Its approved requirements were not changed."
                        ),
                        "task": work_item_json(item),
                    }
                apply_task_contract(item, contract)
                await get_or_create_project_state(db, project_id)
                await add_event(
                    db,
                    item,
                    kind="requirements",
                    title="Structured requirements saved",
                    detail=f"{len(contract.requirements)} requirements, "
                    f"{len(contract.acceptance_criteria)} acceptance criteria",
                    payload={"source_message_id": source_message_id},
                )
                target = "REQUIREMENTS_READY" if is_task_ready(item) else "CLARIFICATION"
                if item.pm_phase != target:
                    if not can_transition(item.pm_phase, target) and item.pm_phase not in {
                        "DISCUSSION",
                        "CLARIFICATION",
                        "CHANGES_REQUESTED",
                    }:
                        await record_scope_change(
                            db,
                            item,
                            detail="Structured task changed after its requirements phase",
                            source_message_id=source_message_id,
                        )
                        if item.pm_phase in {"DONE", "CANCELLED"}:
                            raise ValueError(
                                "Closed task cannot be changed; create a separate change request"
                            )
                        await transition_pm_phase(
                            db,
                            item,
                            "CHANGES_REQUESTED",
                            detail="Requirements changed",
                        )
                    await transition_pm_phase(
                        db,
                        item,
                        target,
                        detail="Structured requirements saved",
                        payload={"source_message_id": source_message_id},
                    )
                await db.commit()
                return {
                    "ok": True,
                    "duplicate": item.source_message_id == source_message_id
                    and item.id != (context or {}).get("work_item_id"),
                    "task": work_item_json(item),
                }

            async def pm_get_task(work_item_id: int = 0) -> dict[str, Any]:
                """Read authoritative task, decision, run, and audit state."""
                item = await get_work_item(
                    db, work_item_id or (context or {}).get("work_item_id")
                )
                if item is None or item.agent_id != agent.id:
                    raise ValueError("PM task not found")
                events = list(
                    reversed(await list_events(db, item.id, limit=120))
                )
                decisions = (
                    await db.scalars(
                        select(DecisionRecord)
                        .where(DecisionRecord.work_item_id == item.id)
                        .order_by(DecisionRecord.created_at)
                    )
                ).all()
                runs = (
                    await db.scalars(
                        select(CursorRun)
                        .where(CursorRun.work_item_id == item.id)
                        .order_by(CursorRun.attempt)
                    )
                ).all()
                return {
                    "task": work_item_json(item, events=events),
                    "decisions": [
                        {
                            "id": row.id,
                            "topic": row.topic,
                            "decision": row.decision,
                            "confirmed_by": row.confirmed_by,
                        }
                        for row in decisions
                    ],
                    "cursor_runs": [
                        {
                            "id": row.id,
                            "attempt": row.attempt,
                            "status": row.status,
                            "error": row.error,
                        }
                        for row in runs
                    ],
                }

            async def pm_record_decision(
                project_id: str,
                topic: str,
                decision: str,
                confirmed_by: str = "",
                work_item_id: int = 0,
            ) -> dict[str, Any]:
                """Persist a confirmed project decision idempotently."""
                target_work_item_id = work_item_id or (context or {}).get("work_item_id")
                target_item = await get_work_item(db, target_work_item_id)
                if (
                    target_item is None
                    or target_item.agent_id != agent.id
                    or str(target_item.project_id or "") != project_id
                ):
                    raise PermissionError(
                        "Decision must reference this agent's task in the same project"
                    )
                row = await record_decision(
                    db,
                    project_id=project_id,
                    topic=topic,
                    decision=decision,
                    confirmed_by=confirmed_by,
                    source_message_id=str((context or {}).get("message_id") or "") or None,
                    work_item_id=target_item.id,
                )
                if getattr(row, "_pm_created", False):
                    await add_event(
                        db,
                        target_item,
                        kind="decision",
                        title=topic,
                        detail=decision,
                        payload={"decision_id": row.id},
                    )
                await db.commit()
                return {"ok": True, "decision_id": row.id}

            async def pm_transition_task(
                to_phase: str,
                detail: str = "",
                work_item_id: int = 0,
            ) -> dict[str, Any]:
                """Apply an allowed PM lifecycle transition and append its audit event."""
                item = await get_work_item(
                    db, work_item_id or (context or {}).get("work_item_id")
                )
                if item is None or item.agent_id != agent.id:
                    raise ValueError("PM task not found")
                if to_phase in {"IN_DEVELOPMENT", "DEV_COMPLETE", "QA", "DONE"}:
                    raise ValueError(
                        f"{to_phase} is controlled by the development/QA adapter"
                    )
                transition_payload: dict[str, Any] = {}
                if to_phase == "CLIENT_CONFIRMED":
                    internal_sources = {
                        "employee_tick",
                        "employee_heartbeat",
                        "scheduled",
                        "consult_resolved",
                        "intake_flush",
                    }
                    source = str((context or {}).get("source") or "")
                    message_id = str((context or {}).get("message_id") or "")
                    client_id = str(
                        (context or {}).get("client_id")
                        or (context or {}).get("sender_id")
                        or ""
                    )
                    if (
                        source in internal_sources
                        or (context or {}).get("is_admin")
                        or not message_id
                        or not client_id
                    ):
                        raise PermissionError(
                            "CLIENT_CONFIRMED requires an identifiable current client message"
                        )
                    transition_payload = {
                        "confirmed_by": client_id,
                        "source_message_id": message_id,
                    }
                if to_phase in {"REQUIREMENTS_READY", "CLIENT_CONFIRMED", "READY_FOR_DEV"}:
                    issues = readiness_issues(item)
                    if issues:
                        raise ValueError("; ".join(issues))
                if to_phase == "CANCELLED" and item.active_cursor_run_id:
                    active_run = await db.get(CursorRun, item.active_cursor_run_id)
                    if active_run is not None and active_run.status in {"pending", "running"}:
                        raise PermissionError(
                            "Cannot cancel while Cursor is active without confirmed "
                            "remote termination"
                        )
                await transition_pm_phase(
                    db,
                    item,
                    to_phase,
                    detail=detail,
                    payload=transition_payload,
                )
                if to_phase == "CANCELLED" and item.active_cursor_run_id:
                    item.active_cursor_run_id = None
                await db.commit()
                return {"ok": True, "task": work_item_json(item)}

            registry.register(
                pm_structure_task,
                "pm_structure_task",
                "Create or update a validated structured development task; never use for an idea.",
                parameters={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "task_type": {
                            "type": "string",
                            "enum": ["feature", "bug", "change", "technical"],
                        },
                        "title": {"type": "string"},
                        "requirements": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "acceptance_criteria": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "context_json": {"type": "object"},
                        "constraints": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "edge_cases": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "dependencies": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "related_tasks": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "priority": {
                            "type": "string",
                            "enum": ["critical", "high", "normal", "low"],
                        },
                        "create_new_task": {
                            "type": "boolean",
                            "description": (
                                "True only for a separate new requirement; false for "
                                "clarification of the current task."
                            ),
                        },
                    },
                    "required": [
                        "project_id",
                        "task_type",
                        "title",
                        "requirements",
                        "acceptance_criteria",
                    ],
                },
            )
            registry.register(pm_get_task, "pm_get_task")
            registry.register(pm_record_decision, "pm_record_decision")
            registry.register(pm_transition_task, "pm_transition_task")
        cursor_state = {"finished": False, "prompt_sent": False}
        if self.mcp and mcp_server_names:
            await self.mcp.register_tools(registry, mcp_server_names)
            if (context or {}).get("_pm_mode"):
                # PM agents use the structured adapter below, not arbitrary Cursor prompts.
                for tool_name in list(registry.tools):
                    lowered = tool_name.lower()
                    if lowered in {"mcp_cursorremote_tools", "mcp_cursorremote_run"}:
                        registry.tools.pop(tool_name, None)
            cursor_session = next(
                (
                    session
                    for name, session in self.mcp.sessions.items()
                    if name.lower() == "cursorremote" and (
                        mcp_server_names is None or name in mcp_server_names
                    )
                ),
                None,
            )
            if cursor_session is not None:
                from .cursorremote_drive import (
                    CURSOR_CHECK_ONLY_MESSAGE,
                    check_and_drive,
                    pin_cursor_followup_message,
                    send_prompt_and_drive,
                )
                from .cursor_assets import collect_images_for_cursor

                async def cursorremote_do(prompt: str) -> dict[str, Any]:
                    """Send a task to Cursor IDE, auto-click Allow/Accept/Run, wait until it actually finishes or times out."""
                    from .work_items import get_work_item

                    item = await get_work_item(db, (context or {}).get("work_item_id"))
                    if (context or {}).get("_intake_collecting") or (
                        item is not None and item.status == "collecting"
                    ):
                        return {
                            "ok": False,
                            "done": False,
                            "skipped_prompt": True,
                            "prompt_sent": False,
                            "reason": (
                                "Still collecting the customer's assignment. "
                                "Do not start Cursor. Reply naturally and do not mention a wait."
                            ),
                        }
                    is_fresh_assignment = bool((context or {}).get("_intake_flush")) or str(
                        (context or {}).get("source") or ""
                    ) in {"intake_flush", "consult_resolved"}
                    in_flight = (
                        not is_fresh_assignment
                        and item is not None
                        and (
                            item.status == "waiting_external"
                            or bool((item.metadata_json or {}).get("cursor_in_flight"))
                        )
                    )
                    already_sent = bool(cursor_state.get("prompt_sent"))
                    if in_flight or already_sent:
                        result = await check_and_drive(cursor_session)
                        result = {
                            **result,
                            "skipped_prompt": True,
                            "prompt_sent": False,
                            "reason": (
                                "This case already has a Cursor job. "
                                "Did not send another prompt (that would duplicate the task). "
                                "If done=true, report the result. If done=false, wait — "
                                "search/explore is not a stop."
                            ),
                            "next": result.get("next") or CURSOR_CHECK_ONLY_MESSAGE,
                        }
                        if result.get("done"):
                            cursor_state["finished"] = True
                        return result
                    result = await send_prompt_and_drive(
                        cursor_session,
                        prompt,
                        attachments=collect_images_for_cursor(context, item),
                        work_item_id=(item.id if item is not None else context.get("work_item_id")),
                        public_base_url=self.settings.public_base_url,
                        secret_key=self.settings.secret_key.get_secret_value(),
                    )
                    if result.get("prompt_sent"):
                        cursor_state["prompt_sent"] = True
                        from .work_items import stamp_cursor_prompt_sent

                        await stamp_cursor_prompt_sent(db, item)
                    if result.get("done"):
                        cursor_state["finished"] = True
                    return result

                async def cursorremote_check() -> dict[str, Any]:
                    """Poll Cursor for an already sent task: click Allow if needed, return done=true only when idle after work."""
                    result = await check_and_drive(cursor_session)
                    if result.get("done"):
                        cursor_state["finished"] = True
                    return result

                if (context or {}).get("_pm_mode") and db is not None:
                    from .db import CursorRun
                    from .pm_state import (
                        get_or_create_cursor_run,
                        get_or_create_project_state,
                        is_task_ready,
                        render_task_brief,
                        submission_requires_approval,
                        transition_pm_phase,
                        update_cursor_run,
                    )
                    from .work_items import add_event, get_work_item, work_item_json

                    async def _pm_cursor_result(
                        item: Any,
                        run: CursorRun,
                        result: dict[str, Any],
                    ) -> dict[str, Any]:
                        if not result.get("done"):
                            await update_cursor_run(db, run, status="running", result=result)
                            await db.commit()
                            return {
                                "task_id": str(item.id),
                                "run_id": run.id,
                                "status": "in_progress",
                                "done": False,
                            }
                        structured: dict[str, Any] | None = None
                        candidates = [result.get("result"), result.get("summary")]
                        candidates.extend(
                            reversed(result.get("messages") or [])
                            if isinstance(result.get("messages"), list)
                            else []
                        )
                        from .pm_state import parse_cursor_result

                        for candidate in candidates:
                            if isinstance(candidate, dict):
                                candidate = candidate.get("content") or candidate.get("text") or candidate
                            try:
                                structured = parse_cursor_result(candidate)
                                break
                            except (TypeError, ValueError):
                                continue
                        if structured is not None and str(
                            structured.get("task_id") or ""
                        ) != str(item.id):
                            structured = None
                        if structured is None:
                            structured = {
                                "status": "blocked",
                                "implementation": {"summary": str(result.get("summary") or "")},
                                "verification": {},
                                "questions": [
                                    "Cursor returned no valid structured completion payload."
                                ],
                                "risks": [],
                                "limitations": [],
                            }
                        cursor_state["finished"] = True
                        await update_cursor_run(
                            db,
                            run,
                            status=structured["status"],
                            result=structured,
                            error=(
                                "Invalid or blocked Cursor result"
                                if structured["status"] != "completed"
                                else None
                            ),
                        )
                        if structured["status"] == "completed":
                            await transition_pm_phase(
                                db, item, "DEV_COMPLETE", detail="Cursor development complete"
                            )
                            await transition_pm_phase(
                                db, item, "QA", detail="Result awaits acceptance checks"
                            )
                            item.status = "in_progress"
                            item.wait_owner = "self"
                            item.next_action = (
                                "Verify acceptance criteria and explicitly accept QA"
                            )
                            item.last_error = None
                        else:
                            await transition_pm_phase(
                                db,
                                item,
                                "BLOCKED",
                                detail="Cursor result blocked or invalid",
                            )
                            item.status = "failed"
                            item.wait_owner = "self"
                            item.next_action = "Review Cursor result or request a fix"
                            item.last_error = "Cursor result blocked, failed, or invalid"
                        await db.commit()
                        return {
                            "task_id": str(item.id),
                            "run_id": run.id,
                            "status": structured["status"],
                            "done": structured["status"] == "completed",
                            "qa_required": structured["status"] == "completed",
                            "result": structured,
                        }

                    async def _submit_pm_item(
                        item: Any,
                        *,
                        fix_request: str = "",
                    ) -> dict[str, Any]:
                        if not is_task_ready(item):
                            raise ValueError("Task requirements and acceptance criteria are incomplete")
                        project = await get_or_create_project_state(
                            db, item.project_id or f"agent-{agent.id}"
                        )
                        confirmed = item.pm_phase in {
                            "CLIENT_CONFIRMED",
                            "READY_FOR_DEV",
                            "CHANGES_REQUESTED",
                        }
                        inside_scope = bool(
                            (item.context_json or {}).get(
                                "inside_agreed_scope",
                                confirmed,
                            )
                        )
                        small_fix = bool((item.context_json or {}).get("small_fix"))
                        high_risk = bool((item.context_json or {}).get("high_risk"))
                        owner_approved = bool(
                            (item.context_json or {}).get("owner_approved")
                        )
                        if high_risk and not owner_approved:
                            raise PermissionError(
                                "High-risk development requires explicit owner approval"
                            )
                        if submission_requires_approval(
                            project.autonomy_level,
                            task_type=item.task_type,
                            client_confirmed=confirmed,
                            inside_agreed_scope=inside_scope,
                            small_fix=small_fix,
                            high_risk=False,
                        ) and not confirmed:
                            raise PermissionError(
                                "Project autonomy requires client confirmation before development"
                            )
                        if item.pm_phase in {"REQUIREMENTS_READY", "CLIENT_CONFIRMED", "CHANGES_REQUESTED"}:
                            await transition_pm_phase(
                                db, item, "READY_FOR_DEV", detail="Development gate passed"
                            )
                        if item.pm_phase != "READY_FOR_DEV":
                            raise ValueError(
                                f"Task phase {item.pm_phase} cannot be submitted for development"
                            )
                        attempt = int(
                            await db.scalar(
                                select(func.count())
                                .select_from(CursorRun)
                                .where(CursorRun.work_item_id == item.id)
                            )
                            or 0
                        ) + 1
                        brief = render_task_brief(item)
                        if fix_request:
                            brief += f"\n## Required fix\n{fix_request}\n"
                        brief += (
                            "\nReturn a JSON object with task_id, status "
                            "(completed|blocked|failed), implementation {summary, files_changed, tests}, "
                            "verification {tests_passed, lint_passed, acceptance_criteria: "
                            "[{criterion, passed, evidence}]}, questions, risks, and limitations. "
                            "Include one evidence entry for every acceptance criterion exactly as written."
                        )
                        run, created = await get_or_create_cursor_run(
                            db,
                            item,
                            attempt=attempt,
                            request={"brief": brief, "fix_request": fix_request},
                        )
                        if not created:
                            return {
                                "task_id": str(item.id),
                                "run_id": run.id,
                                "status": run.status,
                                "duplicate": True,
                            }
                        await transition_pm_phase(
                            db, item, "IN_DEVELOPMENT", detail=f"Cursor run #{attempt} started"
                        )
                        await update_cursor_run(db, run, status="running")
                        item.status = "waiting_external"
                        item.wait_owner = "external"
                        item.next_action = "Wait for structured Cursor result"
                        await db.commit()
                        try:
                            result = await send_prompt_and_drive(
                                cursor_session,
                                brief,
                                attachments=collect_images_for_cursor(context, item),
                                work_item_id=item.id,
                                public_base_url=self.settings.public_base_url,
                                secret_key=self.settings.secret_key.get_secret_value(),
                            )
                        except Exception as exc:
                            await update_cursor_run(
                                db, run, status="failed", error=str(exc)[:2000]
                            )
                            await transition_pm_phase(
                                db, item, "BLOCKED", detail="Cursor unavailable or timed out"
                            )
                            item.status = "failed"
                            item.last_error = str(exc)[:2000]
                            await db.commit()
                            raise
                        return await _pm_cursor_result(item, run, result)

                    async def submit_development_task(
                        work_item_id: int = 0,
                    ) -> dict[str, Any]:
                        """Submit only a ready stored task to Cursor using the structured contract."""
                        item = await get_work_item(
                            db, work_item_id or (context or {}).get("work_item_id")
                        )
                        if item is None or item.agent_id != agent.id:
                            raise ValueError("PM task not found")
                        return await _submit_pm_item(item)

                    async def get_development_status(
                        work_item_id: int = 0,
                    ) -> dict[str, Any]:
                        """Poll the single active Cursor run and persist its state."""
                        item = await get_work_item(
                            db, work_item_id or (context or {}).get("work_item_id")
                        )
                        if (
                            item is None
                            or item.agent_id != agent.id
                            or not item.active_cursor_run_id
                        ):
                            raise ValueError("No active Cursor run")
                        run = await db.get(CursorRun, item.active_cursor_run_id)
                        if run is None:
                            raise ValueError("Cursor run not found")
                        if run.status in {"completed", "blocked", "failed", "cancelled"}:
                            return {
                                "task": work_item_json(item),
                                "run_id": run.id,
                                "status": run.status,
                                "result": run.result_json,
                            }
                        return await _pm_cursor_result(
                            item, run, await check_and_drive(cursor_session)
                        )

                    async def request_development_fix(
                        fix_request: str,
                        work_item_id: int = 0,
                    ) -> dict[str, Any]:
                        """Start a new attempt for failed acceptance criteria, not a duplicate task."""
                        item = await get_work_item(
                            db, work_item_id or (context or {}).get("work_item_id")
                        )
                        if item is None or item.agent_id != agent.id:
                            raise ValueError("PM task not found")
                        if item.pm_phase in {"QA", "CLIENT_REVIEW"}:
                            await transition_pm_phase(
                                db, item, "CHANGES_REQUESTED", detail=fix_request
                            )
                        elif item.pm_phase == "BLOCKED":
                            await transition_pm_phase(
                                db, item, "READY_FOR_DEV", detail=fix_request
                            )
                        return await _submit_pm_item(item, fix_request=fix_request)

                    async def get_development_result(
                        work_item_id: int = 0,
                    ) -> dict[str, Any]:
                        """Return the persisted structured Cursor result without changing state."""
                        item = await get_work_item(
                            db, work_item_id or (context or {}).get("work_item_id")
                        )
                        if (
                            item is None
                            or item.agent_id != agent.id
                            or not item.active_cursor_run_id
                        ):
                            raise ValueError("No Cursor result for this task")
                        run = await db.get(CursorRun, item.active_cursor_run_id)
                        if run is None:
                            raise ValueError("Cursor run not found")
                        return {
                            "task_id": str(item.id),
                            "run_id": run.id,
                            "status": run.status,
                            "result": run.result_json,
                            "error": run.error,
                        }

                    async def pm_accept_task(
                        work_item_id: int = 0,
                    ) -> dict[str, Any]:
                        """Accept QA only when the latest structured verification passed."""
                        item = await get_work_item(
                            db, work_item_id or (context or {}).get("work_item_id")
                        )
                        if (
                            item is None
                            or item.agent_id != agent.id
                            or item.pm_phase not in {"QA", "CLIENT_REVIEW"}
                        ):
                            raise ValueError("Task is not ready for QA acceptance")
                        run = await db.get(CursorRun, item.active_cursor_run_id)
                        verification = (run.result_json or {}).get("verification", {}) if run else {}
                        evidence_rows = verification.get("acceptance_criteria", [])
                        evidence = {
                            str(row.get("criterion") or ""): row
                            for row in evidence_rows
                            if isinstance(row, dict)
                        }
                        criteria_passed = all(
                            criterion in evidence
                            and evidence[criterion].get("passed") is True
                            and bool(str(evidence[criterion].get("evidence") or "").strip())
                            for criterion in list(item.acceptance_criteria or [])
                        )
                        if (
                            run is None
                            or run.status != "completed"
                            or verification.get("tests_passed") is not True
                            or verification.get("lint_passed") is not True
                            or not item.acceptance_criteria
                            or not criteria_passed
                        ):
                            raise ValueError(
                                "Latest Cursor run lacks passing evidence for every acceptance criterion"
                            )
                        await transition_pm_phase(db, item, "DONE", detail="QA accepted")
                        item.status = "done"
                        item.wait_owner = "none"
                        item.next_action = ""
                        await add_event(
                            db,
                            item,
                            kind="accepted",
                            title="QA accepted",
                            detail="Acceptance criteria verified",
                        )
                        await db.commit()
                        return {"ok": True, "task": work_item_json(item)}

                    registry.register(submit_development_task)
                    registry.register(get_development_status)
                    registry.register(get_development_result)
                    registry.register(request_development_fix)
                    registry.register(pm_accept_task)
                else:
                    registry.register(
                        cursorremote_do,
                        "cursorremote_do",
                        (
                            "Give Cursor a NEW coding task only if this case has not already sent one. "
                            "One case = one Cursor job. Never send a second prompt for the next bullet "
                            "or because Cursor is searching. done=true only after Cursor finished."
                        ),
                    )
                    registry.register(
                        cursorremote_check,
                        "cursorremote_check",
                        "Check an already running Cursor job; never start a duplicate.",
                    )
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
                """Schedule a one-time follow-up for this agent (ISO datetime)."""
                if cursor_state["finished"]:
                    return {
                        "ok": False,
                        "skipped": True,
                        "reason": (
                            "Cursor already finished (done=true). Do not schedule another check. "
                            "Write the result to the original Telegram chat."
                        ),
                    }
                try:
                    target = datetime.fromisoformat(run_at.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("run_at must be an ISO date and time") from exc
                if target.tzinfo is None:
                    target = target.replace(tzinfo=ZoneInfo(runtime_settings.timezone))
                target = target.astimezone(timezone.utc)
                if target <= datetime.now(timezone.utc):
                    raise ValueError("run_at must be in the future")
                payload = build_followup_payload(
                    message=message,
                    run_at_iso=target.isoformat(),
                    timezone=runtime_settings.timezone,
                    context=context,
                    account_phone=phone,
                )
                from .cursorremote_drive import pin_cursor_followup_message

                payload["message"] = pin_cursor_followup_message(str(payload.get("message") or message))
                job = await save_once_job(
                    db,
                    self.scheduler,
                    agent_id=agent.id,
                    name=name,
                    payload=payload,
                    current_job_id=(context or {}).get("_cron_job_id"),
                )
                return {
                    "ok": True,
                    "job_id": job.id,
                    "name": job.name,
                    "run_at": target.isoformat(),
                    "message": message,
                }

            registry.register(
                schedule_self,
                "schedule_self",
                (
                    "Schedule yourself a one-time internal task at an ISO datetime. "
                    "If Cursor is still running (done=false), schedule cursorremote_check in ~2 minutes. "
                    "If done=true, do not schedule — message the requester instead. "
                    "The follow-up keeps the original Telegram chat so you can write back when finished."
                ),
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
                await self._register_employee_tools(registry, db, agent, profile, context)

        attach_tool_plane(registry)
        return registry

    async def _register_employee_tools(
        self,
        registry: ToolRegistry,
        db: AsyncSession,
        agent: Agent,
        profile: Any,
        context: dict[str, Any] | None = None,
    ) -> None:
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

        async def consult_manager(question: str, context_text: str = "", kind: str = "decision") -> dict[str, Any]:
            """Ask the manager a question via Telegram; wait for /answer on next ticks."""
            return await self.employee.create_consultation(
                db,
                agent,
                question=question,
                context=context_text,
                requires_approval=False,
                need_kind=kind if kind in NEED_KINDS else "decision",
                work_item_id=(context or {}).get("work_item_id"),
            )

        async def request_approval(action_name: str, reason: str, context_text: str = "") -> dict[str, Any]:
            """Request manager approval before a dangerous action. action_name must be a tool id (e.g. sip_dial)."""
            tool_name = normalize_action_name(action_name)
            return await self.employee.create_consultation(
                db,
                agent,
                question=f"Approve action `{tool_name}`: {reason}",
                context=context_text,
                requires_approval=True,
                action_name=tool_name,
                work_item_id=(context or {}).get("work_item_id"),
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
            """Return current mission, schedule, needs and open consultations."""
            jobs = await list_agent_jobs(db, agent.id)
            needs = await list_open_needs(db, agent.id)
            consults = await list_open_consultations(db, agent.id)
            return {
                "mission": profile.mission,
                "role_title": profile.role_title,
                "paused": profile.paused,
                "autonomy_enabled": profile.autonomy_enabled,
                "ticks_used_today": profile.ticks_used_today,
                "budget_ticks_per_day": profile.budget_ticks_per_day,
                "jobs": [
                    {
                        "id": job.id,
                        "name": job.name,
                        "cron": job.cron,
                        "enabled": job.enabled,
                        "payload": job.payload or {},
                    }
                    for job in jobs
                ],
                "needs": [need_json(n) for n in needs],
                "consultations": [consultation_json(c) for c in consults],
            }

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
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not agent.enabled:
            return {"ok": False, "skipped": True, "reason": "agent_disabled"}
        async with self._lock_for(agent.id):
            profile = await get_or_create_profile(db, agent.id)
            prepared = await self.employee.prepare_tick_context(db, agent, profile, force=force)
            if prepared.get("skip"):
                return {"ok": True, "skipped": True, "reason": prepared.get("reason")}
            extra = extra or {}
            pending = await watchdog_items(db, agent.id)
            watchdog_reason = reason in {"heartbeat", "employee_heartbeat"}
            if not extra.get("work_item_id") and not pending and not force and watchdog_reason:
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "no_open_work",
                    "watchdog": {"count": 0, "ids": []},
                }
            await self.employee.mark_tick(db, profile)
            origin = collect_origin_from_jobs(
                await list_agent_jobs(db, agent.id, enabled_only=False)
            )
            context = {
                "source": "employee_tick",
                "employee_tick": True,
                "force_tick": force,
                "tick_reason": reason,
                "user_id": f"employee:{agent.id}",
                **{key: value for key, value in origin.items() if value not in (None, "")},
                **{
                    key: extra[key]
                    for key in (
                        "work_item_id",
                        "reply_chat_id",
                        "reply_phone",
                        "chat_id",
                        "consultation_id",
                        "consultation_status",
                        "approved_by",
                    )
                    if extra.get(key) not in (None, "")
                },
            }
            if extra.get("work_item_id"):
                context["work_item_id"] = extra["work_item_id"]
            elif pending:
                context["work_item_id"] = pending[0].id
            from .work_items import get_work_item, work_item_aborted

            tick_item = await get_work_item(db, context.get("work_item_id"))
            if work_item_aborted(tick_item):
                return {"ok": True, "skipped": True, "reason": "work_item_aborted"}
            cursor_session = await self._cursorremote_session(db, agent)
            if cursor_session is not None:
                poll_items = list(pending)
                if tick_item is not None and all(item.id != tick_item.id for item in poll_items):
                    poll_items.append(tick_item)
                await sync_cursor_work_items(
                    db,
                    agent,
                    cursor_session,
                    poll_items,
                    employee=self.employee,
                )
                tick_item = await get_work_item(db, context.get("work_item_id"))
                pending = await watchdog_items(db, agent.id)
            if pending:
                message = build_watchdog_instruction(pending)
            else:
                message = build_employee_tick_instruction(profile)
            focus = await get_work_item(db, context.get("work_item_id"))
            if focus is not None and focus.status == "collecting" and focus.wait_until:
                from .work_items import _as_aware

                if _as_aware(focus.wait_until) <= utcnow():
                    context["source"] = "intake_flush"
                    context["kind"] = "intake_flush"
                    context.pop("employee_tick", None)
                    message = compile_intake_brief(focus)
                    if focus.chat_id:
                        context.setdefault("reply_chat_id", focus.chat_id)
                        context.setdefault("chat_id", focus.chat_id)
                    if focus.reply_phone:
                        context.setdefault("reply_phone", focus.reply_phone)
            if extra.get("instruction"):
                message = f"{extra['instruction']}\n\n{message}"
            elif extra.get("message") and reason in {"consult_resolved", "manual"}:
                message = f"{extra['message']}\n\n{message}"
            result = await self._run_impl(db, agent, message, context)
            try:
                await self.employee.maybe_send_daily_digest(db, agent, profile)
            except Exception:
                pass
            await self.events.publish(
                "employee.tick",
                {"agent_id": agent.id, "reason": reason, "force": force},
            )
            return {
                "ok": True,
                "skipped": False,
                "result": result,
                "notes": list(context.get("_job_notes") or []),
                "deliver_origin": bool(context.get("_deliver_origin_reply")),
                "origin_already_sent": bool(context.get("_origin_already_sent")),
                "reply_phone": context.get("reply_phone") or context.get("phone"),
                "reply_chat_id": origin_chat_id(context),
                "watchdog": {"count": len(pending), "ids": [item.id for item in pending]},
            }

    async def run(
        self,
        db: AsyncSession,
        agent: Agent,
        message: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        async with self._lock_for(agent.id):
            return await self._run_impl(db, agent, message, context)

    async def _run_impl(
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
        context["_user_message"] = message
        await bind_work_item(db, agent, context, message)
        from .work_items import get_work_item, work_item_aborted

        bound_item = await get_work_item(db, context.get("work_item_id"))
        if work_item_aborted(bound_item):
            context["_suppress_telegram_reply"] = True
            context["_suppress_telegram_reason"] = "work_item_aborted"
            return NO_TELEGRAM_REPLY
        await self.events.publish("agent.started", {"agent_id": agent.id})
        user_id = str(context.get("user_id") or context.get("sender_id") or context.get("chat_id") or "global")
        memories: list[dict[str, Any]] = []
        memory_scope = None
        memory_context = ""
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
        # If no explicit attach rows, enable all connected MCP servers when agent has the mcp tool.
        # Exception: `cursorremote` must always be explicitly attached (project-scoped IDE control).
        tools = set((agent.config or {}).get("tools") or [])
        if not mcp_server_names and "mcp" in tools:
            mcp_server_names = {
                name
                for name in (self.mcp.sessions if self.mcp else {})
                if name != "cursorremote"
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
            attachments = [
                item
                for item in (context.get("_attachments") or context.get("attachments") or [])
                if isinstance(item, dict)
            ]
            if attachments:
                message, attachments = await ingest_attachments_for_llm(
                    client, message, attachments
                )
                context["_attachments"] = attachments
                context["_user_message"] = message
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
                bind_conversation_from_config(state, agent.config, context)
                context["_conversation_state"] = state
                memory_scope = resolve_memory_scope(context, agent, state=state)
                context["_memory_scope"] = memory_scope
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
                memory_scope = resolve_memory_scope(context, agent)
                context["_memory_scope"] = memory_scope
            if runtime_settings.memory_enabled and memory_scope is not None:
                try:
                    memories = await prefetch_memories(
                        self.memory,
                        message,
                        memory_scope,
                        limit=8,
                    )
                except Exception:
                    memories = []
            memory_context = format_memory_hits(memories)
            scope_line = memory_scope_prompt(memory_scope) if memory_scope else ""
            employee_profile = await get_or_create_profile(db, agent.id)
            context["_pm_mode"] = pm_mode_enabled(employee_profile)
            work_item = await get_work_item(db, context.get("work_item_id"))
            debounce_minutes = intake_debounce_minutes(employee_profile)
            is_intake_flush = str(context.get("source") or "") == "intake_flush" or str(
                context.get("kind") or ""
            ) == "intake_flush"
            was_in_flight = bool(
                work_item is not None
                and (
                    work_item.status == "waiting_external"
                    or (work_item.metadata_json or {}).get("cursor_in_flight")
                )
            )
            if work_item is not None and is_intake_flush:
                from .work_items import intake_flush_already_started

                duplicate_flush = intake_flush_already_started(work_item)
                context["_intake_flush"] = True
                context["_intake_collecting"] = False
                if duplicate_flush:
                    context["_duplicate_intake_flush"] = True
                    context["_suppress_telegram_reply"] = True
                    return NO_TELEGRAM_REPLY
                compiled = compile_intake_brief(work_item)
                if compiled:
                    message = compiled
                    context["_user_message"] = message
                work_item = await mark_intake_executing(
                    db, work_item, scheduler=self.employee.scheduler
                )
                context["_duplicate_intake_flush"] = duplicate_flush
                context["_cursor_was_in_flight"] = False
                from .cursor_assets import load_customer_images

                stored_images = load_customer_images(work_item)
                if stored_images:
                    attachments = list(attachments) + [
                        image
                        for image in stored_images
                        if image.get("digest")
                        not in {
                            str(item.get("digest") or "")
                            for item in attachments
                            if isinstance(item, dict)
                        }
                    ]
                    context["_attachments"] = attachments
            elif work_item is not None and should_collect_customer_intake(
                work_item, context, minutes=debounce_minutes
            ):
                work_item = await begin_customer_intake(
                    db,
                    work_item,
                    message,
                    minutes=debounce_minutes,
                    scheduler=self.employee.scheduler,
                    agent_id=agent.id,
                    attachments=attachments,
                )
                context["_intake_collecting"] = True
                context["work_item_id"] = work_item.id
                context["_cursor_was_in_flight"] = False
            else:
                context["_cursor_was_in_flight"] = was_in_flight
                if (
                    work_item is not None
                    and attachments
                    and not context.get("is_admin")
                    and str(context.get("source") or "") == "telegram"
                ):
                    from .cursor_assets import persist_customer_images

                    persist_customer_images(work_item, attachments)
                    await db.commit()
            system_prompt = await assemble_system_prompt(db, agent)
            employee_block = ""
            if employee_profile.autonomy_enabled or context.get("employee_tick"):
                from .employee import build_employee_context_block

                employee_block = build_employee_context_block(
                    employee_profile,
                    await list_agent_jobs(db, agent.id),
                    await list_open_needs(db, agent.id),
                    await list_open_consultations(db, agent.id),
                    await list_open_work_items(db, agent.id),
                )
            is_employee_tick = bool(
                context.get("employee_tick")
                or context.get("source") in {"employee_tick", "employee_heartbeat"}
            )
            origin_followup = context.get("source") == "scheduled" and origin_chat_id(context) is not None
            is_intake_flush = bool(context.get("_intake_flush"))
            phone_hint = ""
            if is_telegram and not context.get("is_admin"):
                lower = message.lower()
                digits = "".join(ch for ch in message if ch.isdigit())
                call_intent = any(
                    word in lower
                    for word in ("звони", "позвони", "перезвони", "набер", "call me", "call")
                )
                sender_id = str(context.get("sender_id") or "")
                if call_intent and len(digits) < 10:
                    phone_hint = (
                        "The customer asked for a call but did NOT provide a phone number. "
                        "Do NOT call sip_dial yet and do NOT use Telegram sender_id/chat_id "
                        f"(sender_id={sender_id}) as a phone number. "
                        "Ask once, naturally, for their mobile number (+7…). "
                        "Only call sip_dial after they send a full phone number."
                    )
                elif len(digits) >= 10:
                    phone_hint = (
                        f"The customer sent digits that may be a phone number ({digits}). "
                        "If they asked for a call, invoke sip_dial with the full mobile number "
                        "(79XXXXXXXXX). Never use sender_id or chat_id."
                    )
            if origin_followup:
                chat = origin_chat_id(context)
                role_instruction = (
                    "This is a scheduled follow-up for a customer request. "
                    f"The original Telegram chat is {chat}. "
                    "If THIS assignment finished (done=true), write the customer-facing result — "
                    "the platform will send that text to the chat. "
                    "Otherwise write a short status for the manager, not a journal for the customer. "
                    "Do not claim that a Telegram message was already sent. "
                    + customer_result_only_instruction()
                    + customer_telegram_instruction()
                )
            elif is_intake_flush:
                role_instruction = (
                    customer_intake_flush_instruction()
                    + "\n"
                    + customer_telegram_instruction()
                )
            elif is_employee_tick:
                role_instruction = build_employee_tick_instruction(employee_profile)
            elif is_telegram:
                role_instruction = (
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
                    "After a successful sip_dial the platform already suppresses the Telegram reply — "
                    "do not describe the call. "
                    "Never describe silence in a message. "
                    "Photos in the current user message are visible to you — describe and use them. "
                    "Voice notes are transcribed into the user text; treat that transcript as what they said. "
                    f"If that tool is unavailable, return exactly {NO_TELEGRAM_REPLY}.\n"
                    + (
                        manager_telegram_instruction()
                        if context.get("is_admin")
                        else customer_telegram_instruction()
                    )
                    + (customer_intake_instruction() if context.get("_intake_collecting") else "")
                    + (f"\n{phone_hint}" if phone_hint else "")
                )
            else:
                role_instruction = (
                    "Never claim that an external action succeeded unless its tool call "
                    "returned successfully. Report tool errors truthfully and explicitly."
                )
            if pm_mode_enabled(employee_profile):
                role_instruction = pm_system_instruction() + "\n\n" + role_instruction
            messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "system",
                    "content": (
                        (
                            scope_line + "\n"
                            if scope_line
                            else ""
                        )
                        + (
                            "Relevant long-term memories:\n" + memory_context
                            if memory_context
                            else "No relevant long-term memories were found."
                        )
                    ),
                },
                {"role": "system", "content": role_instruction},
                {"role": "system", "content": conversation_context},
            ]
            if employee_block:
                messages.append({"role": "system", "content": employee_block})
            messages.append(
                {
                    "role": "user",
                    "content": llm_user_content(message, attachments),
                }
            )
            permissions = resolve_tool_permissions(
                agent.config,
                employee_autonomy=bool(employee_profile.autonomy_enabled),
                cursorremote_attached="cursorremote" in mcp_server_names,
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
                    effective = effective_tool_name(tool_name, arguments)
                    if not approval_required_for_tool(employee_profile, effective, context):
                        return
                    ok = await self.employee.has_approval(db, agent.id, effective)
                    if not ok:
                        raise PermissionError(
                            f"Tool '{effective}' requires manager approval. "
                            f"Call request_approval(action_name='{effective}', reason=...) first."
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
                        work_item_id=context.get("work_item_id"),
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
                chat_id=context.get("chat_id") or context.get("reply_chat_id"),
                sender_id=context.get("sender_id"),
                sender_username=(
                    str(context["sender_username"])
                    if context.get("sender_username")
                    else None
                ),
                source=context.get("source"),
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
                                work_item_id=context.get("work_item_id"),
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
                    exchange_metadata = {
                        "kind": "exchange",
                        "category": "exchange",
                        "agent_name": agent.name,
                        "chat_id": str(context.get("chat_id") or ""),
                        "inbound_at": iso_utc(inbound_at),
                        "outbound_at": iso_utc(outbound_at),
                        "last_message_at": iso_utc(outbound_at),
                    }
                    if memory_scope is not None:
                        exchange_metadata.update(
                            build_memory_metadata(
                                memory_scope,
                                category="exchange",
                            )
                        )
                    await self.memory.add(
                        (
                            f"User: {message}\nAssistant: [no reply sent]"
                            if suppressed
                            else f"User: {message}\nAssistant: {result}"
                        ),
                        user_id=user_id,
                        agent_id=str(agent.id),
                        metadata=exchange_metadata,
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
                        work_item_id=context.get("work_item_id"),
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
                        work_item_id=context.get("work_item_id"),
                    )
                )
                await db.commit()
            await self.events.publish("agent.completed", {"agent_id": agent.id, "text": result})
            context["_job_notes"] = notes_from_audit(registry.audit)
            context["_origin_already_sent"] = telegram_already_sent(registry.audit)
            result_ready = cursor_result_ready_for_customer(
                registry.audit,
                cursor_was_in_flight=bool(context.get("_cursor_was_in_flight")),
            )
            if context.get("_pm_mode"):
                result_ready = any(
                    isinstance(call, dict)
                    and call.get("tool") == "pm_accept_task"
                    and call.get("status") == "success"
                    for call in registry.audit
                )
            if result_ready and not suppressed:
                context["_deliver_origin_reply"] = True
            elif (
                is_internal_execution(context)
                and not suppressed
                and result.strip()
            ):
                notify_phone = (
                    (account.phone if account else None)
                    or context.get("reply_phone")
                    or context.get("phone")
                )
                await self._notify_manager_status(notify_phone, agent, context, result)
            await after_agent_run(
                db, agent, context, result, registry.audit, employee=self.employee
            )
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
