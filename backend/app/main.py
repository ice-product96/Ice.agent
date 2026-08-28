import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from .api import router
from .config import Settings, get_settings
from .contract import router as contract_router, websocket_events
from .db import (
    AdminSettings, Agent, CronJob, LlmProfile, McpServer, RuntimeSettings, SessionLocal,
    SipAccount, TelegramAccount, create_schema,
    WorkItem,
)
from .employee import list_agent_jobs, save_once_job
from .events import events
from .integrations import McpManager, MemoryStore, WebSearch, exception_text
from .job_result import collect_origin_from_jobs, origin_chat_id, origin_phone, send_origin_reply
from .routing import TelegramEventRouter
from .runtime import AgentRuntime, TaskBus
from .scheduler import CronManager
from .sip import SipGateway
from .telegram import TelegramGateway
from .secrets import SecretStore

logger = logging.getLogger(__name__)
STARTUP_TIMEOUT_SECONDS = 25


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await create_schema()
    memory = MemoryStore()
    telegram = TelegramGateway(settings)
    sip = SipGateway(settings, events, memory)
    mcp = McpManager()
    search = WebSearch()
    runtime = AgentRuntime(settings, memory, search, events, telegram, mcp, sip=sip)
    task_bus = TaskBus(SessionLocal, events)
    runtime.bind_task_bus(task_bus)
    task_bus.bind_runtime(runtime)
    telegram_router = TelegramEventRouter(SessionLocal, runtime, telegram, events)
    telegram.register_callback("new_message", telegram_router.new_message)
    telegram.register_callback("message_edited", telegram_router.message_edited)
    telegram.register_callback("callback_query", telegram_router.callback_query)
    async with SessionLocal() as db:
        accounts = (await db.scalars(select(TelegramAccount))).all()
        sip_accounts = (await db.scalars(select(SipAccount))).all()
        servers = (await db.scalars(select(McpServer).where(McpServer.enabled.is_(True)))).all()
        admin_settings = await db.get(AdminSettings, 1)
        runtime_settings = await db.get(RuntimeSettings, 1)
        if runtime_settings is None:
            runtime_settings = RuntimeSettings(id=1)
            db.add(runtime_settings)
            await db.commit()
            await db.refresh(runtime_settings)
        secrets = SecretStore.from_settings(settings)
        memory_profile = (
            await db.get(LlmProfile, runtime_settings.memory_llm_profile_id)
            if runtime_settings.memory_llm_profile_id is not None
            else None
        )
        import json

        for server in servers:
            if server.env and not server.env_ciphertext:
                server.env_ciphertext = secrets.encrypt(
                    json.dumps(server.env, ensure_ascii=False)
                )
                server.env = {}
        await db.commit()
    if admin_settings:
        telegram.set_admin_ids(admin_settings.telegram_ids)
    telegram.configure_runtime(runtime_settings)
    search.configure(
        runtime_settings.search_provider,
        runtime_settings.searxng_url,
        secrets.decrypt(runtime_settings.tavily_api_key_ciphertext),
        runtime_settings.tavily_http_proxy,
    )
    try:
        memory_llm = None
        if memory_profile and memory_profile.enabled:
            memory_key = secrets.decrypt(memory_profile.api_key_ciphertext)
            if memory_key:
                memory_llm = {
                    "api_key": memory_key,
                    "base_url": memory_profile.base_url,
                    "model": memory_profile.default_model,
                    "provider": memory_profile.provider,
                    "http_proxy": (memory_profile.http_proxy or "").strip() or None,
                }

        async def start_memory() -> None:
            try:
                await memory.reconfigure(
                    runtime_settings,
                    secrets.decrypt(runtime_settings.mem0_api_key_ciphertext),
                    memory_llm,
                )
                if memory.last_error:
                    await events.publish("memory.startup_failed", {"error": memory.last_error})
            except Exception as exc:
                await events.publish("memory.startup_failed", {"error": str(exc)})

        # Fastembed may download HuggingFace weights; never block /health on that.
        asyncio.create_task(start_memory(), name="memory-startup")
    except Exception as exc:
        await events.publish("memory.startup_failed", {"error": str(exc)})
    try:
        async with asyncio.timeout(STARTUP_TIMEOUT_SECONDS):
            await telegram.restore(accounts)
    except TimeoutError:
        await events.publish(
            "telegram.startup_failed",
            {"error": f"restore timed out after {STARTUP_TIMEOUT_SECONDS}s"},
        )
    except Exception as exc:
        await events.publish("telegram.startup_failed", {"error": str(exc)})
    try:
        async with asyncio.timeout(STARTUP_TIMEOUT_SECONDS):
            await sip.restore(list(sip_accounts))
    except TimeoutError:
        await events.publish(
            "sip.startup_failed",
            {"error": f"restore timed out after {STARTUP_TIMEOUT_SECONDS}s"},
        )
    except Exception as exc:
        await events.publish("sip.startup_failed", {"error": str(exc)})
    mcp_startup_stops: dict[int, asyncio.Event] = {}

    async def connect_mcp(server: McpServer) -> None:
        stop = mcp_startup_stops[server.id]
        config = {
            "transport": server.transport,
            "command": server.command,
            "args": server.args,
            "url": server.url,
            "env": json.loads(secrets.decrypt(server.env_ciphertext) or "{}"),
        }
        try:
            try:
                async with asyncio.timeout(15):
                    await mcp.hot_reload(server.name, config)
            except Exception as exc:
                await events.publish(
                    "mcp.connection_failed",
                    {"server": server.name, "error": exception_text(exc)},
                )
            # Hold this task until shutdown so McpManager can reconnect in the background.
            await stop.wait()
        finally:
            await mcp.disconnect(server.name)

    # External MCP servers must never block API health/startup.
    for server in servers:
        mcp_startup_stops[server.id] = asyncio.Event()
    mcp_startup_tasks = [
        asyncio.create_task(connect_mcp(server), name=f"mcp-connect-{server.id}")
        for server in servers
    ]

    async def run_scheduled(agent_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        async with SessionLocal() as db:
            agent = await db.get(Agent, agent_id)
            if not agent:
                return {"ok": False, "skipped": True, "reason": "agent_missing"}

            async def deliver_result(
                text: Any,
                *,
                already_sent: bool = False,
                extra: dict[str, Any] | None = None,
            ) -> dict[str, Any]:
                if already_sent:
                    return {"sent": True, "reason": "агент уже отправил сообщение в Telegram."}
                body = str(text or "").strip()
                merged = {**(payload or {}), **(extra or {})}
                account_phone = None
                if agent.telegram_account_id is not None:
                    account = await db.get(TelegramAccount, agent.telegram_account_id)
                    account_phone = account.phone if account else None
                phone = origin_phone(merged, account_phone)
                chat_id = origin_chat_id(merged)
                if chat_id in (None, "", False):
                    recovered = collect_origin_from_jobs(
                        await list_agent_jobs(db, agent.id, enabled_only=False)
                    )
                    phone = origin_phone(recovered, phone)
                    chat_id = origin_chat_id(recovered)
                return await send_origin_reply(telegram, phone, chat_id, body)

            async def schedule_delivery_retry(
                text: str,
                *,
                attempt: int,
                extra: dict[str, Any] | None = None,
            ) -> CronJob:
                run_at = datetime.now(timezone.utc) + timedelta(minutes=2)
                merged = {**payload, **(extra or {})}
                retry_payload = {
                    **merged,
                    "kind": "cursor_result_delivery",
                    "source": "scheduled_delivery",
                    "message": text,
                    "delivery_attempt": attempt,
                    "delivery_key": merged.get("delivery_key"),
                    "run_once_at": run_at.isoformat(),
                    "timezone": "UTC",
                }
                return await save_once_job(
                    db,
                    scheduler,
                    agent_id=agent.id,
                    name=f"case{merged.get('work_item_id')}-cursor-delivery",
                    payload=retry_payload,
                    current_job_id=payload.get("_cron_job_id"),
                )

            async def deliver_cursor_result_once(
                text: str,
                *,
                extra: dict[str, Any] | None = None,
            ) -> dict[str, Any]:
                merged = {**payload, **(extra or {})}
                item_id = merged.get("work_item_id")
                item = await db.get(WorkItem, item_id) if item_id else None
                assignment = (
                    merged.get("cursor_assignment_seq")
                    if merged.get("cursor_assignment_seq") is not None
                    else (
                        (item.metadata_json or {}).get("cursor_assignment_seq", 0)
                        if item is not None
                        else 0
                    )
                )
                key = str(merged.get("delivery_key") or "").strip() or hashlib.sha256(
                    f"{item_id}:{assignment}:{text}".encode("utf-8")
                ).hexdigest()
                payload["delivery_key"] = key
                if item is not None:
                    meta = dict(item.metadata_json or {})
                    delivery_state = (
                        dict(meta.get("cursor_delivery") or {})
                        if isinstance(meta.get("cursor_delivery"), dict)
                        else {}
                    )
                    if delivery_state.get("key") == key and delivery_state.get(
                        "status"
                    ) in {"sending", "sent"}:
                        return {
                            "sent": True,
                            "deduplicated": True,
                            "reason": "результат уже отправлен или отправляется.",
                        }
                    meta["cursor_delivery"] = {
                        "key": key,
                        "status": "sending",
                        "attempt": int(merged.get("delivery_attempt") or 0),
                    }
                    item.metadata_json = meta
                    await db.commit()
                delivery = await deliver_result(text, extra=extra)
                if item is not None:
                    await db.refresh(item)
                    meta = dict(item.metadata_json or {})
                    state = dict(meta.get("cursor_delivery") or {})
                    if state.get("key") == key:
                        state["status"] = (
                            "sent" if delivery.get("sent") else "failed"
                        )
                        state["reason"] = str(delivery.get("reason") or "")[:500]
                        meta["cursor_delivery"] = state
                        item.metadata_json = meta
                        await db.commit()
                return delivery

            kind = str(payload.get("kind") or "")
            try:
                if kind == "cursor_result_delivery":
                    delivery = await deliver_cursor_result_once(
                        str(payload.get("message") or "")
                    )
                    attempt = int(payload.get("delivery_attempt") or 1)
                    outcome = {
                        "ok": bool(delivery.get("sent")),
                        "poll_only": True,
                        "delivery": delivery,
                        "notified": bool(delivery.get("sent")),
                    }
                    if not delivery.get("sent") and attempt < 5:
                        job = await schedule_delivery_retry(
                            str(payload.get("message") or ""),
                            attempt=attempt + 1,
                        )
                        outcome["rescheduled"] = True
                        outcome["job_id"] = job.id
                    elif not delivery.get("sent"):
                        phone = origin_phone(payload)
                        if phone and telegram.admin_ids:
                            await telegram.notify_admins(
                                phone,
                                (
                                    "[Ice.agent] Не удалось доставить итог заказчику\n"
                                    f"Кейс #{payload.get('work_item_id')}: "
                                    f"{delivery.get('reason') or 'неизвестная ошибка'}"
                                ),
                            )
                    return outcome
                if kind == "employee_tick" or payload.get("source") in {
                    "employee_heartbeat",
                    "consult_resolved",
                    "employee_tick",
                }:
                    tick_result = await runtime.tick(
                        db,
                        agent,
                        force=bool(payload.get("force")),
                        reason=str(payload.get("source") or kind or "heartbeat"),
                        extra=payload,
                    )
                    if tick_result.get("deliver_origin"):
                        delivery = await deliver_result(
                            tick_result.get("result"),
                            already_sent=bool(tick_result.get("origin_already_sent")),
                            extra={
                                "reply_phone": tick_result.get("reply_phone"),
                                "reply_chat_id": tick_result.get("reply_chat_id"),
                            },
                        )
                        tick_result["delivery"] = delivery
                        tick_result["notified"] = bool(delivery.get("sent"))
                    return tick_result
                from .cursorremote_drive import is_cursor_poll_followup

                if is_cursor_poll_followup(payload):
                    poll_result = await runtime.poll_cursor_followup(
                        db,
                        agent,
                        payload,
                    )
                    if poll_result.get("deliver_origin"):
                        delivery = await deliver_cursor_result_once(
                            str(poll_result.get("result") or ""),
                            extra={
                                "reply_phone": poll_result.get("reply_phone"),
                                "reply_chat_id": poll_result.get("reply_chat_id"),
                            },
                        )
                        poll_result["delivery"] = delivery
                        poll_result["notified"] = bool(delivery.get("sent"))
                        if not delivery.get("sent"):
                            job = await schedule_delivery_retry(
                                str(poll_result.get("result") or ""),
                                attempt=1,
                                extra={
                                    "reply_phone": poll_result.get("reply_phone"),
                                    "reply_chat_id": poll_result.get("reply_chat_id"),
                                },
                            )
                            poll_result["delivery_rescheduled"] = True
                            poll_result["delivery_job_id"] = job.id
                    if not poll_result.get("fallback_llm"):
                        return poll_result
                result = await runtime.run(db, agent, str(payload.get("message", "")), payload)
                outcome = {
                    "ok": True,
                    "result": result,
                    "notified": False,
                    "notes": list(payload.get("_job_notes") or []),
                }
                if payload.get("_deliver_origin_reply"):
                    delivery = await deliver_result(
                        result,
                        already_sent=bool(payload.get("_origin_already_sent")),
                    )
                    outcome["delivery"] = delivery
                    outcome["notified"] = bool(delivery.get("sent"))
                return outcome
            except Exception as exc:
                try:
                    await db.rollback()
                except Exception:
                    logger.exception("rollback after scheduled run failed")
                from .work_items import handle_run_failure

                await handle_run_failure(db, agent, payload, exc, runtime.employee)
                raise

    scheduler = CronManager(SessionLocal, run_scheduled)
    runtime.bind_scheduler(scheduler)
    try:
        await scheduler.load()
    except Exception as exc:
        logger.exception("scheduler.load failed: %s", exc)
        await events.publish("scheduler.startup_failed", {"error": str(exc)})
    scheduler.start()
    if runtime_settings.task_workers > 0:
        await task_bus.start(runtime_settings.task_workers)
    app.state.memory = memory
    app.state.search = search
    app.state.telegram = telegram
    app.state.sip = sip
    app.state.mcp = mcp
    app.state.runtime = runtime
    app.state.task_bus = task_bus
    app.state.telegram_router = telegram_router
    app.state.scheduler = scheduler
    yield
    for stop in mcp_startup_stops.values():
        stop.set()
    await asyncio.gather(*mcp_startup_tasks, return_exceptions=True)
    scheduler.shutdown()
    await task_bus.stop()
    await sip.close()
    await telegram.close()
    await mcp.close()


app = FastAPI(title="Ice.agent API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ],
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(contract_router)
app.include_router(router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.websocket("/ws/events")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str,
    settings: Settings = Depends(get_settings),
) -> None:
    await websocket_events(websocket, token, settings)
