from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from contextlib import AsyncExitStack
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from openai import APIStatusError, AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from .tool_plane import schemas_for_tool_plane
from .tools import MAX_LLM_TOOLS, ToolRegistry

logger = logging.getLogger(__name__)


def _chat_tools_need_no_reasoning(model: str) -> bool:
    """gpt-5.6-* rejects tools + reasoning_effort on /v1/chat/completions."""
    return "gpt-5.6" in (model or "").strip().lower()


def _is_tools_reasoning_conflict(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "reasoning_effort" in text and "function tools" in text


def _attachment_mime(attachment: dict[str, Any]) -> str:
    mime = str(attachment.get("mime_type") or "").strip().lower()
    if mime == "image/jpg":
        return "image/jpeg"
    return mime


def _audio_input_format(attachment: dict[str, Any]) -> str | None:
    mime = _attachment_mime(attachment)
    name = str(attachment.get("filename") or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    if mime.endswith("wav") or ext in {"wav", "wave"}:
        return "wav"
    if mime.endswith("mp3") or mime.endswith("mpeg") or ext == "mp3":
        return "mp3"
    return None


def llm_user_content(
    text: str,
    attachments: list[dict[str, Any]] | None = None,
) -> str | list[dict[str, Any]]:
    """Build chat-completions user content, attaching images and supported audio."""
    parts: list[dict[str, Any]] = []
    body = str(text or "").strip()
    if body:
        parts.append({"type": "text", "text": body})
    for attachment in attachments or []:
        data_b64 = attachment.get("data_b64")
        if not data_b64:
            continue
        kind = str(attachment.get("kind") or "")
        if kind == "image":
            mime = _attachment_mime(attachment) or "image/jpeg"
            if not mime.startswith("image/"):
                mime = "image/jpeg"
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{data_b64}"},
                }
            )
        elif kind in {"voice", "audio"}:
            fmt = _audio_input_format(attachment)
            if fmt:
                parts.append(
                    {
                        "type": "input_audio",
                        "input_audio": {"data": data_b64, "format": fmt},
                    }
                )
    if not parts:
        return body
    if len(parts) == 1 and parts[0].get("type") == "text":
        return parts[0]["text"]
    if not any(part.get("type") == "text" for part in parts):
        parts.insert(0, {"type": "text", "text": "Пользователь отправил вложение."})
    return parts


async def ingest_attachments_for_llm(
    client: LLMClient,
    text: str,
    attachments: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Transcribe voice/audio and keep binary attachments for the chat request."""
    lines: list[str] = []
    caption = str(text or "").strip()
    if caption:
        lines.append(caption)
    image_count = 0
    for attachment in attachments:
        kind = str(attachment.get("kind") or "")
        if kind in {"voice", "audio"} and attachment.get("data_b64"):
            label = "Голосовое сообщение" if kind == "voice" else "Аудиофайл"
            try:
                raw = base64.b64decode(attachment["data_b64"])
                transcript = (
                    await client.transcribe_audio(
                        raw,
                        filename=str(
                            attachment.get("filename")
                            or ("voice.ogg" if kind == "voice" else "audio.mp3")
                        ),
                    )
                ).strip()
            except Exception as exc:
                logger.warning("audio transcription failed: %s", exc)
                transcript = ""
                attachment["transcript_error"] = str(exc)
            attachment["transcript"] = transcript
            if transcript:
                lines.append(f"[{label}]: {transcript}")
            else:
                lines.append(f"[{label}: речь не распознана]")
        elif kind == "image":
            image_count += 1
        elif kind == "video":
            lines.append("[Видео]")
        elif kind == "file":
            name = attachment.get("filename") or attachment.get("mime_type") or "вложение"
            lines.append(f"[Файл: {name}]")
        elif kind == "sticker":
            lines.append("[Стикер]")
    if image_count and not caption:
        lines.insert(
            0,
            "[Изображение]" if image_count == 1 else f"[Изображения: {image_count}]",
        )
    return "\n".join(line for line in lines if line).strip(), attachments


def _messages_have_media(messages: list[dict[str, Any]]) -> bool:
    for item in messages:
        content = item.get("content")
        if isinstance(content, list) and any(
            isinstance(part, dict)
            and part.get("type") in {"image_url", "input_audio", "file"}
            for part in content
        ):
            return True
    return False


def _flatten_message_content(content: Any) -> str:
    if isinstance(content, str) or content is None:
        return content or ""
    if not isinstance(content, list):
        return str(content)
    texts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            texts.append(str(part.get("text") or ""))
        elif kind == "image_url":
            texts.append("[изображение]")
        elif kind == "input_audio":
            texts.append("[аудио]")
    return "\n".join(item for item in texts if item)


def _text_only_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for item in messages:
        cloned = dict(item)
        if "content" in cloned:
            cloned["content"] = _flatten_message_content(cloned.get("content"))
        flattened.append(cloned)
    return flattened


def _looks_like_multimodal_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        needle in text
        for needle in (
            "image_url",
            "input_audio",
            "unsupported content",
            "invalid content",
            "does not support",
            "vision",
            "modalit",
        )
    )


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
        tool_schemas = schemas_for_tool_plane(tools, permissions, limit=MAX_LLM_TOOLS) or None
        reasoning_effort: str | None = "none" if tool_schemas and _chat_tools_need_no_reasoning(self.model) else None
        recent_signatures: list[str] = []
        for round_idx in range(self.max_rounds):
            response = await self._chat_complete(
                conversation,
                tool_schemas,
                reasoning_effort=reasoning_effort,
            )
            message = response.choices[0].message
            conversation.append(message.model_dump(exclude_none=True))
            if not message.tool_calls:
                return message.content or ""
            signatures: list[str] = []
            for call in message.tool_calls:
                arguments = json.loads(call.function.arguments or "{}")
                signatures.append(
                    f"{call.function.name}:{json.dumps(arguments, sort_keys=True, ensure_ascii=False)}"
                )
                try:
                    result = await tools.call(call.function.name, arguments, permissions)
                    content = json.dumps(result, ensure_ascii=False, default=str)
                except Exception as exc:
                    content = json.dumps({"error": str(exc)})
                conversation.append({"role": "tool", "tool_call_id": call.id, "content": content})
            # Same tool+args three rounds in a row → force a text reply.
            round_key = "|".join(signatures)
            if round_key and recent_signatures[-2:] == [round_key, round_key]:
                logger.warning(
                    "llm.tool_loop_break model=%s round=%s tools=%s",
                    self.model,
                    round_idx + 1,
                    signatures,
                )
                conversation.append(
                    {
                        "role": "user",
                        "content": (
                            "You are repeating the same tool calls without progress. "
                            "Stop calling tools. Reply to the user now with a short status "
                            "of what you know and what is blocked."
                        ),
                    }
                )
                break
            recent_signatures.append(round_key)
            if len(recent_signatures) > 4:
                recent_signatures = recent_signatures[-4:]

        tool_names = [str(item.get("tool") or "?") for item in (tools.audit or [])]
        logger.warning(
            "llm.max_tool_rounds model=%s rounds=%s tools=%s",
            self.model,
            self.max_rounds,
            ",".join(tool_names[-40:]) or "(none)",
        )
        # Soft landing: one final reply without tools instead of crashing Telegram routing.
        conversation.append(
            {
                "role": "user",
                "content": (
                    "Tool budget exhausted. Do not call tools. "
                    "Reply to the user in their language with a brief status: what was done, "
                    "what is blocked, and the next concrete step. Keep it under 8 sentences."
                ),
            }
        )
        try:
            response = await self._chat_complete(
                conversation,
                None,
                reasoning_effort=None,
            )
            text = (response.choices[0].message.content or "").strip()
            if text:
                return text
        except Exception as exc:
            logger.warning("llm.max_tool_rounds_final_failed: %s", exc)
        unique = []
        for name in tool_names:
            if name not in unique:
                unique.append(name)
        tools_hint = ", ".join(unique[-12:]) if unique else "нет"
        return (
            "Не успел завершить обработку за лимит шагов инструментов "
            f"({self.max_rounds}). Уже вызывал: {tools_hint}. "
            "Повторите короткое указание или откройте кейс в панели и нажмите «Продолжи»."
        )

    async def transcribe_audio(
        self,
        data: bytes,
        *,
        filename: str = "voice.ogg",
    ) -> str:
        if not data:
            return ""
        buffer = BytesIO(data)
        buffer.name = filename or "voice.ogg"
        result = await self.client.audio.transcriptions.create(
            model="whisper-1",
            file=buffer,
        )
        return str(getattr(result, "text", None) or "").strip()

    async def _chat_complete(
        self,
        conversation: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]] | None,
        *,
        reasoning_effort: str | None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": conversation,
            "tools": tool_schemas,
        }
        if reasoning_effort is not None:
            kwargs["reasoning_effort"] = reasoning_effort
        try:
            return await self.client.chat.completions.create(**kwargs)
        except APIStatusError as exc:
            if tool_schemas and reasoning_effort != "none" and _is_tools_reasoning_conflict(exc):
                kwargs["reasoning_effort"] = "none"
                return await self.client.chat.completions.create(**kwargs)
            if _looks_like_multimodal_error(exc) and _messages_have_media(conversation):
                text_only = _text_only_messages(conversation)
                conversation[:] = text_only
                kwargs["messages"] = conversation
                logger.warning("LLM rejected multimodal content; retrying text-only")
                return await self.client.chat.completions.create(**kwargs)
            raise


class MemoryStore:
    def __init__(self, _bootstrap: Any = None) -> None:
        self._fallback: dict[str, dict[str, Any]] = {}
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._client: Any = None
        self.last_error: str | None = None
 
    @staticmethod
    def _qdrant_config(url: str | None, embedding_dims: int) -> dict[str, Any]:
        vector: dict[str, Any] = {
            "collection_name": f"ice_agent_memory_v2_{embedding_dims}",
            "embedding_model_dims": embedding_dims,
        }
        raw = (url or "").strip() or "http://qdrant:6333"
        from urllib.parse import urlparse

        parsed = urlparse(raw if "://" in raw else f"http://{raw}")
        host = parsed.hostname or "qdrant"
        port = parsed.port or (443 if parsed.scheme == "https" else 6333)
        # Mem0/Qdrant client accepts only one of: url, host, path, location.
        vector.update(host=host, port=port)
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

    @staticmethod
    def _fastembed_cache_dir() -> Path:
        raw = (os.environ.get("FASTEMBED_CACHE_PATH") or "").strip()
        if raw:
            path = Path(raw)
        elif Path("/app/data").is_dir():
            path = Path("/app/data/hf/fastembed")
        else:
            path = Path.home() / ".cache" / "ice-agent" / "fastembed"
        path.mkdir(parents=True, exist_ok=True)
        os.environ["FASTEMBED_CACHE_PATH"] = str(path)
        os.environ.setdefault("HF_HOME", str(path.parent))
        return path

    @classmethod
    def _patch_fastembed_cache(cls) -> Path:
        cache_dir = cls._fastembed_cache_dir()
        try:
            from fastembed import TextEmbedding
        except ImportError:
            return cache_dir
        if getattr(TextEmbedding, "_ice_cache_patched", False):
            return cache_dir
        original = TextEmbedding.__init__
        local_only = any(cache_dir.rglob("*.onnx"))

        def wrapped(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            kwargs.setdefault("cache_dir", str(cache_dir))
            if local_only:
                kwargs.setdefault("local_files_only", True)
            try:
                return original(self, *args, **kwargs)
            except TypeError:
                kwargs.pop("local_files_only", None)
                try:
                    return original(self, *args, **kwargs)
                except TypeError:
                    kwargs.pop("cache_dir", None)
                    return original(self, *args, **kwargs)

        TextEmbedding.__init__ = wrapped  # type: ignore[method-assign]
        TextEmbedding._ice_cache_patched = True
        return cache_dir

    @staticmethod
    def _uses_openai_embeddings(llm: dict[str, Any]) -> bool:
        provider = str(llm.get("provider") or "").strip().lower()
        base = str(llm.get("base_url") or "").strip().lower()
        if "deepseek" in provider or "deepseek" in base:
            return False
        return "openai.com" in base or provider in {"openai", "custom-openai-compatible", ""}

    @classmethod
    def _local_mem0_config(cls, settings: Any, llm: dict[str, Any] | None) -> dict[str, Any]:
        if llm and cls._uses_openai_embeddings(llm):
            dims = 1536
            embedder_cfg: dict[str, Any] = {
                "api_key": llm["api_key"],
                "model": "text-embedding-3-small",
                "openai_base_url": llm.get("base_url") or "https://api.openai.com/v1",
            }
            if llm.get("http_proxy"):
                embedder_cfg["http_client_proxies"] = llm["http_proxy"]
            embedder = {"provider": "openai", "config": embedder_cfg}
        else:
            dims = 384
            cls._patch_fastembed_cache()
            embedder = {
                "provider": "fastembed",
                "config": {
                    "model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                    "embedding_dims": dims,
                },
            }
        config: dict[str, Any] = {
            "vector_store": cls._qdrant_config(settings.qdrant_url, dims),
            "embedder": embedder,
        }
        if llm:
            config["llm"] = cls._openai_compatible(llm, model=str(llm["model"]))
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
                    qdrant_url = (settings.qdrant_url or "").strip() or "http://qdrant:6333"
                    settings.qdrant_url = qdrant_url
                    config = self._local_mem0_config(settings, llm)

                    def _from_config() -> Any:
                        proxy = str((llm or {}).get("http_proxy") or "").strip()
                        if proxy:
                            os.environ.setdefault("HTTPS_PROXY", proxy)
                            os.environ.setdefault("HTTP_PROXY", proxy)
                        if config.get("embedder", {}).get("provider") == "fastembed":
                            self._patch_fastembed_cache()
                        return Memory.from_config(config)

                    self._client = await asyncio.to_thread(_from_config)
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

    def fallback_items(
        self,
        agent_id: str | None = None,
        query: str = "",
    ) -> list[dict[str, Any]]:
        scope = self._scope(None, agent_id)
        words = query.lower().split()
        return [
            item
            for item in self._fallback.values()
            if self._matches(item, scope)
            and (
                not words
                or any(word in str(item.get("memory", "")).lower() for word in words)
            )
        ]

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

    async def search_scoped(
        self,
        query: str,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
        project_id: str | None = None,
        filters: dict[str, Any] | None = None,
        include_global: bool = True,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 10), 50))
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add_items(items: list[dict[str, Any]]) -> None:
            for item in items:
                identity = str(item.get("id") or id(item))
                if identity in seen:
                    continue
                seen.add(identity)
                merged.append(item)

        extra_filters = dict(filters or {})
        normalized_project = str(project_id or "").strip()
        if normalized_project:
            add_items(
                await self.search(
                    query,
                    user_id=user_id,
                    agent_id=agent_id,
                    filters={**extra_filters, "project_id": normalized_project},
                    limit=limit,
                )
            )
        if include_global:
            broad = await self.search(
                query,
                user_id=user_id,
                agent_id=agent_id,
                filters=extra_filters or None,
                limit=max(limit, limit * 2),
            )
            global_items = [
                item
                for item in broad
                if not str((item.get("metadata") or {}).get("project_id") or "").strip()
            ]
            add_items(global_items)
        elif not normalized_project:
            add_items(
                await self.search(
                    query,
                    user_id=user_id,
                    agent_id=agent_id,
                    filters=extra_filters or None,
                    limit=limit,
                )
            )
        return merged[:limit]

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
        self.tavily_api_key: str | None = None
        self.tavily_http_proxy: str | None = None

    def configure(
        self,
        provider: str,
        searxng_url: str | None = None,
        tavily_api_key: str | None = None,
        tavily_http_proxy: str | None = None,
    ) -> None:
        self.provider = provider
        self.searxng_url = searxng_url
        self.tavily_api_key = (tavily_api_key or "").strip() or None
        self.tavily_http_proxy = (tavily_http_proxy or "").strip() or None

    def _httpx_client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"timeout": 30.0, "trust_env": False}
        if self.provider == "tavily" and self.tavily_http_proxy:
            kwargs["proxy"] = self.tavily_http_proxy
        return kwargs

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
        limit = max(1, min(int(limit or 5), 20))
        if self.provider == "tavily":
            return await self._search_tavily(query, limit)
        if self.provider == "searxng":
            return await self._search_searxng(query, limit)
        return await self._search_ddg(query, limit)

    async def _search_tavily(self, query: str, limit: int) -> list[dict[str, Any]]:
        if not self.tavily_api_key:
            raise RuntimeError("Tavily API key is not configured")
        try:
            async with httpx.AsyncClient(**self._httpx_client_kwargs()) as client:
                response = await client.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": self.tavily_api_key,
                        "query": query,
                        "max_results": limit,
                        "search_depth": "basic",
                        "include_answer": False,
                    },
                )
                if response.status_code == 401:
                    raise RuntimeError("Tavily rejected the API key")
                if response.status_code == 403:
                    hint = (
                        " (часто блокировка по IP/региону — укажите HTTP-прокси Tavily в Runtime)"
                        if not self.tavily_http_proxy
                        else " (прокси задан, но Tavily всё ещё отвечает 403 — проверьте прокси/ключ)"
                    )
                    raise RuntimeError(f"Tavily returned 403 Forbidden{hint}")
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Tavily request failed: {exc}") from exc
        except ValueError as exc:
            raise RuntimeError("Tavily returned non-JSON") from exc
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise RuntimeError("Tavily response has no results list")
        return self._normalize(results, limit)

    async def _search_searxng(self, query: str, limit: int) -> list[dict[str, Any]]:
        if not self.searxng_url:
            raise RuntimeError("SearXNG URL is not configured")
        url = f"{self.searxng_url.rstrip('/')}/search"
        try:
            async with httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                headers={"Accept": "application/json"},
                trust_env=False,
            ) as client:
                failures: list[Any] = []
                # Prefer Bing first: public default engines are often rate-limited.
                for engines in ("bing", None, "duckduckgo"):
                    params: dict[str, Any] = {
                        "q": query,
                        "format": "json",
                        "language": "ru-RU",
                    }
                    if engines:
                        params["engines"] = engines
                    response = await client.get(url, params=params)
                    response.raise_for_status()
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise RuntimeError(
                            "SearXNG returned non-JSON. Enable the JSON format in SearXNG settings."
                        ) from exc
                    results = payload.get("results") if isinstance(payload, dict) else None
                    if not isinstance(results, list):
                        raise RuntimeError("SearXNG response has no results list")
                    normalized = self._normalize(results, limit)
                    if normalized:
                        return normalized
                    failures.extend(payload.get("unresponsive_engines") or [])
        except httpx.HTTPError as exc:
            raise RuntimeError(f"SearXNG request failed: {exc}") from exc
        if failures:
            detail = ", ".join(
                f"{item[0]}: {item[1]}"
                for item in failures
                if isinstance(item, list) and len(item) >= 2
            )
            raise RuntimeError(
                f"SearXNG returned no results; engine failures: {detail}"
            )
        return []

    async def _search_ddg(self, query: str, limit: int) -> list[dict[str, Any]]:
        try:
            from duckduckgo_search import DDGS

            rows = await asyncio.to_thread(
                lambda: list(DDGS().text(query, max_results=limit))
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


def mcp_session_dead(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    needles = (
        "session terminated",
        "session closed",
        "connection closed",
        "not connected",
        "closed resource",
        "broken pipe",
        "connection reset",
    )
    return any(needle in text for needle in needles) or name in {
        "closedresourceerror",
        "connectionreseterror",
        "brokenpipeerror",
    }


class McpManager:
    def __init__(self) -> None:
        self.sessions: dict[str, Any] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stops: dict[str, asyncio.Event] = {}
        self._configs: dict[str, dict[str, Any]] = {}
        self._reconnect_tasks: dict[str, asyncio.Task[None]] = {}
        self._closing = False

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
                while not stop.is_set():
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=20)
                        break
                    except TimeoutError:
                        ping = getattr(session, "send_ping", None)
                        if ping is None:
                            continue
                        try:
                            await asyncio.wait_for(ping(), timeout=8)
                        except Exception as exc:
                            logger.warning(
                                "MCP session %s died: %s",
                                name,
                                exception_text(exc),
                            )
                            break
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            raise
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            else:
                logger.warning("MCP connection %s ended: %s", name, exception_text(exc))
        finally:
            if self.sessions.get(name) is session:
                self.sessions.pop(name, None)
            if not stop.is_set() and not self._closing and self._configs.get(name):
                self._schedule_reconnect(name)

    async def hot_reload(self, name: str, config: dict[str, Any]) -> None:
        self._configs[name] = dict(config)
        await self.disconnect(name, forget=False)
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

    async def disconnect(self, name: str, *, forget: bool = True) -> None:
        if forget:
            self._configs.pop(name, None)
            reconnect = self._reconnect_tasks.pop(name, None)
            if reconnect is not None and not reconnect.done():
                reconnect.cancel()
        stop = self._stops.pop(name, None)
        task = self._tasks.pop(name, None)
        if stop is not None:
            stop.set()
        if task is not None and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)
        self.sessions.pop(name, None)

    def _schedule_reconnect(self, name: str) -> None:
        if self._closing or not self._configs.get(name):
            return
        existing = self._reconnect_tasks.get(name)
        if existing is not None and not existing.done():
            return
        self._reconnect_tasks[name] = asyncio.create_task(
            self._reconnect_loop(name),
            name=f"mcp-reconnect-{name}",
        )

    async def _reconnect_loop(self, name: str) -> None:
        delay = 2.0
        while not self._closing and name in self._configs and name not in self.sessions:
            logger.info("MCP reconnecting %s in %.0fs", name, delay)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            if self._closing or name not in self._configs or name in self.sessions:
                return
            config = self._configs.get(name)
            if not config:
                return
            try:
                await self.hot_reload(name, config)
                logger.info("MCP reconnected %s", name)
                return
            except Exception as exc:
                logger.warning("MCP reconnect failed for %s: %s", name, exception_text(exc))
                delay = min(delay * 2, 60.0)

    async def register_tools(
        self,
        registry: ToolRegistry,
        server_names: set[str] | None = None,
    ) -> None:
        for server_name, session in list(self.sessions.items()):
            if server_names is not None and server_name not in server_names:
                continue
            try:
                async with asyncio.timeout(8):
                    result = await session.list_tools()
            except Exception as exc:
                logger.warning(
                    "MCP list_tools failed for %s: %s — skipping this turn",
                    server_name,
                    exception_text(exc),
                )
                if self.sessions.get(server_name) is session:
                    self.sessions.pop(server_name, None)
                self._schedule_reconnect(server_name)
                continue
            self._register_mcp_gateway(registry, server_name, list(result.tools))

    def _register_mcp_gateway(
        self,
        registry: ToolRegistry,
        server_name: str,
        definitions: list[Any] | None = None,
    ) -> None:
        catalog: list[Any] = []

        async def list_tools() -> list[dict[str, Any]]:
            items = definitions
            if items is None:
                session = self.sessions.get(server_name)
                if session is None:
                    raise RuntimeError(f"MCP server '{server_name}' is disconnected")
                result = await session.list_tools()
                items = list(result.tools)
            if not catalog:
                catalog.extend(items)
            return [
                {
                    "name": item.name,
                    "description": item.description or item.name,
                    "input_schema": getattr(
                        item, "inputSchema", {"type": "object", "properties": {}}
                    ),
                }
                for item in catalog
            ]

        async def run_tool(tool: str, arguments: dict[str, Any] | None = None) -> Any:
            tool_name = str(tool or "").strip()
            if not tool_name:
                raise ValueError("tool name is required")
            session = self.sessions.get(server_name)
            if session is None:
                self._schedule_reconnect(server_name)
                raise RuntimeError(
                    f"MCP server '{server_name}' is disconnected; retry after it reconnects"
                )
            kwargs = dict(arguments or {})
            try:
                response = await session.call_tool(tool_name, kwargs)
            except Exception as exc:
                if mcp_session_dead(exc):
                    if self.sessions.get(server_name) is session:
                        self.sessions.pop(server_name, None)
                    self._schedule_reconnect(server_name)
                    raise RuntimeError(
                        f"MCP server '{server_name}' session ended: {exception_text(exc)}"
                    ) from exc
                raise
            content = [item.model_dump() for item in response.content]
            if getattr(response, "isError", False):
                detail = "; ".join(str(item.get("text") or item) for item in content)
                raise RuntimeError(detail or f"MCP tool {tool_name} failed")
            if "cursorremote" in server_name.lower() and tool_name == "send_prompt":
                from .cursorremote_drive import FOLLOW_UP_HINT, click_pending_approvals, parse_mcp_payload

                approvals: list[dict[str, Any]] = []
                for _ in range(3):
                    live = self.sessions.get(server_name)
                    if live is None:
                        break
                    clicked = await click_pending_approvals(live)
                    if not clicked:
                        break
                    approvals.extend(clicked)
                return {
                    "result": parse_mcp_payload(content),
                    "approvals": approvals,
                    "done": False,
                    "next": FOLLOW_UP_HINT,
                }
            return content

        registry.register(
            list_tools,
            f"mcp_{server_name}_tools",
            f"List tools on MCP server '{server_name}' (name, description, input_schema).",
        )
        registry.register(
            run_tool,
            f"mcp_{server_name}_run",
            (
                f"Run a tool on MCP server '{server_name}'. "
                f"Call mcp_{server_name}_tools first for names and schemas."
                + (
                    " Use cursorremote_do / cursorremote_check to wait until Cursor finishes; "
                    "send_prompt alone is not completion."
                    if "cursorremote" in server_name.lower()
                    else ""
                )
            ),
            parameters={
                "type": "object",
                "properties": {
                    "tool": {
                        "type": "string",
                        "description": "Tool name returned by list_tools",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "Arguments object for the MCP tool",
                    },
                },
                "required": ["tool"],
            },
        )

    async def close(self) -> None:
        self._closing = True
        for task in list(self._reconnect_tasks.values()):
            task.cancel()
        self._reconnect_tasks.clear()
        names = set(self.sessions) | set(self._tasks) | set(self._configs)
        await asyncio.gather(
            *(self.disconnect(name) for name in names),
            return_exceptions=True,
        )
