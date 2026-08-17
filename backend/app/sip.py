"""SipGateway — per-account SIP UA registry + OpenAI Realtime call sessions."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from sqlalchemy import select

import os

from .config import Settings
from .db import Agent, LlmProfile, RuntimeSettings, SessionLocal, SipAccount, SipCall, utcnow
from .events import EventHub
from .integrations import MemoryStore, exception_text
from .realtime_bridge import (
    CALL_HISTORY_TOOL,
    DEFAULT_REALTIME_MODEL,
    HANGUP_INSTRUCTION,
    MEMORY_ADD_TOOL,
    MEMORY_SEARCH_TOOL,
    MEMORY_VOICE_INSTRUCTION,
    RealtimeSession,
)
from .secrets import SecretStore
from .sip_dial import validate_sip_dial_target
from .sip_ua import ActiveCall, SipEndpointConfig, SipUserAgent

logger = logging.getLogger(__name__)

INBOUND_GREETING_INSTRUCTION = (
    "Это входящий телефонный звонок. Сразу после соединения коротко поздоровайся "
    "голосом (например «Ало!» или «Здравствуйте»), представься и спроси, чем помочь. "
    "Не жди, пока абонент заговорит первым. После приветствия трубку не клади."
)
DEFAULT_INBOUND_GREETING = "Скажи коротко: Ало! Чем могу помочь?"


def sip_memory_user_id(number: str) -> str:
    digits = re.sub(r"\D+", "", number or "")
    if len(digits) >= 11:
        return digits[-11:]
    if len(digits) >= 10:
        return digits
    return (number or "").strip() or "unknown"


def _compact_memory_text(item: dict[str, Any]) -> str:
    return str(item.get("memory") or item.get("text") or "").strip()


def _call_when(row: SipCall) -> str:
    stamp = row.started_at or row.created_at
    if stamp is None:
        return ""
    return stamp.astimezone().strftime("%Y-%m-%d %H:%M")


def _history_item(row: SipCall, *, transcript_limit: int = 1200) -> dict[str, Any]:
    transcript = (row.transcript or "").strip()
    if len(transcript) > transcript_limit:
        transcript = transcript[: transcript_limit - 3] + "..."
    return {
        "when": _call_when(row),
        "direction": row.direction,
        "remote_number": row.remote_number,
        "transcript": transcript,
    }


def _ring_delay_seconds(account_value: Any, fallback: float | None) -> float:
    if account_value is not None:
        return max(0.0, float(account_value))
    if fallback is not None:
        return max(0.0, float(fallback))
    return 4.0


def _resolve_openai_http_proxy(profile_proxy: str | None, settings: Settings) -> str | None:
    for candidate in (
        profile_proxy,
        settings.openai_http_proxy,
        os.environ.get("ICE_OPENAI_HTTP_PROXY"),
        os.environ.get("HTTPS_PROXY"),
        os.environ.get("https_proxy"),
        os.environ.get("HTTP_PROXY"),
        os.environ.get("http_proxy"),
    ):
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    return None


class SipGateway:
    def __init__(
        self,
        settings: Settings,
        events: EventHub,
        memory: MemoryStore | None = None,
    ) -> None:
        self.settings = settings
        self.events = events
        self.memory = memory
        self.secrets = SecretStore.from_settings(settings)
        self._agents: dict[int, SipUserAgent] = {}
        self._reg_status: dict[int, dict[str, Any]] = {}
        self._call_map: dict[str, int] = {}  # sip call-id -> sip_calls.id
        self._realtime: dict[str, RealtimeSession] = {}  # sip call-id -> session
        self._pending_realtime: dict[str, asyncio.Task[RealtimeSession]] = {}
        self._desired: dict[int, SipAccount] = {}
        self._remembered_calls: set[str] = set()
        self._keeper_task: asyncio.Task[None] | None = None
        self._account_locks: dict[int, asyncio.Lock] = {}
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return True

    def registration(self, account_id: int) -> dict[str, Any]:
        return self._reg_status.get(
            account_id,
            {"registered": False, "status": "offline"},
        )

    def list_active_calls(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for account_id, ua in self._agents.items():
            for call in ua.calls.values():
                items.append(
                    {
                        "sip_call_id": call.call_id,
                        "db_id": self._call_map.get(call.call_id),
                        "sip_account_id": account_id,
                        "direction": call.direction,
                        "remote_number": call.remote_number,
                        "status": call.state,
                    }
                )
        return items

    async def restore(self, accounts: list[SipAccount]) -> dict[int, str]:
        results: dict[int, str] = {}
        for account in accounts:
            if not account.enabled:
                results[account.id] = "skipped"
                continue
            self._desired[account.id] = account
            try:
                await self.register_account(account)
                results[account.id] = "registered"
            except Exception as exc:
                logger.warning("SIP initial REGISTER failed for %s: %s — keeper will retry", account.login, exc)
                detail = exception_text(exc)
                results[account.id] = f"error:{detail}"
                self._reg_status[account.id] = {
                    "registered": False,
                    "status": f"error:{detail}",
                    "error": detail,
                }
                await self.events.publish(
                    "sip.register_failed",
                    {"account_id": account.id, "error": detail},
                )
        if self._keeper_task is None or self._keeper_task.done():
            self._ensure_keeper()
        return results

    def _account_lock(self, account_id: int) -> asyncio.Lock:
        lock = self._account_locks.get(account_id)
        if lock is None:
            lock = asyncio.Lock()
            self._account_locks[account_id] = lock
        return lock

    def _ensure_keeper(self) -> None:
        if self._keeper_task is None or self._keeper_task.done():
            self._keeper_task = asyncio.create_task(self._registration_keeper(), name="sip-keeper")

    def _build_config(self, account: SipAccount, password: str, local_port: int) -> SipEndpointConfig:
        return SipEndpointConfig(
            account_id=account.id,
            login=account.login,
            password=password,
            domain=account.domain,
            sip_server=account.sip_server,
            auth_username=account.auth_username,
            transport=account.transport or "udp",
            sip_proxy=account.sip_proxy,
            display_name=account.display_name or account.name,
            caller_id=account.caller_id,
            stun_server=account.stun_server or self.settings.sip_stun_server or None,
            public_ip=account.public_ip or self.settings.sip_public_ip or None,
            max_concurrent_calls=max(1, account.max_concurrent_calls or 1),
            ring_delay_seconds=_ring_delay_seconds(
                getattr(account, "ring_delay_seconds", None),
                self.settings.sip_ring_delay_seconds,
            ),
            wait_first_rtp_seconds=max(
                0.0,
                float(getattr(self.settings, "sip_wait_first_rtp_seconds", 5.0) or 5.0),
            ),
            local_sip_port=local_port,
            rtp_port_min=self.settings.sip_rtp_port_min,
            rtp_port_max=self.settings.sip_rtp_port_max,
        )

    def _ua_matches(self, ua: SipUserAgent, config: SipEndpointConfig) -> bool:
        cur = ua.config
        return (
            cur.login == config.login
            and cur.password == config.password
            and cur.domain == config.domain
            and cur.sip_server == config.sip_server
            and (cur.auth_username or "") == (config.auth_username or "")
            and (cur.transport or "udp").lower() == (config.transport or "udp").lower()
            and (cur.sip_proxy or "") == (config.sip_proxy or "")
            and (cur.public_ip or "") == (config.public_ip or "")
            and (cur.stun_server or "") == (config.stun_server or "")
        )

    def _apply_reg_status(self, account_id: int, ua: SipUserAgent, error: str | None = None) -> None:
        self._reg_status[account_id] = {
            "registered": ua.registered,
            "status": ua.registration_status,
            "error": error,
        }

    async def _drop_ua(self, account_id: int) -> None:
        ua = self._agents.pop(account_id, None)
        if ua is None:
            return
        try:
            await ua.close()
        except Exception:
            logger.exception("SIP UA close failed for account %s", account_id)

    async def _registration_keeper(self) -> None:
        """Keep enabled SIP accounts registered across restarts, NAT drops, and calls."""
        while True:
            await asyncio.sleep(8)
            for account_id, account in list(self._desired.items()):
                if not account.enabled:
                    continue
                ua = self._agents.get(account_id)
                if ua is not None and ua.transport_alive and ua.registered:
                    continue
                logger.info("SIP keeper: registering %s", account.login)
                try:
                    await self.register_account(account)
                except Exception as exc:
                    logger.warning("SIP keeper retry failed for %s: %s", account.login, exc)

    async def register_account(self, account: SipAccount, *, force: bool = False) -> None:
        password = self.secrets.decrypt(account.password_ciphertext)
        if not password:
            self._reg_status[account.id] = {
                "registered": False,
                "status": "error:no_password",
                "error": "SIP account has no password",
            }
            raise RuntimeError("SIP account has no password")
        if account.enabled:
            self._desired[account.id] = account
            self._ensure_keeper()
        async with self._account_lock(account.id):
            ua = self._agents.get(account.id)
            if ua is not None and ua.transport_alive and not force:
                probe = self._build_config(account, password, ua.config.local_sip_port)
                if self._ua_matches(ua, probe):
                    try:
                        await ua.register()
                        self._apply_reg_status(account.id, ua)
                        await self.events.publish(
                            "sip.registered",
                            {"account_id": account.id, "status": ua.registration_status},
                        )
                        return
                    except Exception as exc:
                        logger.warning(
                            "SIP in-place REGISTER failed for %s — rebuilding UA: %s",
                            account.login,
                            exc,
                        )
            await self._rebuild_ua(account, password)

    async def _rebuild_ua(self, account: SipAccount, password: str) -> None:
        await self._drop_ua(account.id)
        async with self._lock:
            local_port = 0 if self._agents else self.settings.sip_bind_port
            config = self._build_config(account, password, local_port)
            ua = SipUserAgent(
                config,
                on_incoming=lambda call, aid=account.id: self._on_incoming(aid, call),
                on_reg_state=lambda ok, status, aid=account.id: self._on_reg(aid, ok, status),
                on_call_state=lambda cid, payload, aid=account.id: self._on_call_state(aid, cid, payload),
            )
            try:
                await ua.start()
            except Exception as exc:
                try:
                    await ua.close()
                except Exception:
                    pass
                detail = str(exc).strip() or exception_text(exc)
                self._reg_status[account.id] = {
                    "registered": False,
                    "status": f"error:{detail}",
                    "error": detail,
                }
                await self.events.publish(
                    "sip.register_failed",
                    {"account_id": account.id, "error": detail},
                )
                raise
            self._agents[account.id] = ua
        try:
            await ua.register()
        except Exception as exc:
            detail = str(exc).strip() or exception_text(exc)
            self._apply_reg_status(account.id, ua, error=detail)
            await self.events.publish(
                "sip.register_failed",
                {"account_id": account.id, "error": detail},
            )
            raise
        if account.enabled:
            self._desired[account.id] = account
        self._apply_reg_status(account.id, ua)
        await self.events.publish(
            "sip.registered",
            {"account_id": account.id, "status": ua.registration_status},
        )

    async def _ensure_registered(self, account: SipAccount) -> SipUserAgent:
        ua = self._agents.get(account.id)
        if ua is not None and ua.transport_alive and ua.registered:
            return ua
        logger.info("SIP ensuring REGISTER for %s before outbound call", account.login)
        await self.register_account(account)
        ua = self._agents.get(account.id)
        if ua is None or not ua.transport_alive:
            raise RuntimeError("SIP transport is not running")
        if not ua.registered:
            status = self.registration(account.id)
            detail = status.get("error") or status.get("status") or "offline"
            raise RuntimeError(f"SIP account is not registered ({detail})")
        return ua

    async def unregister_account(self, account_id: int) -> None:
        self._desired.pop(account_id, None)
        async with self._account_lock(account_id):
            async with self._lock:
                await self._drop_ua(account_id)
            self._reg_status[account_id] = {"registered": False, "status": "offline"}

    async def close(self) -> None:
        if self._keeper_task:
            self._keeper_task.cancel()
            try:
                await self._keeper_task
            except asyncio.CancelledError:
                pass
            self._keeper_task = None
        self._desired.clear()
        async with self._lock:
            agents = list(self._agents.items())
            self._agents.clear()
        for account_id, ua in agents:
            try:
                await ua.close()
            except Exception:
                logger.exception("SIP close failed for account %s", account_id)
            self._reg_status[account_id] = {"registered": False, "status": "offline"}
        for session in list(self._realtime.values()):
            await session.close()
        self._realtime.clear()

    async def _on_reg(self, account_id: int, ok: bool, status: str) -> None:
        self._reg_status[account_id] = {
            "registered": ok,
            "status": status,
            "error": None if ok else status,
        }
        await self.events.publish(
            "sip.registration",
            {"account_id": account_id, "registered": ok, "status": status},
        )

    async def _resolve_agent(self, account_id: int) -> Agent | None:
        async with SessionLocal() as db:
            return await db.scalar(
                select(Agent)
                .where(Agent.sip_account_id == account_id, Agent.enabled.is_(True))
                .order_by(Agent.id)
                .limit(1)
            )

    async def _memory_enabled(self) -> bool:
        async with SessionLocal() as db:
            settings = await db.get(RuntimeSettings, 1)
        return bool(settings and settings.memory_enabled and self.memory is not None)

    async def _load_call_history(
        self,
        *,
        agent_id: int,
        remote_number: str,
        limit: int = 3,
        exclude_sip_call_id: str | None = None,
    ) -> list[dict[str, Any]]:
        key = sip_memory_user_id(remote_number)
        if key == "unknown":
            return []
        limit = max(1, min(int(limit or 3), 5))
        async with SessionLocal() as db:
            rows = (await db.scalars(
                select(SipCall)
                .where(SipCall.agent_id == agent_id, SipCall.transcript != "")
                .order_by(SipCall.id.desc())
                .limit(30)
            )).all()
        items: list[dict[str, Any]] = []
        for row in rows:
            if sip_memory_user_id(row.remote_number) != key:
                continue
            sip_id = str((row.metadata_json or {}).get("sip_call_id") or "")
            if exclude_sip_call_id and sip_id == exclude_sip_call_id:
                continue
            if not (row.transcript or "").strip():
                continue
            items.append(_history_item(row))
            if len(items) >= limit:
                break
        return items

    async def _voice_context_block(
        self,
        agent: Agent,
        remote_number: str,
        *,
        exclude_sip_call_id: str | None = None,
    ) -> str:
        user_id = sip_memory_user_id(remote_number)
        memories: list[str] = []
        if await self._memory_enabled() and user_id != "unknown" and self.memory is not None:
            try:
                found = await self.memory.search(
                    f"абонент {remote_number} прошлые звонки договорённости факты имя",
                    user_id=user_id,
                    agent_id=str(agent.id),
                    limit=8,
                )
                memories = [text for item in found if (text := _compact_memory_text(item))]
            except Exception:
                logger.exception("SIP memory search failed for %s", remote_number)
        history = await self._load_call_history(
            agent_id=agent.id,
            remote_number=remote_number,
            limit=2,
            exclude_sip_call_id=exclude_sip_call_id,
        )
        parts = [MEMORY_VOICE_INSTRUCTION]
        if memories:
            parts.append("Relevant long-term memories:")
            parts.extend(f"- {text[:400]}" for text in memories[:8])
        else:
            parts.append("No relevant long-term memories were found.")
        if history:
            parts.append("Prior calls with this number (do not read aloud unless asked):")
            for item in history:
                parts.append(
                    f"[{item['when']} {item['direction']}]\n{item['transcript'][:800]}"
                )
        return "\n".join(parts)

    async def _remember_ended_call(self, sip_call_id: str, transcript: str) -> None:
        if sip_call_id in self._remembered_calls:
            return
        self._remembered_calls.add(sip_call_id)
        text = (transcript or "").strip()
        if not text or not await self._memory_enabled() or self.memory is None:
            return
        db_id = self._call_map.get(sip_call_id)
        agent_id: int | None = None
        remote_number = ""
        agent_name = ""
        async with SessionLocal() as db:
            row = await db.get(SipCall, db_id) if db_id else None
            if row is None:
                return
            agent_id = row.agent_id
            remote_number = row.remote_number or ""
            if agent_id is not None:
                agent = await db.get(Agent, agent_id)
                agent_name = agent.name if agent else ""
        user_id = sip_memory_user_id(remote_number)
        if user_id == "unknown":
            return
        try:
            await self.memory.add(
                f"Phone call with {remote_number}\n{text[:4000]}",
                user_id=user_id,
                agent_id=str(agent_id) if agent_id is not None else None,
                metadata={
                    "kind": "sip_call",
                    "channel": "sip",
                    "remote_number": remote_number,
                    "sip_call_id": sip_call_id,
                    "agent_name": agent_name,
                },
            )
            logger.info("SIP memory stored for %s agent=%s", user_id, agent_id)
        except Exception:
            logger.exception("SIP memory add failed for %s", sip_call_id[:24])

    async def _create_db_call(
        self,
        *,
        agent_id: int | None,
        sip_account_id: int,
        direction: str,
        remote_number: str,
        status: str,
        sip_call_id: str,
    ) -> int:
        async with SessionLocal() as db:
            row = SipCall(
                agent_id=agent_id,
                sip_account_id=sip_account_id,
                direction=direction,
                remote_number=remote_number,
                status=status,
                started_at=utcnow(),
                answered_at=utcnow() if status == "answered" else None,
                metadata_json={"sip_call_id": sip_call_id},
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            self._call_map[sip_call_id] = row.id
            return row.id

    async def _update_db_call(self, sip_call_id: str, **fields: Any) -> None:
        db_id = self._call_map.get(sip_call_id)
        if db_id is None:
            return
        async with SessionLocal() as db:
            row = await db.get(SipCall, db_id)
            if row is None:
                return
            for key, value in fields.items():
                if key == "hangup_cause" and value is not None:
                    value = str(value).replace("\n", " ").strip()[:500]
                setattr(row, key, value)
            await db.commit()

    async def dial(
        self,
        *,
        account: SipAccount,
        agent: Agent,
        number: str,
    ) -> dict[str, Any]:
        ua = await self._ensure_registered(account)
        number = validate_sip_dial_target(number)

        # Prepare Realtime first so media callbacks are ready when RTP starts.
        # Temporary call id until dial returns the real SIP Call-ID.
        bootstrap = await self._prepare_realtime(agent, remote_number=number)
        session = bootstrap["session"]
        try:
            call = await ua.dial(
                number,
                on_rtp=session.send_pcm24,
                playback_provider=None,
            )
        except Exception:
            await session.close()
            raise

        sip_ref = bootstrap["sip_ref"]
        sip_ref.clear()
        sip_ref.append(call.call_id)
        self._realtime[call.call_id] = session
        # MtzVersion pattern: learn symmetric RTP before speaking.
        await ua.wait_first_rtp(call)
        call.on_rtp = session.send_pcm24
        call.playback_provider = session.read_playback_frame
        db_id = await self._create_db_call(
            agent_id=agent.id,
            sip_account_id=account.id,
            direction="outbound",
            remote_number=number,
            status=call.state,
            sip_call_id=call.call_id,
        )
        await self.events.publish(
            "sip.call.started",
            {
                "db_id": db_id,
                "sip_call_id": call.call_id,
                "direction": "outbound",
                "remote_number": number,
                "agent_id": agent.id,
                "sip_account_id": account.id,
            },
        )
        return {
            "sip_call_id": call.call_id,
            "db_id": db_id,
            "status": call.state,
            "remote_number": call.remote_number,
            "direction": "outbound",
            "agent_id": agent.id,
            "sip_account_id": account.id,
        }

    async def _prepare_realtime(self, agent: Agent, *, remote_number: str = "") -> dict[str, Any]:
        sip_ref: list[str] = []
        session = await self._build_realtime_session(
            agent,
            sip_ref,
            remote_number=remote_number,
        )
        await session.connect()
        return {"session": session, "sip_ref": sip_ref}

    async def _build_realtime_session(
        self,
        agent: Agent,
        sip_call_id_ref: list[str],
        *,
        inbound: bool = False,
        remote_number: str = "",
    ) -> RealtimeSession:
        if agent.llm_profile_id is None:
            raise RuntimeError("Agent has no LLM profile for Realtime")
        async with SessionLocal() as db:
            profile = await db.get(LlmProfile, agent.llm_profile_id)
            if profile is None or not profile.enabled:
                raise RuntimeError("LLM profile missing or disabled")
            api_key = self.secrets.decrypt(profile.api_key_ciphertext)
            if not api_key:
                raise RuntimeError("LLM profile has no API key")
            base_url = profile.base_url
            http_proxy = _resolve_openai_http_proxy(profile.http_proxy, self.settings)
        if not http_proxy:
            logger.warning(
                "Agent %s LLM profile has no HTTP proxy — OpenAI Realtime WSS may timeout outside US/EU",
                agent.id,
            )
        config = agent.config or {}
        voice = str(config.get("realtime_voice") or "marin")
        model = str(config.get("realtime_model") or DEFAULT_REALTIME_MODEL).strip() or DEFAULT_REALTIME_MODEL
        instructions = agent.prompt or "You are a helpful voice agent on a phone call."
        instructions = f"{instructions}\n\n{HANGUP_INSTRUCTION}"
        if inbound:
            extra = str(config.get("inbound_greeting") or "").strip()
            instructions = (
                f"{instructions}\n\n{INBOUND_GREETING_INSTRUCTION}"
                + (f"\nФормулировка приветствия: {extra}" if extra else "")
            )
        memory_on = await self._memory_enabled()
        extra_tools = [CALL_HISTORY_TOOL]
        if memory_on:
            extra_tools = [MEMORY_SEARCH_TOOL, MEMORY_ADD_TOOL, CALL_HISTORY_TOOL]
        exclude_id = sip_call_id_ref[0] if sip_call_id_ref else None
        if remote_number:
            try:
                context_block = await self._voice_context_block(
                    agent,
                    remote_number,
                    exclude_sip_call_id=exclude_id,
                )
                instructions = f"{instructions}\n\n{context_block}"
            except Exception:
                logger.exception("SIP voice memory context failed")
                instructions = f"{instructions}\n\n{MEMORY_VOICE_INSTRUCTION}"
        else:
            instructions = f"{instructions}\n\n{MEMORY_VOICE_INSTRUCTION}"
        agent_id = str(agent.id)
        agent_name = agent.name

        async def on_hangup(reason: str) -> None:
            sip_call_id = sip_call_id_ref[0] if sip_call_id_ref else None
            if not sip_call_id:
                for key, value in self._realtime.items():
                    if value is session:
                        sip_call_id = key
                        break
            if not sip_call_id:
                logger.error("Agent hangup skipped: no SIP Call-ID (reason=%s)", reason)
                return
            logger.info("Agent hangup %s reason=%s", sip_call_id[:24], reason)
            for ua in self._agents.values():
                if sip_call_id in ua.calls:
                    await ua.hangup(sip_call_id, cause="agent_hangup")
                    return
            logger.error(
                "Agent hangup: call %s not in UA map — forcing gateway hangup",
                sip_call_id[:24],
            )
            try:
                await self.hangup(sip_call_id=sip_call_id)
            except Exception:
                logger.exception("Gateway hangup after agent end_call failed")

        async def on_transcript(role: str, text: str) -> None:
            sip_call_id = sip_call_id_ref[0] if sip_call_id_ref else None
            if not sip_call_id:
                for key, value in self._realtime.items():
                    if value is session:
                        sip_call_id = key
                        break
            if not sip_call_id:
                return
            current = self._realtime.get(sip_call_id)
            transcript = current.transcript_text() if current else f"{role}: {text}"
            await self._update_db_call(sip_call_id, transcript=transcript)
            await self.events.publish(
                "sip.transcript",
                {
                    "sip_call_id": sip_call_id,
                    "db_id": self._call_map.get(sip_call_id),
                    "role": role,
                    "text": text,
                },
            )

        async def on_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
            tool = (name or "").strip().lower()
            number = remote_number
            if not number:
                sip_call_id = sip_call_id_ref[0] if sip_call_id_ref else ""
                for ua in self._agents.values():
                    call = ua.calls.get(sip_call_id)
                    if call is not None:
                        number = call.remote_number
                        break
            scope_user = sip_memory_user_id(number)
            if tool == "memory_search":
                if not memory_on or self.memory is None:
                    return {"memories": [], "note": "memory disabled"}
                query = str(args.get("query") or number or "").strip()
                found = await self.memory.search(
                    query,
                    user_id=scope_user if scope_user != "unknown" else None,
                    agent_id=agent_id,
                    limit=8,
                )
                texts = [text for item in found if (text := _compact_memory_text(item)[:400])]
                logger.info("SIP memory_search hits=%s query=%s", len(texts), query[:80])
                return {"memories": texts} if texts else {"memories": [], "note": "nothing found"}
            if tool == "memory_add":
                if not memory_on or self.memory is None:
                    return {"ok": False, "error": "memory disabled"}
                fact = str(args.get("text") or "").strip()
                if not fact:
                    return {"ok": False, "error": "empty text"}
                await self.memory.add(
                    fact,
                    user_id=scope_user,
                    agent_id=agent_id,
                    metadata={
                        "kind": "sip_fact",
                        "channel": "sip",
                        "remote_number": number,
                        "agent_name": agent_name,
                    },
                )
                return {"ok": True}
            if tool == "call_history":
                try:
                    limit = int(args.get("limit") or 3)
                except (TypeError, ValueError):
                    limit = 3
                calls = await self._load_call_history(
                    agent_id=agent.id,
                    remote_number=number,
                    limit=limit,
                    exclude_sip_call_id=sip_call_id_ref[0] if sip_call_id_ref else None,
                )
                logger.info("SIP call_history hits=%s number=%s", len(calls), number)
                return {"calls": calls} if calls else {"calls": [], "note": "no previous calls"}
            return {"error": f"unknown tool {name}"}

        session = RealtimeSession(
            api_key=api_key,
            base_url=base_url,
            instructions=instructions,
            voice=voice,
            model=model,
            http_proxy=http_proxy,
            on_transcript=on_transcript,
            on_hangup=on_hangup,
            on_tool=on_tool,
            extra_tools=extra_tools,
        )
        return session

    async def _start_realtime(
        self,
        agent: Agent,
        call: ActiveCall,
        *,
        inbound: bool = False,
    ) -> RealtimeSession:
        sip_ref = [call.call_id]
        pending = self._pending_realtime.pop(call.call_id, None)
        if pending is not None:
            try:
                session = await pending
            except Exception as prefetch_exc:
                logger.warning("Realtime prefetch failed (%s) — reconnecting", prefetch_exc)
                session = await self._build_realtime_session(
                    agent,
                    sip_ref,
                    inbound=inbound,
                    remote_number=call.remote_number,
                )
                await session.connect()
        else:
            session = await self._build_realtime_session(
                agent,
                sip_ref,
                inbound=inbound,
                remote_number=call.remote_number,
            )
            await session.connect()
        self._realtime[call.call_id] = session
        # Mic path immediately; enable silence TX (NAT), wait briefly for peer, then greet.
        call.on_rtp = session.send_pcm24
        call.playback_provider = session.read_playback_frame
        call.media_tx_enabled = True
        await self._wait_call_rtp(account_id=None, call=call)
        if inbound:
            greeting = str((agent.config or {}).get("inbound_greeting") or "").strip()
            await session.request_response(greeting or DEFAULT_INBOUND_GREETING)
            # Wait until first audio chunk or error (Mtz plays as soon as deltas arrive).
            for _ in range(40):
                if session._audio_chunks > 0 or session.last_error or session.closed:
                    break
                await asyncio.sleep(0.1)
            if session._audio_chunks == 0:
                logger.error(
                    "Realtime greeting produced no audio chunks (err=%s)",
                    session.last_error,
                )
                await self._update_db_call(
                    call.call_id,
                    hangup_cause=f"realtime_silent:{session.last_error or 'no_audio'}",
                )
        return session

    async def _wait_call_rtp(self, *, account_id: int | None, call: ActiveCall) -> None:
        ua: SipUserAgent | None = None
        if account_id is not None:
            ua = self._agents.get(account_id)
        if ua is None:
            for candidate in self._agents.values():
                if call.call_id in candidate.calls:
                    ua = candidate
                    break
        if ua is None:
            call.media_tx_enabled = True
            return
        await ua.wait_first_rtp(call)

    async def _prefetch_realtime(
        self,
        account_id: int,
        sip_call_id: str,
        remote_number: str = "",
    ) -> None:
        if sip_call_id in self._pending_realtime or sip_call_id in self._realtime:
            return
        agent = await self._resolve_agent(account_id)
        if agent is None:
            return

        async def _build() -> RealtimeSession:
            session = await self._build_realtime_session(
                agent,
                [sip_call_id],
                inbound=True,
                remote_number=remote_number,
            )
            await session.connect()
            return session

        self._pending_realtime[sip_call_id] = asyncio.create_task(
            _build(),
            name=f"rt-prefetch-{sip_call_id[:16]}",
        )

    async def _cancel_prefetch(self, sip_call_id: str) -> None:
        task = self._pending_realtime.pop(sip_call_id, None)
        if task is None:
            return
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                return
            except Exception:
                return
        try:
            session = task.result()
        except Exception:
            return
        try:
            await session.close()
        except Exception:
            pass

    async def _on_incoming(self, account_id: int, call: ActiveCall) -> None:
        agent = await self._resolve_agent(account_id)
        if call.call_id in self._call_map:
            await self._update_db_call(
                call.call_id,
                status="answered",
                answered_at=utcnow(),
                agent_id=agent.id if agent else None,
            )
            db_id = self._call_map[call.call_id]
        else:
            db_id = await self._create_db_call(
                agent_id=agent.id if agent else None,
                sip_account_id=account_id,
                direction="inbound",
                remote_number=call.remote_number,
                status="answered",
                sip_call_id=call.call_id,
            )
        await self.events.publish(
            "sip.call.started",
            {
                "db_id": db_id,
                "sip_call_id": call.call_id,
                "direction": "inbound",
                "remote_number": call.remote_number,
                "agent_id": agent.id if agent else None,
                "sip_account_id": account_id,
            },
        )
        if agent is None:
            logger.warning("Inbound SIP call on account %s with no agent", account_id)
            await self._cancel_prefetch(call.call_id)
            return
        try:
            await self._start_realtime(agent, call, inbound=True)
            logger.info(
                "Realtime bound to inbound call %s agent=%s",
                call.call_id[:24],
                agent.id,
            )
        except Exception as exc:
            logger.exception("Realtime start failed for inbound call")
            await self._cancel_prefetch(call.call_id)
            detail = exception_text(exc)
            await self._update_db_call(call.call_id, status="failed", hangup_cause=detail)
            ua = self._agents.get(account_id)
            if ua:
                await ua.hangup(call.call_id, cause=detail)

    async def _on_call_state(self, account_id: int, sip_call_id: str, payload: dict[str, Any]) -> None:
        status = str(payload.get("status") or "")
        fields: dict[str, Any] = {}
        if status == "answered":
            fields = {"status": "answered", "answered_at": utcnow()}
        elif status == "ringing":
            if sip_call_id not in self._call_map:
                agent = await self._resolve_agent(account_id)
                await self._create_db_call(
                    agent_id=agent.id if agent else None,
                    sip_account_id=account_id,
                    direction=str(payload.get("direction") or "inbound"),
                    remote_number=str(payload.get("remote_number") or ""),
                    status="ringing",
                    sip_call_id=sip_call_id,
                )
            fields = {"status": "ringing"}
            if str(payload.get("direction") or "inbound") == "inbound":
                await self._prefetch_realtime(
                    account_id,
                    sip_call_id,
                    str(payload.get("remote_number") or ""),
                )
        elif status == "failed":
            await self._cancel_prefetch(sip_call_id)
            fields = {"status": "failed", "ended_at": utcnow(), "hangup_cause": str(payload.get("code") or "failed")}
        elif status == "ended":
            await self._cancel_prefetch(sip_call_id)
            session = self._realtime.pop(sip_call_id, None)
            transcript = ""
            if session:
                transcript = session.transcript_text()
                await session.close()
            cause = str(payload.get("cause") or "ended")
            normal_causes = {
                "local_hangup",
                "remote_bye",
                "cancelled",
                "rtp_timeout",
                "ended",
                "busy",
                "agent_hangup",
            }
            fields = {
                "status": "ended" if cause in normal_causes else "failed",
                "ended_at": utcnow(),
                "hangup_cause": cause,
                "transcript": transcript,
            }
            # NAT mapping is kept by OPTIONS keepalive; a full REGISTER here
            # used to get 481/401 and drop inbound until the user clicked Register.
            ua = self._agents.get(account_id)
            if ua is not None and not ua.registered:
                asyncio.create_task(self._refresh_register(account_id), name=f"sip-refresh-{account_id}")
            if transcript.strip():
                asyncio.create_task(
                    self._remember_ended_call(sip_call_id, transcript),
                    name=f"sip-mem-{sip_call_id[:12]}",
                )
        if fields:
            await self._update_db_call(sip_call_id, **fields)
        await self.events.publish(
            "sip.call.state",
            {"sip_account_id": account_id, "sip_call_id": sip_call_id, **payload},
        )

    async def _refresh_register(self, account_id: int) -> None:
        account = self._desired.get(account_id)
        if account is not None:
            try:
                await self.register_account(account)
            except Exception as exc:
                logger.warning("SIP re-REGISTER after call failed for account %s: %s", account_id, exc)
            return
        ua = self._agents.get(account_id)
        if ua is None:
            return
        try:
            await ua.register()
            self._apply_reg_status(account_id, ua)
        except Exception as exc:
            logger.warning("SIP re-REGISTER after call failed for account %s: %s", account_id, exc)

    async def hangup(self, *, sip_call_id: str | None = None, db_id: int | None = None) -> None:
        if sip_call_id is None and db_id is not None:
            for key, value in self._call_map.items():
                if value == db_id:
                    sip_call_id = key
                    break
        if not sip_call_id:
            raise RuntimeError("Call not found")
        for ua in self._agents.values():
            if sip_call_id in ua.calls:
                await ua.hangup(sip_call_id, cause="local_hangup")
                break
        session = self._realtime.pop(sip_call_id, None)
        if session:
            transcript = session.transcript_text()
            await self._update_db_call(
                sip_call_id,
                status="ended",
                ended_at=utcnow(),
                hangup_cause="local_hangup",
                transcript=transcript,
            )
            await session.close()
            if transcript.strip():
                asyncio.create_task(
                    self._remember_ended_call(sip_call_id, transcript),
                    name=f"sip-mem-{sip_call_id[:12]}",
                )

    async def status(self, account_id: int | None = None) -> dict[str, Any]:
        if account_id is not None:
            return {
                "account_id": account_id,
                **self.registration(account_id),
                "active_calls": [c for c in self.list_active_calls() if c["sip_account_id"] == account_id],
            }
        return {
            "accounts": {
                str(aid): self.registration(aid) for aid in set(self._reg_status) | set(self._agents)
            },
            "active_calls": self.list_active_calls(),
        }
