"""OpenAI Realtime bridge: client_secrets + WebSocket PCM 24 kHz audio."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import ssl
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

import httpx
import websockets
from websockets.asyncio.client import ClientConnection

from .sip_audio import OPENAI_FRAME_SAMPLES, PlaybackBuffer, silence_pcm16

logger = logging.getLogger(__name__)

OnTranscript = Callable[[str, str], Awaitable[None]]  # role, text
OnEvent = Callable[[dict[str, Any]], Awaitable[None]]


def _realtime_http_base(base_url: str | None) -> str:
    raw = (base_url or "https://api.openai.com/v1").rstrip("/")
    if raw.endswith("/v1"):
        return raw
    return f"{raw}/v1"


def _realtime_ws_url(base_url: str | None) -> str:
    http_base = _realtime_http_base(base_url)
    parsed = urlparse(http_base)
    scheme = "wss" if parsed.scheme != "http" else "ws"
    return f"{scheme}://{parsed.netloc}{parsed.path}/realtime"


async def _open_proxied_socket(ws_url: str, http_proxy: str):
    """Open a TCP socket to the Realtime host via HTTP(S)/SOCKS proxy (python-socks).

    SSL for wss:// is applied by websockets.connect(ssl=...).
    """
    from python_socks.async_.asyncio import Proxy

    parsed = urlparse(ws_url)
    host = parsed.hostname
    if not host:
        raise RuntimeError(f"Invalid Realtime WebSocket URL: {ws_url}")
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    proxy = Proxy.from_url(http_proxy.strip())
    return await proxy.connect(dest_host=host, dest_port=port)


def build_session_config(
    *,
    instructions: str,
    voice: str = "marin",
    model: str = "gpt-realtime",
) -> dict[str, Any]:
    return {
        "type": "realtime",
        "model": model,
        "instructions": instructions,
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "transcription": {"model": "gpt-realtime-whisper"},
                "noise_reduction": {"type": "far_field"},
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 500,
                    "idle_timeout_ms": None,
                },
            },
            "output": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "voice": voice,
            },
        },
        "output_modalities": ["audio"],
        "tools": [],
        "max_output_tokens": "inf",
        "reasoning": {"effort": "low"},
    }


async def create_client_secret(
    *,
    api_key: str,
    base_url: str | None,
    session: dict[str, Any],
    http_proxy: str | None = None,
) -> str:
    url = f"{_realtime_http_base(base_url)}/realtime/client_secrets"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    client_kwargs: dict[str, Any] = {"timeout": 30.0}
    if http_proxy:
        client_kwargs["proxy"] = http_proxy.strip()
    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await client.post(url, headers=headers, json={"session": session})
        response.raise_for_status()
        payload = response.json()
    # GA responses expose value under client_secret / value / secret
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
        model: str = "gpt-realtime",
        http_proxy: str | None = None,
        on_transcript: OnTranscript | None = None,
        on_event: OnEvent | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.instructions = instructions
        self.voice = voice
        self.model = model
        self.http_proxy = (http_proxy or "").strip() or None
        self.on_transcript = on_transcript
        self.on_event = on_event
        self.playback = PlaybackBuffer()
        self._ws: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()
        self._session_ready = asyncio.Event()
        self._user_parts: list[str] = []
        self._assistant_parts: list[str] = []

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    async def connect(self) -> None:
        session = build_session_config(
            instructions=self.instructions,
            voice=self.voice,
            model=self.model,
        )
        secret = await create_client_secret(
            api_key=self.api_key,
            base_url=self.base_url,
            session=session,
            http_proxy=self.http_proxy,
        )
        ws_url = _realtime_ws_url(self.base_url)
        extra_headers = {"Authorization": f"Bearer {secret}"}
        connect_kwargs: dict[str, Any] = {
            "additional_headers": extra_headers,
            "max_size": 8 * 1024 * 1024,
            "ping_interval": 20,
        }
        if self.http_proxy:
            parsed_ws = urlparse(ws_url)
            connect_kwargs["sock"] = await _open_proxied_socket(ws_url, self.http_proxy)
            if parsed_ws.scheme == "wss":
                connect_kwargs["ssl"] = ssl.create_default_context()
                connect_kwargs["server_hostname"] = parsed_ws.hostname
        self._ws = await websockets.connect(ws_url, **connect_kwargs)
        self._reader = asyncio.create_task(self._read_loop(), name="realtime-reader")
        try:
            await asyncio.wait_for(self._session_ready.wait(), timeout=8)
        except TimeoutError:
            logger.warning("Realtime session.created timed out, greeting may fail")

    async def request_response(self, instructions: str | None = None) -> None:
        """Ask the model to speak immediately (incoming-call greeting)."""
        if self._ws is None or self.closed:
            return
        if not self._session_ready.is_set():
            try:
                await asyncio.wait_for(self._session_ready.wait(), timeout=5)
            except TimeoutError:
                logger.warning("Realtime not ready for response.create")
                return
        event: dict[str, Any] = {"type": "response.create"}
        if instructions:
            event["response"] = {"instructions": instructions}
        await self._ws.send(json.dumps(event))

    async def close(self) -> None:
        self._closed.set()
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
        # Stream in ~100 ms chunks max to stay under event size limits
        chunk = 24000 * 2 // 10  # 100 ms
        for offset in range(0, len(pcm24), chunk):
            piece = pcm24[offset : offset + chunk]
            event = {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(piece).decode("ascii"),
            }
            await self._ws.send(json.dumps(event))

    def read_playback_frame(self, samples: int = OPENAI_FRAME_SAMPLES) -> bytes:
        data = self.playback.read(samples * 2)
        if len(data) < samples * 2:
            data += silence_pcm16(samples - len(data) // 2)
        return data

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
            logger.warning("Realtime WebSocket ended: %s", exc)
        finally:
            self._closed.set()

    async def _handle_event(self, event: dict[str, Any]) -> None:
        etype = str(event.get("type") or "")
        if etype in {"session.created", "session.updated"}:
            self._session_ready.set()
        if self.on_event:
            try:
                await self.on_event(event)
            except Exception:
                logger.exception("on_event failed")

        if etype in {"response.output_audio.delta", "response.audio.delta"}:
            b64 = event.get("delta") or event.get("audio")
            if isinstance(b64, str) and b64:
                self.playback.append(base64.b64decode(b64))
            return

        if etype == "input_audio_buffer.speech_started":
            self.playback.clear()
            return

        if etype in {
            "conversation.item.input_audio_transcription.completed",
            "conversation.item.input_audio_transcription.delta",
        }:
            text = event.get("transcript") or event.get("delta") or ""
            if text and etype.endswith("completed"):
                self._user_parts.append(str(text))
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
            if text and etype.endswith("done"):
                self._assistant_parts.append(str(text))
                if self.on_transcript:
                    await self.on_transcript("assistant", str(text))
            elif text and etype.endswith("delta"):
                # accumulate deltas lightly
                if self._assistant_parts and not self._assistant_parts[-1].endswith(" "):
                    self._assistant_parts[-1] = self._assistant_parts[-1] + str(text)
                else:
                    self._assistant_parts.append(str(text))
            return
