from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings, get_settings
from .db import (
    AdminSettings, Agent, AgentLink, AgentTask, CronJob, LlmProfile, McpServer,
    ConversationState, MessageLog, RuntimeSettings, TelegramAccount, get_db,
)
from .events import events
from .integrations import exception_text
from .schemas import LlmProfileBody, RuntimeSettingsBody
from .secrets import SecretStore, masked_secret
from .security import issue_token, require_admin, valid_password, verify_token

router = APIRouter(prefix="/api/v1")
auth = [Depends(require_admin)]


def iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def row(instance: Any) -> dict[str, Any]:
    return {column.name: iso(getattr(instance, column.name)) for column in instance.__table__.columns}


def as_int(value: int | str, label: str = "id") -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid {label}") from exc


async def one(db: AsyncSession, model: type[Any], item_id: int | str) -> Any:
    instance = await db.get(model, as_int(item_id))
    if instance is None:
        raise HTTPException(status_code=404, detail="Not found")
    return instance


class LoginBody(BaseModel):
    username: str
    password: str


class AgentBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    description: str = ""
    prompt: str = ""
    model: str = ""
    provider: str = "openai"
    account_id: int | str | None = None
    telegram_account_id: int | str | None = None
    llm_profile_id: int | str | None = None
    tools: list[Any] = Field(default_factory=list)
    tool_permissions: list[str] = Field(default_factory=list)
    links: list[Any] = Field(default_factory=list)
    typing_enabled: bool = True
    enabled: bool = True
    status: str | None = None


class TelegramLoginBody(BaseModel):
    name: str = ""
    phone: str
    api_id: int
    api_hash: str
    http_proxy: str | None = None
    mtproto_host: str | None = None
    mtproto_port: int | None = None
    mtproto_dc_id: int | None = None


class TelegramProxyBody(BaseModel):
    http_proxy: str | None = None
    mtproto_host: str | None = None
    mtproto_port: int | None = None
    mtproto_dc_id: int | None = None
    clear_proxy: bool = False


class TelegramVerifyBody(BaseModel):
    session_id: int | str
    code: str
    password: str | None = None


class McpBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    transport: str = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    clear_env: bool = False


class CronBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    agent_id: int | str
    schedule: str
    run_once_at: str | None = None
    prompt: str = ""
    timezone: str = "UTC"
    enabled: bool = True


class AdminBody(BaseModel):
    model_config = ConfigDict(extra="allow")
    admin_ids: list[int | str] = Field(default_factory=list)
    escalation_enabled: bool = False
    escalation_chat_id: int | str | None = None
    escalation_prompt: str = ""


@router.post("/auth/login")
async def login(payload: LoginBody, settings: Settings = Depends(get_settings)) -> dict[str, str]:
    if not payload.username.strip() or not valid_password(payload.password, settings):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token, _ = issue_token(settings)
    return {"access_token": token, "token_type": "bearer"}


@router.get("/auth/me", dependencies=auth)
async def me() -> dict[str, Any]:
    return {"id": "admin", "username": "admin", "role": "admin", "is_admin": True}


@router.get("/dashboard", dependencies=auth)
async def dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    async def count(model: type[Any]) -> int:
        return int(await db.scalar(select(func.count()).select_from(model)) or 0)

    recent_logs = (await db.scalars(select(MessageLog).order_by(MessageLog.created_at.desc()).limit(10))).all()
    recent_tasks = (await db.scalars(select(AgentTask).order_by(AgentTask.created_at.desc()).limit(10))).all()
    counts = {
        "agents": await count(Agent),
        "telegram_accounts": await count(TelegramAccount),
        "mcp_servers": await count(McpServer),
        "cron_jobs": await count(CronJob),
        "tasks": await count(AgentTask),
        "logs": await count(MessageLog),
        "active_conversations": await count(ConversationState),
    }
    profiles = {
        item.id: item
        for item in (await db.scalars(select(LlmProfile))).all()
    }
    accounts = {
        item.id: item
        for item in (await db.scalars(select(TelegramAccount))).all()
    }
    agent_readiness = []
    for agent in (await db.scalars(select(Agent).order_by(Agent.id))).all():
        reasons: list[str] = []
        profile = profiles.get(agent.llm_profile_id)
        if profile is None:
            reasons.append("llm_profile_missing")
        elif not profile.enabled:
            reasons.append("llm_profile_disabled")
        elif not profile.api_key_ciphertext:
            reasons.append("llm_api_key_missing")
        if agent.telegram_account_id is not None:
            account = accounts.get(agent.telegram_account_id)
            if account is None:
                reasons.append("telegram_account_missing")
            elif not account.authorized:
                reasons.append("telegram_account_unauthorized")
            elif not account.api_id or not account.api_hash_ciphertext:
                reasons.append("telegram_credentials_missing")
        agent_readiness.append({
            "agent_id": agent.id,
            "ready": agent.enabled and not reasons,
            "reasons": reasons if agent.enabled else ["agent_disabled", *reasons],
        })
    runtime_settings = await db.get(RuntimeSettings, 1)
    enabled_mcp = int(
        await db.scalar(
            select(func.count())
            .select_from(McpServer)
            .where(McpServer.enabled.is_(True))
        )
        or 0
    )
    connected_mcp = len(request.app.state.mcp.sessions)
    connections = {
        "llm": {
            "status": "configured" if any(
                item.enabled and item.api_key_ciphertext for item in profiles.values()
            ) else "missing"
        },
        "search": {
            "status": (
                "missing"
                if runtime_settings
                and runtime_settings.search_provider == "searxng"
                and not runtime_settings.searxng_url
                else "configured"
            )
        },
        "memory": {
            "status": (
                "disabled"
                if not runtime_settings or not runtime_settings.memory_enabled
                else "degraded"
                if request.app.state.memory.last_error
                else "configured"
            ),
            "reason": (
                "initialization_failed"
                if request.app.state.memory.last_error
                else None
            ),
            "detail": (
                request.app.state.memory.last_error
                if request.app.state.memory.last_error
                else (
                    "Отключена"
                    if not runtime_settings or not runtime_settings.memory_enabled
                    else "Mem0 + Qdrant"
                )
            ),
        },
        "telegram": {
            "status": (
                "missing"
                if not accounts
                else "degraded"
                if any(
                    not item.authorized
                    or not item.api_id
                    or not item.api_hash_ciphertext
                    for item in accounts.values()
                )
                else "configured"
            )
        },
        "mcp": {
            "status": (
                "missing"
                if not enabled_mcp
                else "configured"
                if connected_mcp >= enabled_mcp
                else "degraded"
            ),
            "configured": enabled_mcp,
            "connected": connected_mcp,
        },
    }
    agents_total = counts["agents"]
    agents_online = sum(1 for item in agent_readiness if item["ready"])
    agents_errors = sum(1 for item in agent_readiness if item["reasons"])
    telegram_connected = sum(1 for item in accounts.values() if item.authorized and item.enabled)
    tasks_running = int(
        await db.scalar(select(func.count()).select_from(AgentTask).where(AgentTask.status == "running")) or 0
    )
    tasks_queued = int(
        await db.scalar(select(func.count()).select_from(AgentTask).where(AgentTask.status == "queued")) or 0
    )
    memory_items = 0
    try:
        listed = await request.app.state.memory.get_all()
        memory_items = len(listed or [])
    except Exception:
        memory_items = 0

    return {
        "counts": counts,
        **{f"{key}_count": value for key, value in counts.items()},
        "recent_logs": [row(item) for item in recent_logs],
        "recent_tasks": [row(item) for item in recent_tasks],
        "status": "ok",
        "connections": connections,
        "agent_readiness": agent_readiness,
        # Shape expected by the admin UI Overview screen
        "agents": {
            "total": agents_total,
            "online": agents_online,
            "errors": agents_errors,
        },
        "telegram_accounts": {
            "total": counts["telegram_accounts"],
            "connected": telegram_connected,
        },
        "tasks": {
            "running": tasks_running,
            "queued": tasks_queued,
            "completed_today": 0,
        },
        "memory_items": memory_items,
        "mcp_servers": {
            "total": counts["mcp_servers"],
            "online": connected_mcp,
        },
    }


@router.get("/status", dependencies=auth)
async def operational_status(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    data = await dashboard(request, db)
    return {
        "status": data["status"],
        "connections": data["connections"],
        "agents": data["agent_readiness"],
    }


async def agent_json(db: AsyncSession, agent: Agent) -> dict[str, Any]:
    links = (await db.scalars(select(AgentLink).where(AgentLink.source_agent_id == agent.id))).all()
    config = agent.config or {}
    return {
        "id": agent.id,
        "name": agent.name,
        "description": config.get("description", ""),
        "prompt": agent.prompt,
        "model": agent.model_name,
        "provider": agent.model_provider,
        "account_id": agent.telegram_account_id,
        "telegram_account_id": agent.telegram_account_id,
        "llm_profile_id": agent.llm_profile_id,
        "tools": config.get("tools", []),
        "tool_permissions": config.get("tool_permissions", []),
        "links": [
            {
                "id": link.id,
                "agent_id": link.target_agent_id,
                "target_agent_id": link.target_agent_id,
                "can_delegate": link.can_delegate,
                "can_message": link.can_message,
                "permissions": link.permissions,
            }
            for link in links
        ],
        "typing_enabled": config.get("typing_enabled", True),
        "enabled": agent.enabled,
        "status": "active" if agent.enabled else "disabled",
        "created_at": iso(agent.created_at),
        "updated_at": iso(agent.updated_at),
    }


def unpack_link(value: Any) -> tuple[int, bool, bool, list[str]]:
    if isinstance(value, dict):
        target = value.get("target_agent_id", value.get("agent_id", value.get("id")))
        return as_int(target, "link target"), bool(value.get("can_delegate", True)), bool(value.get("can_message", True)), list(value.get("permissions", []))
    return as_int(value, "link target"), True, True, []


async def replace_links(db: AsyncSession, source_id: int, links: list[Any]) -> None:
    await db.execute(delete(AgentLink).where(AgentLink.source_agent_id == source_id))
    for value in links:
        target, can_delegate, can_message, permissions = unpack_link(value)
        if target == source_id:
            continue
        if await db.get(Agent, target) is None:
            raise HTTPException(status_code=422, detail=f"Linked agent {target} not found")
        db.add(AgentLink(
            source_agent_id=source_id,
            target_agent_id=target,
            can_delegate=can_delegate,
            can_message=can_message,
            permissions=permissions,
        ))


def apply_agent(agent: Agent, payload: AgentBody) -> None:
    account = payload.telegram_account_id if payload.telegram_account_id is not None else payload.account_id
    agent.name = payload.name
    agent.prompt = payload.prompt
    agent.model_name = payload.model
    agent.model_provider = payload.provider
    agent.telegram_account_id = as_int(account, "account_id") if account is not None else None
    agent.llm_profile_id = (
        as_int(payload.llm_profile_id, "llm_profile_id")
        if payload.llm_profile_id is not None
        else None
    )
    agent.enabled = payload.enabled and payload.status not in {"disabled", "inactive", "offline"}
    config = dict(agent.config or {})
    config.update(
        description=payload.description,
        tools=payload.tools,
        tool_permissions=payload.tool_permissions,
        typing_enabled=payload.typing_enabled,
    )
    agent.config = config


@router.get("/agents", dependencies=auth)
async def agents(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    items = (await db.scalars(select(Agent).order_by(Agent.id))).all()
    return [await agent_json(db, item) for item in items]


@router.post("/agents", dependencies=auth, status_code=201)
async def create_agent(payload: AgentBody, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    if payload.llm_profile_id is not None and await db.get(
        LlmProfile, as_int(payload.llm_profile_id, "llm_profile_id")
    ) is None:
        raise HTTPException(status_code=422, detail="LLM profile not found")
    agent = Agent(name=payload.name)
    apply_agent(agent, payload)
    db.add(agent)
    await db.flush()
    await replace_links(db, agent.id, payload.links)
    await db.commit()
    await db.refresh(agent)
    return await agent_json(db, agent)


@router.get("/agents/{agent_id}", dependencies=auth)
async def get_agent(agent_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await agent_json(db, await one(db, Agent, agent_id))


@router.put("/agents/{agent_id}", dependencies=auth)
@router.patch("/agents/{agent_id}", dependencies=auth)
async def update_agent(agent_id: str, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    agent = await one(db, Agent, agent_id)
    current = await agent_json(db, agent)
    raw = await request.json()
    payload = AgentBody.model_validate({**current, **raw})
    if payload.llm_profile_id is not None and await db.get(
        LlmProfile, as_int(payload.llm_profile_id, "llm_profile_id")
    ) is None:
        raise HTTPException(status_code=422, detail="LLM profile not found")
    apply_agent(agent, payload)
    await replace_links(db, agent.id, payload.links)
    await db.commit()
    await db.refresh(agent)
    return await agent_json(db, agent)


@router.delete("/agents/{agent_id}", dependencies=auth, status_code=204)
async def delete_agent(agent_id: str, db: AsyncSession = Depends(get_db)) -> Response:
    agent = await one(db, Agent, agent_id)
    await db.execute(delete(AgentLink).where(
        (AgentLink.source_agent_id == agent.id) | (AgentLink.target_agent_id == agent.id)
    ))
    await db.delete(agent)
    await db.commit()
    return Response(status_code=204)


def llm_profile_json(profile: LlmProfile) -> dict[str, Any]:
    return {
        "id": profile.id,
        "name": profile.name,
        "provider": profile.provider,
        "base_url": profile.base_url,
        "http_proxy": profile.http_proxy,
        "default_model": profile.default_model,
        "enabled": profile.enabled,
        "has_api_key": bool(profile.api_key_ciphertext),
        "api_key_masked": masked_secret(profile.api_key_ciphertext),
        "created_at": iso(profile.created_at),
        "updated_at": iso(profile.updated_at),
    }


def apply_llm_profile(
    profile: LlmProfile,
    payload: LlmProfileBody,
    secrets: SecretStore,
) -> None:
    profile.name = payload.name
    profile.provider = payload.provider
    profile.base_url = payload.base_url
    profile.http_proxy = (payload.http_proxy or "").strip() or None
    profile.default_model = payload.default_model
    profile.enabled = payload.enabled
    if payload.clear_api_key:
        profile.api_key_ciphertext = None
    elif payload.api_key:
        profile.api_key_ciphertext = secrets.encrypt(payload.api_key)


@router.get("/llm-profiles", dependencies=auth)
async def llm_profiles(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    profiles = (await db.scalars(select(LlmProfile).order_by(LlmProfile.id))).all()
    return [llm_profile_json(item) for item in profiles]


@router.post("/llm-profiles", dependencies=auth, status_code=201)
async def create_llm_profile(
    payload: LlmProfileBody,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    profile = LlmProfile()
    apply_llm_profile(profile, payload, SecretStore.from_settings(get_settings()))
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    return llm_profile_json(profile)


@router.get("/llm-profiles/{profile_id}", dependencies=auth)
async def get_llm_profile(
    profile_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return llm_profile_json(await one(db, LlmProfile, profile_id))


@router.put("/llm-profiles/{profile_id}", dependencies=auth)
@router.patch("/llm-profiles/{profile_id}", dependencies=auth)
async def update_llm_profile(
    profile_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    profile = await one(db, LlmProfile, profile_id)
    current = llm_profile_json(profile)
    payload = LlmProfileBody.model_validate({**current, **await request.json()})
    apply_llm_profile(profile, payload, SecretStore.from_settings(get_settings()))
    await db.commit()
    await db.refresh(profile)
    return llm_profile_json(profile)


@router.delete("/llm-profiles/{profile_id}", dependencies=auth, status_code=204)
async def delete_llm_profile(
    profile_id: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    profile = await one(db, LlmProfile, profile_id)
    assigned = await db.scalar(
        select(func.count()).select_from(Agent).where(Agent.llm_profile_id == profile.id)
    )
    if assigned:
        raise HTTPException(
            status_code=409,
            detail="LLM profile is assigned to one or more agents; reassign them first",
        )
    await db.delete(profile)
    await db.commit()
    return Response(status_code=204)


@router.post("/llm-profiles/{profile_id}/test", dependencies=auth)
async def test_llm_profile(
    profile_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from .integrations import LLMClient

    profile = await one(db, LlmProfile, profile_id)
    if not profile.enabled:
        return {"ok": False, "status": "disabled", "detail": "Profile is disabled"}
    key = SecretStore.from_settings(get_settings()).decrypt(profile.api_key_ciphertext)
    if not key:
        return {"ok": False, "status": "missing", "detail": "API key is not configured"}
    try:
        await LLMClient(
            api_key=key,
            base_url=profile.base_url,
            model=profile.default_model,
            max_rounds=1,
            http_proxy=profile.http_proxy,
        ).test_connection()
    except Exception as exc:
        return {"ok": False, "status": "error", "detail": str(exc)}
    return {"ok": True, "status": "connected", "detail": "Connection successful"}


def apply_telegram_network(
    account: TelegramAccount,
    *,
    http_proxy: str | None = None,
    mtproto_host: str | None = None,
    mtproto_port: int | None = None,
    mtproto_dc_id: int | None = None,
    clear_proxy: bool = False,
) -> None:
    if clear_proxy:
        account.http_proxy = None
        account.mtproto_host = None
        account.mtproto_port = None
        account.mtproto_dc_id = None
        return
    account.http_proxy = (http_proxy or "").strip() or None
    host = (mtproto_host or "").strip() or None
    account.mtproto_host = host
    account.mtproto_dc_id = int(mtproto_dc_id) if host and mtproto_dc_id else None
    account.mtproto_port = int(mtproto_port) if host and mtproto_port else (443 if host else None)


def telegram_json(account: TelegramAccount) -> dict[str, Any]:
    return {
        "id": account.id,
        "session_id": str(account.id),
        "name": account.name,
        "phone": account.phone,
        "api_id": account.api_id,
        "has_api_hash": bool(account.api_hash_ciphertext),
        "api_hash_masked": masked_secret(account.api_hash_ciphertext),
        "http_proxy": account.http_proxy,
        "mtproto_host": account.mtproto_host,
        "mtproto_port": account.mtproto_port,
        "mtproto_dc_id": account.mtproto_dc_id,
        "proxy_enabled": bool(account.http_proxy or (account.mtproto_host and account.mtproto_dc_id)),
        "enabled": account.enabled,
        "authorized": account.authorized,
        "status": "connected" if account.authorized else "pending",
        "created_at": iso(account.created_at),
        "updated_at": iso(account.updated_at),
    }


@router.get("/telegram/accounts", dependencies=auth)
async def telegram_accounts(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    items = (await db.scalars(select(TelegramAccount).order_by(TelegramAccount.id))).all()
    return [telegram_json(item) for item in items]


@router.delete("/telegram/accounts/{account_id}", dependencies=auth, status_code=204)
async def delete_telegram_account(account_id: str, db: AsyncSession = Depends(get_db)) -> Response:
    await db.delete(await one(db, TelegramAccount, account_id))
    await db.commit()
    return Response(status_code=204)


@router.patch("/telegram/accounts/{account_id}/proxy", dependencies=auth)
async def update_telegram_proxy(
    account_id: str,
    payload: TelegramProxyBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    account = await one(db, TelegramAccount, account_id)
    apply_telegram_network(
        account,
        http_proxy=payload.http_proxy,
        mtproto_host=payload.mtproto_host,
        mtproto_port=payload.mtproto_port,
        mtproto_dc_id=payload.mtproto_dc_id,
        clear_proxy=payload.clear_proxy,
    )
    await db.commit()
    await db.refresh(account)
    try:
        await request.app.state.telegram.reconnect(account)
    except Exception:
        pass
    return telegram_json(account)


@router.post("/telegram/accounts/login", dependencies=auth)
async def telegram_login(payload: TelegramLoginBody, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    if not payload.api_hash:
        raise HTTPException(status_code=422, detail="api_hash is required")
    secrets = SecretStore.from_settings(get_settings())
    account = await db.scalar(select(TelegramAccount).where(TelegramAccount.phone == payload.phone))
    if account is None:
        safe = "".join(char for char in payload.phone if char.isdigit() or char == "+")
        account = TelegramAccount(
            phone=payload.phone,
            name=payload.name,
            session_path=str(get_settings().session_dir / f"{safe}.session"),
            api_id=payload.api_id,
            api_hash_ciphertext=secrets.encrypt(payload.api_hash),
        )
        db.add(account)
        await db.commit()
        await db.refresh(account)
    else:
        account.name = payload.name or account.name
        account.api_id = payload.api_id
        account.api_hash_ciphertext = secrets.encrypt(payload.api_hash)
        await db.commit()
    apply_telegram_network(
        account,
        http_proxy=payload.http_proxy,
        mtproto_host=payload.mtproto_host,
        mtproto_port=payload.mtproto_port,
        mtproto_dc_id=payload.mtproto_dc_id,
    )
    await db.commit()
    await db.refresh(account)
    try:
        code_hash = await request.app.state.telegram.request_code(account)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Telegram login unavailable: {exc}") from exc
    return {"session_id": str(account.id), "phone_code_hash": code_hash}


@router.post("/telegram/accounts/verify", dependencies=auth)
async def telegram_verify(payload: TelegramVerifyBody, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    account = await one(db, TelegramAccount, payload.session_id)
    await request.app.state.telegram.verify_code(account, payload.code, payload.password)
    account.authorized = True
    await db.commit()
    await db.refresh(account)
    return telegram_json(account)


@router.get("/memory", dependencies=auth)
async def memory_search(
    request: Request,
    search: str = "",
    agent_id: str | None = None,
) -> list[dict[str, Any]]:
    items = (
        await request.app.state.memory.search(search, agent_id=agent_id)
        if search
        else await request.app.state.memory.get_all(agent_id=agent_id)
    )
    normalized = []
    for item in items:
        metadata = item.get("metadata") or {}
        normalized.append({
            "id": str(item.get("id", "")),
            "agent_id": item.get("agent_id", metadata.get("agent_id")),
            "scope": str(item.get("user_id", metadata.get("user_id", "global"))),
            "key": str(metadata.get("kind") or item.get("key") or item.get("id", "memory")),
            "content": str(item.get("memory", item.get("text", item.get("content", "")))),
            "metadata": metadata,
            "created_at": (
                item.get("created_at")
                or item.get("updated_at")
                or metadata.get("last_message_at")
                or ""
            ),
        })
    return normalized


@router.post("/memory/migrate", dependencies=auth)
async def memory_migrate(request: Request) -> dict[str, Any]:
    memory = request.app.state.memory
    if memory.last_error or memory._client is None:
        raise HTTPException(
            status_code=409,
            detail=memory.last_error or "Memory backend is not connected",
        )
    pending = memory.fallback_count()
    result = await memory.migrate_fallback()
    return {"ok": True, "pending_before": pending, **result}


@router.delete("/memory/{memory_id}", dependencies=auth, status_code=204)
async def memory_delete(memory_id: str, request: Request) -> Response:
    await request.app.state.memory.delete(memory_id)
    return Response(status_code=204)


def mcp_json(server: McpServer) -> dict[str, Any]:
    return {
        "id": server.id,
        "name": server.name,
        "transport": server.transport,
        "command": server.command,
        "args": server.args or [],
        "url": server.url,
        "enabled": server.enabled,
        "env": {key: "********" for key in decrypt_mcp_env(server)},
        "has_env": bool(server.env_ciphertext or server.env),
        "created_at": iso(server.created_at),
        "updated_at": iso(server.updated_at),
    }


def decrypt_mcp_env(server: McpServer) -> dict[str, Any]:
    if not server.env_ciphertext:
        return dict(server.env or {})
    import json

    try:
        value = SecretStore.from_settings(get_settings()).decrypt(server.env_ciphertext)
        return json.loads(value or "{}")
    except Exception:
        return {}


def encrypt_mcp_env(value: dict[str, Any]) -> str | None:
    import json

    cleaned = {str(key): str(val) for key, val in (value or {}).items() if str(key).strip()}
    if not cleaned:
        return None
    return SecretStore.from_settings(get_settings()).encrypt(
        json.dumps(cleaned, ensure_ascii=False)
    )


def _mcp_row_fields(payload: McpBody) -> dict[str, Any]:
    return {
        "name": payload.name.strip(),
        "transport": payload.transport,
        "command": payload.command or None,
        "args": list(payload.args or []),
        "url": payload.url or None,
        "enabled": payload.enabled,
    }


@router.get("/mcp/servers", dependencies=auth)
async def mcp_servers(request: Request, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    results = []
    for item in (await db.scalars(select(McpServer).order_by(McpServer.id))).all():
        result = mcp_json(item)
        result["connection_status"] = (
            "connected" if item.name in request.app.state.mcp.sessions else "disconnected"
        )
        results.append(result)
    return results


@router.post("/mcp/servers", dependencies=auth, status_code=201)
async def create_mcp(payload: McpBody, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    if payload.transport not in {"stdio", "sse", "streamable-http"}:
        raise HTTPException(status_code=422, detail="Unsupported MCP transport")
    if not payload.name.strip():
        raise HTTPException(status_code=422, detail="name is required")
    if payload.transport != "stdio" and not (payload.url or "").strip():
        raise HTTPException(status_code=422, detail="url is required for remote MCP")
    if payload.transport == "stdio" and not (payload.command or "").strip():
        raise HTTPException(status_code=422, detail="command is required for stdio MCP")
    fields = _mcp_row_fields(payload)
    server = McpServer(**fields, env={}, env_ciphertext=encrypt_mcp_env(payload.env))
    db.add(server)
    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Unable to save MCP server: {exc}") from exc
    await db.refresh(server)
    result = mcp_json(server)
    result["connection_status"] = "disconnected"
    if server.enabled:
        try:
            await request.app.state.mcp.hot_reload(server.name, {
                **fields,
                "env": decrypt_mcp_env(server),
            })
            result["connection_status"] = "connected"
        except BaseException as exc:
            result["connection_status"] = "error"
            result["connection_error"] = exception_text(exc)
    return result


@router.put("/mcp/servers/{server_id}", dependencies=auth)
@router.patch("/mcp/servers/{server_id}", dependencies=auth)
async def update_mcp(server_id: str, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    server = await one(db, McpServer, server_id)
    old_name = server.name
    raw = await request.json()
    current = {
        "name": server.name,
        "transport": server.transport,
        "command": server.command,
        "args": server.args or [],
        "url": server.url,
        "enabled": server.enabled,
        "env": {},
        "clear_env": False,
    }
    payload = McpBody.model_validate({**current, **raw})
    if payload.transport not in {"stdio", "sse", "streamable-http"}:
        raise HTTPException(status_code=422, detail="Unsupported MCP transport")
    fields = _mcp_row_fields(payload)
    for key, value in fields.items():
        setattr(server, key, value)
    if payload.clear_env:
        server.env_ciphertext = None
        server.env = {}
    elif "env" in raw and payload.env:
        server.env_ciphertext = encrypt_mcp_env(payload.env)
        server.env = {}
    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Unable to update MCP server: {exc}") from exc
    await db.refresh(server)
    result = mcp_json(server)
    result["connection_status"] = "disconnected"
    if server.enabled:
        try:
            await request.app.state.mcp.hot_reload(server.name, {
                **fields,
                "env": decrypt_mcp_env(server),
            })
            result["connection_status"] = "connected"
        except BaseException as exc:
            result["connection_status"] = "error"
            result["connection_error"] = exception_text(exc)
    return result


@router.delete("/mcp/servers/{server_id}", dependencies=auth, status_code=204)
async def delete_mcp(server_id: str, request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    server = await one(db, McpServer, server_id)
    try:
        await request.app.state.mcp.disconnect(server.name)
    except BaseException:
        pass
    await db.delete(server)
    await db.commit()
    return Response(status_code=204)


def cron_json(job: CronJob) -> dict[str, Any]:
    payload = job.payload or {}
    return {
        "id": job.id,
        "name": job.name,
        "agent_id": job.agent_id,
        "schedule": job.cron,
        "run_once_at": payload.get("run_once_at"),
        "prompt": payload.get("prompt", payload.get("message", "")),
        "timezone": payload.get("timezone", "UTC"),
        "enabled": job.enabled,
        "last_run_at": iso(job.last_run_at),
        "created_at": iso(job.created_at),
        "updated_at": iso(job.updated_at),
    }


def cron_values(payload: CronBody) -> tuple[str, dict[str, Any]]:
    try:
        zone = ZoneInfo(payload.timezone)
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(status_code=422, detail="Unknown IANA timezone") from exc
    run_once_at = (payload.run_once_at or "").strip()
    if run_once_at:
        try:
            run_at = datetime.fromisoformat(run_once_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail="run_once_at must be a valid date and time",
            ) from exc
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=zone)
        run_at = run_at.astimezone(timezone.utc)
        if payload.enabled and run_at <= datetime.now(timezone.utc):
            raise HTTPException(
                status_code=422,
                detail="One-time launch must be in the future",
            )
        schedule = "@once"
        normalized_run_at: str | None = run_at.isoformat()
    else:
        schedule = payload.schedule.strip()
        if schedule == "@once":
            raise HTTPException(
                status_code=422,
                detail="Specify the date and time for a one-time launch",
            )
        try:
            CronTrigger.from_crontab(schedule, timezone=zone)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid cron expression: {exc}") from exc
        normalized_run_at = None
    return schedule, {
        "prompt": payload.prompt,
        "message": payload.prompt,
        "timezone": payload.timezone,
        **({"run_once_at": normalized_run_at} if normalized_run_at else {}),
    }


@router.get("/cron", dependencies=auth)
async def cron_jobs(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    return [cron_json(item) for item in (await db.scalars(select(CronJob).order_by(CronJob.id))).all()]


@router.post("/cron", dependencies=auth, status_code=201)
async def create_cron(payload: CronBody, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    schedule, job_payload = cron_values(payload)
    job = CronJob(
        name=payload.name,
        agent_id=as_int(payload.agent_id, "agent_id"),
        cron=schedule,
        payload=job_payload,
        enabled=payload.enabled,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    if job.enabled:
        request.app.state.scheduler.upsert(job)
    return cron_json(job)


@router.put("/cron/{job_id}", dependencies=auth)
@router.patch("/cron/{job_id}", dependencies=auth)
async def update_cron(job_id: str, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    job = await one(db, CronJob, job_id)
    payload = CronBody.model_validate({**cron_json(job), **await request.json()})
    schedule, job_payload = cron_values(payload)
    job.name = payload.name
    job.agent_id = as_int(payload.agent_id, "agent_id")
    job.cron = schedule
    job.payload = job_payload
    job.enabled = payload.enabled
    await db.commit()
    await db.refresh(job)
    request.app.state.scheduler.remove(job.id)
    if job.enabled:
        request.app.state.scheduler.upsert(job)
    return cron_json(job)


@router.delete("/cron/{job_id}", dependencies=auth, status_code=204)
async def delete_cron(job_id: str, request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    job = await one(db, CronJob, job_id)
    request.app.state.scheduler.remove(job.id)
    await db.delete(job)
    await db.commit()
    return Response(status_code=204)


def admin_json(settings: AdminSettings) -> dict[str, Any]:
    values = settings.settings or {}
    return {
        "admin_ids": settings.telegram_ids,
        "escalation_enabled": values.get("escalation_enabled", False),
        "escalation_chat_id": values.get("escalation_chat_id"),
        "escalation_prompt": values.get("escalation_prompt", ""),
        **{key: value for key, value in values.items() if key not in {"escalation_enabled", "escalation_chat_id", "escalation_prompt"}},
        "updated_at": iso(settings.updated_at),
    }


async def get_admin(db: AsyncSession) -> AdminSettings:
    settings = await db.get(AdminSettings, 1)
    if settings is None:
        defaults = get_settings()
        settings = AdminSettings(id=1, telegram_ids=[str(value) for value in sorted(defaults.admin_ids)], settings={})
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return settings


@router.get("/settings/admin", dependencies=auth)
async def admin_settings(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return admin_json(await get_admin(db))


@router.put("/settings/admin", dependencies=auth)
async def update_admin_settings(
    payload: AdminBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    settings = await get_admin(db)
    settings.telegram_ids = [str(value) for value in payload.admin_ids]
    values = payload.model_dump(exclude={"admin_ids"})
    extras = payload.model_extra or {}
    settings.settings = {**values, **extras}
    await db.commit()
    await db.refresh(settings)
    request.app.state.telegram.set_admin_ids(settings.telegram_ids)
    return admin_json(settings)


async def get_runtime_settings(db: AsyncSession) -> RuntimeSettings:
    settings = await db.get(RuntimeSettings, 1)
    if settings is None:
        settings = RuntimeSettings(id=1)
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return settings


def runtime_json(settings: RuntimeSettings, *, memory_error: str | None = None) -> dict[str, Any]:
    return {
        "search_provider": settings.search_provider,
        "searxng_url": settings.searxng_url,
        "memory_enabled": settings.memory_enabled,
        "memory_backend": settings.memory_backend,
        "has_mem0_api_key": bool(settings.mem0_api_key_ciphertext),
        "mem0_api_key_masked": masked_secret(settings.mem0_api_key_ciphertext),
        "memory_status": "error" if memory_error else ("enabled" if settings.memory_enabled else "disabled"),
        "memory_error": memory_error,
        "qdrant_url": settings.qdrant_url,
        "memory_llm_profile_id": settings.memory_llm_profile_id,
        "typing_min_seconds": settings.typing_min_seconds,
        "typing_max_seconds": settings.typing_max_seconds,
        "typing_jitter_seconds": settings.typing_jitter_seconds,
        "typing_chunk_size": settings.typing_chunk_size,
        "typing_presence": settings.typing_presence,
        "task_workers": settings.task_workers,
        "max_tool_rounds": settings.max_tool_rounds,
        "timezone": settings.timezone,
        "telegram_history_limit": settings.telegram_history_limit,
        "recent_context_messages": settings.recent_context_messages,
        "context_max_chars": settings.context_max_chars,
        "summarization_enabled": settings.summarization_enabled,
        "summarize_after_messages": settings.summarize_after_messages,
        "updated_at": iso(settings.updated_at),
    }


@router.get("/settings/runtime", dependencies=auth)
async def read_runtime_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return runtime_json(
        await get_runtime_settings(db),
        memory_error=request.app.state.memory.last_error,
    )


@router.put("/settings/runtime", dependencies=auth)
async def update_runtime_configuration(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    settings = await get_runtime_settings(db)
    payload = RuntimeSettingsBody.model_validate(
        {
            **runtime_json(settings, memory_error=request.app.state.memory.last_error),
            **await request.json(),
        }
    )
    if payload.typing_min_seconds > payload.typing_max_seconds:
        raise HTTPException(
            status_code=422,
            detail="typing_min_seconds must not exceed typing_max_seconds",
        )
    if payload.search_provider == "searxng" and not payload.searxng_url:
        raise HTTPException(
            status_code=422,
            detail="searxng_url is required for the searxng provider",
        )
    if payload.memory_llm_profile_id is not None and await db.get(
        LlmProfile, payload.memory_llm_profile_id
    ) is None:
        raise HTTPException(status_code=422, detail="Memory LLM profile not found")
    for key, value in payload.model_dump(
        exclude={"mem0_api_key", "clear_mem0_api_key"}
    ).items():
        setattr(settings, key, value)
    secrets = SecretStore.from_settings(get_settings())
    if payload.clear_mem0_api_key:
        settings.mem0_api_key_ciphertext = None
    elif payload.mem0_api_key:
        settings.mem0_api_key_ciphertext = secrets.encrypt(payload.mem0_api_key)
    await db.commit()
    await db.refresh(settings)
    secret = secrets.decrypt(settings.mem0_api_key_ciphertext)
    memory_llm = None
    if settings.memory_llm_profile_id is not None:
        profile = await db.get(LlmProfile, settings.memory_llm_profile_id)
        if profile and profile.enabled:
            profile_key = secrets.decrypt(profile.api_key_ciphertext)
            if profile_key:
                memory_llm = {
                    "api_key": profile_key,
                    "base_url": profile.base_url,
                    "model": profile.default_model,
                }
    request.app.state.search.configure(settings.search_provider, settings.searxng_url)
    request.app.state.telegram.configure_runtime(settings)
    await request.app.state.memory.reconfigure(settings, secret, memory_llm)
    if len(request.app.state.task_bus._workers) != settings.task_workers:
        await request.app.state.task_bus.stop()
        if settings.task_workers:
            await request.app.state.task_bus.start(settings.task_workers)
    return runtime_json(settings, memory_error=request.app.state.memory.last_error)


def conversation_json(state: ConversationState) -> dict[str, Any]:
    return {
        "id": state.id,
        "agent_id": state.agent_id,
        "account_id": state.account_id,
        "chat_id": state.chat_id,
        "user_id": state.user_id,
        "rolling_summary": state.rolling_summary,
        "summary_through_message_id": state.summary_through_message_id,
        "summary_through_message_at": iso(state.summary_through_message_at),
        "last_message_id": state.last_message_id,
        "last_message_at": iso(state.last_message_at),
        "last_user_message_at": iso(state.last_user_message_at),
        "last_agent_message_at": iso(state.last_agent_message_at),
        "message_count": state.message_count,
        "metadata_json": state.metadata_json,
        "created_at": iso(state.created_at),
        "updated_at": iso(state.updated_at),
    }


@router.get("/conversations", dependencies=auth)
async def conversations(
    db: AsyncSession = Depends(get_db),
    agent_id: int | None = None,
    search: str = "",
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    filters = []
    if agent_id is not None:
        filters.append(ConversationState.agent_id == agent_id)
    if search.strip():
        pattern = f"%{search.strip()}%"
        filters.append(or_(
            ConversationState.chat_id.ilike(pattern),
            ConversationState.user_id.ilike(pattern),
            ConversationState.rolling_summary.ilike(pattern),
        ))
    total = int(
        await db.scalar(
            select(func.count()).select_from(ConversationState).where(*filters)
        )
        or 0
    )
    items = (
        await db.scalars(
            select(ConversationState)
            .where(*filters)
            .order_by(ConversationState.last_message_at.desc(), ConversationState.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return {
        "items": [conversation_json(item) for item in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/conversations/{conversation_id}", dependencies=auth)
async def conversation_detail(
    conversation_id: int,
    db: AsyncSession = Depends(get_db),
    message_limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    state = await one(db, ConversationState, conversation_id)
    messages = (
        await db.scalars(
            select(MessageLog)
            .where(
                MessageLog.agent_id == state.agent_id,
                MessageLog.account_id == state.account_id,
                MessageLog.chat_id == state.chat_id,
                MessageLog.user_id == state.user_id,
                MessageLog.direction.in_(("in", "out")),
            )
            .order_by(
                func.coalesce(MessageLog.message_at, MessageLog.created_at).desc(),
                MessageLog.id.desc(),
            )
            .limit(message_limit)
        )
    ).all()
    return {
        **conversation_json(state),
        "messages": [row(item) for item in reversed(messages)],
    }


@router.delete("/conversations/{conversation_id}", dependencies=auth, status_code=204)
async def clear_conversation(
    conversation_id: int,
    db: AsyncSession = Depends(get_db),
) -> Response:
    state = await one(db, ConversationState, conversation_id)
    await db.execute(
        delete(MessageLog).where(
            MessageLog.agent_id == state.agent_id,
            MessageLog.account_id == state.account_id,
            MessageLog.chat_id == state.chat_id,
            MessageLog.user_id == state.user_id,
        )
    )
    await db.delete(state)
    await db.commit()
    return Response(status_code=204)


@router.post("/settings/runtime/test-search", dependencies=auth)
async def test_search(request: Request) -> dict[str, Any]:
    search = request.app.state.search
    try:
        results = await search.search("частная школа Екатеринбург", 3)
    except Exception as exc:
        return {
            "ok": False,
            "status": "error",
            "detail": str(exc),
            "provider": search.provider,
            "searxng_url": search.searxng_url,
        }
    sample = results[0]["title"] if results else None
    if not results:
        return {
            "ok": False,
            "status": "empty",
            "detail": (
                "Провайдер ответил, но вернул 0 результатов. "
                "Проверьте, что у SearXNG включён JSON format и поисковые движки."
            ),
            "provider": search.provider,
            "searxng_url": search.searxng_url,
            "result_count": 0,
        }
    return {
        "ok": True,
        "status": "connected",
        "detail": f"Поиск работает · {len(results)} результат(ов)"
        + (f" · пример: {sample}" if sample else ""),
        "provider": search.provider,
        "searxng_url": search.searxng_url,
        "result_count": len(results),
        "sample": sample,
    }


@router.get("/logs", dependencies=auth)
async def logs(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    items = (await db.scalars(select(MessageLog).order_by(MessageLog.created_at.desc()).offset(offset).limit(limit))).all()
    return [
        {
            "id": item.id,
            "timestamp": iso(item.created_at),
            "level": (
                "error"
                if item.direction == "tool"
                and (item.metadata_json or {}).get("status") == "error"
                else "info"
            ),
            "source": (
                str((item.metadata_json or {}).get("tool", "tool"))
                if item.direction == "tool"
                else f"agent.{item.direction}"
            ),
            "message": item.text,
            "context": item.metadata_json or {},
        }
        for item in items
    ]


@router.get("/tasks", dependencies=auth)
async def tasks(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    items = (await db.scalars(select(AgentTask).order_by(AgentTask.created_at.desc()).offset(offset).limit(limit))).all()
    return [row(item) for item in items]


async def websocket_events(websocket: WebSocket, token: str, settings: Settings) -> None:
    try:
        verify_token(token, settings)
    except HTTPException:
        await websocket.close(code=4401)
        return
    await events.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        events.disconnect(websocket)


@router.websocket("/ws/events")
async def versioned_websocket(websocket: WebSocket, token: str, settings: Settings = Depends(get_settings)) -> None:
    await websocket_events(websocket, token, settings)
