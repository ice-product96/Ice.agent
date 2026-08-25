from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings, get_settings
from .db import (
    AdminSettings, Agent, AgentLink, AgentTask, CronJob, LlmProfile, McpServer,
    Consultation, ConversationState, CursorRun, Customer, DecisionRecord, EmployeeNeed, EmployeePlan, EmployeeProfile,
    MessageLog, PromptSection, RuntimeSettings, SipAccount, SipCall,
    ProjectState, TelegramAccount, WorkItem, agent_mcp_servers, get_db,
)
from .employee import (
    HEARTBEAT_JOB_PREFIX,
    PROMPT_SECTION_KEYS,
    consultation_json,
    ensure_prompt_sections,
    get_or_create_profile,
    list_agent_jobs,
    list_prompt_revisions,
    need_json,
    plan_json,
    profile_json,
    prompt_revision_json,
    restore_prompt_revision,
    save_prompt_section,
    schedule_immediate_tick,
    sync_heartbeat_job,
)
from .events import events
from .integrations import exception_text
from .job_result import public_job_result
from .memory_clear import clear_journals
from .schemas import ConversationPatch, LlmProfileBody, RuntimeSettingsBody
from .secrets import SecretStore, masked_secret
from .security import issue_token, require_admin, valid_password, verify_token
from .timezones import normalize_timezone

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
    sip_account_id: int | str | None = None
    llm_profile_id: int | str | None = None
    tools: list[Any] = Field(default_factory=list)
    tool_permissions: list[str] = Field(default_factory=list)
    links: list[Any] = Field(default_factory=list)
    typing_enabled: bool = True
    enabled: bool = True
    status: str | None = None
    realtime_voice: str | None = None
    realtime_model: str | None = None
    inbound_greeting: str | None = None


class SipAccountBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    sip_server: str = "voice.telphin.com:5068"
    domain: str = "sip.telphin.com"
    login: str
    auth_username: str | None = None
    password: str = ""
    clear_password: bool = False
    transport: str = "udp"
    sip_proxy: str | None = None
    display_name: str = ""
    caller_id: str | None = None
    stun_server: str | None = None
    public_ip: str | None = None
    enabled: bool = True
    register_on_startup: bool = True
    max_concurrent_calls: int = 1
    ring_delay_seconds: float = 4.0


class SipDialBody(BaseModel):
    agent_id: int | str
    number: str
    sip_account_id: int | str | None = None


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
        "sip_accounts": await count(SipAccount),
        "sip_calls": await count(SipCall),
        "mcp_servers": await count(McpServer),
        "cron_jobs": await count(CronJob),
        "tasks": await count(AgentTask),
        "logs": await count(MessageLog),
        "active_conversations": await count(ConversationState),
        "open_consultations": int(
            await db.scalar(
                select(func.count()).select_from(Consultation).where(Consultation.status == "open")
            )
            or 0
        ),
        "failed_work_items": int(
            await db.scalar(
                select(func.count()).select_from(WorkItem).where(WorkItem.status == "failed")
            )
            or 0
        ),
        "waiting_manager": int(
            await db.scalar(
                select(func.count()).select_from(WorkItem).where(WorkItem.status == "waiting_manager")
            )
            or 0
        ),
        "autonomous_agents": int(
            await db.scalar(
                select(func.count()).select_from(EmployeeProfile).where(
                    EmployeeProfile.autonomy_enabled.is_(True)
                )
            )
            or 0
        ),
    }
    profiles = {
        item.id: item
        for item in (await db.scalars(select(LlmProfile))).all()
    }
    accounts = {
        item.id: item
        for item in (await db.scalars(select(TelegramAccount))).all()
    }
    sip_accounts = {
        item.id: item
        for item in (await db.scalars(select(SipAccount))).all()
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
        if agent.sip_account_id is not None:
            sip_account = sip_accounts.get(agent.sip_account_id)
            if sip_account is None:
                reasons.append("sip_account_missing")
            elif not sip_account.enabled:
                reasons.append("sip_account_disabled")
            elif not sip_account.password_ciphertext:
                reasons.append("sip_credentials_missing")
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
                and (
                    (
                        runtime_settings.search_provider == "searxng"
                        and not runtime_settings.searxng_url
                    )
                    or (
                        runtime_settings.search_provider == "tavily"
                        and not runtime_settings.tavily_api_key_ciphertext
                    )
                )
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
        "sip": {
            "status": (
                "missing"
                if not sip_accounts
                else "degraded"
                if any(
                    not item.enabled or not item.password_ciphertext
                    for item in sip_accounts.values()
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
    sip_gateway = getattr(request.app.state, "sip", None)
    sip_registered = 0
    active_sip_calls = 0
    if sip_gateway is not None:
        for item in sip_accounts.values():
            reg = sip_gateway.registration(item.id)
            if reg.get("registered"):
                sip_registered += 1
        active_sip_calls = len(sip_gateway.list_active_calls())
    else:
        sip_registered = sum(1 for item in sip_accounts.values() if item.enabled and item.password_ciphertext)
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
        "sip_accounts": {
            "total": counts["sip_accounts"],
            "registered": sip_registered,
            "active_calls": active_sip_calls,
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
        "sip_account_id": agent.sip_account_id,
        "llm_profile_id": agent.llm_profile_id,
        "tools": config.get("tools", []),
        "tool_permissions": config.get("tool_permissions", []),
        "realtime_voice": config.get("realtime_voice", "marin"),
        "realtime_model": config.get("realtime_model", "gpt-realtime-2"),
        "inbound_greeting": config.get("inbound_greeting", ""),
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
    agent.sip_account_id = (
        as_int(payload.sip_account_id, "sip_account_id")
        if payload.sip_account_id is not None and str(payload.sip_account_id).strip() != ""
        else None
    )
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
    if payload.realtime_voice is not None:
        config["realtime_voice"] = payload.realtime_voice
    if payload.realtime_model is not None:
        config["realtime_model"] = payload.realtime_model.strip().lower()
    if payload.inbound_greeting is not None:
        config["inbound_greeting"] = payload.inbound_greeting
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
    if payload.sip_account_id is not None and str(payload.sip_account_id).strip() != "":
        if await db.get(SipAccount, as_int(payload.sip_account_id, "sip_account_id")) is None:
            raise HTTPException(status_code=422, detail="SIP account not found")
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


class EmployeeProfileBody(BaseModel):
    autonomy_enabled: bool | None = None
    paused: bool | None = None
    heartbeat_minutes: int | None = Field(default=None, ge=1, le=120)
    workday_start: str | None = None
    workday_end: str | None = None
    timezone: str | None = None
    budget_ticks_per_day: int | None = Field(default=None, ge=1, le=500)
    role_title: str | None = None
    mission: str | None = None
    prompt_sections: dict[str, str] | None = None
    policy: dict[str, Any] | None = None


@router.get("/employees/policy-catalog", dependencies=auth)
async def employee_policy_catalog() -> dict[str, Any]:
    from .employee_policy import policy_catalog

    return policy_catalog()


class ConsultationResolveBody(BaseModel):
    status: str = "answered"  # answered|approved|rejected
    answer_text: str = ""
    schedule_tick: bool = True


class ConsultationDismissBody(BaseModel):
    reason: str = "Не актуально"


class WorkItemNoteBody(BaseModel):
    note: str = ""


class WorkItemWaitBody(BaseModel):
    minutes: int = Field(default=15, ge=1, le=180)
    note: str = ""


class ProjectStateBody(BaseModel):
    autonomy_level: str | None = Field(default=None, pattern=r"^LEVEL_[0-3]$")
    config: dict[str, Any] | None = None


def project_state_json(state: ProjectState) -> dict[str, Any]:
    from .project_schedule import enrich_project_state_payload

    payload = {
        "project_id": state.project_id,
        "autonomy_level": state.autonomy_level,
        "config": state.config or {},
        "created_at": state.created_at.isoformat() if state.created_at else None,
        "updated_at": state.updated_at.isoformat() if state.updated_at else None,
    }
    payload.update(enrich_project_state_payload(state))
    return payload


def decision_record_json(record: DecisionRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "project_id": record.project_id,
        "work_item_id": record.work_item_id,
        "topic": getattr(record, "topic", ""),
        "decision": record.decision,
        "rationale": record.rationale,
        "confirmed_by": getattr(record, "confirmed_by", ""),
        "source_message_id": getattr(record, "source_message_id", None),
        "context": record.context_json or {},
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def cursor_run_json(run: CursorRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "work_item_id": run.work_item_id,
        "project_id": run.project_id,
        "attempt": run.attempt,
        "status": run.status,
        "request": run.request_json or {},
        "result": run.result_json,
        "error": run.error,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


@router.get("/pm/projects", dependencies=auth)
async def list_pm_projects(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    rows = (await db.scalars(select(ProjectState).order_by(ProjectState.project_id))).all()
    return [project_state_json(row) for row in rows]


@router.get("/pm/projects/{project_id}", dependencies=auth)
async def get_pm_project(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    state = await db.get(ProjectState, project_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Project state not found")
    decisions = (
        await db.scalars(
            select(DecisionRecord)
            .where(DecisionRecord.project_id == project_id)
            .order_by(DecisionRecord.created_at.desc())
            .limit(200)
        )
    ).all()
    tasks = (
        await db.scalars(
            select(WorkItem)
            .where(WorkItem.project_id == project_id)
            .order_by(WorkItem.updated_at.desc())
            .limit(200)
        )
    ).all()
    from .work_items import work_item_json

    return {
        **project_state_json(state),
        "decisions": [decision_record_json(row) for row in decisions],
        "tasks": [work_item_json(row) for row in tasks],
    }


@router.patch("/pm/projects/{project_id}", dependencies=auth)
async def update_pm_project(
    project_id: str,
    payload: ProjectStateBody,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from .pm_state import get_or_create_project_state, normalize_autonomy_level

    state = await get_or_create_project_state(db, project_id)
    if payload.autonomy_level is not None:
        state.autonomy_level = normalize_autonomy_level(payload.autonomy_level)
    if payload.config is not None:
        merged = dict(state.config or {})
        for key, value in dict(payload.config).items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        state.config = merged
    await db.commit()
    return project_state_json(state)


class CustomerBody(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=200)
    notes: str = ""
    agent_id: int | str | None = None
    project_id: str = ""
    cursor_workspace: str = ""
    cursor_window_id: str | None = None
    is_default: bool = False


@router.get("/customers", dependencies=auth)
async def list_customers(
    db: AsyncSession = Depends(get_db),
    agent_id: int | None = None,
) -> list[dict[str, Any]]:
    from .customers import customer_json

    query = select(Customer).order_by(Customer.is_default.desc(), Customer.name.asc())
    if agent_id is not None:
        query = query.where(Customer.agent_id == agent_id)
    rows = (await db.scalars(query)).all()
    return [customer_json(row) for row in rows]


@router.get("/cursor/projects", dependencies=auth)
async def list_cursor_projects(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    from .customers import collect_cursor_projects

    mcp = getattr(request.app.state, "mcp", None)
    # Refresh must reattach after CursorRemote restarts — otherwise the UI keeps
    # only stale saved customers and never sees the live allowlist.
    if mcp is not None:
        servers = (
            await db.scalars(select(McpServer).where(McpServer.enabled.is_(True)))
        ).all()
        for server in servers:
            if "cursorremote" not in (server.name or "").lower():
                continue
            if mcp.sessions.get(server.name) is not None:
                continue
            try:
                await mcp.hot_reload(
                    server.name,
                    {
                        "transport": server.transport,
                        "command": server.command,
                        "args": server.args or [],
                        "url": server.url,
                        "env": decrypt_mcp_env(server),
                    },
                )
            except BaseException:
                import logging

                logging.getLogger(__name__).warning(
                    "CursorRemote reconnect failed while listing projects",
                    exc_info=True,
                )
    return await collect_cursor_projects(db, mcp)


@router.post("/customers", dependencies=auth, status_code=201)
async def create_customer(
    payload: CustomerBody,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from .customers import apply_customer_defaults, customer_json, slugify

    customer_id = slugify(payload.id or payload.name)
    if await db.get(Customer, customer_id) is not None:
        raise HTTPException(status_code=409, detail=f"Customer '{customer_id}' already exists")
    agent_id = (
        as_int(payload.agent_id, "agent_id")
        if payload.agent_id is not None and str(payload.agent_id).strip() != ""
        else None
    )
    if agent_id is not None and await db.get(Agent, agent_id) is None:
        raise HTTPException(status_code=422, detail="agent_id not found")
    project_id = (payload.project_id or "").strip() or customer_id
    customer = Customer(
        id=customer_id,
        name=payload.name.strip(),
        notes=(payload.notes or "").strip(),
        agent_id=agent_id,
        project_id=project_id,
        cursor_workspace=(payload.cursor_workspace or "").strip(),
        cursor_window_id=(payload.cursor_window_id or None),
        is_default=False,
    )
    db.add(customer)
    await db.flush()
    make_default = bool(payload.is_default)
    if agent_id is not None and not make_default:
        others = await db.scalar(
            select(func.count())
            .select_from(Customer)
            .where(Customer.agent_id == agent_id, Customer.id != customer.id)
        )
        make_default = int(others or 0) == 0
    await apply_customer_defaults(db, customer, make_default=make_default)
    await db.commit()
    await db.refresh(customer)
    return customer_json(customer)


@router.patch("/customers/{customer_id}", dependencies=auth)
async def update_customer(
    customer_id: str,
    payload: CustomerBody,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from .customers import apply_customer_defaults, customer_json

    customer = await db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    agent_id = (
        as_int(payload.agent_id, "agent_id")
        if payload.agent_id is not None and str(payload.agent_id).strip() != ""
        else None
    )
    if agent_id is not None and await db.get(Agent, agent_id) is None:
        raise HTTPException(status_code=422, detail="agent_id not found")
    customer.name = payload.name.strip()
    customer.notes = (payload.notes or "").strip()
    customer.agent_id = agent_id
    customer.project_id = (payload.project_id or "").strip() or customer.id
    customer.cursor_workspace = (payload.cursor_workspace or "").strip()
    customer.cursor_window_id = payload.cursor_window_id or None
    await apply_customer_defaults(db, customer, make_default=payload.is_default)
    await db.commit()
    await db.refresh(customer)
    return customer_json(customer)


@router.delete("/customers/{customer_id}", dependencies=auth, status_code=204)
async def delete_customer(customer_id: str, db: AsyncSession = Depends(get_db)) -> Response:
    customer = await db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    if customer.agent_id is not None:
        agent = await db.get(Agent, customer.agent_id)
        if agent is not None:
            config = dict(agent.config or {})
            memory = dict(config.get("memory") or {})
            if memory.get("default_customer_id") == customer.id:
                memory.pop("default_customer_id", None)
                memory.pop("default_project_id", None)
                config["memory"] = memory
                agent.config = config
    await db.delete(customer)
    await db.commit()
    return Response(status_code=204)


@router.get("/agents/{agent_id}/employee", dependencies=auth)
async def get_employee(agent_id: str, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    agent = await one(db, Agent, agent_id)
    profile = await get_or_create_profile(db, agent.id)
    sections = await ensure_prompt_sections(db, agent)
    plans = (
        await db.scalars(
            select(EmployeePlan)
            .where(EmployeePlan.agent_id == agent.id)
            .order_by(EmployeePlan.id.desc())
            .limit(40)
        )
    ).all()
    needs = (
        await db.scalars(
            select(EmployeeNeed)
            .where(EmployeeNeed.agent_id == agent.id)
            .order_by(EmployeeNeed.priority.asc(), EmployeeNeed.id.desc())
            .limit(40)
        )
    ).all()
    jobs = await list_agent_jobs(db, agent.id, enabled_only=False)
    consults = (
        await db.scalars(
            select(Consultation)
            .where(Consultation.agent_id == agent.id, Consultation.status == "open")
            .order_by(Consultation.id.desc())
            .limit(40)
        )
    ).all()
    from .work_items import counts_for_agent, list_work_items, work_item_json

    work_items = await list_work_items(db, agent.id, status="all", limit=80)
    from .customers import customer_json

    customers = (
        await db.scalars(
            select(Customer)
            .where(Customer.agent_id == agent.id)
            .order_by(Customer.is_default.desc(), Customer.name.asc())
        )
    ).all()
    return {
        "agent_id": agent.id,
        "agent_name": agent.name,
        "profile": profile_json(profile),
        "prompt_sections": sections,
        "plans": [plan_json(p) for p in plans],
        "jobs": [cron_json(job) for job in jobs[:40]],
        "needs": [need_json(n) for n in needs],
        "consultations": [consultation_json(c) for c in consults],
        "work_items": [work_item_json(item) for item in work_items],
        "work_item_counts": await counts_for_agent(db, agent.id),
        "customers": [customer_json(row) for row in customers],
    }


@router.patch("/agents/{agent_id}/employee", dependencies=auth)
async def patch_employee(
    agent_id: str,
    payload: EmployeeProfileBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    agent = await one(db, Agent, agent_id)
    profile = await get_or_create_profile(db, agent.id)
    data = payload.model_dump(exclude_unset=True)
    sections_payload = data.pop("prompt_sections", None)
    policy_payload = data.pop("policy", None)
    for key, value in data.items():
        if value is not None:
            if key == "timezone":
                value = normalize_timezone(str(value))
            setattr(profile, key, value)
    if policy_payload is not None:
        current = dict(profile.config_json or {})
        merged_policy = dict(current.get("policy") or {})
        merged_policy.update(policy_payload)
        current["policy"] = merged_policy
        profile.config_json = current
    if sections_payload:
        await ensure_prompt_sections(db, agent)
        for key, content in sections_payload.items():
            if key not in PROMPT_SECTION_KEYS:
                continue
            await save_prompt_section(
                db,
                agent,
                key,
                str(content or ""),
                source="manager",
                note="Сохранено руководителем",
            )
    await db.commit()
    await db.refresh(profile)
    scheduler = getattr(request.app.state, "scheduler", None)
    await sync_heartbeat_job(db, scheduler, profile)
    return await get_employee(str(agent.id), request, db)


@router.get("/agents/{agent_id}/employee/prompt-revisions", dependencies=auth)
async def get_employee_prompt_revisions(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    key: str | None = Query(default=None),
    limit: int = Query(40, ge=1, le=100),
) -> dict[str, Any]:
    agent = await one(db, Agent, agent_id)
    try:
        rows = await list_prompt_revisions(db, agent.id, key=key, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "items": [prompt_revision_json(row) for row in rows],
        "total": len(rows),
        "key": key,
    }


class PromptRevisionRestoreBody(BaseModel):
    note: str = ""


@router.post(
    "/agents/{agent_id}/employee/prompt-revisions/{revision_id}/restore",
    dependencies=auth,
)
async def restore_employee_prompt_revision(
    agent_id: str,
    revision_id: int,
    payload: PromptRevisionRestoreBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    agent = await one(db, Agent, agent_id)
    try:
        section = await restore_prompt_revision(
            db,
            agent,
            revision_id,
            note=payload.note,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Revision not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    employee = await get_employee(str(agent.id), request, db)
    return {
        "ok": True,
        "restored_key": section.key,
        "revision_id": revision_id,
        "employee": employee,
    }


@router.post("/agents/{agent_id}/employee/pause", dependencies=auth)
async def pause_employee(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    paused: bool = Query(True),
) -> dict[str, Any]:
    agent = await one(db, Agent, agent_id)
    profile = await get_or_create_profile(db, agent.id)
    profile.paused = bool(paused)
    await db.commit()
    scheduler = getattr(request.app.state, "scheduler", None)
    await sync_heartbeat_job(db, scheduler, profile)
    return {"ok": True, "paused": profile.paused, "profile": profile_json(profile)}


@router.post("/agents/{agent_id}/employee/tick", dependencies=auth)
async def force_employee_tick(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    agent = await one(db, Agent, agent_id)
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="Runtime unavailable")
    return await runtime.tick(db, agent, force=True, reason="manual")


async def _agent_work_item(db: AsyncSession, agent_id: str, work_item_id: int) -> tuple[Agent, Any]:
    from .work_items import get_work_item

    agent = await one(db, Agent, agent_id)
    item = await get_work_item(db, work_item_id)
    if item is None or item.agent_id != agent.id:
        raise HTTPException(status_code=404, detail="Work item not found")
    return agent, item


@router.get("/agents/{agent_id}/work-items", dependencies=auth)
async def list_agent_work_items(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    status: str = "open",
    limit: int = Query(80, ge=1, le=200),
) -> dict[str, Any]:
    from .work_items import counts_for_agent, list_work_items, work_item_json

    agent = await one(db, Agent, agent_id)
    items = await list_work_items(db, agent.id, status=status, limit=limit)
    return {
        "items": [work_item_json(item) for item in items],
        "counts": await counts_for_agent(db, agent.id),
    }


@router.get("/agents/{agent_id}/work-items/{work_item_id}", dependencies=auth)
async def get_agent_work_item(
    agent_id: str,
    work_item_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from .work_items import list_events, work_item_json

    _, item = await _agent_work_item(db, agent_id, work_item_id)
    events = list(reversed(await list_events(db, item.id, limit=120)))
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
    project = await db.get(ProjectState, item.project_id) if item.project_id else None
    result = work_item_json(item, events=events)
    result.update(
        decisions=[decision_record_json(row) for row in decisions],
        cursor_runs=[cursor_run_json(row) for row in runs],
        project=project_state_json(project) if project else None,
    )
    from .employee import get_or_create_profile
    from .project_schedule import enrich_work_item_commerce

    profile = await get_or_create_profile(db, item.agent_id)
    result.update(
        enrich_work_item_commerce(item, project, list(runs), profile=profile)
    )
    return result


@router.post("/agents/{agent_id}/work-items/{work_item_id}/resume", dependencies=auth)
async def resume_agent_work_item(
    agent_id: str,
    work_item_id: int,
    payload: WorkItemNoteBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from .work_items import resume_work_item, work_item_aborted, work_item_json

    agent, item = await _agent_work_item(db, agent_id, work_item_id)
    if work_item_aborted(item):
        raise HTTPException(status_code=409, detail="work item aborted")
    await resume_work_item(db, item, note=payload.note)
    scheduler = getattr(request.app.state, "scheduler", None)
    await schedule_immediate_tick(
        db,
        scheduler,
        agent.id,
        reason="manual",
        work_item_id=item.id,
        manager_answer=(payload.note or "").strip(),
    )
    return {
        "ok": True,
        "item": work_item_json(item),
        "message": "Тик запланирован — кейс продолжится в фоне.",
    }


@router.post("/agents/{agent_id}/work-items/{work_item_id}/pause", dependencies=auth)
async def pause_agent_work_item(
    agent_id: str,
    work_item_id: int,
    payload: WorkItemNoteBody,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from .work_items import pause_work_item, work_item_json

    _, item = await _agent_work_item(db, agent_id, work_item_id)
    await pause_work_item(db, item, note=payload.note)
    return {"ok": True, "item": work_item_json(item)}


@router.post("/agents/{agent_id}/work-items/{work_item_id}/close", dependencies=auth)
async def close_agent_work_item(
    agent_id: str,
    work_item_id: int,
    payload: WorkItemNoteBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from .work_items import close_work_item, work_item_json

    _, item = await _agent_work_item(db, agent_id, work_item_id)
    if item.pm_phase not in {"DISCUSSION", "DONE"}:
        raise HTTPException(
            status_code=409,
            detail="Structured PM task must pass pm_accept_task before closure",
        )
    scheduler = getattr(request.app.state, "scheduler", None)
    await close_work_item(db, item, note=payload.note, scheduler=scheduler)
    return {"ok": True, "item": work_item_json(item)}


@router.post("/agents/{agent_id}/work-items/{work_item_id}/abort", dependencies=auth)
async def abort_agent_work_item(
    agent_id: str,
    work_item_id: int,
    payload: WorkItemNoteBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from .work_items import abort_work_item, work_item_aborted, work_item_json

    _, item = await _agent_work_item(db, agent_id, work_item_id)
    if work_item_aborted(item):
        return {
            "ok": True,
            "item": work_item_json(item),
            "message": "Кейс уже отменён.",
        }
    if item.active_cursor_run_id:
        active_run = await db.get(CursorRun, item.active_cursor_run_id)
        if active_run is not None and active_run.status in {"pending", "running"}:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Cursor run is still active; cancellation requires confirmed "
                    "remote termination"
                ),
            )
    scheduler = getattr(request.app.state, "scheduler", None)
    await abort_work_item(db, item, note=payload.note, scheduler=scheduler)
    return {
        "ok": True,
        "item": work_item_json(item),
        "message": "Кейс отменён: расписания сняты, агент больше не продолжит это задание.",
    }


@router.delete("/agents/{agent_id}/work-items/{work_item_id}", dependencies=auth)
async def delete_agent_work_item(
    agent_id: str,
    work_item_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from .work_items import delete_work_item

    _, item = await _agent_work_item(db, agent_id, work_item_id)
    if item.active_cursor_run_id:
        active_run = await db.get(CursorRun, item.active_cursor_run_id)
        if active_run is not None and active_run.status in {"pending", "running"}:
            raise HTTPException(
                status_code=409,
                detail="Cannot delete a task while Cursor execution is active",
            )
    scheduler = getattr(request.app.state, "scheduler", None)
    await delete_work_item(db, item, scheduler=scheduler)
    return {"ok": True, "deleted_id": work_item_id}


@router.post("/agents/{agent_id}/work-items/{work_item_id}/instruct", dependencies=auth)
async def instruct_agent_work_item(
    agent_id: str,
    work_item_id: int,
    payload: WorkItemNoteBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from .work_items import resume_work_item, work_item_aborted, work_item_json

    agent, item = await _agent_work_item(db, agent_id, work_item_id)
    if work_item_aborted(item):
        raise HTTPException(status_code=409, detail="work item aborted")
    note = (payload.note or "").strip()
    if not note:
        raise HTTPException(status_code=422, detail="note is required")
    await resume_work_item(db, item, note=note)
    scheduler = getattr(request.app.state, "scheduler", None)
    await schedule_immediate_tick(
        db,
        scheduler,
        agent.id,
        reason="manual",
        work_item_id=item.id,
        manager_answer=note,
    )
    return {
        "ok": True,
        "item": work_item_json(item),
        "message": "Указание принято — тик запланирован в фоне.",
    }


@router.post("/agents/{agent_id}/work-items/{work_item_id}/reset-cursor", dependencies=auth)
async def reset_agent_work_item_cursor(
    agent_id: str,
    work_item_id: int,
    payload: WorkItemNoteBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from .work_items import reset_cursor_assignment, work_item_aborted, work_item_json

    _, item = await _agent_work_item(db, agent_id, work_item_id)
    if work_item_aborted(item):
        raise HTTPException(status_code=409, detail="work item aborted")
    note = (payload.note or "").strip() or "Сброс привязки case→Cursor по указанию руководителя."
    if item.pm_phase in {"DONE", "CANCELLED"}:
        raise HTTPException(status_code=409, detail="Closed PM task cannot reset Cursor")
    if item.pm_phase != "DISCUSSION":
        from .pm_state import transition_pm_phase

        if item.active_cursor_run_id:
            active_run = await db.get(CursorRun, item.active_cursor_run_id)
            if active_run is not None and active_run.status in {"pending", "running"}:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Cursor run is still active; reset is unsafe until remote "
                        "execution reaches a terminal state"
                    ),
                )
            item.active_cursor_run_id = None
        if item.pm_phase == "IN_DEVELOPMENT":
            await transition_pm_phase(db, item, "BLOCKED", detail=note)
        if item.pm_phase in {"DEV_COMPLETE", "QA", "CLIENT_REVIEW"}:
            await transition_pm_phase(db, item, "CHANGES_REQUESTED", detail=note)
        if item.pm_phase in {"BLOCKED", "CHANGES_REQUESTED"}:
            await transition_pm_phase(db, item, "READY_FOR_DEV", detail=note)
    await reset_cursor_assignment(db, item, note=note)
    return {
        "ok": True,
        "item": work_item_json(item),
        "message": "Привязка к Cursor сброшена. Запустите кейс вручную (Продолжи / Force tick), если нужно.",
    }


@router.post("/agents/{agent_id}/work-items/{work_item_id}/flush-intake", dependencies=auth)
async def flush_agent_work_item_intake(
    agent_id: str,
    work_item_id: int,
    payload: WorkItemNoteBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from datetime import timedelta

    from .db import utcnow
    from .employee import save_once_job
    from .work_items import (
        compile_intake_brief,
        mark_intake_executing,
        work_item_aborted,
        work_item_json,
    )

    agent, item = await _agent_work_item(db, agent_id, work_item_id)
    if work_item_aborted(item):
        raise HTTPException(status_code=409, detail="work item aborted")
    scheduler = getattr(request.app.state, "scheduler", None)
    brief = compile_intake_brief(item)
    note = (payload.note or "").strip()
    message = brief
    if note:
        message = f"Указание руководителя: {note}\n\n{brief}"
    item = await mark_intake_executing(db, item, scheduler=scheduler)
    run_at = utcnow() + timedelta(seconds=3)
    await save_once_job(
        db,
        scheduler,
        agent_id=agent.id,
        name=f"intake-flush-{item.id}",
        payload={
            "kind": "intake_flush",
            "source": "intake_flush",
            "work_item_id": item.id,
            "message": message or item.goal or item.title,
            "run_once_at": run_at.isoformat(),
            "timezone": "UTC",
            "reply_chat_id": item.chat_id,
            "reply_phone": item.reply_phone,
            "chat_id": item.chat_id,
            "phone": item.reply_phone,
        },
        current_job_id=item.cron_job_id,
    )
    return {
        "ok": True,
        "item": work_item_json(item),
        "message": "Накопленное задание запланировано — выполнится в фоне через несколько секунд.",
    }


@router.post("/agents/{agent_id}/work-items/{work_item_id}/intake-wait", dependencies=auth)
async def extend_agent_work_item_intake(
    agent_id: str,
    work_item_id: int,
    payload: WorkItemWaitBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from .work_items import begin_customer_intake, work_item_json

    agent, item = await _agent_work_item(db, agent_id, work_item_id)
    scheduler = getattr(request.app.state, "scheduler", None)
    item = await begin_customer_intake(
        db,
        item,
        payload.note,
        minutes=payload.minutes,
        scheduler=scheduler,
        agent_id=agent.id,
    )
    return {"ok": True, "item": work_item_json(item)}


@router.get("/agents/{agent_id}/employee/plans", dependencies=auth)
async def employee_plans(agent_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    agent = await one(db, Agent, agent_id)
    plans = (
        await db.scalars(
            select(EmployeePlan)
            .where(EmployeePlan.agent_id == agent.id)
            .order_by(EmployeePlan.id.desc())
            .limit(100)
        )
    ).all()
    return {"items": [plan_json(p) for p in plans]}


@router.get("/consultations", dependencies=auth)
async def list_consultations(
    db: AsyncSession = Depends(get_db),
    agent_id: int | None = None,
    status: str = "open",
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    filters = []
    if agent_id is not None:
        filters.append(Consultation.agent_id == agent_id)
    if status and status != "all":
        filters.append(Consultation.status == status)
    items = (
        await db.scalars(
            select(Consultation)
            .where(*filters)
            .order_by(Consultation.id.desc())
            .limit(limit)
        )
    ).all()
    return {"items": [consultation_json(item) for item in items], "total": len(items)}


@router.post("/consultations/{consultation_id}/resolve", dependencies=auth)
async def resolve_consultation_api(
    consultation_id: int,
    payload: ConsultationResolveBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="Runtime unavailable")
    status = payload.status if payload.status in {"answered", "approved", "rejected"} else "answered"
    try:
        item = await runtime.employee.resolve_consultation(
            db,
            consultation_id,
            status=status,
            answer_text=payload.answer_text,
            answered_by="web",
            schedule_tick=False,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Consultation not found") from exc
    tick_result = None
    if payload.schedule_tick:
        agent = await db.get(Agent, item.agent_id)
        if agent is not None:
            answer = (item.answer_text or payload.answer_text or status).strip()
            tick_result = await runtime.tick(
                db,
                agent,
                force=True,
                reason="consult_resolved",
                extra={
                    "work_item_id": item.work_item_id,
                    "consultation_id": item.id,
                    "consultation_status": item.status,
                    "approved_by": item.answered_by,
                    "instruction": (
                        f"Ответ руководителя на консультацию #{item.id}: {answer}. "
                        "Выполни указание. Не создавай новую consult_manager по этому же вопросу."
                    ),
                },
            )
    return {
        "ok": True,
        "consultation": consultation_json(item),
        "tick": tick_result,
        "message": (
            "Агент продолжит работу по ответу."
            if tick_result and not tick_result.get("skipped")
            else "Консультация закрыта."
        ),
    }


@router.post("/consultations/{consultation_id}/dismiss", dependencies=auth)
async def dismiss_consultation_api(
    consultation_id: int,
    payload: ConsultationDismissBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="Runtime unavailable")
    try:
        item = await runtime.employee.dismiss_consultation(
            db,
            consultation_id,
            reason=payload.reason,
            answered_by="web",
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Consultation not found") from exc
    return {
        "ok": True,
        "consultation": consultation_json(item),
        "message": "Консультация снята с очереди без запуска агента.",
    }


@router.get("/work-assets/{work_item_id}/{digest}/{filename}")
async def download_work_asset(
    work_item_id: int,
    digest: str,
    filename: str,
    token: str = Query(..., min_length=8),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    from .cursor_assets import assets_root, verify_asset_access_token

    secret = settings.secret_key.get_secret_value()
    if not verify_asset_access_token(work_item_id, digest, token, secret):
        raise HTTPException(status_code=403, detail="Invalid or expired asset token")
    item = await db.get(WorkItem, work_item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Work item not found")
    for entry in list((item.metadata_json or {}).get("customer_images") or []):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("digest") or "") != digest:
            continue
        path = Path(str(entry.get("path") or ""))
        if not path.is_file():
            fallback = assets_root() / str(work_item_id) / path.name
            if fallback.is_file():
                path = fallback
        if path.is_file():
            return FileResponse(path, filename=filename or path.name)
    raise HTTPException(status_code=404, detail="Asset not found")


@router.get("/employees", dependencies=auth)
async def list_employees(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    from .work_items import counts_for_agent

    agents = (await db.scalars(select(Agent).order_by(Agent.id))).all()
    profiles = {
        profile.agent_id: profile
        for profile in (await db.scalars(select(EmployeeProfile))).all()
    }
    items: list[dict[str, Any]] = []
    open_consults = 0
    paused = 0
    autonomous = 0
    failed_work = 0
    waiting_manager = 0
    actionable_work = 0
    for agent in agents:
        profile = profiles.get(agent.id)
        counts = await counts_for_agent(db, agent.id)
        agent_open_consults = int(
            await db.scalar(
                select(func.count())
                .select_from(Consultation)
                .where(
                    Consultation.agent_id == agent.id,
                    Consultation.status == "open",
                )
            )
            or 0
        )
        failed_count = int(counts.get("failed") or 0)
        waiting_count = int(counts.get("waiting_manager") or 0)
        actionable_count = int(counts.get("actionable") or 0)
        open_consults += agent_open_consults
        failed_work += failed_count
        waiting_manager += waiting_count
        actionable_work += actionable_count
        if profile is not None:
            if profile.paused and profile.autonomy_enabled:
                paused += 1
            if profile.autonomy_enabled:
                autonomous += 1
        entry: dict[str, Any] = {
            "agent_id": agent.id,
            "agent_name": agent.name,
            "agent_enabled": agent.enabled,
            "telegram_account_id": agent.telegram_account_id,
            "has_profile": profile is not None,
            "open_consultations": agent_open_consults,
            "work_item_counts": counts,
            "failed_work_items": failed_count,
            "waiting_manager": waiting_count,
            "actionable_work_items": actionable_count,
        }
        if profile is not None:
            entry.update(profile_json(profile))
        else:
            entry.update(
                {
                    "id": None,
                    "autonomy_enabled": False,
                    "paused": False,
                    "heartbeat_minutes": 15,
                    "workday_start": "09:00",
                    "workday_end": "18:00",
                    "timezone": "UTC",
                    "budget_ticks_per_day": 48,
                    "ticks_used_today": 0,
                    "role_title": "",
                    "mission": "",
                }
            )
        items.append(entry)
    return {
        "items": items,
        "open_consultations": open_consults,
        "paused_agents": paused,
        "autonomous_agents": autonomous,
        "failed_work_items": failed_work,
        "waiting_manager": waiting_manager,
        "actionable_work_items": actionable_work,
    }


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


def sip_json(account: SipAccount, request: Request | None = None) -> dict[str, Any]:
    registered = False
    registration_status = "offline"
    last_error: str | None = None
    if request is not None and getattr(request.app.state, "sip", None) is not None:
        reg = request.app.state.sip.registration(account.id)
        registered = bool(reg.get("registered"))
        registration_status = str(reg.get("status") or ("registered" if registered else "offline"))
        err = reg.get("error")
        if isinstance(err, str) and err.strip():
            last_error = err.strip()
        elif registration_status.startswith("error:"):
            last_error = registration_status[6:].strip() or registration_status
    if not account.enabled:
        ui_status = "offline"
    elif registered:
        ui_status = "online"
    elif last_error or registration_status.startswith("error") or registration_status.startswith("failed"):
        ui_status = "error"
    elif not account.password_ciphertext:
        ui_status = "error"
        last_error = last_error or "Пароль не задан"
    else:
        ui_status = "pending"
    return {
        "id": account.id,
        "name": account.name,
        "sip_server": account.sip_server,
        "domain": account.domain,
        "login": account.login,
        "auth_username": account.auth_username,
        "has_password": bool(account.password_ciphertext),
        "password_masked": masked_secret(account.password_ciphertext),
        "transport": account.transport,
        "sip_proxy": account.sip_proxy,
        "display_name": account.display_name,
        "caller_id": account.caller_id,
        "stun_server": account.stun_server,
        "public_ip": account.public_ip,
        "enabled": account.enabled,
        "register_on_startup": account.register_on_startup,
        "max_concurrent_calls": account.max_concurrent_calls,
        "ring_delay_seconds": (
            4.0
            if getattr(account, "ring_delay_seconds", None) is None
            else float(account.ring_delay_seconds)
        ),
        "registered": registered,
        "registration_status": registration_status,
        "last_error": last_error,
        "status": ui_status,
        "created_at": iso(account.created_at),
        "updated_at": iso(account.updated_at),
    }


def sip_call_json(call: SipCall) -> dict[str, Any]:
    return {
        "id": call.id,
        "agent_id": call.agent_id,
        "sip_account_id": call.sip_account_id,
        "direction": call.direction,
        "remote_number": call.remote_number,
        "status": call.status,
        "started_at": iso(call.started_at),
        "answered_at": iso(call.answered_at),
        "ended_at": iso(call.ended_at),
        "hangup_cause": call.hangup_cause,
        "transcript": call.transcript,
        "metadata_json": call.metadata_json or {},
        "sip_call_id": (call.metadata_json or {}).get("sip_call_id"),
        "created_at": iso(call.created_at),
        "updated_at": iso(call.updated_at),
    }


@router.get("/sip/accounts", dependencies=auth)
async def list_sip_accounts(request: Request, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    items = (await db.scalars(select(SipAccount).order_by(SipAccount.id))).all()
    return [sip_json(item, request) for item in items]


@router.post("/sip/accounts", dependencies=auth, status_code=201)
async def create_sip_account(
    payload: SipAccountBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    secrets = SecretStore.from_settings(get_settings())
    if not payload.password:
        raise HTTPException(status_code=422, detail="password is required")
    existing = await db.scalar(select(SipAccount).where(SipAccount.name == payload.name))
    if existing is not None:
        raise HTTPException(status_code=409, detail="SIP account name already exists")
    account = SipAccount(
        name=payload.name,
        sip_server=payload.sip_server,
        domain=payload.domain,
        login=payload.login,
        auth_username=payload.auth_username or payload.login,
        password_ciphertext=secrets.encrypt(payload.password),
        transport=payload.transport or "udp",
        sip_proxy=payload.sip_proxy,
        display_name=payload.display_name or payload.name,
        caller_id=payload.caller_id,
        stun_server=payload.stun_server,
        public_ip=payload.public_ip,
        enabled=payload.enabled,
        register_on_startup=payload.register_on_startup,
        max_concurrent_calls=max(1, payload.max_concurrent_calls),
        ring_delay_seconds=max(0.0, min(30.0, float(payload.ring_delay_seconds))),
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    if account.enabled and getattr(request.app.state, "sip", None):
        try:
            await request.app.state.sip.register_account(account)
        except Exception as exc:
            await events.publish("sip.register_failed", {"account_id": account.id, "error": str(exc)})
    return sip_json(account, request)


@router.patch("/sip/accounts/{account_id}", dependencies=auth)
async def update_sip_account(
    account_id: str,
    payload: SipAccountBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    secrets = SecretStore.from_settings(get_settings())
    account = await one(db, SipAccount, account_id)
    account.name = payload.name
    account.sip_server = payload.sip_server
    account.domain = payload.domain
    account.login = payload.login
    account.auth_username = payload.auth_username or payload.login
    account.transport = payload.transport or "udp"
    account.sip_proxy = payload.sip_proxy
    account.display_name = payload.display_name or payload.name
    account.caller_id = payload.caller_id
    account.stun_server = payload.stun_server
    account.public_ip = payload.public_ip
    account.enabled = payload.enabled
    account.register_on_startup = payload.register_on_startup
    account.max_concurrent_calls = max(1, payload.max_concurrent_calls)
    account.ring_delay_seconds = max(0.0, min(30.0, float(payload.ring_delay_seconds)))
    if payload.clear_password:
        account.password_ciphertext = None
    elif payload.password:
        account.password_ciphertext = secrets.encrypt(payload.password)
    await db.commit()
    await db.refresh(account)
    sip = getattr(request.app.state, "sip", None)
    if sip is not None:
        try:
            if account.enabled and account.password_ciphertext:
                await sip.register_account(account)
            else:
                await sip.unregister_account(account.id)
        except Exception as exc:
            await events.publish("sip.register_failed", {"account_id": account.id, "error": str(exc)})
    return sip_json(account, request)


@router.post("/sip/accounts/{account_id}/register", dependencies=auth)
async def register_sip_account(
    account_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    account = await one(db, SipAccount, account_id)
    sip = getattr(request.app.state, "sip", None)
    if sip is None:
        raise HTTPException(status_code=503, detail="SIP gateway is not available")
    try:
        await sip.register_account(account, force=True)
    except Exception as exc:
        detail = str(exc).strip() or exception_text(exc)
        raise HTTPException(status_code=503, detail=f"SIP register failed: {detail}") from exc
    return sip_json(account, request)


@router.delete("/sip/accounts/{account_id}", dependencies=auth, status_code=204)
async def delete_sip_account(
    account_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    account = await one(db, SipAccount, account_id)
    sip = getattr(request.app.state, "sip", None)
    if sip is not None:
        try:
            await sip.unregister_account(account.id)
        except Exception:
            pass
    await db.delete(account)
    await db.commit()
    return Response(status_code=204)


@router.get("/sip/calls", dependencies=auth)
async def list_sip_calls(
    request: Request,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(100, ge=1, le=500),
    active_only: bool = False,
) -> dict[str, Any]:
    query = select(SipCall).order_by(SipCall.created_at.desc()).limit(limit)
    if active_only:
        query = query.where(SipCall.status.in_(["initiated", "dialing", "ringing", "answered", "early"]))
    items = (await db.scalars(query)).all()
    live = []
    sip = getattr(request.app.state, "sip", None)
    if sip is not None:
        live = sip.list_active_calls()
    return {"items": [sip_call_json(item) for item in items], "active": live, "total": len(items)}


@router.post("/sip/calls", dependencies=auth, status_code=201)
async def dial_sip_call(
    payload: SipDialBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    agent = await one(db, Agent, payload.agent_id)
    if not agent.enabled:
        raise HTTPException(status_code=422, detail="Agent is disabled")
    account_id = payload.sip_account_id if payload.sip_account_id is not None else agent.sip_account_id
    if account_id is None:
        raise HTTPException(status_code=422, detail="Agent has no SIP account")
    account = await one(db, SipAccount, account_id)
    if not account.enabled or not account.password_ciphertext:
        raise HTTPException(status_code=422, detail="SIP account is not ready")
    sip = getattr(request.app.state, "sip", None)
    if sip is None:
        raise HTTPException(status_code=503, detail="SIP gateway is not available")
    try:
        result = await sip.dial(account=account, agent=agent, number=payload.number)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Dial failed: {exc}") from exc
    return result


@router.post("/sip/calls/{call_id}/hangup", dependencies=auth)
async def hangup_sip_call(call_id: str, request: Request) -> dict[str, Any]:
    sip = getattr(request.app.state, "sip", None)
    if sip is None:
        raise HTTPException(status_code=503, detail="SIP gateway is not available")
    try:
        # accept db id or sip call-id
        if call_id.isdigit():
            await sip.hangup(db_id=int(call_id))
        else:
            await sip.hangup(sip_call_id=call_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


@router.get("/sip/status", dependencies=auth)
async def sip_status(request: Request) -> dict[str, Any]:
    sip = getattr(request.app.state, "sip", None)
    if sip is None:
        return {"available": False, "accounts": {}, "active_calls": []}
    data = await sip.status()
    data["available"] = True
    return data


@router.get("/memory", dependencies=auth)
async def memory_search(
    request: Request,
    search: str = "",
    agent_id: str | None = None,
    project_id: str | None = None,
    category: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    memory = request.app.state.memory
    agent_ids = (
        [agent_id]
        if agent_id
        else [
            str(value)
            for value in (
                await db.scalars(select(Agent.id).order_by(Agent.id))
            ).all()
        ]
    )
    filters: dict[str, Any] = {}
    if project_id and project_id.strip():
        filters["project_id"] = project_id.strip()
    if category and category.strip():
        filters["category"] = category.strip().lower()
    items: list[dict[str, Any]] = []
    try:
        for current_agent_id in agent_ids:
            scoped = (
                await memory.search(
                    search,
                    agent_id=current_agent_id,
                    filters=filters or None,
                )
                if search
                else await memory.get_all(
                    agent_id=current_agent_id,
                    filters=filters or None,
                )
            )
            items.extend(scoped)
        # Fallback may contain old global entries which have no agent scope.
        items.extend(memory.fallback_items(agent_id=agent_id, query=search))
    except Exception as exc:
        detail = exception_text(exc)
        memory.last_error = detail
        raise HTTPException(
            status_code=502,
            detail=f"Memory backend request failed: {detail}",
        ) from exc
    unique_items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        identity = str(item.get("id") or id(item))
        if identity not in seen:
            seen.add(identity)
            unique_items.append(item)
    normalized = []
    for item in unique_items:
        metadata = item.get("metadata") or {}
        normalized.append({
            "id": str(item.get("id", "")),
            "agent_id": item.get("agent_id", metadata.get("agent_id")),
            "scope": str(item.get("user_id", metadata.get("user_id", "global"))),
            "project_id": metadata.get("project_id"),
            "customer_id": metadata.get("customer_id"),
            "category": metadata.get("category") or metadata.get("kind"),
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


class JournalsClearBody(BaseModel):
    memory: bool = True
    calls: bool = True
    conversations: bool = True


async def _journals_clear(
    request: Request,
    db: AsyncSession,
    *,
    agent_id: int | None,
    payload: JournalsClearBody,
) -> dict[str, Any]:
    try:
        return await clear_journals(
            db=db,
            memory=request.app.state.memory,
            agent_id=agent_id,
            include_memory=payload.memory,
            include_calls=payload.calls,
            include_conversations=payload.conversations,
        )
    except Exception as exc:
        detail = exception_text(exc)
        memory = getattr(request.app.state, "memory", None)
        if memory is not None:
            memory.last_error = detail
        raise HTTPException(
            status_code=502,
            detail=f"Journal clear failed: {detail}",
        ) from exc


@router.post("/journals/clear", dependencies=auth)
async def clear_all_journals(
    request: Request,
    db: AsyncSession = Depends(get_db),
    payload: JournalsClearBody | None = None,
) -> dict[str, Any]:
    return await _journals_clear(request, db, agent_id=None, payload=payload or JournalsClearBody())


@router.post("/agents/{agent_id}/journals/clear", dependencies=auth)
async def clear_agent_journals(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    payload: JournalsClearBody | None = None,
) -> dict[str, Any]:
    agent = await one(db, Agent, agent_id)
    return await _journals_clear(request, db, agent_id=agent.id, payload=payload or JournalsClearBody())


@router.post("/agents/{agent_id}/memory/clear", dependencies=auth)
async def clear_agent_memory_compat(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    payload: JournalsClearBody | None = None,
) -> dict[str, Any]:
    agent = await one(db, Agent, agent_id)
    return await _journals_clear(request, db, agent_id=agent.id, payload=payload or JournalsClearBody())


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


@router.get("/agents/{agent_id}/mcp-servers", dependencies=auth)
async def agent_mcp_server_ids(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    agent = await one(db, Agent, agent_id)
    ids = list(
        await db.scalars(
            select(agent_mcp_servers.c.mcp_server_id)
            .where(agent_mcp_servers.c.agent_id == agent.id)
            .order_by(agent_mcp_servers.c.mcp_server_id)
        )
    )
    return {"agent_id": agent.id, "server_ids": ids}


@router.put(
    "/agents/{agent_id}/mcp-servers/{server_id}",
    dependencies=auth,
    status_code=204,
)
async def attach_agent_mcp_server(
    agent_id: str,
    server_id: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    agent = await one(db, Agent, agent_id)
    server = await one(db, McpServer, server_id)
    exists = await db.scalar(
        select(agent_mcp_servers.c.agent_id).where(
            agent_mcp_servers.c.agent_id == agent.id,
            agent_mcp_servers.c.mcp_server_id == server.id,
        )
    )
    if exists is None:
        await db.execute(
            insert(agent_mcp_servers).values(
                agent_id=agent.id,
                mcp_server_id=server.id,
            )
        )
        await db.commit()
    return Response(status_code=204)


@router.delete(
    "/agents/{agent_id}/mcp-servers/{server_id}",
    dependencies=auth,
    status_code=204,
)
async def detach_agent_mcp_server(
    agent_id: str,
    server_id: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    agent = await one(db, Agent, agent_id)
    await db.execute(
        delete(agent_mcp_servers).where(
            agent_mcp_servers.c.agent_id == agent.id,
            agent_mcp_servers.c.mcp_server_id == as_int(server_id, "server_id"),
        )
    )
    await db.commit()
    return Response(status_code=204)


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


def cron_job_status(job: CronJob) -> str:
    payload = job.payload or {}
    once = job.cron == "@once" or bool(payload.get("run_once_at"))
    if job.enabled:
        return "active"
    if once and job.last_run_at is not None:
        return "completed"
    return "paused"


def cron_job_kind(job: CronJob) -> str:
    payload = job.payload or {}
    name = str(job.name or "")
    source = str(payload.get("source") or "")
    if name.startswith(HEARTBEAT_JOB_PREFIX) or source == "employee_heartbeat":
        return "heartbeat"
    if (
        payload.get("work_item_id")
        or name.startswith("employee-tick-once-")
        or source in {"scheduled", "consult_resolved", "intake_flush"}
        or str(payload.get("kind") or "") in {"employee_tick", "intake_flush"}
    ):
        return "followup"
    return "cron"


def cron_json(job: CronJob) -> dict[str, Any]:
    payload = job.payload or {}
    kind = cron_job_kind(job)
    last_result = public_job_result(payload.get("last_result"))
    if kind == "heartbeat":
        last_result = None
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
        "status": cron_job_status(job),
        "kind": kind,
        "work_item_id": payload.get("work_item_id"),
        "last_result": last_result,
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


def scoped_cron_name(name: str, agent_id: int) -> str:
    from .employee import clip_cron_name, unique_cron_name

    base = clip_cron_name(name) or "job"
    candidate = f"a{agent_id}-{base}"[:120]
    if candidate == base:
        return unique_cron_name(base, agent_id)
    return candidate


@router.post("/cron", dependencies=auth, status_code=201)
async def create_cron(payload: CronBody, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    from .employee import unique_cron_name

    schedule, job_payload = cron_values(payload)
    agent_id = as_int(payload.agent_id, "agent_id")
    job_name = scoped_cron_name(payload.name, agent_id)
    if await db.scalar(select(CronJob.id).where(CronJob.name == job_name)):
        job_name = unique_cron_name(payload.name, agent_id)
    job = CronJob(
        name=job_name,
        agent_id=agent_id,
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
    previous = dict(job.payload or {})
    schedule, job_payload = cron_values(payload)
    if previous.get("last_result"):
        job_payload["last_result"] = previous["last_result"]
    for key in (
        "kind",
        "source",
        "reply_to_chat",
        "reply_chat_id",
        "reply_phone",
        "chat_id",
        "phone",
        "sender_id",
        "sender_username",
        "is_admin",
        "message_id",
    ):
        if key not in job_payload and previous.get(key) not in (None, ""):
            job_payload[key] = previous[key]
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
        "has_tavily_api_key": bool(settings.tavily_api_key_ciphertext),
        "tavily_api_key_masked": masked_secret(settings.tavily_api_key_ciphertext),
        "tavily_http_proxy": settings.tavily_http_proxy,
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
    secrets = SecretStore.from_settings(get_settings())
    tavily_key_present = bool(
        payload.tavily_api_key
        or (settings.tavily_api_key_ciphertext and not payload.clear_tavily_api_key)
    )
    if payload.search_provider == "tavily" and not tavily_key_present:
        raise HTTPException(
            status_code=422,
            detail="tavily_api_key is required for the tavily provider",
        )
    if payload.memory_llm_profile_id is not None and await db.get(
        LlmProfile, payload.memory_llm_profile_id
    ) is None:
        raise HTTPException(status_code=422, detail="Memory LLM profile not found")
    if (
        payload.memory_enabled
        and payload.memory_backend == "local"
        and not (payload.qdrant_url or "").strip()
    ):
        payload.qdrant_url = "http://qdrant:6333"
    for key, value in payload.model_dump(
        exclude={
            "mem0_api_key",
            "clear_mem0_api_key",
            "tavily_api_key",
            "clear_tavily_api_key",
        }
    ).items():
        setattr(settings, key, value)
    if isinstance(settings.qdrant_url, str):
        settings.qdrant_url = settings.qdrant_url.strip() or None
    if isinstance(settings.tavily_http_proxy, str):
        settings.tavily_http_proxy = settings.tavily_http_proxy.strip() or None
    if payload.clear_tavily_api_key:
        settings.tavily_api_key_ciphertext = None
    elif payload.tavily_api_key:
        settings.tavily_api_key_ciphertext = secrets.encrypt(payload.tavily_api_key)
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
                    "provider": profile.provider,
                    "http_proxy": (profile.http_proxy or "").strip() or None,
                }
    request.app.state.search.configure(
        settings.search_provider,
        settings.searxng_url,
        secrets.decrypt(settings.tavily_api_key_ciphertext),
        settings.tavily_http_proxy,
    )
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
        "thread_id": state.thread_id,
        "thread_id": state.thread_id,
        "project_id": state.project_id,
        "customer_id": state.customer_id,
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
    project_id: str | None = None,
    customer_id: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    filters = []
    if agent_id is not None:
        filters.append(ConversationState.agent_id == agent_id)
    if project_id and project_id.strip():
        filters.append(ConversationState.project_id.ilike(f"%{project_id.strip()}%"))
    if customer_id and customer_id.strip():
        filters.append(ConversationState.customer_id.ilike(f"%{customer_id.strip()}%"))
    if search.strip():
        pattern = f"%{search.strip()}%"
        filters.append(or_(
            ConversationState.chat_id.ilike(pattern),
            ConversationState.user_id.ilike(pattern),
            ConversationState.project_id.ilike(pattern),
            ConversationState.customer_id.ilike(pattern),
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


@router.patch("/conversations/{conversation_id}", dependencies=auth)
async def patch_conversation(
    conversation_id: int,
    payload: ConversationPatch,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    state = await one(db, ConversationState, conversation_id)
    if payload.clear_project:
        state.project_id = None
    elif payload.project_id is not None:
        normalized = payload.project_id.strip()
        state.project_id = normalized or None
    if payload.clear_customer:
        state.customer_id = None
    elif payload.customer_id is not None:
        normalized = payload.customer_id.strip()
        state.customer_id = normalized or None
    await db.commit()
    await db.refresh(state)
    return conversation_json(state)


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
            "has_tavily_api_key": bool(search.tavily_api_key),
        }
    sample = results[0]["title"] if results else None
    if not results:
        return {
            "ok": False,
            "status": "empty",
            "detail": (
                "Провайдер ответил, но вернул 0 результатов. "
                "Проверьте ключ Tavily / URL SearXNG и поисковые движки."
            ),
            "provider": search.provider,
            "searxng_url": search.searxng_url,
            "has_tavily_api_key": bool(search.tavily_api_key),
            "result_count": 0,
        }
    return {
        "ok": True,
        "status": "connected",
        "detail": f"Поиск работает · {len(results)} результат(ов)"
        + (f" · пример: {sample}" if sample else ""),
        "provider": search.provider,
        "searxng_url": search.searxng_url,
        "has_tavily_api_key": bool(search.tavily_api_key),
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
