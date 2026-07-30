from collections import deque
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conversation import as_utc
from .db import Agent, MessageLog, RuntimeSettings, TelegramAccount
from .events import EventHub
from .runtime import AgentRuntime, NO_TELEGRAM_REPLY
from .telegram import TelegramGateway

ADMIN_ACK_TEXT = "Принято, обрабатываю…"
logger = logging.getLogger(__name__)


class TelegramEventRouter:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        runtime: AgentRuntime,
        telegram: TelegramGateway,
        events: EventHub,
    ) -> None:
        self.sessions = sessions
        self.runtime = runtime
        self.telegram = telegram
        self.events = events
        self._recent: deque[tuple[str, str, str]] = deque()
        self._recent_set: set[tuple[str, str, str]] = set()
        self._recent_limit = 2000

    def _seen(self, payload: dict[str, Any]) -> bool:
        key = (
            str(payload.get("phone", "")),
            str(payload.get("chat_id", "")),
            str(payload.get("message_id", "")),
        )
        if key in self._recent_set:
            return True
        if len(self._recent) >= self._recent_limit:
            self._recent_set.discard(self._recent.popleft())
        self._recent.append(key)
        self._recent_set.add(key)
        return False

    async def _target(
        self,
        db: AsyncSession,
        phone: str,
    ) -> tuple[TelegramAccount | None, Agent | None]:
        result = await db.execute(
            select(TelegramAccount, Agent)
            .join(Agent, Agent.telegram_account_id == TelegramAccount.id)
            .where(
                TelegramAccount.phone == phone,
                TelegramAccount.enabled.is_(True),
                Agent.enabled.is_(True),
            )
            .order_by(Agent.id)
            .limit(1)
        )
        found = result.first()
        return found if found else (None, None)

    async def new_message(self, payload: dict[str, Any]) -> None:
        text = str(payload.get("text") or "").strip()
        if (
            payload.get("outgoing")
            or payload.get("service")
            or not text
            or self._seen(payload)
        ):
            return
        if payload.get("sender_is_bot"):
            await self.events.publish(
                "telegram.bot_message_ignored",
                {
                    "phone": payload.get("phone"),
                    "chat_id": payload.get("chat_id"),
                    "sender_id": payload.get("sender_id"),
                    "message_id": payload.get("message_id"),
                },
            )
            return
        phone = str(payload.get("phone") or "")
        async with self.sessions() as db:
            account, agent = await self._target(db, phone)
            if account is None or agent is None:
                logger.warning(
                    "telegram.unrouted phone=%s message_id=%s (no enabled agent/account)",
                    phone,
                    payload.get("message_id"),
                )
                await self.events.publish(
                    "telegram.unrouted",
                    {"phone": phone, "message_id": payload.get("message_id")},
                )
                return
            chat_id = str(payload.get("chat_id") or payload.get("sender_id") or "")
            message_id = str(payload.get("message_id") or "")
            if message_id and await db.scalar(
                select(MessageLog.id).where(
                    MessageLog.agent_id == agent.id,
                    MessageLog.account_id == account.id,
                    MessageLog.chat_id == chat_id,
                    MessageLog.message_id == message_id,
                )
            ):
                logger.info(
                    "telegram.duplicate ignored agent=%s chat=%s message_id=%s",
                    agent.id,
                    chat_id,
                    message_id,
                )
                return
            is_admin = bool(payload.get("is_admin"))
            is_admin_command = text.lower().startswith(("/admin", "/system"))
            if is_admin_command and not is_admin:
                db.add(MessageLog(
                    agent_id=agent.id,
                    account_id=account.id,
                    direction="rejected",
                    chat_id=chat_id,
                    user_id=str(payload.get("sender_id") or chat_id),
                    sender_id=str(payload.get("sender_id") or "") or None,
                    message_id=message_id or None,
                    message_at=as_utc(payload.get("date")),
                    text=text,
                    metadata_json={"reason": "admin_command_denied"},
                ))
                await db.commit()
                await self.events.publish(
                    "telegram.admin_command_denied",
                    {"agent_id": agent.id, "sender_id": payload.get("sender_id")},
                )
                return
            runtime_settings = await db.get(RuntimeSettings, 1)
            history: list[dict[str, Any]] = []
            entity = payload.get("chat_id") or payload.get("sender_id")
            if entity is not None:
                try:
                    history = await self.telegram.get_conversation_history(
                        phone,
                        entity,
                        limit=(
                            runtime_settings.telegram_history_limit
                            if runtime_settings
                            else 100
                        ),
                    )
                except Exception:
                    history = []
            context = {
                "source": "telegram",
                "phone": phone,
                "sender_id": payload.get("sender_id"),
                "sender_username": payload.get("sender_username"),
                "sender_is_bot": payload.get("sender_is_bot", False),
                "chat_id": payload.get("chat_id"),
                "message_id": payload.get("message_id"),
                "message_at": payload.get("date"),
                "is_admin": is_admin,
                "admin_command": is_admin_command,
                "telegram_history": history,
            }
            logger.info(
                "telegram.routing agent=%s phone=%s chat=%s admin=%s text=%r",
                agent.id,
                phone,
                chat_id,
                is_admin,
                text[:120],
            )
            if is_admin and entity is not None:
                try:
                    await self.telegram.send_message(
                        phone,
                        entity,
                        ADMIN_ACK_TEXT,
                        reply_to=payload.get("message_id"),
                        humanize=False,
                    )
                    await self.events.publish(
                        "telegram.admin_ack",
                        {
                            "agent_id": agent.id,
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "sender_id": payload.get("sender_id"),
                        },
                    )
                except Exception as exc:
                    logger.exception(
                        "telegram.admin_ack_failed agent=%s chat=%s",
                        agent.id,
                        chat_id,
                    )
                    await self.events.publish(
                        "telegram.admin_ack_failed",
                        {
                            "agent_id": agent.id,
                            "chat_id": chat_id,
                            "error": str(exc),
                        },
                    )
            try:
                reply = await self.runtime.run(db, agent, text, context)
                suppressed = bool(context.get("_suppress_telegram_reply")) or (
                    reply.strip() == NO_TELEGRAM_REPLY
                )
                if suppressed:
                    logger.info(
                        "telegram.reply_suppressed agent=%s reason=%s",
                        agent.id,
                        context.get("_suppress_telegram_reason"),
                    )
                    await self.events.publish(
                        "telegram.reply_suppressed",
                        {
                            "agent_id": agent.id,
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "reason": context.get("_suppress_telegram_reason"),
                        },
                    )
                elif entity is not None and reply:
                    sent = await self.telegram.send_message(
                        phone,
                        entity,
                        reply,
                        reply_to=payload.get("message_id"),
                    )
                    await self.runtime.update_telegram_outbound(db, context, sent)
                    logger.info(
                        "telegram.reply_sent agent=%s chat=%s chars=%s",
                        agent.id,
                        chat_id,
                        len(reply),
                    )
                elif entity is not None and not reply:
                    logger.warning(
                        "telegram.empty_reply agent=%s chat=%s",
                        agent.id,
                        chat_id,
                    )
                    if is_admin:
                        await self.telegram.send_message(
                            phone,
                            entity,
                            "Готово, но текстовый ответ пустой.",
                            reply_to=payload.get("message_id"),
                            humanize=False,
                        )
            except Exception as exc:
                logger.exception(
                    "telegram.routing_failed agent=%s chat=%s",
                    agent.id,
                    chat_id,
                )
                await self.events.publish(
                    "telegram.routing_failed",
                    {"agent_id": agent.id, "error": str(exc)},
                )
                if entity is not None:
                    try:
                        await self.telegram.send_message(
                            phone,
                            entity,
                            f"Ошибка обработки: {exc}",
                            reply_to=payload.get("message_id"),
                            humanize=False,
                        )
                    except Exception:
                        logger.exception(
                            "telegram.error_reply_failed agent=%s chat=%s",
                            agent.id,
                            chat_id,
                        )

    async def _record_event(self, event_name: str, payload: dict[str, Any]) -> None:
        phone = str(payload.get("phone") or "")
        async with self.sessions() as db:
            account, agent = await self._target(db, phone)
            db.add(MessageLog(
                agent_id=agent.id if agent else None,
                account_id=account.id if account else None,
                direction="event",
                chat_id=str(payload.get("chat_id") or payload.get("sender_id") or ""),
                user_id=str(payload.get("sender_id") or "") or None,
                sender_id=str(payload.get("sender_id") or "") or None,
                message_id=str(payload.get("message_id") or "") or None,
                message_at=as_utc(payload.get("date")),
                text=str(payload.get("text") or payload.get("callback_data") or event_name),
                metadata_json={"telegram_event": event_name, "message_id": payload.get("message_id")},
            ))
            await db.commit()
        await self.events.publish(f"telegram.{event_name}", payload)

    async def message_edited(self, payload: dict[str, Any]) -> None:
        if not payload.get("outgoing"):
            await self._record_event("message_edited", payload)

    async def callback_query(self, payload: dict[str, Any]) -> None:
        await self._record_event("callback_query", payload)
