"""SipGateway — per-account SIP UA registry + OpenAI Realtime call sessions."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select

import os

from .config import Settings
from .db import Agent, LlmProfile, SessionLocal, SipAccount, SipCall, utcnow
from .events import EventHub
from .integrations import exception_text
from .realtime_bridge import DEFAULT_REALTIME_MODEL, HANGUP_INSTRUCTION, RealtimeSession
from .secrets import SecretStore
from .sip_ua import ActiveCall, SipEndpointConfig, SipUserAgent

logger = logging.getLogger(__name__)

INBOUND_GREETING_INSTRUCTION = (
    "Это входящий телефонный звонок. Сразу после соединения коротко поздоровайся "
    "голосом (например «Ало!» или «Здравствуйте»), представься и спроси, чем помочь. "
    "Не жди, пока абонент заговорит первым. После приветствия трубку не клади."
)
DEFAULT_INBOUND_GREETING = "Скажи коротко: Ало! Чем могу помочь?"


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
    def __init__(self, settings: Settings, events: EventHub) -> None:
        self.settings = settings
        self.events = events
        self.secrets = SecretStore.from_settings(settings)
        self._agents: dict[int, SipUserAgent] = {}
        self._reg_status: dict[int, dict[str, Any]] = {}
        self._call_map: dict[str, int] = {}  # sip call-id -> sip_calls.id
        self._realtime: dict[str, RealtimeSession] = {}  # sip call-id -> session
        self._pending_realtime: dict[str, asyncio.Task[RealtimeSession]] = {}
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
            if not account.enabled or not account.register_on_startup:
                results[account.id] = "skipped"
                continue
            try:
                await self.register_account(account)
                results[account.id] = "registered"
            except Exception as exc:
                logger.exception("SIP restore failed for %s", account.login)
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
        return results

    async def register_account(self, account: SipAccount) -> None:
        password = self.secrets.decrypt(account.password_ciphertext)
        if not password:
            self._reg_status[account.id] = {
                "registered": False,
                "status": "error:no_password",
                "error": "SIP account has no password",
            }
            raise RuntimeError("SIP account has no password")
        async with self._lock:
            existing = self._agents.pop(account.id, None)
            if existing:
                await existing.close()
            local_port = 0 if self._agents else self.settings.sip_bind_port
            config = SipEndpointConfig(
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

            ua = SipUserAgent(
                config,
                on_incoming=lambda call, aid=account.id: self._on_incoming(aid, call),
                on_reg_state=lambda ok, status, aid=account.id: self._on_reg(aid, ok, status),
                on_call_state=lambda cid, payload, aid=account.id: self._on_call_state(aid, cid, payload),
            )
            try:
                await ua.start()
                await ua.register()
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
            self._reg_status[account.id] = {
                "registered": ua.registered,
                "status": ua.registration_status,
                "error": None,
            }
            await self.events.publish(
                "sip.registered",
                {"account_id": account.id, "status": ua.registration_status},
            )

    async def unregister_account(self, account_id: int) -> None:
        async with self._lock:
            ua = self._agents.pop(account_id, None)
            if ua:
                await ua.close()
            self._reg_status[account_id] = {"registered": False, "status": "offline"}

    async def close(self) -> None:
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
        ua = self._agents.get(account.id)
        if ua is None:
            await self.register_account(account)
            ua = self._agents[account.id]

        # Prepare Realtime first so media callbacks are ready when RTP starts.
        # Temporary call id until dial returns the real SIP Call-ID.
        bootstrap = await self._prepare_realtime(agent)
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

    async def _prepare_realtime(self, agent: Agent) -> dict[str, Any]:
        session = await self._build_realtime_session(agent, sip_call_id_ref=[])
        await session.connect()
        return {"session": session}

    async def _build_realtime_session(
        self,
        agent: Agent,
        sip_call_id_ref: list[str],
        *,
        inbound: bool = False,
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

        async def on_hangup(reason: str) -> None:
            sip_call_id = sip_call_id_ref[0] if sip_call_id_ref else None
            if not sip_call_id:
                return
            logger.info("Agent hangup %s reason=%s", sip_call_id[:24], reason)
            for ua in self._agents.values():
                if sip_call_id in ua.calls:
                    await ua.hangup(sip_call_id, cause="agent_hangup")
                    return

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

        session = RealtimeSession(
            api_key=api_key,
            base_url=base_url,
            instructions=instructions,
            voice=voice,
            model=model,
            http_proxy=http_proxy,
            on_transcript=on_transcript,
            on_hangup=on_hangup,
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
                session = await self._build_realtime_session(agent, sip_ref, inbound=inbound)
                await session.connect()
        else:
            session = await self._build_realtime_session(agent, sip_ref, inbound=inbound)
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

    async def _prefetch_realtime(self, account_id: int, sip_call_id: str) -> None:
        if sip_call_id in self._pending_realtime or sip_call_id in self._realtime:
            return
        agent = await self._resolve_agent(account_id)
        if agent is None:
            return

        async def _build() -> RealtimeSession:
            session = await self._build_realtime_session(agent, [sip_call_id], inbound=True)
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
                await self._prefetch_realtime(account_id, sip_call_id)
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
            # Refresh REGISTER so NAT binding / Contact stays alive after a call.
            ua = self._agents.get(account_id)
            if ua is not None and ua.registered:
                asyncio.create_task(self._refresh_register(account_id), name=f"sip-refresh-{account_id}")
        if fields:
            await self._update_db_call(sip_call_id, **fields)
        await self.events.publish(
            "sip.call.state",
            {"sip_account_id": account_id, "sip_call_id": sip_call_id, **payload},
        )

    async def _refresh_register(self, account_id: int) -> None:
        ua = self._agents.get(account_id)
        if ua is None:
            return
        try:
            await ua.register()
            self._reg_status[account_id] = {
                "registered": ua.registered,
                "status": ua.registration_status,
                "error": None,
            }
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
            await self._update_db_call(
                sip_call_id,
                status="ended",
                ended_at=utcnow(),
                hangup_cause="local_hangup",
                transcript=session.transcript_text(),
            )
            await session.close()

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
