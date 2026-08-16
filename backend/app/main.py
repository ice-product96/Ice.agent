import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from .api import router
from .config import Settings, get_settings
from .contract import router as contract_router, websocket_events
from .db import (
    AdminSettings, Agent, LlmProfile, McpServer, RuntimeSettings, SessionLocal,
    SipAccount, TelegramAccount, create_schema,
)
from .events import events
from .integrations import McpManager, MemoryStore, WebSearch, exception_text
from .routing import TelegramEventRouter
from .runtime import AgentRuntime, TaskBus
from .scheduler import CronManager
from .sip import SipGateway
from .telegram import TelegramGateway
from .secrets import SecretStore


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
        await telegram.restore(accounts)
    except Exception as exc:
        await events.publish("telegram.startup_failed", {"error": str(exc)})
    try:
        await sip.restore(list(sip_accounts))
    except Exception as exc:
        await events.publish("sip.startup_failed", {"error": str(exc)})
    mcp_startup_stops: dict[int, asyncio.Event] = {}

    async def connect_mcp(server: McpServer) -> None:
        stop = mcp_startup_stops[server.id]
        try:
            async with asyncio.timeout(15):
                await mcp.hot_reload(server.name, {
                    "transport": server.transport,
                    "command": server.command,
                    "args": server.args,
                    "url": server.url,
                    "env": json.loads(secrets.decrypt(server.env_ciphertext) or "{}"),
                })
            # Keep ownership of AnyIO cancel scopes in this task until shutdown.
            await stop.wait()
        except BaseException as exc:
            await events.publish(
                "mcp.connection_failed",
                {"server": server.name, "error": exception_text(exc)},
            )
        finally:
            await mcp.disconnect(server.name)

    # External MCP servers must never block API health/startup.
    for server in servers:
        mcp_startup_stops[server.id] = asyncio.Event()
    mcp_startup_tasks = [
        asyncio.create_task(connect_mcp(server), name=f"mcp-connect-{server.id}")
        for server in servers
    ]

    async def run_scheduled(agent_id: int, payload: dict[str, Any]) -> None:
        async with SessionLocal() as db:
            agent = await db.get(Agent, agent_id)
            if not agent:
                return
            kind = str(payload.get("kind") or "")
            if kind == "employee_tick" or payload.get("source") in {
                "employee_heartbeat",
                "consult_resolved",
                "employee_tick",
            }:
                await runtime.tick(
                    db,
                    agent,
                    force=bool(payload.get("force")),
                    reason=str(payload.get("source") or kind or "heartbeat"),
                )
                return
            result = await runtime.run(db, agent, str(payload.get("message", "")), payload)
            phone = payload.get("reply_phone")
            chat_id = payload.get("reply_chat_id")
            if phone and chat_id:
                entity = int(chat_id) if str(chat_id).lstrip("-").isdigit() else str(chat_id)
                await telegram.send_message(str(phone), entity, result)

    scheduler = CronManager(SessionLocal, run_scheduled)
    runtime.bind_scheduler(scheduler)
    await scheduler.load()
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
