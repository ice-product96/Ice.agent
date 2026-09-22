"""Project- and customer-scoped long-term memory helpers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

MEMORY_CATEGORIES = frozenset({
    "contact",
    "project",
    "decision",
    "preference",
    "constraint",
    "domain_fact",
    "person",
    "integration",
    "risk",
    "note",
    "fact",
    "exchange",
})

FACT_KIND_PRIORITY = (
    "decision",
    "constraint",
    "preference",
    "risk",
    "integration",
    "person",
    "domain_fact",
    "project",
    "contact",
    "fact",
    "note",
    "exchange",
)

logger = logging.getLogger(__name__)


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


def customer_namespace(customer_id: str | None) -> str | None:
    cleaned = _clean(customer_id)
    return f"customer:{cleaned}" if cleaned else None


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


def _item_kind(item: dict[str, Any]) -> str:
    metadata = item.get("metadata") or {}
    return str(metadata.get("kind") or metadata.get("category") or "note").strip().lower()


def rerank_memories(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedup then prefer durable fact kinds over raw exchanges."""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in items:
        text = str(item.get("memory") or item.get("text") or "").strip().lower()
        identity = str(item.get("id") or "") or text
        if not identity or identity in seen:
            continue
        seen.add(identity)
        unique.append(item)
    order = {kind: index for index, kind in enumerate(FACT_KIND_PRIORITY)}
    unique.sort(key=lambda item: order.get(_item_kind(item), len(order)))
    return unique


async def _search_one(
    memory: Any,
    query: str,
    scope: MemoryScope,
    *,
    limit: int,
    include_global: bool,
) -> list[dict[str, Any]]:
    search = getattr(memory, "search_scoped", None)
    if callable(search):
        return await search(
            query,
            user_id=scope.user_id,
            agent_id=scope.agent_id,
            project_id=scope.project_id,
            customer_id=scope.customer_id,
            include_global=include_global,
            limit=limit,
        )
    filters: dict[str, Any] = {}
    if scope.project_id:
        filters["project_id"] = scope.project_id
    if scope.customer_id:
        filters["customer_id"] = scope.customer_id
    return await memory.search(
        query,
        user_id=scope.user_id,
        agent_id=scope.agent_id,
        filters=filters or None,
        limit=limit,
    )


async def prefetch_memories(
    memory: Any,
    query: str,
    scope: MemoryScope,
    *,
    limit: int = 8,
    include_global: bool = True,
    judgment: Any | None = None,
    situation: dict[str, Any] | None = None,
    db: Any = None,
    agent_id: int | None = None,
    work_item_id: int | None = None,
) -> list[dict[str, Any]]:
    """Retrieve memories. With a judge, plan 2-5 queries and merge/rerank."""
    if getattr(memory, "degraded", False):
        return []
    queries = [str(query or "").strip()] if str(query or "").strip() else []
    if judgment is not None and situation is not None:
        try:
            planned = await judgment.judge(
                "retrieval_plan",
                {
                    "situation": situation,
                    "fallback_query": query,
                    "scope": {
                        "project_id": scope.project_id,
                        "customer_id": scope.customer_id,
                    },
                },
                db=db,
                agent_id=agent_id,
                work_item_id=work_item_id,
                project_id=scope.project_id,
            )
            verdict = planned.verdict
            if verdict is not None and getattr(verdict, "verdict", "") == "planned":
                planned_queries = [
                    str(row.text).strip()
                    for row in list(getattr(verdict, "queries", None) or [])
                    if str(getattr(row, "text", "") or "").strip()
                ]
                if planned_queries:
                    queries = planned_queries[:5]
        except Exception as exc:
            logger.info("retrieval_plan failed: %s", exc)
    if not queries:
        queries = [query or "project decisions preferences constraints"]
    batches = await asyncio.gather(
        *(
            _search_one(
                memory,
                text,
                scope,
                limit=max(limit, 6),
                include_global=include_global,
            )
            for text in queries
        ),
        return_exceptions=True,
    )
    merged: list[dict[str, Any]] = []
    for batch in batches:
        if isinstance(batch, Exception):
            logger.info("memory search failed: %s", batch)
            continue
        merged.extend(batch)
    return rerank_memories(merged)[:limit]


async def extract_and_store_memories(
    memory: Any,
    scope: MemoryScope,
    *,
    user_text: str,
    assistant_text: str,
    judgment: Any | None,
    known_facts: list[str] | None = None,
    db: Any = None,
    agent_id: int | None = None,
    work_item_id: int | None = None,
    source_message_id: str | None = None,
) -> dict[str, Any]:
    """Write typed facts from one exchange. Returns extract meta."""
    if getattr(memory, "degraded", False):
        return {"stored": 0, "degraded": True, "verdict": "memory_degraded"}
    if judgment is None:
        return {"stored": 0, "verdict": "no_judge"}
    payload = {
        "user": (user_text or "")[:4000],
        "assistant": (assistant_text or "")[:4000],
        "known_facts": list(known_facts or [])[:20],
        "scope": {
            "project_id": scope.project_id,
            "customer_id": scope.customer_id,
        },
    }
    result = await judgment.judge(
        "memory_extract",
        payload,
        db=db,
        agent_id=agent_id,
        work_item_id=work_item_id,
        project_id=scope.project_id,
    )
    verdict = result.verdict
    facts = list(getattr(verdict, "facts", None) or []) if verdict is not None else []
    stored = 0
    write_user = customer_namespace(scope.customer_id) or scope.user_id
    for fact in facts:
        text = str(getattr(fact, "text", "") or "").strip()
        if not text:
            continue
        kind = str(getattr(fact, "kind", "") or "note")
        extra = {
            "confidence": float(getattr(fact, "confidence", 0.0) or 0.0),
            "subject": str(getattr(fact, "subject", "") or ""),
            "source_message_id": source_message_id,
        }
        hint = getattr(fact, "supersedes_hint", None)
        if hint:
            extra["supersedes"] = str(hint)
        ttl = getattr(fact, "ttl_days", None)
        if ttl is not None:
            extra["ttl_days"] = int(ttl)
        try:
            await memory.add(
                text,
                write_user,
                scope.agent_id,
                build_memory_metadata(scope, category=kind, extra=extra),
            )
            stored += 1
        except Exception as exc:
            logger.info("memory extract store failed: %s", exc)
    return {
        "stored": stored,
        "verdict": result.value("nothing_new"),
        "degraded": result.degraded,
        "record_id": result.record_id,
    }


async def check_known_already(
    judgment: Any,
    *,
    question: str,
    spec: dict[str, Any] | None,
    decisions: list[dict[str, Any]] | None,
    memories: list[dict[str, Any]] | None,
    db: Any = None,
    agent_id: int | None = None,
    work_item_id: int | None = None,
    project_id: str | None = None,
) -> Any | None:
    """Ask whether a planned customer question is already answered."""
    if judgment is None or not str(question or "").strip():
        return None
    return await judgment.judge(
        "known_already",
        {
            "question": question[:2000],
            "spec": spec or {},
            "decisions": (decisions or [])[:12],
            "memories": [
                str(item.get("memory") or item.get("text") or "")[:400]
                for item in (memories or [])[:12]
            ],
        },
        db=db,
        agent_id=agent_id,
        work_item_id=work_item_id,
        project_id=project_id,
    )
