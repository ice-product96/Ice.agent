from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import ConversationState, MessageLog, RuntimeSettings, utcnow

Summarizer = Callable[[str], Awaitable[str]]


def setting(settings: RuntimeSettings, name: str, default: Any) -> Any:
    value = getattr(settings, name, None)
    return default if value is None else value


def as_utc(value: Any, default: datetime | None = None) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            value = None
    if not isinstance(value, datetime):
        value = default or utcnow()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso_utc(value: datetime | None) -> str | None:
    return as_utc(value).isoformat().replace("+00:00", "Z") if value else None


def elapsed_text(start: datetime | None, end: datetime) -> str:
    if start is None:
        return "no previous message"
    seconds = max(0, int((as_utc(end) - as_utc(start)).total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


class ConversationContextService:
    async def _state(
        self,
        db: AsyncSession,
        *,
        agent_id: int,
        account_id: int,
        chat_id: str,
        user_id: str,
    ) -> ConversationState:
        state = await db.scalar(
            select(ConversationState).where(
                ConversationState.agent_id == agent_id,
                ConversationState.account_id == account_id,
                ConversationState.chat_id == chat_id,
                ConversationState.user_id == user_id,
            )
        )
        if state is None:
            state = ConversationState(
                agent_id=agent_id,
                account_id=account_id,
                chat_id=chat_id,
                user_id=user_id,
            )
            db.add(state)
            await db.flush()
        return state

    async def _message_exists(
        self,
        db: AsyncSession,
        agent_id: int,
        account_id: int,
        chat_id: str,
        message_id: str | None,
    ) -> bool:
        if not message_id:
            return False
        return bool(
            await db.scalar(
                select(MessageLog.id).where(
                    MessageLog.agent_id == agent_id,
                    MessageLog.account_id == account_id,
                    MessageLog.chat_id == chat_id,
                    MessageLog.message_id == message_id,
                )
            )
        )

    async def synchronize(
        self,
        db: AsyncSession,
        state: ConversationState,
        messages: Iterable[dict[str, Any]],
    ) -> None:
        normalized = sorted(
            messages,
            key=lambda item: (
                as_utc(item.get("date")),
                int(item.get("id") or 0),
            ),
        )
        for item in normalized:
            message_id = str(item.get("id") or "")
            text = str(item.get("text") or "")
            if not message_id or not text or await self._message_exists(
                db, state.agent_id, state.account_id, state.chat_id, message_id
            ):
                continue
            outgoing = bool(item.get("outgoing"))
            message_at = as_utc(item.get("date"))
            db.add(
                MessageLog(
                    agent_id=state.agent_id,
                    account_id=state.account_id,
                    direction="out" if outgoing else "in",
                    chat_id=state.chat_id,
                    user_id=state.user_id,
                    sender_id=str(item.get("sender_id") or "") or None,
                    message_id=message_id,
                    message_at=message_at,
                    text=text,
                    metadata_json={"source": "telegram_history"},
                )
            )
        await db.flush()

    async def _refresh_state(self, db: AsyncSession, state: ConversationState) -> None:
        scope = (
            MessageLog.agent_id == state.agent_id,
            MessageLog.account_id == state.account_id,
            MessageLog.chat_id == state.chat_id,
            MessageLog.user_id == state.user_id,
            MessageLog.direction.in_(("in", "out")),
        )
        logs = (
            await db.scalars(
                select(MessageLog)
                .where(*scope)
                .order_by(
                    func.coalesce(MessageLog.message_at, MessageLog.created_at),
                    MessageLog.id,
                )
            )
        ).all()
        state.message_count = len(logs)
        if not logs:
            return
        last = logs[-1]
        state.last_message_id = last.message_id
        state.last_message_at = last.message_at or last.created_at
        incoming = next((item for item in reversed(logs) if item.direction == "in"), None)
        outgoing = next((item for item in reversed(logs) if item.direction == "out"), None)
        state.last_user_message_at = (
            incoming.message_at or incoming.created_at if incoming else None
        )
        state.last_agent_message_at = (
            outgoing.message_at or outgoing.created_at if outgoing else None
        )

    async def _maybe_summarize(
        self,
        db: AsyncSession,
        state: ConversationState,
        settings: RuntimeSettings,
        summarizer: Summarizer | None,
    ) -> None:
        if not setting(settings, "summarization_enabled", True) or summarizer is None:
            return
        filters = [
            MessageLog.agent_id == state.agent_id,
            MessageLog.account_id == state.account_id,
            MessageLog.chat_id == state.chat_id,
            MessageLog.user_id == state.user_id,
            MessageLog.direction.in_(("in", "out")),
        ]
        if state.summary_through_message_at is not None:
            filters.append(
                func.coalesce(MessageLog.message_at, MessageLog.created_at)
                > state.summary_through_message_at
            )
        logs = (
            await db.scalars(
                select(MessageLog)
                .where(*filters)
                .order_by(
                    func.coalesce(MessageLog.message_at, MessageLog.created_at),
                    MessageLog.id,
                )
            )
        ).all()
        total_chars = sum(len(item.text) for item in logs)
        if (
            len(logs) <= setting(settings, "summarize_after_messages", 80)
            and total_chars <= setting(settings, "context_max_chars", 30000)
        ):
            return
        keep = max(1, setting(settings, "recent_context_messages", 30))
        older = logs[:-keep]
        if not older:
            return
        timezone_name = setting(settings, "timezone", "UTC")
        transcript = "\n".join(self._line(item, timezone_name) for item in older)
        request = (
            "Update the rolling conversation summary. Preserve names/entities, commitments, "
            "objections, product facts, pending questions, and every relevant date/time. "
            "Be concise and factual.\n\nExisting summary:\n"
            f"{state.rolling_summary or '(none)'}\n\nMessages to incorporate:\n{transcript}"
        )
        try:
            summary = (await summarizer(request)).strip()
        except Exception:
            return
        if not summary:
            return
        cursor = older[-1]
        state.rolling_summary = summary[: setting(settings, "context_max_chars", 30000)]
        state.summary_through_message_id = cursor.message_id or str(cursor.id)
        state.summary_through_message_at = cursor.message_at or cursor.created_at
        await db.flush()

    @staticmethod
    def _line(item: MessageLog, timezone_name: str) -> str:
        at = as_utc(item.message_at or item.created_at)
        local = at.astimezone(ZoneInfo(timezone_name))
        speaker = "Agent" if item.direction == "out" else "User"
        sender = f" sender={item.sender_id}" if item.sender_id else ""
        return f"[{local.isoformat()} | {iso_utc(at)}] {speaker}{sender}: {item.text}"

    @staticmethod
    def temporal_context(settings: RuntimeSettings, now: datetime | None = None) -> str:
        current = as_utc(now)
        timezone_name = setting(settings, "timezone", "UTC")
        local = current.astimezone(ZoneInfo(timezone_name))
        return (
            "Temporal context:\n"
            f"- Current local date/time ({timezone_name}): {local.isoformat()}\n"
            f"- Current UTC date/time: {iso_utc(current)}"
        )

    async def prepare(
        self,
        db: AsyncSession,
        *,
        agent_id: int,
        account_id: int,
        message: str,
        context: dict[str, Any],
        settings: RuntimeSettings,
        summarizer: Summarizer | None = None,
        now: datetime | None = None,
    ) -> tuple[str, ConversationState, MessageLog]:
        chat_id = str(context.get("chat_id") or context.get("sender_id") or "")
        user_id = str(context.get("user_id") or context.get("sender_id") or chat_id)
        state = await self._state(
            db,
            agent_id=agent_id,
            account_id=account_id,
            chat_id=chat_id,
            user_id=user_id,
        )
        await self.synchronize(db, state, context.get("telegram_history") or [])
        message_id = str(context.get("message_id") or "") or None
        event_at = as_utc(context.get("message_at") or context.get("date"))
        previous = await db.scalar(
            select(MessageLog)
            .where(
                MessageLog.agent_id == agent_id,
                MessageLog.account_id == account_id,
                MessageLog.chat_id == chat_id,
                MessageLog.user_id == user_id,
                MessageLog.direction.in_(("in", "out")),
                func.coalesce(MessageLog.message_at, MessageLog.created_at) <= event_at,
                (
                    or_(
                        MessageLog.message_id.is_(None),
                        MessageLog.message_id != message_id,
                    )
                    if message_id
                    else True
                ),
            )
            .order_by(
                func.coalesce(MessageLog.message_at, MessageLog.created_at).desc(),
                MessageLog.id.desc(),
            )
            .limit(1)
        )
        previous_at = (
            previous.message_at or previous.created_at
            if previous is not None
            else state.last_message_at
        )
        inbound = None
        if message_id:
            inbound = await db.scalar(
                select(MessageLog).where(
                    MessageLog.agent_id == agent_id,
                    MessageLog.account_id == account_id,
                    MessageLog.chat_id == chat_id,
                    MessageLog.message_id == message_id,
                )
            )
        if inbound is None:
            inbound = MessageLog(
                agent_id=agent_id,
                account_id=account_id,
                direction="in",
                chat_id=chat_id,
                user_id=user_id,
                sender_id=str(context.get("sender_id") or "") or None,
                message_id=message_id,
                message_at=event_at,
                text=message,
                metadata_json={
                    key: value
                    for key, value in context.items()
                    if key not in {"telegram_history", "_outbound_log_id"}
                },
            )
            db.add(inbound)
            await db.flush()
        await self._refresh_state(db, state)
        await self._maybe_summarize(db, state, settings, summarizer)

        tail_filters = [
            MessageLog.agent_id == agent_id,
            MessageLog.account_id == account_id,
            MessageLog.chat_id == chat_id,
            MessageLog.user_id == user_id,
            MessageLog.direction.in_(("in", "out")),
            MessageLog.id != inbound.id,
        ]
        if state.summary_through_message_at is not None:
            tail_filters.append(
                func.coalesce(MessageLog.message_at, MessageLog.created_at)
                > state.summary_through_message_at
            )
        tail = (
            await db.scalars(
                select(MessageLog)
                .where(*tail_filters)
                .order_by(
                    func.coalesce(MessageLog.message_at, MessageLog.created_at).desc(),
                    MessageLog.id.desc(),
                )
                .limit(setting(settings, "recent_context_messages", 30))
            )
        ).all()
        timezone_name = setting(settings, "timezone", "UTC")
        context_max_chars = setting(settings, "context_max_chars", 30000)
        lines = [self._line(item, timezone_name) for item in reversed(tail)]
        budget = max(0, context_max_chars - len(state.rolling_summary or "") - 2000)
        while lines and sum(len(line) + 1 for line in lines) > budget:
            lines.pop(0)
        current_at = as_utc(now)
        last_at = as_utc(inbound.message_at or inbound.created_at)
        local_last = last_at.astimezone(ZoneInfo(timezone_name))
        system = (
            f"{self.temporal_context(settings, current_at)}\n"
            "Conversation identity:\n"
            f"- agent_id: {agent_id}\n- account_id: {account_id}\n"
            f"- chat_id: {chat_id}\n- user_id: {user_id}\n"
            f"- Last message date/time: {local_last.isoformat()} ({iso_utc(last_at)})\n"
            f"- Elapsed since previous message: {elapsed_text(previous_at, last_at)}\n"
            f"Rolling summary:\n{state.rolling_summary or '(none)'}\n"
            "Recent chronological transcript (current inbound is supplied as the user message):\n"
            + ("\n".join(lines) if lines else "(none)")
        )
        await db.commit()
        return system[:context_max_chars], state, inbound

    async def record_outbound(
        self,
        db: AsyncSession,
        state: ConversationState,
        text: str,
        context: dict[str, Any],
        at: datetime | None = None,
    ) -> MessageLog:
        log = MessageLog(
            agent_id=state.agent_id,
            account_id=state.account_id,
            direction="out",
            chat_id=state.chat_id,
            user_id=state.user_id,
            message_at=as_utc(at),
            text=text,
            metadata_json={"source": context.get("source", "runtime"), "delivery": "generated"},
        )
        db.add(log)
        await db.flush()
        context["_outbound_log_id"] = log.id
        await self._refresh_state(db, state)
        await db.commit()
        return log

    async def update_outbound_delivery(
        self,
        db: AsyncSession,
        log_id: int,
        sent: dict[str, Any],
    ) -> None:
        log = await db.get(MessageLog, log_id)
        if log is None:
            return
        log.message_id = str(sent.get("id") or "") or log.message_id
        log.sender_id = str(sent.get("sender_id") or "") or log.sender_id
        log.message_at = as_utc(sent.get("date"), log.message_at or log.created_at)
        log.metadata_json = {**(log.metadata_json or {}), "delivery": "sent"}
        state = await db.scalar(
            select(ConversationState).where(
                ConversationState.agent_id == log.agent_id,
                ConversationState.account_id == log.account_id,
                ConversationState.chat_id == log.chat_id,
                ConversationState.user_id == log.user_id,
            )
        )
        if state:
            await self._refresh_state(db, state)
        await db.commit()
