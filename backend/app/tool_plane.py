"""Tool plane: surface hot tools in the LLM prompt, route the rest via catalog search."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .tools import MAX_LLM_TOOLS, RegisteredTool, ToolRegistry

# Always visible when the agent has access (meta-layer + essentials).
META_TOOL_NAMES = frozenset({"tools_search", "tools_describe", "tools_run"})

CORE_SURFACE_TOOLS = frozenset({"web_search", "memory_search", "memory_add", "memory_set_project"})

TELEGRAM_SURFACE_TOOLS = frozenset(
    {
        "telegram_send_message",
        "telegram_suppress_reply",
        "telegram_get_conversation_history",
        "telegram_get_history",
        "telegram_get_messages",
    }
)

SIP_SURFACE_TOOLS = frozenset({"sip_dial", "sip_hangup", "sip_status"})

CURSORREMOTE_SURFACE_TOOLS = frozenset({"cursorremote_do"})

EMPLOYEE_SURFACE_TOOLS = frozenset(
    {
        "plan_get",
        "plan_upsert",
        "need_upsert",
        "consult_manager",
        "request_approval",
        "self_configure",
        "employee_status",
    }
)

AGENT_SURFACE_TOOLS = frozenset({"agent_create_task", "agent_notify"})

SCHEDULER_SURFACE_TOOLS = frozenset(
    {"schedule_self", "schedule_self_list", "schedule_self_cancel"}
)


def tool_namespace(name: str) -> str:
    tool = str(name or "").strip()
    if not tool:
        return "core"
    if tool in META_TOOL_NAMES or tool in {"sleep", "parse_json"}:
        return "core"
    if tool.startswith("memory_"):
        return "memory"
    if tool.startswith("telegram_"):
        return "telegram"
    if tool.startswith("sip_"):
        return "sip"
    if tool.startswith(("plan_", "need_", "consult_", "request_", "self_", "employee_")):
        return "employee"
    if tool.startswith("schedule_self"):
        return "scheduler"
    if tool.startswith("agent_"):
        return "agent"
    if tool.startswith("mcp_"):
        parts = tool.split("_", 2)
        if len(parts) >= 2:
            return f"mcp:{parts[1]}"
        return "mcp"
    if tool.startswith("tools_"):
        return "core"
    return "core"


def is_surface_tool(name: str) -> bool:
    tool = str(name or "").strip()
    if tool in META_TOOL_NAMES or tool in CORE_SURFACE_TOOLS:
        return True
    if tool in TELEGRAM_SURFACE_TOOLS | SIP_SURFACE_TOOLS | EMPLOYEE_SURFACE_TOOLS | CURSORREMOTE_SURFACE_TOOLS:
        return True
    if tool in AGENT_SURFACE_TOOLS | SCHEDULER_SURFACE_TOOLS:
        return True
    if tool.endswith("_tools") and tool.startswith("mcp_"):
        return True
    if tool.endswith("_run") and tool.startswith("mcp_"):
        return True
    if tool in {"sleep", "parse_json"}:
        return False
    return False


@dataclass(slots=True)
class CatalogEntry:
    name: str
    namespace: str
    description: str
    surface: bool


def _tool_sort_key(name: str) -> tuple[int, str]:
    if name.startswith("tools_"):
        return (0, name)
    if name.startswith("memory_") or name in {"web_search"}:
        return (1, name)
    if name in {"telegram_send_message", "telegram_suppress_reply"}:
        return (2, name)
    if name.startswith("sip_"):
        return (3, name)
    if name.startswith(
        ("plan_", "need_", "consult_manager", "request_approval", "self_configure", "employee_")
    ):
        return (4, name)
    if name.startswith("schedule_self"):
        return (5, name)
    if name.startswith("telegram_"):
        return (6, name)
    if name.endswith("_tools") and name.startswith("mcp_"):
        return (7, name)
    if name.endswith("_run") and name.startswith("mcp_"):
        return (8, name)
    if name.startswith("agent_"):
        return (9, name)
    return (10, name)


def build_catalog(registry: ToolRegistry) -> list[CatalogEntry]:
    entries: list[CatalogEntry] = []
    for name, tool in registry.tools.items():
        if name in META_TOOL_NAMES:
            continue
        entries.append(
            CatalogEntry(
                name=name,
                namespace=getattr(tool, "namespace", None) or tool_namespace(name),
                description=tool.description or name,
                surface=is_surface_tool(name),
            )
        )
    return entries


def search_catalog(
    catalog: list[CatalogEntry],
    query: str,
    *,
    namespace: str = "",
    limit: int = 15,
) -> list[dict[str, Any]]:
    words = [word for word in re.split(r"\W+", query.lower()) if len(word) >= 2]
    ns_filter = namespace.strip().lower()
    scored: list[tuple[int, str, CatalogEntry]] = []
    for entry in catalog:
        if ns_filter and entry.namespace.lower() != ns_filter:
            continue
        haystack = f"{entry.name} {entry.namespace} {entry.description}".lower()
        if words:
            score = sum(3 if word in entry.name.lower() else 0 for word in words)
            score += sum(2 if word in entry.namespace.lower() else 0 for word in words)
            score += sum(1 if word in haystack else 0 for word in words)
            if score <= 0:
                continue
        else:
            score = 0
        scored.append((score, entry.name, entry))
    scored.sort(key=lambda item: (-item[0], item[1]))
    limit = max(1, min(int(limit or 15), 30))
    return [
        {
            "name": entry.name,
            "namespace": entry.namespace,
            "description": entry.description,
            "surface": entry.surface,
        }
        for _, _, entry in scored[:limit]
    ]


def attach_tool_plane(registry: ToolRegistry) -> None:
    """Register catalog meta-tools. Full registry stays callable via tools_run."""

    async def tools_search(
        query: str,
        namespace: str = "",
        limit: int = 15,
    ) -> list[dict[str, Any]]:
        """Search the tool catalog by keyword. Use before tools_run for non-surface tools."""
        catalog = build_catalog(registry)
        return search_catalog(catalog, query, namespace=namespace, limit=limit)

    async def tools_describe(tool_names: list[str]) -> list[dict[str, Any]]:
        """Return full JSON schemas for tool names returned by tools_search."""
        names = [str(name).strip() for name in (tool_names or []) if str(name).strip()]
        if not names:
            raise ValueError("tool_names must not be empty")
        schemas: list[dict[str, Any]] = []
        for name in names:
            tool = registry.tools.get(name)
            if tool is None:
                schemas.append({"name": name, "error": "unknown tool"})
                continue
            schema = tool.schema()["function"]
            schemas.append(
                {
                    "name": schema["name"],
                    "namespace": getattr(tool, "namespace", None) or tool_namespace(name),
                    "description": schema.get("description") or "",
                    "parameters": schema.get("parameters") or {"type": "object", "properties": {}},
                }
            )
        return schemas

    async def tools_run(tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Run any registered tool by exact name. Call tools_search + tools_describe first if unsure."""
        name = str(tool_name or "").strip()
        if not name:
            raise ValueError("tool_name is required")
        if name in META_TOOL_NAMES:
            raise ValueError("Use meta tools directly, not via tools_run")
        return await registry.call(name, dict(arguments or {}), permissions=registry.llm_permissions)

    registry.register(
        tools_search,
        "tools_search",
        (
            "Search tools beyond the surface set exposed in this request. "
            "Returns name, namespace, description. Then tools_describe + tools_run."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords, e.g. 'delete telegram message'"},
                "namespace": {
                    "type": "string",
                    "description": "Optional filter: telegram, memory, sip, employee, mcp:server_name, ...",
                },
                "limit": {"type": "integer", "description": "Max hits (default 15)"},
            },
            "required": ["query"],
        },
    )
    registry.register(
        tools_describe,
        "tools_describe",
        "Fetch JSON parameter schemas for tool names before calling tools_run.",
        parameters={
            "type": "object",
            "properties": {
                "tool_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Exact tool names from tools_search",
                }
            },
            "required": ["tool_names"],
        },
    )
    registry.register(
        tools_run,
        "tools_run",
        (
            "Execute a catalog tool by exact name. "
            "Prefer direct surface tools when listed; otherwise tools_search → tools_describe → tools_run."
        ),
        parameters={
            "type": "object",
            "properties": {
                "tool_name": {"type": "string", "description": "Exact registered tool name"},
                "arguments": {
                    "type": "object",
                    "description": "Arguments object matching tools_describe parameters",
                },
            },
            "required": ["tool_name"],
        },
    )


def schemas_for_tool_plane(
    registry: ToolRegistry,
    permissions: set[str] | None,
    *,
    limit: int = MAX_LLM_TOOLS,
) -> list[dict[str, Any]]:
    """Expose surface tools + meta layer; hide extended catalog behind tools_search."""
    registry.llm_permissions = permissions
    catalog = build_catalog(registry)
    eligible_names: list[str] = []
    for entry in catalog:
        tool = registry.tools[entry.name]
        if registry.policy.is_dangerous(entry.name) and (
            permissions is None or entry.name not in permissions
        ):
            continue
        if entry.surface:
            eligible_names.append(entry.name)

    has_extended = False
    for entry in catalog:
        if registry.policy.is_dangerous(entry.name) and (
            permissions is None or entry.name not in permissions
        ):
            continue
        if not entry.surface:
            has_extended = True
            break
    if has_extended:
        for meta in META_TOOL_NAMES:
            if meta in registry.tools and meta not in eligible_names:
                eligible_names.append(meta)

    eligible_names.sort(key=_tool_sort_key)
    if len(eligible_names) > limit:
        import logging

        logging.getLogger(__name__).warning(
            "Tool plane surface truncated from %s to %s",
            len(eligible_names),
            limit,
        )
        eligible_names = eligible_names[:limit]

    return [registry.tools[name].schema() for name in eligible_names if name in registry.tools]
