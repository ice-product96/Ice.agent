"""OpenAI Realtime bridge: server WebSocket PCM 24 kHz (+ optional client_secrets)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import re
import socket
import ssl
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import websockets
from websockets.asyncio.client import ClientConnection

from .sip_audio import OPENAI_FRAME_SAMPLES, OPENAI_RATE, PlaybackBuffer

logger = logging.getLogger(__name__)

OnTranscript = Callable[[str, str], Awaitable[None]]  # role, text
OnEvent = Callable[[dict[str, Any]], Awaitable[None]]
OnHangup = Callable[[str], Awaitable[None]]  # reason
OnTool = Callable[[str, dict[str, Any]], Awaitable[Any]]

DEFAULT_REALTIME_MODEL = "gpt-realtime-2"
END_CALL_TOOL_NAMES = {"end_call", "sip_hangup", "hangup", "endcall"}
_HANGUP_MARKER_RE = re.compile(
    r"\[{1,2}\s*SIP[_ ]?HANGUP\s*\]{1,2}"
    r"|\bSIP[_ ]HANGUP\b"
    r"|\bsip\.hangup\b"
    r"|\bend[_\s-]?call\b",
    re.IGNORECASE,
)
_FAREWELL_RE = re.compile(
    r"до\s+свидан|всего\s+добр|до\s+связи|хорошего\s+(дня|вечера)|"
    r"\bgoodbye\b|\bbye-?bye\b|\bhang\s*up\b",
    re.IGNORECASE,
)
_USER_BYE_RE = re.compile(
    r"до\s+свидан|(?<![А-Яа-яЁё])пока(?![А-Яа-яЁё])|клади\s+трубк|"
    r"заверш[аи].{0,16}звон|\bgoodbye\b|\bhang\s*up\b|(?<![A-Za-z])bye(?![A-Za-z])",
    re.IGNORECASE,
)
HANGUP_INSTRUCTION = (
    "Завершение звонка: сначала коротко попрощайся обычными словами "
    "(«До свидания», «Всего доброго»). "
    "Потом молча вызови инструмент end_call — без слов вслух. "
    "Никогда не произноси sip_hangup, end_call, SIP_HANGUP и похожие служебные слова. "
    "Если инструмент недоступен, в самый конец реплики добавь маркер [[SIP_HANGUP]] "
    "и не читай его. Пока разговор может продолжаться — трубку не клади."
)
END_CALL_TOOL = {
    "type": "function",
    "name": "end_call",
    "description": "Hang up the phone after you have said goodbye. Use only when the call should end.",
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "Short reason for hanging up"},
        },
    },
}
MEMORY_SEARCH_TOOL = {
    "type": "function",
    "name": "memory_search",
    "description": (
        "Search long-term memory about this caller. "
        "Use when they ask about past facts, names, agreements, or previous conversations."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look up"},
        },
        "required": ["query"],
    },
}
MEMORY_ADD_TOOL = {
    "type": "function",
    "name": "memory_add",
    "description": "Save an important fact about this caller for future calls. Do not announce this.",
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Fact to remember"},
        },
        "required": ["text"],
    },
}
CALL_HISTORY_TOOL = {
    "type": "function",
    "name": "call_history",
    "description": (
        "Get transcripts of previous phone calls with this number. "
        "Use when the caller asks what you discussed last time."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "How many past calls to return (1-5)"},
        },
    },
}
MEMORY_VOICE_INSTRUCTION = (
    "Память и история звонков: блок ниже — служебный контекст, не зачитывай его целиком. "
    "Если абонент спрашивает о прошлых разговорах, договорённостях или фактах, "
    "молча вызови memory_search или call_history и ответь своими словами. "
    "Важные новые факты сохрани через memory_add. "
    "Никогда не произноси имена инструментов и не читай JSON вслух."
)
_TOOL_EVENT_TYPES = {
    "response.done",
    "response.output_item.done",
    "conversation.item.created",
    "response.function_call_arguments.done",
}

def _tool_name(blob: Any) -> str:
    if not isinstance(blob, dict):
        return ""
    nested = blob.get("function") if isinstance(blob.get("function"), dict) else {}
    raw = blob.get("name") or nested.get("name") or ""
    return str(raw).strip().lower().replace("functions.", "").replace(" ", "_")


def _tool_arguments(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("arguments")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _as_function_call(blob: dict[str, Any]) -> dict[str, Any] | None:
    nested = blob.get("item") if isinstance(blob.get("item"), dict) else {}
    cur = nested or blob
    item_type = str(cur.get("type") or blob.get("type") or "")
    name = _tool_name(cur) or _tool_name(blob)
    if not name:
        return None
    is_call = item_type in {"function_call", "response.function_call_arguments.done"}
    if not is_call:
        return None
    return {
        "type": "function_call",
        "name": name,
        "call_id": str(cur.get("call_id") or blob.get("call_id") or cur.get("id") or blob.get("id") or ""),
        "arguments": cur.get("arguments") if cur.get("arguments") is not None else blob.get("arguments") or "{}",
    }


def _event_function_call(event: dict[str, Any]) -> dict[str, Any] | None:
    """Find a named function call in a Realtime event (hangup or memory/history)."""
    found = _as_function_call(event)
    if found is not None:
        return found
    stack: list[Any] = [event]
    seen = 0
    while stack and seen < 40:
        cur = stack.pop()
        seen += 1
        if isinstance(cur, dict):
            if str(cur.get("type") or "") in {"function", "session"}:
                continue
            found = _as_function_call(cur)
            if found is not None:
                return found
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def _normalize_realtime_model(model: str | None) -> str:
    """OpenAI realtime model ids are lowercase; UI/presets sometimes send GPT-Realtime-2."""
    name = (model or "").strip() or DEFAULT_REALTIME_MODEL
    return name.lower()


def _realtime_http_base(base_url: str | None) -> str:
    raw = (base_url or "https://api.openai.com/v1").rstrip("/")
    if raw.endswith("/v1"):
        return raw
    return f"{raw}/v1"


def _realtime_ws_url(base_url: str | None, *, model: str | None = None) -> str:
    http_base = _realtime_http_base(base_url)
    parsed = urlparse(http_base)
    scheme = "wss" if parsed.scheme != "http" else "ws"
    url = f"{scheme}://{parsed.netloc}{parsed.path}/realtime"
    if model:
        url = f"{url}?{urlencode({'model': model})}"
    return url


def _redact_proxy(proxy_url: str) -> str:
    pu = urlparse(proxy_url.strip())
    host = pu.hostname or "?"
    port = pu.port or ("443" if pu.scheme == "https" else "80")
    auth = "***@" if pu.username else ""
    return f"{pu.scheme or 'http'}://{auth}{host}:{port}"


def _rewrite_loopback_proxy(proxy_url: str) -> str:
    """127.0.0.1 inside Docker is the container, not the host proxy."""
    from pathlib import Path

    if not Path("/.dockerenv").exists():
        return proxy_url
    pu = urlparse(proxy_url.strip())
    host = (pu.hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return proxy_url
    rewritten = (
        proxy_url.replace("://127.0.0.1", "://host.docker.internal", 1)
        .replace("://localhost", "://host.docker.internal", 1)
        .replace("://[::1]", "://host.docker.internal", 1)
    )
    logger.warning(
        "Realtime proxy %s is loopback inside Docker — using %s (add extra_hosts host-gateway)",
        _redact_proxy(proxy_url),
        _redact_proxy(rewritten),
    )
    return rewritten


def _tcp_via_http_connect(proxy_url: str, target_host: str, target_port: int, timeout: float = 45.0) -> socket.socket:
    """HTTP CONNECT tunnel — same approach as MtzVersion openai_wss_proxy."""
    proxy_url = _rewrite_loopback_proxy(proxy_url)
    pu = urlparse(proxy_url.strip())
    if not pu.hostname:
        raise RuntimeError("HTTP proxy URL has no host")
    phost = pu.hostname
    scheme = (pu.scheme or "http").lower()
    pport = int(pu.port or (443 if scheme == "https" else 80))
    target = f"{target_host}:{int(target_port)}"
    logger.info("Realtime WSS CONNECT %s via %s", target, _redact_proxy(proxy_url))
    try:
        sock = socket.create_connection((phost, pport), timeout=timeout)
    except TimeoutError as exc:
        raise TimeoutError(
            f"TCP to HTTP proxy {phost}:{pport} timed out — from Docker use host LAN IP or host.docker.internal, not 127.0.0.1"
        ) from exc
    try:
        if scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=phost)
        lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
        if pu.username:
            token = base64.b64encode(f"{pu.username}:{pu.password or ''}".encode()).decode("ascii")
            lines.append(f"Proxy-Authorization: Basic {token}")
        lines.append("Proxy-Connection: keep-alive")
        lines.append("")
        sock.sendall("\r\n".join(lines).encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            try:
                chunk = sock.recv(16384)
            except TimeoutError as exc:
                raise TimeoutError(
                    f"HTTP CONNECT {target} via {phost}:{pport} got no reply — "
                    "this proxy does not tunnel WSS. Need CONNECT to port 443, or socks5h://user:pass@host:1080"
                ) from exc
            if not chunk:
                raise OSError("proxy closed before CONNECT response")
            buf += chunk
            if len(buf) > 262144:
                raise OSError("CONNECT response too large")
        status = buf.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
        if " 200" not in status:
            raise OSError(
                f"CONNECT rejected: {status!r} — proxy must allow CONNECT to {target} (not just HTTP GET/POST)"
            )
        sock.settimeout(None)
        logger.info("Realtime WSS HTTP CONNECT ok %s via %s:%s", target, phost, pport)
        return sock
    except Exception:
        try:
            sock.close()
        except OSError:
            pass
        raise


async def _open_proxied_socket(ws_url: str, http_proxy: str):
    """TCP to Realtime host via HTTP CONNECT or SOCKS5/SOCKS5h."""
    parsed = urlparse(ws_url)
    host = parsed.hostname
    if not host:
        raise RuntimeError(f"Invalid Realtime WebSocket URL: {ws_url}")
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    proxy_url = _rewrite_loopback_proxy(http_proxy)
    scheme = (urlparse(proxy_url.strip()).scheme or "http").lower()
    if scheme in {"http", "https", ""}:
        return await asyncio.to_thread(_tcp_via_http_connect, proxy_url, host, port, 45.0)
    if scheme not in {"socks5", "socks5h", "socks4", "socks4a"}:
        raise RuntimeError(
            f"Unsupported proxy scheme {scheme!r}. Use http:// (CONNECT) or socks5h://host:1080"
        )
    from python_socks.async_.asyncio import Proxy

    # python_socks only accepts socks5/socks4/http — socks5h means SOCKS5 + remote DNS.
    socks_url = proxy_url.strip()
    rdns = scheme in {"socks5h", "socks4a"}
    if scheme == "socks5h":
        socks_url = "socks5://" + socks_url.split("://", 1)[-1]
    elif scheme == "socks4a":
        socks_url = "socks4://" + socks_url.split("://", 1)[-1]
    try:
        proxy = Proxy.from_url(socks_url, rdns=rdns)
    except TypeError:
        proxy = Proxy.from_url(socks_url)
    logger.info(
        "Realtime WSS SOCKS %s via %s rdns=%s",
        f"{host}:{port}",
        _redact_proxy(proxy_url),
        rdns,
    )
    return await asyncio.wait_for(proxy.connect(dest_host=host, dest_port=port), timeout=45.0)


def build_client_secret_session(*, model: str) -> dict[str, Any]:
    """Minimal GA session for POST /realtime/client_secrets (full config via session.update after connect)."""
    return {
        "type": "realtime",
        "model": model,
    }


def _http_error_detail(response: httpx.Response) -> str:
    text = (response.text or "").strip()
    try:
        payload = response.json()
        err = payload.get("error")
        if isinstance(err, dict):
            parts = [str(err.get("message") or err.get("code") or "invalid_request_error")]
            param = err.get("param")
            if param:
                parts.append(f"param={param}")
            return f"HTTP {response.status_code}: {'; '.join(parts)}"
    except Exception:
        pass
    return f"HTTP {response.status_code}: {text[:400]}"


def build_session_config(
    *,
    instructions: str,
    voice: str = "marin",
    model: str = DEFAULT_REALTIME_MODEL,
    reasoning_effort: str = "low",
    extra_tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """GA Realtime session — matches working MtzVersion sip_realtime_bridge."""
    effort = reasoning_effort if reasoning_effort in {"minimal", "low", "medium", "high", "xhigh"} else "low"
    tools = [END_CALL_TOOL, *(extra_tools or [])]
    return {
        "type": "realtime",
        "model": model,
        "instructions": instructions,
        "output_modalities": ["audio"],
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "turn_detection": {"type": "semantic_vad"},
                "noise_reduction": {"type": "far_field"},
            },
            "output": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "voice": voice,
            },
        },
        "reasoning": {"effort": effort},
        "tools": tools,
        "tool_choice": "auto",
    }


def beep_pcm24(duration_ms: int = 400, freq_hz: float = 880.0, amplitude: float = 0.22) -> bytes:
    """Short PCM16LE 24 kHz tone to verify SIP RTP path independently of OpenAI."""
    samples = max(1, OPENAI_RATE * duration_ms // 1000)
    out = bytearray(samples * 2)
    view = memoryview(out).cast("h")
    for i in range(samples):
        # soft attack/release so it is not clipped harshly
        env = 1.0
        attack = min(i, 240) / 240.0
        release = min(samples - 1 - i, 240) / 240.0
        env = min(attack, release, 1.0)
        view[i] = int(amplitude * env * 32767.0 * math.sin(2.0 * math.pi * freq_hz * i / OPENAI_RATE))
    return bytes(out)


async def create_client_secret(
    *,
    api_key: str,
    base_url: str | None,
    model: str,
    http_proxy: str | None = None,
) -> str:
    url = f"{_realtime_http_base(base_url)}/realtime/client_secrets"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    session = build_client_secret_session(model=model)
    client_kwargs: dict[str, Any] = {"timeout": 30.0}
    if http_proxy:
        client_kwargs["proxy"] = http_proxy.strip()
    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await client.post(url, headers=headers, json={"session": session})
        if response.status_code >= 400:
            raise RuntimeError(_http_error_detail(response))
        payload = response.json()
    if isinstance(payload.get("value"), str):
        return payload["value"]
    secret = payload.get("client_secret")
    if isinstance(secret, dict) and isinstance(secret.get("value"), str):
        return secret["value"]
    if isinstance(secret, str):
        return secret
    raise RuntimeError(f"Unexpected client_secrets response: {payload!r}")


class RealtimeSession:
    """One OpenAI Realtime WebSocket bound to a single SIP call."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None,
        instructions: str,
        voice: str = "marin",
        model: str = DEFAULT_REALTIME_MODEL,
        http_proxy: str | None = None,
        on_transcript: OnTranscript | None = None,
        on_event: OnEvent | None = None,
        on_hangup: OnHangup | None = None,
        on_tool: OnTool | None = None,
        extra_tools: list[dict[str, Any]] | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.instructions = instructions
        self.voice = voice
        self.model = _normalize_realtime_model(model)
        self.http_proxy = _rewrite_loopback_proxy((http_proxy or "").strip()) if (http_proxy or "").strip() else None
        self.on_transcript = on_transcript
        self.on_event = on_event
        self.on_hangup = on_hangup
        self.on_tool = on_tool
        self.extra_tools = list(extra_tools or [])
        self.playback = PlaybackBuffer()
        self.last_error: str | None = None
        self._ws: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._hangup_task: asyncio.Task[None] | None = None
        self._hangup_reason: str | None = None
        self._closed = asyncio.Event()
        self._session_ready = asyncio.Event()
        self._session_updated = asyncio.Event()
        self._greeting_until = 0.0
        self._audio_chunks = 0
        self._user_parts: list[str] = []
        self._assistant_parts: list[str] = []
        self._response_count = 0
        self._user_wants_hangup = False
        self._handled_tool_ids: set[str] = set()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    async def _ws_connect(self, ws_url: str, token: str) -> ClientConnection:
        connect_kwargs: dict[str, Any] = {
            "additional_headers": {"Authorization": f"Bearer {token}"},
            "max_size": 8 * 1024 * 1024,
            "ping_interval": 20,
            "open_timeout": 20,
        }
        if self.http_proxy:
            parsed_ws = urlparse(ws_url)
            connect_kwargs["sock"] = await _open_proxied_socket(ws_url, self.http_proxy)
            if parsed_ws.scheme == "wss":
                connect_kwargs["ssl"] = ssl.create_default_context()
                connect_kwargs["server_hostname"] = parsed_ws.hostname
        return await websockets.connect(ws_url, **connect_kwargs)

    async def _close_socket(self) -> None:
        if self._reader:
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                pass
            self._reader = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def _handshake(self, ws_url: str, token: str, session: dict[str, Any]) -> None:
        self._session_ready = asyncio.Event()
        self._session_updated = asyncio.Event()
        self.last_error = None
        self._ws = await self._ws_connect(ws_url, token)
        self._reader = asyncio.create_task(self._read_loop(), name="realtime-reader")
        try:
            await asyncio.wait_for(self._session_ready.wait(), timeout=12)
        except TimeoutError as exc:
            extra = f"; err={self.last_error}" if self.last_error else ""
            raise RuntimeError(
                f"session.created timeout proxy={bool(self.http_proxy)}{extra}"
            ) from exc
        if self.last_error:
            raise RuntimeError(f"Realtime handshake error: {self.last_error}")
        assert self._ws is not None
        self._session_updated = asyncio.Event()
        self.last_error = None
        await self._ws.send(json.dumps({"type": "session.update", "session": session}))
        try:
            await asyncio.wait_for(self._session_updated.wait(), timeout=8)
        except TimeoutError:
            logger.warning("Realtime session.updated timed out — continuing")
        if self.last_error and "tool" in self.last_error.lower():
            logger.warning("Realtime tools rejected (%s) — retrying hangup-only", self.last_error)
            self.last_error = None
            hangup_only = dict(session)
            hangup_only["tools"] = [END_CALL_TOOL]
            hangup_only["tool_choice"] = "auto"
            self._session_updated = asyncio.Event()
            await self._ws.send(json.dumps({"type": "session.update", "session": hangup_only}))
            try:
                await asyncio.wait_for(self._session_updated.wait(), timeout=8)
            except TimeoutError:
                logger.warning("Realtime session.updated (hangup-only) timed out — continuing")
        if self.last_error and "tool" in self.last_error.lower():
            logger.warning("Realtime hangup tool rejected (%s) — continuing without tools", self.last_error)
            self.last_error = None
            stripped = {key: value for key, value in session.items() if key not in {"tools", "tool_choice"}}
            self._session_updated = asyncio.Event()
            await self._ws.send(json.dumps({"type": "session.update", "session": stripped}))
            try:
                await asyncio.wait_for(self._session_updated.wait(), timeout=8)
            except TimeoutError:
                logger.warning("Realtime session.updated (no tools) timed out — continuing")
        if self.last_error:
            raise RuntimeError(f"Realtime session.update error: {self.last_error}")

    async def connect(self) -> None:
        """API-key WebSocket first (Mtz path); client_secrets if that fails."""
        session = build_session_config(
            instructions=self.instructions,
            voice=self.voice,
            model=self.model,
            extra_tools=self.extra_tools,
        )
        direct_url = _realtime_ws_url(self.base_url, model=self.model)
        errors: list[str] = []
        if not self.http_proxy:
            logger.warning(
                "Realtime proxy not set (LLM profile / ICE_OPENAI_HTTP_PROXY / HTTP_PROXY) — "
                "direct WSS to OpenAI often times out from Docker/RU"
            )
        try:
            logger.info(
                "Realtime connecting model=%s url=%s proxy=%s",
                self.model,
                direct_url,
                self.http_proxy and _redact_proxy(self.http_proxy) or "-",
            )
            await self._handshake(direct_url, self.api_key, session)
            logger.info("Realtime connected (API key) model=%s", self.model)
            return
        except Exception as direct_exc:
            errors.append(f"direct: {direct_exc}")
            logger.warning("Realtime direct WS failed: %s — trying client_secrets", direct_exc)
            await self._close_socket()
            # Same TCP CONNECT is required for the ephemeral-key WebSocket — retrying it only burns the call.
            if "CONNECT" in str(direct_exc) or "TCP to HTTP proxy" in str(direct_exc):
                raise RuntimeError(" | ".join(errors)) from direct_exc

        try:
            secret = await create_client_secret(
                api_key=self.api_key,
                base_url=self.base_url,
                model=self.model,
                http_proxy=self.http_proxy,
            )
            # Ephemeral token: connect without ?model= (config comes from client_secrets + session.update).
            secret_url = _realtime_ws_url(self.base_url)
            await self._handshake(secret_url, secret, session)
            logger.info("Realtime connected (client_secrets) model=%s", self.model)
        except Exception as secret_exc:
            errors.append(f"client_secrets: {secret_exc}")
            await self._close_socket()
            raise RuntimeError(" | ".join(errors)) from secret_exc

    async def request_response(self, instructions: str | None = None) -> None:
        """Force the model to speak (inbound greeting) — same as MtzVersion conn.response.create()."""
        if self._ws is None or self.closed:
            return
        if not self._session_ready.is_set():
            try:
                await asyncio.wait_for(self._session_ready.wait(), timeout=5)
            except TimeoutError:
                self.last_error = "not ready for greeting"
                logger.warning("Realtime not ready for greeting")
                return

        self._greeting_until = asyncio.get_running_loop().time() + 8.0
        # Optional one-shot instructions for this response (Mtz relies on session instructions).
        if instructions and instructions.strip():
            await self._ws.send(
                json.dumps(
                    {
                        "type": "response.create",
                        "response": {
                            "output_modalities": ["audio"],
                            "instructions": instructions.strip(),
                        },
                    }
                )
            )
        else:
            await self._ws.send(json.dumps({"type": "response.create"}))
        logger.info("Realtime response.create (greeting) sent")

    def inject_beep(self, duration_ms: int = 350) -> None:
        """Put a local tone into the SIP playback buffer (RTP path check)."""
        self.playback.append(beep_pcm24(duration_ms=duration_ms))

    async def close(self) -> None:
        self._closed.set()
        if self._hangup_task:
            self._hangup_task.cancel()
            try:
                await self._hangup_task
            except asyncio.CancelledError:
                pass
            self._hangup_task = None
        if self._reader:
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                pass
            self._reader = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def send_pcm24(self, pcm24: bytes) -> None:
        if self._ws is None or self.closed or not pcm24:
            return
        chunk = 24000 * 2 // 10  # 100 ms
        for offset in range(0, len(pcm24), chunk):
            piece = pcm24[offset : offset + chunk]
            event = {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(piece).decode("ascii"),
            }
            await self._ws.send(json.dumps(event))

    def read_playback_frame(self, samples: int = OPENAI_FRAME_SAMPLES) -> bytes:
        # Do not pad here — media loop pads and uses empty=end-of-talkspurt for RTP marker.
        return self.playback.read(samples * 2)

    def transcript_text(self) -> str:
        lines: list[str] = []
        for role, parts in (("user", self._user_parts), ("assistant", self._assistant_parts)):
            text = " ".join(p.strip() for p in parts if p.strip())
            if text:
                lines.append(f"{role}: {text}")
        return "\n".join(lines)

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if self.closed:
                    break
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._handle_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = str(exc)
            logger.warning("Realtime WebSocket ended: %s", exc)
        finally:
            self._closed.set()
            if not self._session_ready.is_set():
                if not self.last_error:
                    self.last_error = "websocket closed before session.created"
                self._session_ready.set()
            if not self._session_updated.is_set():
                self._session_updated.set()

    async def _handle_event(self, event: dict[str, Any]) -> None:
        etype = str(event.get("type") or "")
        if etype == "session.created":
            self._session_ready.set()
            logger.info("Realtime session.created")
        elif etype == "session.updated":
            self._session_ready.set()
            self._session_updated.set()
            logger.info("Realtime session.updated")
        elif etype == "error":
            err = event.get("error") or event
            self.last_error = json.dumps(err, ensure_ascii=False)[:500]
            logger.error("Realtime error: %s", self.last_error)
            self._session_ready.set()
            self._session_updated.set()

        if etype not in {
            "response.output_audio.delta",
            "response.audio.delta",
            "response.output_audio_transcript.delta",
            "response.audio_transcript.delta",
        }:
            logger.info("Realtime event %s", etype)

        tool_item = _event_function_call(event) if etype in _TOOL_EVENT_TYPES else None
        if tool_item is not None:
            await self._handle_tool_call(tool_item)

        if self.on_event:
            try:
                await self.on_event(event)
            except Exception:
                logger.exception("on_event failed")

        if etype in {"response.output_audio.delta", "response.audio.delta"}:
            b64 = event.get("delta") or event.get("audio")
            if isinstance(b64, str) and b64:
                self.playback.append(base64.b64decode(b64))
                self._audio_chunks += 1
                if self._audio_chunks == 1:
                    logger.info("Realtime first audio chunk received")
            return

        if etype == "input_audio_buffer.speech_started":
            if asyncio.get_running_loop().time() < self._greeting_until:
                return
            self.playback.clear()
            return

        if etype in {
            "conversation.item.input_audio_transcription.completed",
            "conversation.item.input_audio_transcription.delta",
        }:
            text = event.get("transcript") or event.get("delta") or ""
            if text and etype.endswith("completed"):
                self._user_parts.append(str(text))
                if _USER_BYE_RE.search(str(text)):
                    self._user_wants_hangup = True
                    logger.info("Realtime user asked to hang up: %s", str(text)[:80])
                if self.on_transcript:
                    await self.on_transcript("user", str(text))
            return

        if etype in {
            "response.output_audio_transcript.done",
            "response.audio_transcript.done",
            "response.output_audio_transcript.delta",
            "response.audio_transcript.delta",
        }:
            text = event.get("transcript") or event.get("delta") or ""
            if text and etype.endswith("delta"):
                if self._assistant_parts and not self._assistant_parts[-1].endswith(" "):
                    self._assistant_parts[-1] = self._assistant_parts[-1] + str(text)
                else:
                    self._assistant_parts.append(str(text))
            elif text and etype.endswith("done"):
                self._assistant_parts.append(str(text))
                if self.on_transcript:
                    await self.on_transcript("assistant", str(text))
            combined = " ".join(self._assistant_parts[-3:])
            if text and _HANGUP_MARKER_RE.search(str(text) + " " + combined):
                # Cut unplayed tail so "sip hangup" is not spoken (Mtz bug).
                self.playback.clear()
                self.request_hangup("transcript_marker")
            return

        if etype == "response.done":
            status = (event.get("response") or {}).get("status")
            details = (event.get("response") or {}).get("status_details")
            logger.info(
                "Realtime response.done status=%s audio_chunks=%s details=%s",
                status,
                self._audio_chunks,
                details,
            )
            if status and status != "completed":
                self.last_error = f"response.{status}: {details}"
            output = (event.get("response") or {}).get("output") or []
            had_tool = False
            for item in output:
                if not isinstance(item, dict):
                    continue
                call_item = _as_function_call(item)
                if call_item is None:
                    continue
                had_tool = True
                await self._handle_tool_call(call_item)
            if had_tool:
                return
            self._response_count += 1
            last = " ".join(self._assistant_parts[-3:])
            if self._hangup_reason is None and self._response_count >= 2:
                if _FAREWELL_RE.search(last):
                    logger.info("Realtime farewell detected — hanging up")
                    self.request_hangup("farewell")
                elif self._user_wants_hangup:
                    logger.info("Realtime hangup after user goodbye")
                    self.request_hangup("user_goodbye")

    def request_hangup(self, reason: str = "agent_hangup") -> None:
        if self._hangup_reason is not None or self.closed:
            return
        self._hangup_reason = reason
        logger.info("Realtime hangup requested (%s) playback=%s", reason, self.playback.pending())
        self._hangup_task = asyncio.create_task(self._drain_then_hangup(reason), name="rt-hangup")

    async def _drain_then_hangup(self, reason: str) -> None:
        idle = 0
        needed = 2 if self.playback.pending() == 0 else 6
        for _ in range(80):
            if self.closed:
                return
            if self.playback.pending() > 0:
                idle = 0
            else:
                idle += 1
                if idle >= needed:
                    break
            await asyncio.sleep(0.1)
        if self.closed:
            return
        if self.on_hangup is None:
            logger.error("Realtime hangup: on_hangup callback is missing")
            return
        try:
            await self.on_hangup(reason)
        except Exception:
            logger.exception("Realtime on_hangup failed")

    async def _send_tool_output(self, call_id: str, payload: Any) -> None:
        if self._ws is None or self.closed or not call_id:
            return
        try:
            await self._ws.send(
                json.dumps(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": json.dumps(payload, ensure_ascii=False, default=str),
                        },
                    }
                )
            )
        except Exception:
            logger.warning("Failed to send Realtime tool output for %s", call_id)

    async def _continue_after_tool(self) -> None:
        if self._ws is None or self.closed or self._hangup_reason is not None:
            return
        try:
            await self._ws.send(
                json.dumps(
                    {
                        "type": "response.create",
                        "response": {"output_modalities": ["audio"]},
                    }
                )
            )
        except Exception:
            logger.warning("Failed to continue Realtime after tool")

    async def _handle_tool_call(self, item: dict[str, Any]) -> None:
        name = _tool_name(item)
        if not name:
            nested = item.get("item") if isinstance(item.get("item"), dict) else {}
            name = _tool_name(nested)
            item = nested or item
        call_id = str(item.get("call_id") or item.get("id") or "")
        if call_id and call_id in self._handled_tool_ids:
            return
        if call_id:
            self._handled_tool_ids.add(call_id)
        args = _tool_arguments(item)
        if name in END_CALL_TOOL_NAMES:
            logger.info("Realtime end_call tool name=%s", name)
            reason = str(args.get("reason") or "agent_hangup")[:80]
            await self._send_tool_output(call_id, {"ok": True, "hangup": True})
            self.request_hangup(reason)
            return
        if not name:
            return
        logger.info("Realtime tool %s args=%s", name, json.dumps(args, ensure_ascii=False)[:200])
        result: Any = {"error": f"unknown tool {name}"}
        if self.on_tool is not None:
            try:
                result = await self.on_tool(name, args)
            except Exception as exc:
                logger.exception("Realtime tool %s failed", name)
                result = {"error": str(exc)[:300]}
        await self._send_tool_output(call_id, result)
        await self._continue_after_tool()
