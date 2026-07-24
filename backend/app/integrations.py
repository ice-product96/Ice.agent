from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from typing import Any
from uuid import uuid4

import httpx
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from .tools import ToolRegistry


class LLMClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None,
        model: str,
        max_rounds: int,
        http_proxy: str | None = None,
    ) -> None:
        self.model = model
        self._http_client: httpx.AsyncClient | None = None
        if http_proxy:
            self._http_client = httpx.AsyncClient(proxy=http_proxy.strip(), timeout=60.0)
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=self._http_client,
        )
        self.max_rounds = max_rounds

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def test_connection(self) -> None:
        try:
            await self.client.models.list()
        finally:
            await self.aclose()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: ToolRegistry,
        permissions: set[str] | None = None,
    ) -> str:
        conversation = list(messages)
        for _ in range(self.max_rounds):
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=conversation,
                tools=tools.schemas() or None,
            )
            message = response.choices[0].message
            conversation.append(message.model_dump(exclude_none=True))
            if not message.tool_calls:
                return message.content or ""
            for call in message.tool_calls:
                arguments = json.loads(call.function.arguments or "{}")
                try:
                    result = await tools.call(call.function.name, arguments, permissions)
                    content = json.dumps(result, ensure_ascii=False, default=str)
                except Exception as exc:
                    content = json.dumps({"error": str(exc)})
                conversation.append({"role": "tool", "tool_call_id": call.id, "content": content})
        raise RuntimeError("Maximum tool-call rounds exceeded")


class MemoryStore:
    def __init__(self, _bootstrap: Any = None) -> None:
        self._fallback: dict[str, dict[str, Any]] = {}
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._client: Any = None
        self.last_error: str | None = None
 
    @staticmethod
    def _qdrant_config(url: str | None, embedding_dims: int) -> dict[str, Any]:
        vector: dict[str, Any] = {
            "collection_name": "ice_agent_memory",
            "embedding_model_dims": embedding_dims,
        }
        if not url:
            return {"provider": "qdrant", "config": vector}
        from urllib.parse import urlparse

        parsed = urlparse(url if "://" in url else f"http://{url}")
        host = parsed.hostname or "qdrant"
        port = parsed.port or (443 if parsed.scheme == "https" else 6333)
        vector.update(host=host, port=port, url=f"{parsed.scheme or 'http'}://{host}:{port}")
        return {"provider": "qdrant", "config": vector}

    @staticmethod
    def _openai_compatible(llm: dict[str, Any], *, model: str) -> dict[str, Any]:
        config: dict[str, Any] = {
            "api_key": llm["api_key"],
            "model": model,
        }
        if llm.get("base_url"):
            config["openai_base_url"] = llm["base_url"]
        return {"provider": "openai", "config": config}

    @classmethod
    def _local_mem0_config(cls, settings: Any, llm: dict[str, Any] | None) -> dict[str, Any]:
        base = str((llm or {}).get("base_url") or "").lower()
        # DeepSeek chat models cannot embed; use their embedding model when possible.
        if "deepseek" in base:
            embed_model, dims = "deepseek-embedding", 1024
        else:
            embed_model, dims = "text-embedding-3-small", 1536
        config: dict[str, Any] = {
            "vector_store": cls._qdrant_config(settings.qdrant_url, dims),
        }
        if llm:
            config["llm"] = cls._openai_compatible(llm, model=str(llm["model"]))
            # Mem0 defaults to OpenAI embeddings; without this local mode stays degraded.
            config["embedder"] = cls._openai_compatible(llm, model=embed_model)
        return config

    async def reconfigure(
        self,
        settings: Any,
        mem0_api_key: str | None = None,
        llm: dict[str, Any] | None = None,
    ) -> None:
        self._client = None
        self.last_error = None
        if settings.memory_enabled:
            try:
                if settings.memory_backend == "platform":
                    from mem0 import MemoryClient

                    if not mem0_api_key:
                        raise RuntimeError("Mem0 platform API key is not configured")
                    self._client = MemoryClient(api_key=mem0_api_key)
                else:
                    from mem0 import Memory

                    if not llm:
                        raise RuntimeError(
                            "Local memory requires an enabled LLM profile for fact extraction and embeddings"
                        )
                    if not settings.qdrant_url:
                        raise RuntimeError("Qdrant URL is required for local Mem0 memory")
                    config = self._local_mem0_config(settings, llm)
                    self._client = Memory.from_config(config)
            except Exception as exc:
                self.last_error = exception_text(exc)
                self._client = None
            else:
                if self._client is not None and self._fallback:
                    await self.migrate_fallback()

    async def migrate_fallback(self) -> dict[str, int]:
        """Move in-process fallback memories into the configured Mem0/Qdrant backend."""
        if self._client is None:
            raise RuntimeError("Memory backend is not connected")
        migrated = 0
        failed = 0
        for memory_id, item in list(self._fallback.items()):
            text = str(item.get("memory") or item.get("text") or "").strip()
            if not text:
                self._fallback.pop(memory_id, None)
                continue
            metadata = dict(item.get("metadata") or {})
            user_id = str(item.get("user_id") or metadata.get("user_id") or "global")
            agent_id = item.get("agent_id") or metadata.get("agent_id")
            scope = self._scope(user_id, str(agent_id) if agent_id is not None else None)
            enriched = {**metadata, **scope, "migrated_from": "fallback", "legacy_id": memory_id}
            try:
                await asyncio.to_thread(self._client.add, text, metadata=enriched, **scope)
                self._fallback.pop(memory_id, None)
                self._history.pop(memory_id, None)
                migrated += 1
            except Exception:
                failed += 1
        return {"migrated": migrated, "failed": failed, "remaining": len(self._fallback)}

    def fallback_count(self) -> int:
        return len(self._fallback)

    @staticmethod
    def _items(result: Any) -> list[dict[str, Any]]:
        if isinstance(result, dict) and "results" in result:
            return result["results"]
        if isinstance(result, list):
            return result
        return [result] if isinstance(result, dict) else []

    @staticmethod
    def _scope(user_id: str | None, agent_id: str | None) -> dict[str, str]:
        scope: dict[str, str] = {}
        if user_id is not None:
            scope["user_id"] = str(user_id)
        if agent_id is not None:
            scope["agent_id"] = str(agent_id)
        return scope

    @staticmethod
    def _matches(item: dict[str, Any], scope: dict[str, str], filters: dict[str, Any] | None = None) -> bool:
        metadata = item.get("metadata", {})
        return all(str(item.get(key, metadata.get(key))) == value for key, value in scope.items()) and all(
            metadata.get(key, item.get(key)) == value for key, value in (filters or {}).items()
        )

    async def add(
        self,
        text: str,
        user_id: str,
        agent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        scope = self._scope(user_id, agent_id)
        enriched = {**(metadata or {}), **scope}
        if self._client:
            return await asyncio.to_thread(self._client.add, text, metadata=enriched, **scope)
        key = str(uuid4())
        self._fallback[key] = {"id": key, "memory": text, **scope, "metadata": enriched}
        self._history[key] = [{"event": "ADD", "memory": text}]
        return self._fallback[key]

    async def search(
        self,
        query: str,
        user_id: str | None = None,
        agent_id: str | None = None,
        filters: dict[str, Any] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        scope = self._scope(user_id, agent_id)
        limit = max(1, min(int(limit or 10), 50))
        items: list[dict[str, Any]] = []
        if self._client:
            result = await asyncio.to_thread(
                self._client.search,
                query,
                filters={**scope, **(filters or {})},
                limit=limit,
            )
            items.extend(self._items(result))
        words = query.lower().split()
        items.extend(
            item for item in self._fallback.values()
            if self._matches(item, scope, filters)
            and (not words or any(word in item["memory"].lower() for word in words))
        )
        return items[:limit]

    async def get(self, memory_id: str) -> dict[str, Any] | None:
        if self._client:
            return await asyncio.to_thread(self._client.get, memory_id)
        return self._fallback.get(memory_id)

    async def get_all(
        self,
        user_id: str | None = None,
        agent_id: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        scope = self._scope(user_id, agent_id)
        items: list[dict[str, Any]] = []
        if self._client:
            items.extend(self._items(await asyncio.to_thread(
                self._client.get_all,
                filters={**scope, **(filters or {})},
            )))
        items.extend(
            item for item in self._fallback.values() if self._matches(item, scope, filters)
        )
        return items

    async def list(self, user_id: str, agent_id: str | None = None) -> list[dict[str, Any]]:
        return await self.get_all(user_id, agent_id)

    async def update(self, memory_id: str, text: str) -> Any:
        if self._client:
            return await asyncio.to_thread(self._client.update, memory_id, data=text)
        item = self._fallback.get(memory_id)
        if item is None:
            raise KeyError(memory_id)
        old = item["memory"]
        item["memory"] = text
        self._history.setdefault(memory_id, []).append({"event": "UPDATE", "old_memory": old, "memory": text})
        return item

    async def delete(self, memory_id: str) -> None:
        if self._client:
            await asyncio.to_thread(self._client.delete, memory_id)
        self._fallback.pop(memory_id, None)

    async def delete_all(self, user_id: str | None = None, agent_id: str | None = None) -> None:
        scope = self._scope(user_id, agent_id)
        if self._client:
            await asyncio.to_thread(self._client.delete_all, **scope)
            return
        for memory_id, item in list(self._fallback.items()):
            if self._matches(item, scope):
                self._fallback.pop(memory_id, None)

    async def history(self, memory_id: str) -> list[dict[str, Any]]:
        if self._client:
            return self._items(await asyncio.to_thread(self._client.history, memory_id))
        return list(self._history.get(memory_id, []))

    async def reset(self) -> None:
        if self._client:
            await asyncio.to_thread(self._client.reset)
        self._fallback.clear()
        self._history.clear()


class WebSearch:
    def __init__(self, _bootstrap: Any = None) -> None:
        self.provider = "ddg"
        self.searxng_url: str | None = None

    def configure(self, provider: str, searxng_url: str | None) -> None:
        self.provider = provider
        self.searxng_url = searxng_url

    @staticmethod
    def _normalize(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in items[: max(1, min(int(limit or 5), 20))]:
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or item.get("href") or item.get("link") or "").strip()
            content = str(
                item.get("content")
                or item.get("body")
                or item.get("snippet")
                or item.get("description")
                or ""
            ).strip()
            if not (title or url or content):
                continue
            normalized.append({"title": title, "url": url, "content": content})
        return normalized

    async def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Search the public web and return title/url/content hits."""
        query = str(query or "").strip()
        if not query:
            raise ValueError("Search query must not be empty")
        if self.provider == "searxng":
            if not self.searxng_url:
                raise RuntimeError("SearXNG URL is not configured")
            url = f"{self.searxng_url.rstrip('/')}/search"
            try:
                async with httpx.AsyncClient(
                    timeout=30.0,
                    follow_redirects=True,
                    headers={"Accept": "application/json"},
                ) as client:
                    response = await client.get(
                        url,
                        params={"q": query, "format": "json", "language": "ru-RU"},
                    )
                    response.raise_for_status()
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise RuntimeError(
                            "SearXNG returned non-JSON. Enable the JSON format in SearXNG settings."
                        ) from exc
            except httpx.HTTPError as exc:
                raise RuntimeError(f"SearXNG request failed: {exc}") from exc
            results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(results, list):
                raise RuntimeError("SearXNG response has no results list")
            return self._normalize(results, limit)
        try:
            from duckduckgo_search import DDGS

            rows = await asyncio.to_thread(
                lambda: list(DDGS().text(query, max_results=max(1, min(int(limit or 5), 20))))
            )
            return self._normalize(
                [
                    {
                        "title": row.get("title"),
                        "url": row.get("href"),
                        "content": row.get("body"),
                    }
                    for row in rows
                    if isinstance(row, dict)
                ],
                limit,
            )
        except Exception as exc:
            raise RuntimeError(f"DuckDuckGo search failed: {exc}") from exc


def exception_text(exc: BaseException) -> str:
    """Flatten ExceptionGroup so connection errors are actionable in the UI."""
    nested = getattr(exc, "exceptions", None)
    if nested:
        messages = [exception_text(item) for item in nested]
        return "; ".join(dict.fromkeys(message for message in messages if message))
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


class McpManager:
    def __init__(self) -> None:
        self.sessions: dict[str, Any] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stops: dict[str, asyncio.Event] = {}

    async def _run_connection(
        self,
        name: str,
        config: dict[str, Any],
        stop: asyncio.Event,
        ready: asyncio.Future[None],
    ) -> None:
        import inspect

        import httpx
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.sse import sse_client
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client

        transport_name = config.get("transport", "stdio")
        headers = {
            str(key): str(value)
            for key, value in (config.get("env") or {}).items()
        }
        session: Any = None
        try:
            async with AsyncExitStack() as stack:
                if transport_name == "stdio":
                    transport = stdio_client(
                        StdioServerParameters(
                            command=config["command"],
                            args=config.get("args", []),
                            env=headers or None,
                        )
                    )
                elif transport_name == "sse":
                    transport = sse_client(
                        config["url"],
                        headers=headers or None,
                    )
                elif transport_name == "streamable-http":
                    params = inspect.signature(streamable_http_client).parameters
                    if "headers" in params:
                        transport = streamable_http_client(
                            config["url"],
                            headers=headers or None,
                        )
                    elif "http_client" in params:
                        http_client = await stack.enter_async_context(
                            httpx.AsyncClient(
                                headers=headers,
                                timeout=httpx.Timeout(30.0, read=300.0),
                                follow_redirects=True,
                            )
                        )
                        transport = streamable_http_client(
                            config["url"],
                            http_client=http_client,
                        )
                    else:
                        transport = streamable_http_client(config["url"])
                else:
                    raise ValueError(
                        f"Unsupported MCP transport: {transport_name}"
                    )

                streams = await stack.enter_async_context(transport)
                read, write = streams[:2]
                session = await stack.enter_async_context(
                    ClientSession(read, write)
                )
                await session.initialize()
                self.sessions[name] = session
                if not ready.done():
                    ready.set_result(None)
                await stop.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            elif not isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            if self.sessions.get(name) is session:
                self.sessions.pop(name, None)

    async def hot_reload(self, name: str, config: dict[str, Any]) -> None:
        await self.disconnect(name)
        stop = asyncio.Event()
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(
            self._run_connection(name, dict(config), stop, ready),
            name=f"mcp-session-{name}",
        )
        self._stops[name] = stop
        self._tasks[name] = task
        try:
            async with asyncio.timeout(15):
                await asyncio.shield(ready)
        except BaseException:
            stop.set()
            await asyncio.gather(task, return_exceptions=True)
            self._stops.pop(name, None)
            self._tasks.pop(name, None)
            raise

    async def disconnect(self, name: str) -> None:
        stop = self._stops.pop(name, None)
        task = self._tasks.pop(name, None)
        if stop is not None:
            stop.set()
        if task is not None and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)
        self.sessions.pop(name, None)

    async def register_tools(
        self,
        registry: ToolRegistry,
        server_names: set[str] | None = None,
    ) -> None:
        for server_name, session in self.sessions.items():
            if server_names is not None and server_name not in server_names:
                continue
            result = await session.list_tools()
            for definition in result.tools:
                async def invoke(_server=server_name, _tool=definition.name, **kwargs: Any) -> Any:
                    response = await self.sessions[_server].call_tool(_tool, kwargs)
                    content = [item.model_dump() for item in response.content]
                    if getattr(response, "isError", False):
                        detail = "; ".join(
                            str(item.get("text") or item) for item in content
                        )
                        raise RuntimeError(detail or f"MCP tool {_tool} failed")
                    return content
                registry.register(
                    invoke,
                    f"mcp_{server_name}_{definition.name}",
                    definition.description or definition.name,
                    getattr(definition, "inputSchema", {"type": "object", "properties": {}}),
                )

    async def close(self) -> None:
        names = set(self.sessions) | set(self._tasks)
        await asyncio.gather(
            *(self.disconnect(name) for name in names),
            return_exceptions=True,
        )
