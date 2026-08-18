import asyncio
import base64
import inspect
import logging
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
logger = logging.getLogger(__name__)

MAX_TELEGRAM_ATTACHMENT_BYTES = 20 * 1024 * 1024
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_AUDIO_EXTS = {".ogg", ".oga", ".opus", ".mp3", ".wav", ".m4a", ".flac"}
_IMAGE_MIMES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/bmp",
}
_KIND_LABELS = {
    "image": "изображение",
    "voice": "голосовое сообщение",
    "audio": "аудиофайл",
    "video": "видео",
    "sticker": "стикер",
    "file": "файл",
}


def telegram_topic_id(message: Any) -> int | None:
    reply_to = getattr(message, "reply_to", None)
    if reply_to is not None:
        top_id = getattr(reply_to, "reply_to_top_id", None)
        if top_id:
            return int(top_id)
        if getattr(reply_to, "forum_topic", False):
            msg_id = getattr(reply_to, "reply_to_msg_id", None)
            if msg_id:
                return int(msg_id)
    action = getattr(message, "action", None)
    if action is not None and getattr(action, "title", None):
        message_id = getattr(message, "id", None)
        if message_id:
            return int(message_id)
    return None


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


def classify_telegram_media(message: Any) -> dict[str, Any] | None:
    """Return kind/mime/filename/size for a Telethon message, or None if there is no media."""
    if message is None:
        return None
    file = getattr(message, "file", None)
    has_media = any(
        getattr(message, name, None) is not None
        for name in (
            "media",
            "photo",
            "voice",
            "audio",
            "video",
            "video_note",
            "sticker",
            "gif",
            "document",
            "file",
        )
    )
    if not has_media:
        return None
    mime = str(getattr(file, "mime_type", None) or "").strip().lower()
    name = str(getattr(file, "name", None) or "")
    ext = str(getattr(file, "ext", None) or "").strip().lower()
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    if not ext and "." in name:
        ext = "." + name.rsplit(".", 1)[-1].lower()
    size = int(getattr(file, "size", None) or 0)
    if mime == "application/x-tgsticker":
        kind = "sticker"
    elif getattr(message, "voice", None) is not None:
        kind = "voice"
    elif getattr(message, "photo", None) is not None or mime in _IMAGE_MIMES or ext in _IMAGE_EXTS:
        kind = "image"
    elif getattr(message, "sticker", None) is not None and mime.startswith("image/"):
        kind = "image"
    elif getattr(message, "sticker", None) is not None:
        kind = "sticker"
    elif (
        getattr(message, "audio", None) is not None
        or mime.startswith("audio/")
        or ext in _AUDIO_EXTS
    ):
        kind = "audio"
    elif (
        getattr(message, "video", None) is not None
        or getattr(message, "video_note", None) is not None
        or getattr(message, "gif", None) is not None
        or mime.startswith("video/")
    ):
        kind = "video"
    elif getattr(message, "document", None) is not None or file is not None:
        kind = "file"
    else:
        return None
    return {
        "kind": kind,
        "mime_type": mime,
        "filename": name,
        "size": size,
    }


def attachment_label(attachments: Iterable[dict[str, Any]]) -> str:
    labels = [
        _KIND_LABELS.get(str(item.get("kind") or ""), "вложение")
        for item in attachments
    ]
    unique = [label for label in dict.fromkeys(labels) if label]
    if not unique:
        return ""
    return "[Вложение: " + ", ".join(unique) + "]"


def public_attachment(attachment: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in attachment.items() if key != "data_b64"}


def event_messages(event: Any) -> list[Any]:
    messages = getattr(event, "messages", None)
    if messages:
        return list(messages)
    message = getattr(event, "message", None)
    return [message] if message is not None else []


async def _download_media_bytes(event: Any, message: Any) -> bytes | None:
    client = getattr(event, "client", None) or getattr(message, "client", None)
    if client is not None and getattr(client, "download_media", None):
        data = await client.download_media(message, file=bytes)
    elif getattr(message, "download_media", None):
        data = await message.download_media(file=bytes)
    else:
        return None
    return data if isinstance(data, bytes) else None


async def collect_telegram_attachments(event: Any, messages: Iterable[Any]) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    for message in messages:
        meta = classify_telegram_media(message)
        if meta is None:
            continue
        item = dict(meta)
        kind = str(item.get("kind") or "")
        download = kind in {"image", "voice", "audio", "file", "document"}
        size = int(item.get("size") or 0)
        if download and size > MAX_TELEGRAM_ATTACHMENT_BYTES:
            item["skipped"] = "too_large"
            download = False
        if download:
            try:
                data = await _download_media_bytes(event, message)
            except Exception as exc:
                logger.warning("telegram media download failed: %s", exc)
                item["download_error"] = str(exc)
                data = None
            if data:
                if len(data) > MAX_TELEGRAM_ATTACHMENT_BYTES:
                    item["skipped"] = "too_large"
                    item["size"] = len(data)
                else:
                    item["data_b64"] = base64.b64encode(data).decode("ascii")
                    item["size"] = len(data)
        attachments.append(item)
    return attachments


def normalize_contact_phone(value: str) -> str:
    """Normalize a phone number for Telegram contact import."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Phone number must not be empty")
    has_plus = raw.startswith("+")
    digits = "".join(char for char in raw if char.isdigit())
    if raw.startswith("00") and digits.startswith("00"):
        digits = digits[2:]
        has_plus = True
    if not digits:
        raise ValueError("Phone number must contain digits")
    # Common RU local form 8XXXXXXXXXX -> +7XXXXXXXXXX
    if not has_plus and len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return f"+{digits}"


def _user_summary(user: Any) -> dict[str, Any]:
    return {
        "id": getattr(user, "id", None),
        "username": getattr(user, "username", None),
        "first_name": getattr(user, "first_name", None),
        "last_name": getattr(user, "last_name", None),
        "phone": getattr(user, "phone", None),
        "bot": bool(getattr(user, "bot", False)),
        "scam": bool(getattr(user, "scam", False)),
        "fake": bool(getattr(user, "fake", False)),
        "premium": bool(getattr(user, "premium", False)),
    }


def _participant_role(participant: Any) -> str:
    name = type(participant).__name__
    if "Creator" in name:
        return "creator"
    if "Admin" in name:
        return "admin"
    if "Banned" in name:
        return "banned"
    if "Left" in name:
        return "left"
    return "member"


def telegram_datetime(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalized_message(message: Any) -> dict[str, Any]:
    text = str(
        getattr(message, "message", None)
        or getattr(message, "raw_text", None)
        or ""
    ).strip()
    media = classify_telegram_media(message)
    if not text and media is not None:
        text = attachment_label([media])
    return {
        "id": getattr(message, "id", None),
        "date": telegram_datetime(getattr(message, "date", None)),
        "sender_id": getattr(message, "sender_id", None),
        "outgoing": bool(getattr(message, "out", False)),
        "text": text,
        "media": media,
    }


class TelegramToolAdapter:
    """Reflect only reviewed Telegram methods; mutation tools require confirmation."""

    allowed_methods = {
        "get_dialogs", "get_messages", "get_entity", "get_participants", "get_drafts",
        "get_chat_full", "resolve_phone",
        "download_media", "send_read_acknowledge", "send_message", "send_file",
        "edit_message", "delete_messages", "forward_messages",
    }
    allowed_requests = {
        "JoinChannelRequest", "LeaveChannelRequest", "SendReactionRequest",
        "SaveDraftRequest", "UpdateStatusRequest",
        "GetFullChannelRequest", "GetFullChatRequest",
        "ImportContactsRequest", "DeleteContactsRequest",
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
        messages = event_messages(event)
        message = messages[0] if messages else getattr(event, "message", None)
        sender_id = getattr(event, "sender_id", None) or getattr(message, "sender_id", None)
        sender = getattr(event, "sender", None)
        if sender is None:
            try:
                sender = await event.get_sender()
            except Exception:
                sender = None
        attachments: list[dict[str, Any]] = []
        if event_name == "new_message" and messages:
            attachments = await collect_telegram_attachments(event, messages)
        payload = {
            "event": event_name,
            "phone": phone,
            "sender_id": sender_id,
            "sender_is_bot": bool(getattr(sender, "bot", False)),
            "sender_username": getattr(sender, "username", None),
            "is_admin": self.is_admin_sender(sender_id),
            "chat_id": getattr(event, "chat_id", None) or getattr(message, "chat_id", None),
            "topic_id": telegram_topic_id(message),
            "message_id": getattr(event, "id", None) or getattr(message, "id", None),
            "date": telegram_datetime(
                getattr(message, "date", None) or getattr(event, "date", None)
            ),
            "text": (
                getattr(event, "raw_text", None)
                or getattr(event, "text", None)
                or getattr(message, "message", None)
                or ""
            ),
            "outgoing": bool(getattr(event, "out", False) or getattr(message, "out", False)),
            "service": bool(getattr(message, "action", None)),
            "callback_data": telegram_json(getattr(event, "data", None)),
            "attachments": attachments,
            "data": telegram_json(event),
        }
        logger.info(
            "telegram.%s phone=%s chat=%s sender=%s admin=%s out=%s text=%r attachments=%s",
            event_name,
            phone,
            payload.get("chat_id"),
            sender_id,
            payload.get("is_admin"),
            payload.get("outgoing"),
            str(payload.get("text") or "")[:120],
            [
                {
                    "kind": item.get("kind"),
                    "mime": item.get("mime_type"),
                    "bytes": item.get("size"),
                    "has_data": bool(item.get("data_b64")),
                }
                for item in attachments
            ],
        )
        results = await asyncio.gather(
            *(callback(payload) for callback in self.callbacks[event_name]),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.exception(
                    "Telegram %s callback failed phone=%s chat=%s",
                    event_name,
                    phone,
                    payload.get("chat_id"),
                    exc_info=result,
                )

    def _register_handlers(self, phone: str, client: Any) -> None:
        from telethon import events

        async def new_message(event: Any) -> None:
            if getattr(event, "grouped_id", None):
                return
            await self._dispatch("new_message", event, phone)

        async def album(event: Any) -> None:
            await self._dispatch("new_message", event, phone)

        async def message_edited(event: Any) -> None:
            await self._dispatch("message_edited", event, phone)

        async def callback_query(event: Any) -> None:
            await self._dispatch("callback_query", event, phone)

        client.add_event_handler(new_message, events.NewMessage())
        client.add_event_handler(album, events.Album())
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
                async with asyncio.timeout(20):
                    client = self._client(account)
                    await client.connect()
                    if await client.is_user_authorized():
                        self.clients[account.phone] = client
                        self._register_handlers(account.phone, client)
                        results[account.phone] = "restored"
                    else:
                        await client.disconnect()
                        results[account.phone] = "unauthorized"
            except TimeoutError:
                results[account.phone] = "error: connect timeout"
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
        return telegram_json(await self._get(phone).get_messages(self._coerce_entity(entity), ids=ids))

    @staticmethod
    def _coerce_entity(entity: str | int) -> str | int:
        if isinstance(entity, int):
            return entity
        text = str(entity).strip()
        if not text:
            return entity
        if text.startswith("@"):
            return text
        if text.lstrip("-").isdigit():
            return int(text)
        return entity

    async def send_message(
        self,
        phone: str,
        entity: str | int,
        text: str,
        reply_to: int | None = None,
        *,
        humanize: bool = True,
    ) -> Any:
        await self.limiters.setdefault(phone, AsyncLimiter(20, 60)).acquire()
        client = self._get(phone)
        peer = self._coerce_entity(entity)
        sent = []
        for index, chunk in enumerate(self._split(text)):
            if humanize:
                await self._humanize(client, peer, chunk)
            sent.append(await client.send_message(peer, chunk, reply_to=reply_to if index == 0 else None))
        normalized = [normalized_message(message) for message in sent]
        return normalized[0] if len(normalized) == 1 else normalized

    async def send_file(self, phone: str, entity: str | int, file: str, caption: str = "") -> Any:
        client = self._get(phone)
        await self._humanize(client, entity, caption)
        return telegram_json(await client.send_file(entity, file, caption=caption))

    async def edit_message(self, phone: str, entity: str | int, message: int, text: str) -> Any:
        return telegram_json(await self._get(phone).edit_message(entity, message, text))

    async def delete_messages(self, phone: str, entity: str | int, message_ids: list[int], revoke: bool = True) -> Any:
        """Delete selected messages from a Telegram dialog."""
        return telegram_json(await self._get(phone).delete_messages(entity, message_ids, revoke=revoke))

    async def delete_dialog(self, phone: str, entity: str | int, revoke: bool = False) -> dict[str, Any]:
        """Delete a Telegram dialog and optionally revoke its history for both sides."""
        await self._get(phone).delete_dialog(entity, revoke=revoke)
        return {"ok": True, "entity": str(entity), "revoke": revoke}

    async def forward_messages(self, phone: str, entity: str | int, messages: list[int], from_peer: str | int) -> Any:
        return telegram_json(await self._get(phone).forward_messages(entity, messages, from_peer=from_peer))

    async def get_participants(self, phone: str, entity: str | int, limit: int = 100) -> Any:
        return telegram_json(await self._get(phone).get_participants(entity, limit=limit))

    async def join_channel(self, phone: str, entity: str | int) -> Any:
        from telethon.tl.functions.channels import JoinChannelRequest

        return telegram_json(await self._get(phone)(JoinChannelRequest(entity)))

    async def leave_channel(self, phone: str, entity: str | int) -> Any:
        """Leave a Telegram channel or group."""
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

    async def get_chat_full(self, phone: str, entity: str | int) -> dict[str, Any]:
        """Return full Telegram group/channel info (about, counts, link, admins when available)."""
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.functions.messages import GetFullChatRequest
        from telethon.tl.types import Channel, ChannelParticipantsAdmins, Chat, User

        client = self._get(phone)
        target = await client.get_entity(entity)
        if isinstance(target, User):
            return {
                "kind": "user",
                "entity": telegram_json(target),
                "user": _user_summary(target),
            }

        admins: list[dict[str, Any]] = []
        about = None
        participants_count = None
        online_count = None
        invite_link = None
        linked_chat_id = None
        slowmode_seconds = None
        can_view_participants = None

        if isinstance(target, Channel):
            full = await client(GetFullChannelRequest(channel=target))
            full_chat = getattr(full, "full_chat", None)
            about = getattr(full_chat, "about", None)
            participants_count = getattr(full_chat, "participants_count", None)
            online_count = getattr(full_chat, "online_count", None)
            invite_link = getattr(full_chat, "exported_invite", None)
            if invite_link is not None:
                invite_link = getattr(invite_link, "link", None) or telegram_json(invite_link)
            linked_chat_id = getattr(full_chat, "linked_chat_id", None)
            slowmode_seconds = getattr(full_chat, "slowmode_seconds", None)
            can_view_participants = bool(getattr(full_chat, "can_view_participants", False))
            kind = "channel" if bool(getattr(target, "broadcast", False)) else "supergroup"
            try:
                admin_users = await client.get_participants(
                    target, filter=ChannelParticipantsAdmins()
                )
                for user in admin_users:
                    participant = getattr(user, "participant", None)
                    admins.append(
                        {
                            **_user_summary(user),
                            "role": _participant_role(participant) if participant else "admin",
                            "rank": getattr(participant, "rank", None) if participant else None,
                        }
                    )
            except Exception as exc:
                admins = [{"error": str(exc)}]
            return {
                "kind": kind,
                "id": getattr(target, "id", None),
                "title": getattr(target, "title", None),
                "username": getattr(target, "username", None),
                "about": about,
                "participants_count": participants_count,
                "online_count": online_count,
                "invite_link": invite_link,
                "linked_chat_id": linked_chat_id,
                "slowmode_seconds": slowmode_seconds,
                "can_view_participants": can_view_participants,
                "megagroup": bool(getattr(target, "megagroup", False)),
                "broadcast": bool(getattr(target, "broadcast", False)),
                "verified": bool(getattr(target, "verified", False)),
                "restricted": bool(getattr(target, "restricted", False)),
                "admins": admins,
                "entity": telegram_json(target),
            }

        if isinstance(target, Chat):
            full = await client(GetFullChatRequest(chat_id=target.id))
            full_chat = getattr(full, "full_chat", None)
            about = getattr(full_chat, "about", None)
            participants = getattr(full_chat, "participants", None)
            participant_rows = list(getattr(participants, "participants", []) or [])
            users_by_id = {
                getattr(user, "id", None): user
                for user in (getattr(full, "users", None) or [])
            }
            for row in participant_rows:
                role = _participant_role(row)
                if role not in {"creator", "admin"}:
                    continue
                user_id = getattr(row, "user_id", None)
                user = users_by_id.get(user_id)
                admins.append(
                    {
                        **(_user_summary(user) if user else {"id": user_id}),
                        "role": role,
                        "rank": getattr(row, "rank", None),
                    }
                )
            return {
                "kind": "group",
                "id": getattr(target, "id", None),
                "title": getattr(target, "title", None),
                "about": about,
                "participants_count": getattr(target, "participants_count", None)
                or len(participant_rows)
                or None,
                "admins": admins,
                "entity": telegram_json(target),
            }

        return {
            "kind": type(target).__name__.lower(),
            "entity": telegram_json(target),
        }

    async def get_user_phone(self, phone: str, entity: str | int) -> str | None:
        """Return the user's phone if Telethon exposes it (contact / mutual access)."""
        from .sip_dial import normalize_sip_dial_number

        try:
            user = await self._get(phone).get_entity(entity)
            raw = getattr(user, "phone", None)
            if not raw:
                return None
            return normalize_sip_dial_number(str(raw))
        except Exception:
            return None

    async def resolve_phone(
        self,
        phone: str,
        contact_phone: str,
        first_name: str = "Contact",
        keep_contact: bool = False,
    ) -> dict[str, Any]:
        """Find a Telegram user by phone number (privacy-limited). Optionally keep them in contacts."""
        from telethon.tl.functions.contacts import DeleteContactsRequest, ImportContactsRequest
        from telethon.tl.types import InputPhoneContact

        client = self._get(phone)
        normalized = normalize_contact_phone(contact_phone)
        contact = InputPhoneContact(
            client_id=0,
            phone=normalized,
            first_name=(first_name or "Contact").strip() or "Contact",
            last_name="",
        )
        result = await client(ImportContactsRequest([contact]))
        users = list(getattr(result, "users", None) or [])
        imported = list(getattr(result, "imported", None) or [])
        retry = list(getattr(result, "retry_contacts", None) or [])
        found = [_user_summary(user) for user in users]
        if users and not keep_contact:
            try:
                await client(DeleteContactsRequest(id=users))
            except Exception:
                pass
        return {
            "ok": bool(found),
            "query_phone": normalized,
            "keep_contact": bool(keep_contact),
            "users": found,
            "imported_count": len(imported),
            "retry_contacts": telegram_json(retry),
            "note": (
                None
                if found
                else (
                    "No Telegram user matched this phone. The number may be unused, "
                    "or privacy settings hide the account from phone search."
                )
            ),
        }

    async def get_drafts(self, phone: str) -> Any:
        return telegram_json(await self._get(phone).get_drafts())

    async def escalate(self, phone: str, text: str) -> Any:
        return await self.notify_admins(phone, text)

    async def notify_admins(
        self,
        phone: str,
        text: str,
        *,
        exclude_ids: Iterable[int | str] | None = None,
    ) -> list[Any]:
        if not self.admin_ids:
            raise RuntimeError("No administrator Telegram IDs configured")
        excluded = {int(value) for value in (exclude_ids or []) if str(value).lstrip("-").isdigit()}
        targets = sorted(admin_id for admin_id in self.admin_ids if admin_id not in excluded)
        if not targets:
            return []
        sent: list[Any] = []
        for admin_id in targets:
            sent.append(await self.send_message(phone, admin_id, text))
        return sent

    def set_admin_ids(self, values: Iterable[int | str]) -> None:
        parsed: set[int] = set()
        for value in values:
            try:
                parsed.add(int(str(value).strip()))
            except (TypeError, ValueError):
                continue
        self.admin_ids = parsed
        logger.info("Telegram admin_ids configured: %s", sorted(self.admin_ids))

    def is_admin_sender(self, sender_id: Any) -> bool:
        try:
            return int(sender_id) in self.admin_ids
        except (TypeError, ValueError):
            return False

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
            "edit_message", "delete_messages", "delete_dialog", "forward_messages", "get_participants",
            "get_chat_full", "resolve_phone",
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
