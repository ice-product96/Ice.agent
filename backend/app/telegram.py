import asyncio
import inspect
import random
import shutil
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

from aiolimiter import AsyncLimiter

from .config import Settings
from .secrets import SecretStore
from .tools import DangerousActionError, ToolRegistry

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


def telegram_json(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return telegram_json(value.to_dict())
    if isinstance(value, dict):
        return {str(key): telegram_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [telegram_json(item) for item in value]
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def telegram_datetime(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalized_message(message: Any) -> dict[str, Any]:
    return {
        "id": getattr(message, "id", None),
        "date": telegram_datetime(getattr(message, "date", None)),
        "sender_id": getattr(message, "sender_id", None),
        "outgoing": bool(getattr(message, "out", False)),
        "text": str(
            getattr(message, "message", None)
            or getattr(message, "raw_text", None)
            or ""
        ),
    }


class TelegramToolAdapter:
    """Reflect only reviewed Telegram methods; mutation tools require confirmation."""

    allowed_methods = {
        "get_dialogs", "get_messages", "get_entity", "get_participants", "get_drafts",
        "download_media", "send_read_acknowledge", "send_message", "send_file",
        "edit_message", "delete_messages", "forward_messages",
    }
    allowed_requests = {
        "JoinChannelRequest", "LeaveChannelRequest", "SendReactionRequest",
        "SaveDraftRequest", "UpdateStatusRequest",
    }
    dangerous_methods = {
        "send_message", "send_file", "edit_message", "delete_messages",
        "forward_messages", "join_channel", "leave_channel", "send_reaction",
        "save_draft", "invoke_request",
    }
    denied_fragments = {
        "password", "auth", "delete_account", "reset_authorization", "log_out",
        "destroy", "payment", "password", "takeout", "internal",
    }

    @classmethod
    def classify(cls, name: str) -> str:
        normalized = name.removeprefix("telegram_").lower()
        if name.startswith("_") or any(value in normalized for value in cls.denied_fragments):
            return "deny"
        if normalized in cls.dangerous_methods or any(
            normalized.startswith(prefix) for prefix in ("send_", "edit_", "delete_", "forward_", "join_", "leave_")
        ):
            return "danger"
        return "safe"

    @staticmethod
    def _schema(name: str, function: Callable[..., Any], classification: str) -> dict[str, Any]:
        signature = inspect.signature(function)
        properties: dict[str, Any] = {}
        required: list[str] = []
        for parameter_name, parameter in signature.parameters.items():
            if parameter_name in {"self", "kwargs"} or parameter_name.startswith("_"):
                continue
            properties[parameter_name] = {"description": parameter_name}
            if parameter.annotation is int:
                properties[parameter_name]["type"] = "integer"
            elif parameter.annotation is bool:
                properties[parameter_name]["type"] = "boolean"
            elif parameter.annotation in {list, list[int], list[str]}:
                properties[parameter_name]["type"] = "array"
            else:
                properties[parameter_name]["type"] = "string"
            if parameter.default is inspect.Parameter.empty:
                required.append(parameter_name)
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": f"[{classification}] {inspect.getdoc(function) or name}",
                "parameters": {"type": "object", "properties": properties, "required": required},
            },
            "x-ice-classification": classification,
        }

    @classmethod
    def reflect_client(cls, client_type: type[Any]) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        for name, function in inspect.getmembers(client_type, inspect.iscoroutinefunction):
            if name.startswith("_") or name not in cls.allowed_methods or cls.classify(name) == "deny":
                continue
            schemas.append(cls._schema(f"telegram_{name}", function, cls.classify(name)))
        return schemas

    @classmethod
    def reflect_requests(cls, request_types: Iterable[type[Any]]) -> list[dict[str, Any]]:
        schemas = []
        for request_type in request_types:
            if request_type.__name__ not in cls.allowed_requests:
                continue
            schemas.append(
                cls._schema(
                    f"telegram_request_{request_type.__name__}",
                    request_type.__init__,
                    "danger",
                )
            )
        return schemas

    @classmethod
    async def authorize(
        cls,
        name: str,
        *,
        admin_confirmed: bool = False,
        permissions: set[str] | None = None,
    ) -> None:
        classification = cls.classify(name)
        if classification == "deny":
            raise DangerousActionError(f"Telegram operation '{name}' is denied")
        if classification == "danger" and not admin_confirmed and name not in (permissions or set()):
            raise DangerousActionError(f"Telegram operation '{name}' requires administrator confirmation")


class TelegramGateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.secrets = SecretStore.from_settings(settings)
        self.clients: dict[str, Any] = {}
        self.pending: dict[str, tuple[Any, str]] = {}
        self.limiters: dict[str, AsyncLimiter] = {}
        self.callbacks: dict[str, list[EventCallback]] = defaultdict(list)
        self.admin_ids = set(settings.admin_ids)
        self.typing_min_seconds = 0.4
        self.typing_max_seconds = 2.5
        self.jitter_seconds = 0.35
        self.message_chunk_size = 3800
        self.presence = False
        settings.session_dir.mkdir(parents=True, exist_ok=True)
        settings.backup_dir.mkdir(parents=True, exist_ok=True)

    def _client(self, account: Any) -> Any:
        from telethon import TelegramClient

        api_hash = self.secrets.decrypt(account.api_hash_ciphertext)
        if not account.api_id or not api_hash:
            raise RuntimeError("Telegram account credentials are not configured")
        safe = "".join(
            char for char in account.phone if char.isdigit() or char == "+"
        )
        path = (
            Path(account.session_path)
            if account.session_path
            else self.settings.session_dir / safe
        )
        client = TelegramClient(
            str(path.with_suffix("")) if path.suffix == ".session" else str(path),
            account.api_id,
            api_hash,
            proxy=self._http_proxy(account),
        )
        host = (getattr(account, "mtproto_host", None) or "").strip()
        dc_id = getattr(account, "mtproto_dc_id", None)
        if host and dc_id:
            port = int(getattr(account, "mtproto_port", None) or 443)
            client.session.set_dc(int(dc_id), host, port)
        return client

    def _http_proxy(self, account: Any) -> dict[str, Any] | None:
        from urllib.parse import unquote, urlparse

        raw = (getattr(account, "http_proxy", None) or "").strip()
        if not raw:
            return None
        parsed = urlparse(raw if "://" in raw else f"http://{raw}")
        if not parsed.hostname:
            raise RuntimeError("Telegram HTTP proxy URL is invalid")
        scheme = (parsed.scheme or "http").lower()
        if scheme not in {"http", "https"}:
            raise RuntimeError("Telegram proxy must be an HTTP(S) URL")
        proxy: dict[str, Any] = {
            "proxy_type": scheme,
            "addr": parsed.hostname,
            "port": parsed.port or (443 if scheme == "https" else 80),
        }
        if parsed.username:
            proxy["username"] = unquote(parsed.username)
        if parsed.password:
            proxy["password"] = unquote(parsed.password)
        return proxy

    async def reconnect(self, account: Any) -> str:
        phone = account.phone
        await self.disconnect(phone)
        if not account.enabled or not account.authorized:
            return "disconnected"
        client = self._client(account)
        await client.connect()
        if await client.is_user_authorized():
            self.clients[phone] = client
            self._register_handlers(phone, client)
            return "restored"
        await client.disconnect()
        return "unauthorized"

    def register_callback(self, event_name: str, callback: EventCallback) -> None:
        if event_name not in {"new_message", "message_edited", "callback_query"}:
            raise ValueError("Unsupported Telegram event")
        if not inspect.iscoroutinefunction(callback):
            raise TypeError("Telegram event callbacks must be async")
        self.callbacks[event_name].append(callback)

    async def _dispatch(self, event_name: str, event: Any, phone: str) -> None:
        message = getattr(event, "message", None)
        sender_id = getattr(event, "sender_id", None) or getattr(message, "sender_id", None)
        payload = {
            "event": event_name,
            "phone": phone,
            "sender_id": sender_id,
            "is_admin": sender_id in self.admin_ids,
            "chat_id": getattr(event, "chat_id", None) or getattr(message, "chat_id", None),
            "message_id": getattr(event, "id", None) or getattr(message, "id", None),
            "date": telegram_datetime(
                getattr(message, "date", None) or getattr(event, "date", None)
            ),
            "text": (
                getattr(event, "raw_text", None)
                or getattr(message, "message", None)
                or ""
            ),
            "outgoing": bool(getattr(event, "out", False) or getattr(message, "out", False)),
            "service": bool(getattr(message, "action", None)),
            "callback_data": telegram_json(getattr(event, "data", None)),
            "data": telegram_json(event),
        }
        await asyncio.gather(
            *(callback(payload) for callback in self.callbacks[event_name]),
            return_exceptions=True,
        )

    def _register_handlers(self, phone: str, client: Any) -> None:
        from telethon import events

        async def new_message(event: Any) -> None:
            await self._dispatch("new_message", event, phone)

        async def message_edited(event: Any) -> None:
            await self._dispatch("message_edited", event, phone)

        async def callback_query(event: Any) -> None:
            await self._dispatch("callback_query", event, phone)

        client.add_event_handler(new_message, events.NewMessage())
        client.add_event_handler(message_edited, events.MessageEdited())
        client.add_event_handler(callback_query, events.CallbackQuery())

    async def request_code(self, account: Any) -> str:
        phone = account.phone
        await self.limiters.setdefault(phone, AsyncLimiter(3, 60)).acquire()
        client = self._client(account)
        await client.connect()
        sent = await client.send_code_request(phone)
        self.pending[str(account.id)] = (client, sent.phone_code_hash, phone)
        return sent.phone_code_hash

    async def verify_code(self, account: Any, code: str, password: str | None = None) -> None:
        client, code_hash, phone = self.pending[str(account.id)]
        try:
            await client.sign_in(phone, code, phone_code_hash=code_hash)
        except Exception as exc:
            if password and exc.__class__.__name__ == "SessionPasswordNeededError":
                await client.sign_in(password=password)
            else:
                raise
        self.clients[phone] = client
        self._register_handlers(phone, client)
        self.pending.pop(str(account.id), None)
        self.backup(phone)

    async def restore(self, accounts: Iterable[Any]) -> dict[str, str]:
        results: dict[str, str] = {}
        for account in accounts:
            if not account.enabled or not account.authorized:
                continue
            try:
                client = self._client(account)
                await client.connect()
                if await client.is_user_authorized():
                    self.clients[account.phone] = client
                    self._register_handlers(account.phone, client)
                    results[account.phone] = "restored"
                else:
                    await client.disconnect()
                    results[account.phone] = "unauthorized"
            except Exception as exc:
                results[account.phone] = f"error: {exc}"
        return results

    def _get(self, phone: str) -> Any:
        try:
            return self.clients[phone]
        except KeyError as exc:
            raise RuntimeError(f"Telegram account {phone} is not connected") from exc

    async def _humanize(self, client: Any, entity: Any, text: str) -> None:
        if self.presence:
            from telethon.tl.functions.account import UpdateStatusRequest

            await client(UpdateStatusRequest(offline=False))
        delay = min(
            self.typing_max_seconds,
            max(
                self.typing_min_seconds,
                len(text) * random.uniform(0.015, 0.04)
                + random.uniform(0, self.jitter_seconds),
            ),
        )
        async with client.action(entity, "typing"):
            await asyncio.sleep(delay)

    def _split(self, text: str) -> list[str]:
        size = max(256, min(self.message_chunk_size, 4096))
        chunks: list[str] = []
        remaining = text
        while len(remaining) > size:
            split_at = max(remaining.rfind("\n", 0, size), remaining.rfind(" ", 0, size))
            split_at = split_at if split_at > size // 2 else size
            chunks.append(remaining[:split_at].rstrip())
            remaining = remaining[split_at:].lstrip()
        if remaining or not chunks:
            chunks.append(remaining)
        return chunks

    async def get_dialogs(self, phone: str, limit: int = 100) -> Any:
        return telegram_json(await self._get(phone).get_dialogs(limit=limit))

    async def get_history(self, phone: str, entity: str | int, limit: int = 100, offset_id: int = 0) -> Any:
        messages = await self._get(phone).get_messages(
            entity, limit=max(1, min(limit, 500)), offset_id=offset_id
        )
        return sorted(
            (normalized_message(message) for message in messages),
            key=lambda item: (item["date"] or "", item["id"] or 0),
        )

    async def get_conversation_history(
        self, phone: str, entity: str | int, limit: int = 100
    ) -> list[dict[str, Any]]:
        return await self.get_history(phone, entity, limit=limit)

    async def get_messages(self, phone: str, entity: str | int, ids: int | list[int]) -> Any:
        return telegram_json(await self._get(phone).get_messages(entity, ids=ids))

    async def send_message(self, phone: str, entity: str | int, text: str, reply_to: int | None = None) -> Any:
        await self.limiters.setdefault(phone, AsyncLimiter(20, 60)).acquire()
        client = self._get(phone)
        sent = []
        for index, chunk in enumerate(self._split(text)):
            await self._humanize(client, entity, chunk)
            sent.append(await client.send_message(entity, chunk, reply_to=reply_to if index == 0 else None))
        normalized = [normalized_message(message) for message in sent]
        return normalized[0] if len(normalized) == 1 else normalized

    async def send_file(self, phone: str, entity: str | int, file: str, caption: str = "") -> Any:
        client = self._get(phone)
        await self._humanize(client, entity, caption)
        return telegram_json(await client.send_file(entity, file, caption=caption))

    async def edit_message(self, phone: str, entity: str | int, message: int, text: str) -> Any:
        return telegram_json(await self._get(phone).edit_message(entity, message, text))

    async def delete_messages(self, phone: str, entity: str | int, message_ids: list[int], revoke: bool = True) -> Any:
        return telegram_json(await self._get(phone).delete_messages(entity, message_ids, revoke=revoke))

    async def forward_messages(self, phone: str, entity: str | int, messages: list[int], from_peer: str | int) -> Any:
        return telegram_json(await self._get(phone).forward_messages(entity, messages, from_peer=from_peer))

    async def get_participants(self, phone: str, entity: str | int, limit: int = 100) -> Any:
        return telegram_json(await self._get(phone).get_participants(entity, limit=limit))

    async def join_channel(self, phone: str, entity: str | int) -> Any:
        from telethon.tl.functions.channels import JoinChannelRequest

        return telegram_json(await self._get(phone)(JoinChannelRequest(entity)))

    async def leave_channel(self, phone: str, entity: str | int) -> Any:
        from telethon.tl.functions.channels import LeaveChannelRequest

        return telegram_json(await self._get(phone)(LeaveChannelRequest(entity)))

    async def send_reaction(self, phone: str, entity: str | int, message_id: int, reaction: str) -> Any:
        from telethon.tl.functions.messages import SendReactionRequest
        from telethon.tl.types import ReactionEmoji

        return telegram_json(
            await self._get(phone)(
                SendReactionRequest(peer=entity, msg_id=message_id, reaction=[ReactionEmoji(emoticon=reaction)])
            )
        )

    async def acknowledge_read(self, phone: str, entity: str | int, message: int | None = None) -> Any:
        return telegram_json(await self._get(phone).send_read_acknowledge(entity, message=message))

    async def save_draft(self, phone: str, entity: str | int, text: str, reply_to: int | None = None) -> Any:
        from telethon.tl.functions.messages import SaveDraftRequest

        return telegram_json(await self._get(phone)(SaveDraftRequest(peer=entity, message=text, reply_to=reply_to)))

    async def download_media(self, phone: str, message: Any, file: str | None = None) -> Any:
        return telegram_json(await self._get(phone).download_media(message, file=file))

    async def get_entity(self, phone: str, entity: str | int) -> Any:
        return telegram_json(await self._get(phone).get_entity(entity))

    async def get_drafts(self, phone: str) -> Any:
        return telegram_json(await self._get(phone).get_drafts())

    async def escalate(self, phone: str, text: str) -> Any:
        if not self.admin_ids:
            raise RuntimeError("No administrator Telegram IDs configured")
        return await self.send_message(phone, sorted(self.admin_ids)[0], text)

    def set_admin_ids(self, values: Iterable[int | str]) -> None:
        self.admin_ids = {int(value) for value in values}

    def configure_runtime(self, settings: Any) -> None:
        self.typing_min_seconds = settings.typing_min_seconds
        self.typing_max_seconds = settings.typing_max_seconds
        self.jitter_seconds = settings.typing_jitter_seconds
        self.message_chunk_size = settings.typing_chunk_size
        self.presence = settings.typing_presence

    def tool_registry(self, phone: str) -> ToolRegistry:
        registry = ToolRegistry()
        operations = (
            "get_dialogs", "get_history", "get_conversation_history", "get_messages", "send_message", "send_file",
            "edit_message", "delete_messages", "forward_messages", "get_participants",
            "join_channel", "leave_channel", "send_reaction", "acknowledge_read",
            "save_draft", "get_drafts", "download_media", "get_entity", "escalate",
        )
        for name in operations:
            operation = getattr(self, name)
            registry.register(partial(operation, phone), f"telegram_{name}", inspect.getdoc(operation) or name)
        return registry

    def backup(self, phone: str) -> None:
        safe = "".join(char for char in phone if char.isdigit() or char == "+")
        source = self.settings.session_dir / f"{safe}.session"
        if source.exists():
            shutil.copy2(source, self.settings.backup_dir / source.name)

    async def disconnect(self, phone: str) -> None:
        client = self.clients.pop(phone, None)
        if client:
            await client.disconnect()

    async def close(self) -> None:
        await asyncio.gather(*(client.disconnect() for client in self.clients.values()), return_exceptions=True)
        self.clients.clear()
