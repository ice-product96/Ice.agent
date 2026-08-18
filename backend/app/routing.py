from collections import deque
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conversation import as_utc
from .db import Agent, Consultation, ConversationState, MessageLog, RuntimeSettings, TelegramAccount
from .employee import CONSULT_CMD_RE
from .events import EventHub
from .runtime import AgentRuntime, NO_TELEGRAM_REPLY
from .telegram import TelegramGateway, attachment_label, public_attachment
from .work_items import find_open_for_chat

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

    async def _account_and_agents(
        self,
        db: AsyncSession,
        phone: str,
    ) -> tuple[TelegramAccount | None, list[Agent]]:
        rows = (
            await db.execute(
                select(TelegramAccount, Agent)
                .join(Agent, Agent.telegram_account_id == TelegramAccount.id)
                .where(
                    TelegramAccount.phone == phone,
                    TelegramAccount.enabled.is_(True),
                    Agent.enabled.is_(True),
                )
                .order_by(Agent.id)
            )
        ).all()
        if not rows:
            return None, []
        account = rows[0][0]
        return account, [row[1] for row in rows]

    async def _pick_agent(
        self,
        db: AsyncSession,
        account: TelegramAccount,
        agents: list[Agent],
        *,
        chat_id: str | None = None,
        consult_id: int | None = None,
    ) -> Agent | None:
        if not agents:
            return None
        if len(agents) == 1:
            return agents[0]
        if consult_id is not None:
            consult = await db.get(Consultation, consult_id)
            if consult is not None:
                picked = next((agent for agent in agents if agent.id == consult.agent_id), None)
                if picked is not None:
                    return picked
        if chat_id:
            for agent in agents:
                if await find_open_for_chat(db, agent.id, chat_id):
                    return agent
            agent_ids = [agent.id for agent in agents]
            recent_agent = await db.scalar(
                select(MessageLog.agent_id)
                .where(
                    MessageLog.account_id == account.id,
                    MessageLog.chat_id == str(chat_id),
                    MessageLog.agent_id.in_(agent_ids),
                )
                .order_by(MessageLog.id.desc())
                .limit(1)
            )
            if recent_agent is not None:
                picked = next((agent for agent in agents if agent.id == recent_agent), None)
                if picked is not None:
                    return picked
            conv_agent = await db.scalar(
                select(ConversationState.agent_id)
                .where(
                    ConversationState.account_id == account.id,
                    ConversationState.chat_id == str(chat_id),
                    ConversationState.agent_id.in_(agent_ids),
                )
                .order_by(ConversationState.updated_at.desc())
                .limit(1)
            )
            if conv_agent is not None:
                picked = next((agent for agent in agents if agent.id == conv_agent), None)
                if picked is not None:
                    return picked
        return agents[0]

    async def _target(
        self,
        db: AsyncSession,
        phone: str,
        *,
        chat_id: str | None = None,
        consult_id: int | None = None,
    ) -> tuple[TelegramAccount | None, Agent | None]:
        account, agents = await self._account_and_agents(db, phone)
        if account is None:
            return None, None
        agent = await self._pick_agent(
            db,
            account,
            agents,
            chat_id=chat_id,
            consult_id=consult_id,
        )
        return account, agent

    async def new_message(self, payload: dict[str, Any]) -> None:
        text = str(payload.get("text") or "").strip()
        attachments = [
            item for item in (payload.get("attachments") or [])
            if isinstance(item, dict)
        ]
        if (
            payload.get("outgoing")
            or payload.get("service")
            or (not text and not attachments)
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
        chat_id = str(payload.get("chat_id") or payload.get("sender_id") or "")
        consult_id: int | None = None
        if text:
            consult_match = CONSULT_CMD_RE.match(text)
            if consult_match:
                consult_id = int(consult_match.group(2))
        async with self.sessions() as db:
            account, agent = await self._target(
                db,
                phone,
                chat_id=chat_id or None,
                consult_id=consult_id,
            )
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
            if not text:
                text = attachment_label(attachments)
            is_admin_command = text.lower().startswith(("/admin", "/system"))
            consult_match = CONSULT_CMD_RE.match(text) if is_admin else None
            if consult_match:
                action = consult_match.group(1).lower()
                consult_id = int(consult_match.group(2))
                answer_body = (consult_match.group(3) or "").strip()
                status = {
                    "answer": "answered",
                    "approve": "approved",
                    "reject": "rejected",
                }[action]
                try:
                    item = await self.runtime.employee.resolve_consultation(
                        db,
                        consult_id,
                        status=status,
                        answer_text=answer_body,
                        answered_by=str(payload.get("sender_id") or ""),
                        schedule_tick=True,
                    )
                    confirm = (
                        f"Консультация #{item.id}: {status}."
                        + (f" Ответ сохранён." if answer_body else "")
                    )
                except KeyError:
                    confirm = f"Консультация #{consult_id} не найдена."
                except Exception as exc:
                    confirm = f"Не удалось обработать консультацию #{consult_id}: {exc}"
                entity = payload.get("chat_id") or payload.get("sender_id")
                if entity is not None:
                    try:
                        await self.telegram.send_message(
                            phone,
                            entity,
                            confirm,
                            reply_to=payload.get("message_id"),
                            humanize=False,
                        )
                    except Exception:
                        logger.exception("consult reply failed")
                return
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
                "topic_id": payload.get("topic_id"),
                "thread_id": (
                    str(payload["topic_id"])
                    if payload.get("topic_id") is not None
                    else ""
                ),
                "message_id": payload.get("message_id"),
                "message_at": payload.get("date"),
                "is_admin": is_admin,
                "admin_command": is_admin_command,
                "telegram_history": history,
                "attachments": [public_attachment(item) for item in attachments],
                "_attachments": attachments,
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
                    user_message = (
                        "Не удалось обработать сообщение. Подробности в логах сервера."
                        if is_admin
                        else "Извините, сейчас не могу ответить. Попробуйте чуть позже."
                    )
                    try:
                        await self.telegram.send_message(
                            phone,
                            entity,
                            user_message,
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
