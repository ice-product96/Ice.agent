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
    LlmProfile,
    McpServer,
    MessageLog,
    RuntimeSettings,
    TelegramAccount,
    agent_mcp_servers,
)
from .action_reports import format_admin_action_report
from .events import EventHub
from .integrations import LLMClient, McpManager, MemoryStore, WebSearch
from .tools import ToolRegistry, common_registry, resolve_tool_permissions
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

    def bind_task_bus(self, task_bus: TaskBus) -> None:
        self.task_bus = task_bus

    def bind_scheduler(self, scheduler: Any) -> None:
        self.scheduler = scheduler

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
        return registry

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
            messages = [
                {"role": "system", "content": agent.prompt},
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
                    ),
                },
                {"role": "system", "content": conversation_context},
                {"role": "user", "content": message},
            ]
            permissions = resolve_tool_permissions(agent.config)
            registry = await self.registry(
                agent,
                account.phone if account else None,
                mcp_server_names,
                runtime_settings.memory_enabled,
                db,
                runtime_settings,
                context,
            )
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
