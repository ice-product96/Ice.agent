"""Project-scoped long-term memory helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MEMORY_CATEGORIES = frozenset({
    "contact",
    "project",
    "decision",
    "preference",
    "note",
    "fact",
    "exchange",
})


@dataclass(frozen=True, slots=True)
class MemoryScope:
    user_id: str
    agent_id: str
    project_id: str | None = None
    customer_id: str | None = None
    chat_id: str | None = None
    thread_id: str | None = None


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _memory_config(agent_config: dict[str, Any] | None) -> dict[str, Any]:
    raw = (agent_config or {}).get("memory")
    return raw if isinstance(raw, dict) else {}


def bind_conversation_from_config(
    state: Any,
    agent_config: dict[str, Any] | None,
    context: dict[str, Any] | None = None,
) -> None:
    """Apply agent memory bindings when the conversation has no explicit project."""
    if _clean(getattr(state, "project_id", None)):
        return
    config = _memory_config(agent_config)
    context = context or {}
    thread_id = _clean(context.get("thread_id") or context.get("topic_id"))
    chat_id = _clean(context.get("chat_id"))
    thread_projects = config.get("thread_projects") or {}
    chat_projects = config.get("chat_projects") or {}
    project_id = None
    if thread_id and isinstance(thread_projects, dict):
        project_id = _clean(thread_projects.get(thread_id) or thread_projects.get(str(thread_id)))
    if not project_id and chat_id and isinstance(chat_projects, dict):
        project_id = _clean(chat_projects.get(chat_id) or chat_projects.get(str(chat_id)))
    if not project_id:
        project_id = _clean(config.get("default_project_id"))
    if project_id:
        state.project_id = project_id
    customer_id = _clean(config.get("default_customer_id"))
    if customer_id and not _clean(getattr(state, "customer_id", None)):
        state.customer_id = customer_id


def resolve_memory_scope(
    context: dict[str, Any],
    agent: Any,
    *,
    state: Any | None = None,
) -> MemoryScope:
    user_id = str(
        context.get("user_id")
        or context.get("sender_id")
        or context.get("chat_id")
        or "global"
    )
    thread_id = _clean(context.get("thread_id") or context.get("topic_id"))
    if state is not None:
        thread_id = thread_id or _clean(getattr(state, "thread_id", None))
    project_id = _clean(context.get("project_id"))
    customer_id = _clean(context.get("customer_id"))
    if state is not None:
        project_id = project_id or _clean(getattr(state, "project_id", None))
        customer_id = customer_id or _clean(getattr(state, "customer_id", None))
    if not project_id:
        config = _memory_config(getattr(agent, "config", None))
        thread_projects = config.get("thread_projects") or {}
        chat_projects = config.get("chat_projects") or {}
        chat_id = _clean(context.get("chat_id"))
        if thread_id and isinstance(thread_projects, dict):
            project_id = _clean(
                thread_projects.get(thread_id) or thread_projects.get(str(thread_id))
            )
        if not project_id and chat_id and isinstance(chat_projects, dict):
            project_id = _clean(chat_projects.get(chat_id) or chat_projects.get(str(chat_id)))
        if not project_id:
            project_id = _clean(config.get("default_project_id"))
    return MemoryScope(
        user_id=user_id,
        agent_id=str(agent.id),
        project_id=project_id,
        customer_id=customer_id,
        chat_id=_clean(context.get("chat_id")),
        thread_id=thread_id,
    )


def build_memory_metadata(
    scope: MemoryScope,
    *,
    category: str = "note",
    global_scope: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = category.strip().lower() or "note"
    if normalized not in MEMORY_CATEGORIES:
        normalized = "note"
    metadata: dict[str, Any] = {
        "kind": normalized,
        "category": normalized,
    }
    if scope.customer_id:
        metadata["customer_id"] = scope.customer_id
    if scope.chat_id:
        metadata["chat_id"] = scope.chat_id
    if scope.thread_id:
        metadata["thread_id"] = scope.thread_id
    if not global_scope and scope.project_id:
        metadata["project_id"] = scope.project_id
    if extra:
        metadata.update(extra)
    return metadata


def memory_scope_prompt(scope: MemoryScope) -> str:
    parts = [f"user={scope.user_id}", f"agent={scope.agent_id}"]
    if scope.project_id:
        parts.append(f"project={scope.project_id}")
    if scope.customer_id:
        parts.append(f"customer={scope.customer_id}")
    if scope.thread_id:
        parts.append(f"thread={scope.thread_id}")
    return "Active memory scope: " + ", ".join(parts)


def format_memory_hits(items: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in items:
        text = str(item.get("memory") or item.get("text") or "").strip()
        if not text:
            continue
        metadata = item.get("metadata") or {}
        tags: list[str] = []
        project_id = _clean(metadata.get("project_id"))
        category = _clean(metadata.get("category") or metadata.get("kind"))
        if project_id:
            tags.append(f"project={project_id}")
        if category:
            tags.append(category)
        prefix = f"[{', '.join(tags)}] " if tags else ""
        lines.append(f"- {prefix}{text}")
    return "\n".join(lines)


async def prefetch_memories(
    memory: Any,
    query: str,
    scope: MemoryScope,
    *,
    limit: int = 8,
    include_global: bool = True,
) -> list[dict[str, Any]]:
    search = getattr(memory, "search_scoped", None)
    if callable(search):
        return await search(
            query,
            user_id=scope.user_id,
            agent_id=scope.agent_id,
            project_id=scope.project_id,
            include_global=include_global,
            limit=limit,
        )
    filters: dict[str, Any] = {}
    if scope.project_id:
        filters["project_id"] = scope.project_id
    return await memory.search(
        query,
        user_id=scope.user_id,
        agent_id=scope.agent_id,
        filters=filters or None,
        limit=limit,
    )
