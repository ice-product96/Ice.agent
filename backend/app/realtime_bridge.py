"""OpenAI Realtime bridge: server WebSocket PCM 24 kHz (+ optional client_secrets)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import ssl
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import websockets
from websockets.asyncio.client import ClientConnection

from .sip_audio import OPENAI_FRAME_SAMPLES, OPENAI_RATE, PlaybackBuffer, silence_pcm16

logger = logging.getLogger(__name__)

OnTranscript = Callable[[str, str], Awaitable[None]]  # role, text
OnEvent = Callable[[dict[str, Any]], Awaitable[None]]

DEFAULT_REALTIME_MODEL = "gpt-realtime-2"


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


async def _open_proxied_socket(ws_url: str, http_proxy: str):
    """Open a TCP socket to the Realtime host via HTTP(S)/SOCKS proxy (python-socks)."""
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
    model: str = DEFAULT_REALTIME_MODEL,
) -> dict[str, Any]:
    """GA Realtime session shape (matches OpenAI docs / user's client_secrets curl)."""
    return {
        "type": "realtime",
        "model": model,
        "instructions": instructions,
        "output_modalities": ["audio"],
        "tools": [],
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
                },
            },
            "output": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "voice": voice,
            },
        },
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
        if response.status_code >= 400:
            detail = response.text[:500]
            raise RuntimeError(f"client_secrets HTTP {response.status_code}: {detail}")
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
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.instructions = instructions
        self.voice = voice
        self.model = (model or DEFAULT_REALTIME_MODEL).strip() or DEFAULT_REALTIME_MODEL
        self.http_proxy = (http_proxy or "").strip() or None
        self.on_transcript = on_transcript
        self.on_event = on_event
        self.playback = PlaybackBuffer()
        self.last_error: str | None = None
        self._ws: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()
        self._session_ready = asyncio.Event()
        self._session_updated = asyncio.Event()
        self._greeting_until = 0.0
        self._audio_chunks = 0
        self._user_parts: list[str] = []
        self._assistant_parts: list[str] = []

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    async def _ws_connect(self, ws_url: str, token: str) -> ClientConnection:
        connect_kwargs: dict[str, Any] = {
            "additional_headers": {"Authorization": f"Bearer {token}"},
            "max_size": 8 * 1024 * 1024,
            "ping_interval": 20,
        }
        if self.http_proxy:
            parsed_ws = urlparse(ws_url)
            connect_kwargs["sock"] = await _open_proxied_socket(ws_url, self.http_proxy)
            if parsed_ws.scheme == "wss":
                connect_kwargs["ssl"] = ssl.create_default_context()
                connect_kwargs["server_hostname"] = parsed_ws.hostname
        return await websockets.connect(ws_url, **connect_kwargs)

    async def connect(self) -> None:
        """Server-to-server: API key WebSocket + session.update (preferred)."""
        session = build_session_config(
            instructions=self.instructions,
            voice=self.voice,
            model=self.model,
        )
        # 1) Direct API-key WebSocket (documented server path)
        ws_url = _realtime_ws_url(self.base_url, model=self.model)
        try:
            logger.info("Realtime connecting model=%s url=%s proxy=%s", self.model, ws_url, bool(self.http_proxy))
            self._ws = await self._ws_connect(ws_url, self.api_key)
        except Exception as direct_exc:
            logger.warning("Realtime direct WS failed (%s) — trying client_secrets", direct_exc)
            secret = await create_client_secret(
                api_key=self.api_key,
                base_url=self.base_url,
                session=session,
                http_proxy=self.http_proxy,
            )
            # Ephemeral token usually does not need ?model=
            self._ws = await self._ws_connect(_realtime_ws_url(self.base_url), secret)

        self._reader = asyncio.create_task(self._read_loop(), name="realtime-reader")
        try:
            await asyncio.wait_for(self._session_ready.wait(), timeout=10)
        except TimeoutError as exc:
            self.last_error = "session.created timeout"
            raise RuntimeError("Realtime session.created timed out") from exc

        # Configure session (voice/instructions/audio) then wait for ack.
        await self._ws.send(json.dumps({"type": "session.update", "session": session}))
        try:
            await asyncio.wait_for(self._session_updated.wait(), timeout=8)
        except TimeoutError:
            logger.warning("Realtime session.updated timed out — continuing")
        logger.info("Realtime connected model=%s", self.model)

    async def request_response(self, instructions: str | None = None) -> None:
        """Force the model to speak (inbound greeting)."""
        if self._ws is None or self.closed:
            return
        if not self._session_ready.is_set():
            try:
                await asyncio.wait_for(self._session_ready.wait(), timeout=5)
            except TimeoutError:
                self.last_error = "not ready for greeting"
                logger.warning("Realtime not ready for greeting")
                return

        self._greeting_until = asyncio.get_running_loop().time() + 6.0
        prompt = (instructions or "").strip() or "Скажи коротко: Ало! Чем могу помочь?"

        # Explicit user turn is more reliable than response.create(instructions) alone.
        await self._ws.send(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "Телефонный звонок только что соединился. "
                                    f"Сразу поздоровайся голосом. {prompt}"
                                ),
                            }
                        ],
                    },
                }
            )
        )
        await self._ws.send(
            json.dumps(
                {
                    "type": "response.create",
                    "response": {"output_modalities": ["audio"]},
                }
            )
        )
        logger.info("Realtime greeting requested: %s", prompt[:120])

    def inject_beep(self, duration_ms: int = 350) -> None:
        """Put a local tone into the SIP playback buffer (RTP path check)."""
        self.playback.append(beep_pcm24(duration_ms=duration_ms))

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
            self.last_error = str(exc)
            logger.warning("Realtime WebSocket ended: %s", exc)
        finally:
            self._closed.set()

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
                if self._assistant_parts and not self._assistant_parts[-1].endswith(" "):
                    self._assistant_parts[-1] = self._assistant_parts[-1] + str(text)
                else:
                    self._assistant_parts.append(str(text))
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
