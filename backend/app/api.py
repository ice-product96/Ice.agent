from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings, get_settings
from .db import (
    AdminSettings, Agent, AgentLink, AgentTask, CronJob, McpServer, MessageLog,
    TelegramAccount, agent_mcp_servers, get_db,
)
from .events import events
from .schemas import (
    AdminSettingsIn, AgentIn, AgentLinkIn, AgentPatch, AgentTaskIn, CronJobIn,
    LoginRequest, McpServerIn, MessageLogIn, RunRequest, TelegramAccountIn,
    TelegramCodeRequest, TelegramCodeVerify, TokenResponse,
)
from .security import issue_token, require_admin, valid_password, verify_token
from .runtime import PermissionDenied
from .secrets import SecretStore, masked_secret

router = APIRouter(prefix="/api")

RESOURCE_MAP: dict[str, tuple[type[Any], type[BaseModel]]] = {
    "telegram-accounts": (TelegramAccount, TelegramAccountIn),
    "agents": (Agent, AgentIn),
    "agent-links": (AgentLink, AgentLinkIn),
    "mcp-servers": (McpServer, McpServerIn),
    "cron-jobs": (CronJob, CronJobIn),
    "admin-settings": (AdminSettings, AdminSettingsIn),
    "message-logs": (MessageLog, MessageLogIn),
    "agent-tasks": (AgentTask, AgentTaskIn),
}


def _resource(name: str) -> tuple[type[Any], type[BaseModel]]:
    if name not in RESOURCE_MAP:
        raise HTTPException(status_code=404, detail="Unknown resource")
    return RESOURCE_MAP[name]


def _serialize(instance: Any) -> dict[str, Any]:
    result = {
        column.name: getattr(instance, column.name)
        for column in instance.__table__.columns
        if not column.name.endswith("_ciphertext")
    }
    if isinstance(instance, TelegramAccount):
        result["has_api_hash"] = bool(instance.api_hash_ciphertext)
        result["api_hash_masked"] = masked_secret(instance.api_hash_ciphertext)
    if isinstance(instance, McpServer):
        env = dict(instance.env or {})
        if instance.env_ciphertext:
            import json

            decrypted = SecretStore.from_settings(get_settings()).decrypt(
                instance.env_ciphertext
            )
            env = json.loads(decrypted or "{}")
        result["env"] = {
            key: "********" for key in env
        }
        result["has_env"] = bool(env)
    return result


@router.post("/auth/login", response_model=TokenResponse)
async def login(payload: LoginRequest, settings: Settings = Depends(get_settings)) -> TokenResponse:
    if not valid_password(payload.password, settings):
        raise HTTPException(status_code=401, detail="Invalid password")
    token, expires = issue_token(settings)
    return TokenResponse(token=token, expires_at=expires)


@router.get("/{resource}", dependencies=[Depends(require_admin)])
async def list_resources(resource: str, db: AsyncSession = Depends(get_db), limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    model, _ = _resource(resource)
    rows = (await db.scalars(select(model).offset(offset).limit(min(limit, 500)))).all()
    return [_serialize(row) for row in rows]


@router.post("/{resource}", dependencies=[Depends(require_admin)], status_code=201)
async def create_resource(resource: str, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    model, schema = _resource(resource)
    try:
        payload = schema.model_validate(await request.json()).model_dump()
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    if model is TelegramAccount:
        settings = get_settings()
        safe = "".join(char for char in payload["phone"] if char.isdigit() or char == "+")
        payload["session_path"] = str(settings.session_dir / f"{safe}.session")
        api_hash = payload.pop("api_hash", "")
        payload.pop("clear_api_hash", None)
        payload["api_hash_ciphertext"] = SecretStore.from_settings(settings).encrypt(api_hash)
    elif model is McpServer:
        import json

        env = payload.pop("env", {})
        payload["env"] = {}
        payload["env_ciphertext"] = SecretStore.from_settings(get_settings()).encrypt(
            json.dumps(env, ensure_ascii=False)
        ) if env else None
    instance = model(**payload)
    db.add(instance)
    await db.commit()
    await db.refresh(instance)
    return _serialize(instance)


@router.get("/{resource}/{item_id}", dependencies=[Depends(require_admin)])
async def get_resource(resource: str, item_id: int, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    model, _ = _resource(resource)
    instance = await db.get(model, item_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="Not found")
    return _serialize(instance)


@router.patch("/{resource}/{item_id}", dependencies=[Depends(require_admin)])
async def update_resource(resource: str, item_id: int, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    model, schema = _resource(resource)
    instance = await db.get(model, item_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="Not found")
    raw_payload = await request.json()
    if model is Agent:
        try:
            payload = AgentPatch.model_validate(raw_payload).model_dump(exclude_unset=True)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
    else:
        allowed = set(schema.model_fields)
        unknown = set(raw_payload) - allowed
        if unknown:
            raise HTTPException(status_code=422, detail=f"Unknown fields: {sorted(unknown)}")
        payload = raw_payload
    if model is TelegramAccount:
        api_hash = payload.pop("api_hash", "")
        clear = payload.pop("clear_api_hash", False)
        if clear:
            instance.api_hash_ciphertext = None
        elif api_hash:
            instance.api_hash_ciphertext = SecretStore.from_settings(
                get_settings()
            ).encrypt(api_hash)
    elif model is McpServer and "env" in payload:
        import json

        env = payload.pop("env")
        if env:
            instance.env_ciphertext = SecretStore.from_settings(
                get_settings()
            ).encrypt(json.dumps(env, ensure_ascii=False))
            instance.env = {}
    for key, value in payload.items():
        setattr(instance, key, value)
    await db.commit()
    await db.refresh(instance)
    return _serialize(instance)


@router.delete("/{resource}/{item_id}", dependencies=[Depends(require_admin)], status_code=204)
async def delete_resource(resource: str, item_id: int, db: AsyncSession = Depends(get_db)) -> None:
    model, _ = _resource(resource)
    result = await db.execute(delete(model).where(model.id == item_id))
    if not result.rowcount:
        raise HTTPException(status_code=404, detail="Not found")
    await db.commit()


@router.post("/telegram/request-code", dependencies=[Depends(require_admin)])
async def telegram_request_code(
    payload: TelegramCodeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    account = await db.scalar(
        select(TelegramAccount).where(TelegramAccount.phone == payload.phone)
    )
    if account is None:
        raise HTTPException(status_code=404, detail="Telegram account not found")
    code_hash = await request.app.state.telegram.request_code(account)
    return {"phone_code_hash": code_hash}


@router.post("/telegram/verify-code", dependencies=[Depends(require_admin)])
async def telegram_verify_code(payload: TelegramCodeVerify, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, bool]:
    account = await db.scalar(select(TelegramAccount).where(TelegramAccount.phone == payload.phone))
    if account is None:
        raise HTTPException(status_code=404, detail="Telegram account not found")
    await request.app.state.telegram.verify_code(account, payload.code, payload.password)
    account.authorized = True
    await db.commit()
    return {"authorized": True}


@router.post("/agents/{agent_id}/run", dependencies=[Depends(require_admin)])
async def run_agent(agent_id: int, payload: RunRequest, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    result = await request.app.state.runtime.run(db, agent, payload.message, payload.context)
    return {"response": result}


@router.put("/agents/{agent_id}/mcp-servers/{server_id}", dependencies=[Depends(require_admin)], status_code=204)
async def attach_mcp_server(agent_id: int, server_id: int, db: AsyncSession = Depends(get_db)) -> None:
    if await db.get(Agent, agent_id) is None or await db.get(McpServer, server_id) is None:
        raise HTTPException(status_code=404, detail="Agent or MCP server not found")
    exists = await db.scalar(select(agent_mcp_servers).where(
        agent_mcp_servers.c.agent_id == agent_id,
        agent_mcp_servers.c.mcp_server_id == server_id,
    ))
    if exists is None:
        await db.execute(insert(agent_mcp_servers).values(agent_id=agent_id, mcp_server_id=server_id))
        await db.commit()


@router.delete("/agents/{agent_id}/mcp-servers/{server_id}", dependencies=[Depends(require_admin)], status_code=204)
async def detach_mcp_server(agent_id: int, server_id: int, db: AsyncSession = Depends(get_db)) -> None:
    await db.execute(delete(agent_mcp_servers).where(
        agent_mcp_servers.c.agent_id == agent_id,
        agent_mcp_servers.c.mcp_server_id == server_id,
    ))
    await db.commit()


@router.post("/tasks/delegate", dependencies=[Depends(require_admin)])
async def delegate_task(payload: AgentTaskIn, request: Request) -> dict[str, Any]:
    if payload.source_agent_id is None:
        raise HTTPException(status_code=422, detail="source_agent_id is required")
    try:
        task = await request.app.state.task_bus.delegate(payload.source_agent_id, payload.target_agent_id, payload.input)
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return _serialize(task)


@router.websocket("/events")
async def websocket_events(websocket: WebSocket, token: str, settings: Settings = Depends(get_settings)) -> None:
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
