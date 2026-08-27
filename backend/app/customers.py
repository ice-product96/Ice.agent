"""Customer cards bound to Cursor MCP projects; injected into agent prompts."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .cursorremote_drive import mcp_call, parse_mcp_payload
from .db import Agent, Customer, McpServer, ProjectState, WorkItem
from .secrets import SecretStore


def _mcp_env(server: McpServer) -> dict[str, Any]:
    if not server.env_ciphertext:
        return dict(server.env or {})
    import json

    try:
        value = SecretStore.from_settings(get_settings()).decrypt(server.env_ciphertext)
        return json.loads(value or "{}")
    except Exception:
        return {}

_TRANSLIT = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


def slugify(value: str, *, fallback: str = "customer") -> str:
    text = str(value or "").strip().lower()
    mapped = "".join(_TRANSLIT.get(ch, ch) for ch in text)
    mapped = re.sub(r"[^a-z0-9]+", "-", mapped).strip("-")
    return (mapped or fallback)[:120]


def customer_prompt_block(customer: Customer | None, *, tracker: dict[str, Any] | None = None) -> str:
    if customer is None:
        return ""
    lines = [
        "## Заказчик и проект",
        f"Заказчик: {customer.name} (id={customer.id}).",
    ]
    if (customer.project_id or "").strip():
        lines.append(f"Проект разработки: {customer.project_id.strip()}.")
    if (customer.cursor_workspace or "").strip():
        lines.append(f"Проект Cursor MCP: {customer.cursor_workspace.strip()}.")
    tracker = tracker or {}
    tracker_id = str(tracker.get("tracker_project_id") or "").strip()
    if tracker_id:
        lines.append(f"ice_tracker project_id: {tracker_id}.")
        if tracker.get("tracker_poll_enabled", True):
            lines.append(
                "Периодически проверяй незакрытые задачи этого трекера (pm_poll_tracker) "
                "и бери новые в работу через pm_structure_task с tracker_task_id."
            )
    notes = (customer.notes or "").strip()
    if notes:
        lines.append("Контакты и договорённости заказчика:")
        lines.append(notes)
    lines.append(
        "Представляйся и веди работу от имени этого заказчика и этого проекта. "
        "Не подменяй заказчика, не путай проекты и не выдумывай другого клиента. "
        "Контакты заказчика, проект и workspace бери только из раздела «Заказчики», "
        "не храни их в своих правилах/заметках."
    )
    return "\n".join(lines)


async def customer_json(customer: Customer, db: AsyncSession | None = None) -> dict[str, Any]:
    tracker = {"tracker_project_id": "", "tracker_poll_enabled": False}
    if db is not None and (customer.project_id or "").strip():
        from .tracker_poll import tracker_settings

        state = await db.get(ProjectState, customer.project_id.strip())
        if state is not None:
            tracker = tracker_settings(state.config)
    return {
        "id": customer.id,
        "name": customer.name,
        "notes": customer.notes or "",
        "agent_id": customer.agent_id,
        "project_id": customer.project_id or "",
        "cursor_workspace": customer.cursor_workspace or "",
        "cursor_window_id": customer.cursor_window_id,
        "is_default": bool(customer.is_default),
        "tracker_project_id": tracker.get("tracker_project_id") or "",
        "tracker_poll_enabled": bool(tracker.get("tracker_poll_enabled")),
        "prompt_block": customer_prompt_block(customer, tracker=tracker),
        "created_at": customer.created_at.isoformat() if customer.created_at else None,
        "updated_at": customer.updated_at.isoformat() if customer.updated_at else None,
    }


def _project_id_from_workspace(workspace: str) -> str:
    path = Path(str(workspace or "").strip().rstrip("\\/"))
    name = path.name or str(workspace or "").strip()
    return slugify(name, fallback="project")


def _add_project(
    bucket: dict[str, dict[str, Any]],
    *,
    workspace: str = "",
    label: str = "",
    window_id: str | None = None,
    project_id: str = "",
    source: str = "live",
) -> None:
    workspace = str(workspace or "").strip()
    label = str(label or "").strip() or (Path(workspace).name if workspace else "")
    project_id = str(project_id or "").strip() or (
        _project_id_from_workspace(workspace or label) if (workspace or label) else ""
    )
    if not project_id and not workspace and not label:
        return
    key = (workspace or project_id or label).lower()
    existing = bucket.get(key)
    item = {
        "id": project_id or slugify(label or workspace),
        "label": label or project_id or workspace,
        "workspace": workspace,
        "window_id": window_id,
        "project_id": project_id or slugify(label or workspace),
        "source": source,
    }
    if existing is None:
        bucket[key] = item
        return
    if not existing.get("workspace") and item["workspace"]:
        existing["workspace"] = item["workspace"]
    if not existing.get("window_id") and item["window_id"]:
        existing["window_id"] = item["window_id"]
    if existing.get("source") != "live" and source == "live":
        existing["source"] = "live"
    if label and (not existing.get("label") or existing["label"] == existing.get("project_id")):
        existing["label"] = label


def extract_cursor_projects(payload: Any) -> list[dict[str, Any]]:
    data = parse_mcp_payload(payload)
    bucket: dict[str, dict[str, Any]] = {}

    def consume(item: Any) -> None:
        if isinstance(item, str):
            _add_project(bucket, workspace=item, source="live")
            return
        if not isinstance(item, dict):
            return
        workspace = (
            item.get("workspacePath")
            or item.get("workspace_path")
            or item.get("workspace")
            or item.get("path")
            or item.get("folder")
            or ""
        )
        label = (
            item.get("title")
            or item.get("name")
            or item.get("workspaceName")
            or item.get("label")
            or ""
        )
        window_id = item.get("id") or item.get("windowId") or item.get("targetId")
        project_id = item.get("project_id") or item.get("projectId") or ""
        _add_project(
            bucket,
            workspace=str(workspace or ""),
            label=str(label or ""),
            window_id=str(window_id) if window_id not in (None, "") else None,
            project_id=str(project_id or ""),
            source="live",
        )
        for nested_key in ("windows", "workspaces", "projects", "allowedWorkspaces"):
            nested = item.get(nested_key)
            if isinstance(nested, list):
                for child in nested:
                    consume(child)

    if isinstance(data, list):
        for item in data:
            consume(item)
    else:
        consume(data)
    return list(bucket.values())


def allowed_workspaces_from_env(env: dict[str, Any] | None) -> list[str]:
    if not isinstance(env, dict):
        return []
    raw = (
        env.get("MCP_ALLOWED_WORKSPACES")
        or env.get("ALLOWED_WORKSPACES")
        or env.get("cursorRemote.mcp.allowedWorkspaces")
        or ""
    )
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            import json

            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except Exception:
            pass
    parts = re.split(r"[;\n,]+", text)
    return [part.strip() for part in parts if part.strip()]


async def resolve_customer(
    db: AsyncSession,
    agent: Agent | None,
    *,
    customer_id: str | None = None,
    project_id: str | None = None,
) -> Customer | None:
    cid = str(customer_id or "").strip()
    if cid:
        row = await db.get(Customer, cid)
        if row is not None:
            return row
        row = await db.scalar(
            select(Customer).where(Customer.id.ilike(cid)).limit(1)
        )
        if row is not None:
            return row
    pid = str(project_id or "").strip()
    if pid:
        row = await db.scalar(
            select(Customer).where(Customer.project_id == pid).limit(1)
        )
        if row is not None:
            return row
        row = await db.scalar(
            select(Customer).where(Customer.project_id.ilike(pid)).limit(1)
        )
        if row is not None:
            return row
    if agent is None:
        return None
    return await db.scalar(
        select(Customer)
        .where(Customer.agent_id == agent.id, Customer.is_default.is_(True))
        .limit(1)
    )


def _normalize_match_text(value: str) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"[\s_\-./\\]+", "", text)
    return text


def _match_variants(value: str) -> set[str]:
    raw = _normalize_match_text(value)
    if not raw:
        return set()
    variants = {raw}
    slug = slugify(value, fallback="")
    if slug:
        variants.add(_normalize_match_text(slug))
    return {v for v in variants if len(v) >= 3}


def _token_matches(needle: str, hay: str) -> bool:
    if needle in hay or hay in needle:
        return True
    # Near-translit brands: УралТрейд ↔ uraltrade share prefix uraltr.
    shared = 0
    for left, right in zip(needle, hay):
        if left != right:
            break
        shared += 1
    return shared >= 5


async def match_customer_from_text(
    db: AsyncSession,
    agent: Agent | None,
    text: str,
) -> Customer | None:
    """Find a customer mentioned in free text (title, intake, chat)."""
    hay_variants = _match_variants(text)
    if not hay_variants:
        return None
    stmt = select(Customer)
    if agent is not None:
        stmt = stmt.where(
            (Customer.agent_id == agent.id) | (Customer.agent_id.is_(None))
        )
    rows = list(await db.scalars(stmt))
    best: Customer | None = None
    best_score = 0
    for row in rows:
        needles = [
            row.id,
            row.project_id,
            row.name,
            Path(row.cursor_workspace or "").name if row.cursor_workspace else "",
        ]
        for raw in needles:
            for needle in _match_variants(raw):
                if any(_token_matches(needle, hay) for hay in hay_variants):
                    score = len(needle)
                    if score > best_score:
                        best = row
                        best_score = score
    return best


async def bind_customer_to_work_item(
    db: AsyncSession,
    item: WorkItem,
    customer: Customer | None,
    *,
    context: dict[str, Any] | None = None,
) -> Customer | None:
    if customer is None:
        return None
    if not (item.customer_id or "").strip():
        item.customer_id = customer.id
    if not (item.project_id or "").strip() and (customer.project_id or "").strip():
        item.project_id = customer.project_id.strip()
    ctx = context if isinstance(context, dict) else None
    if ctx is not None:
        ctx["customer_id"] = customer.id
        if customer.project_id:
            ctx["project_id"] = customer.project_id
        if customer.cursor_workspace:
            ctx.setdefault("cursor_workspace", customer.cursor_workspace)
    return customer


async def sync_agent_memory_defaults(db: AsyncSession, customer: Customer) -> None:
    if customer.agent_id is None:
        return
    agent = await db.get(Agent, customer.agent_id)
    if agent is None:
        return
    config = dict(agent.config or {})
    memory = dict(config.get("memory") or {})
    if customer.is_default:
        memory["default_customer_id"] = customer.id
        if customer.project_id:
            memory["default_project_id"] = customer.project_id
        else:
            memory.pop("default_project_id", None)
    else:
        if memory.get("default_customer_id") == customer.id:
            memory.pop("default_customer_id", None)
            memory.pop("default_project_id", None)
    config["memory"] = memory
    agent.config = config


async def apply_customer_defaults(
    db: AsyncSession,
    customer: Customer,
    *,
    make_default: bool,
    tracker_project_id: str | None = None,
    tracker_poll_enabled: bool | None = None,
) -> None:
    if customer.agent_id is not None and make_default:
        await db.execute(
            update(Customer)
            .where(Customer.agent_id == customer.agent_id, Customer.id != customer.id)
            .values(is_default=False)
        )
        customer.is_default = True
    elif make_default:
        customer.is_default = True
    else:
        customer.is_default = False
    await sync_agent_memory_defaults(db, customer)
    if customer.project_id:
        from .pm_state import get_or_create_project_state
        from .tracker_poll import UUID_RE

        state = await get_or_create_project_state(db, customer.project_id)
        config = dict(state.config or {})
        config.update(
            {
                "customer_id": customer.id,
                "customer_name": customer.name,
                "cursor_workspace": customer.cursor_workspace or "",
                "cursor_window_id": customer.cursor_window_id,
            }
        )
        if tracker_project_id is not None:
            tid = str(tracker_project_id or "").strip()
            if tid and UUID_RE.match(tid):
                config["tracker_project_id"] = tid
            elif tid == "":
                config.pop("tracker_project_id", None)
        if tracker_poll_enabled is not None:
            config["tracker_poll_enabled"] = bool(tracker_poll_enabled)
        state.config = config


async def collect_cursor_projects(db: AsyncSession, mcp: Any | None) -> list[dict[str, Any]]:
    bucket: dict[str, dict[str, Any]] = {}

    servers = (
        await db.scalars(select(McpServer).where(McpServer.enabled.is_(True)))
    ).all()
    for server in servers:
        if "cursorremote" not in (server.name or "").lower():
            continue
        for path in allowed_workspaces_from_env(_mcp_env(server)):
            _add_project(
                bucket,
                workspace=path,
                label=Path(path).name or path,
                project_id=_project_id_from_workspace(path),
                source="allowlist",
            )

    customers = (await db.scalars(select(Customer).order_by(Customer.name))).all()
    for customer in customers:
        if customer.cursor_workspace or customer.project_id:
            _add_project(
                bucket,
                workspace=customer.cursor_workspace or "",
                label=customer.name,
                window_id=customer.cursor_window_id,
                project_id=customer.project_id or customer.id,
                source="saved",
            )

    projects = (await db.scalars(select(ProjectState).order_by(ProjectState.project_id))).all()
    for state in projects:
        config = state.config or {}
        _add_project(
            bucket,
            workspace=str(config.get("cursor_workspace") or ""),
            label=str(config.get("customer_name") or state.project_id),
            window_id=str(config.get("cursor_window_id") or "") or None,
            project_id=state.project_id,
            source="saved",
        )

    if mcp is not None:
        session = None
        for name, candidate in list(getattr(mcp, "sessions", {}).items()):
            if "cursorremote" in str(name).lower():
                session = candidate
                break
        if session is not None:
            for tool in ("list_windows", "get_status", "get_state"):
                try:
                    payload = await mcp_call(
                        session,
                        tool,
                        {"messageLimit": 1} if tool == "get_state" else None,
                    )
                except Exception:
                    continue
                for item in extract_cursor_projects(payload):
                    _add_project(
                        bucket,
                        workspace=str(item.get("workspace") or ""),
                        label=str(item.get("label") or ""),
                        window_id=item.get("window_id"),
                        project_id=str(item.get("project_id") or ""),
                        source="live",
                    )

    return sorted(
        bucket.values(),
        key=lambda item: (str(item.get("label") or "").lower(), str(item.get("workspace") or "")),
    )
