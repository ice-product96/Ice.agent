from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Table, Text, UniqueConstraint, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[str]: JSON}


agent_mcp_servers = Table(
    "agent_mcp_servers",
    Base.metadata,
    Column("agent_id", ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True),
    Column("mcp_server_id", ForeignKey("mcp_servers.id", ondelete="CASCADE"), primary_key=True),
)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TelegramAccount(TimestampMixin, Base):
    __tablename__ = "telegram_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    session_path: Mapped[str] = mapped_column(String(512), unique=True)
    api_id: Mapped[int | None] = mapped_column(Integer)
    api_hash_ciphertext: Mapped[str | None] = mapped_column(Text)
    http_proxy: Mapped[str | None] = mapped_column(String(1024))
    mtproto_host: Mapped[str | None] = mapped_column(String(255))
    mtproto_port: Mapped[int | None] = mapped_column(Integer)
    mtproto_dc_id: Mapped[int | None] = mapped_column(Integer)
    # legacy socks5 columns kept for existing DBs; unused by the app
    socks5_host: Mapped[str | None] = mapped_column(String(255))
    socks5_port: Mapped[int | None] = mapped_column(Integer)
    socks5_username: Mapped[str | None] = mapped_column(String(255))
    socks5_password_ciphertext: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    authorized: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    agents: Mapped[list["Agent"]] = relationship(back_populates="telegram_account")


class LlmProfile(TimestampMixin, Base):
    __tablename__ = "llm_profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(48))
    base_url: Mapped[str | None] = mapped_column(String(1024))
    api_key_ciphertext: Mapped[str | None] = mapped_column(Text)
    http_proxy: Mapped[str | None] = mapped_column(String(1024))
    default_model: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    agents: Mapped[list["Agent"]] = relationship(back_populates="llm_profile")


class SipAccount(TimestampMixin, Base):
    __tablename__ = "sip_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    sip_server: Mapped[str] = mapped_column(String(255), default="voice.telphin.com:5068")
    domain: Mapped[str] = mapped_column(String(255), default="sip.telphin.com")
    login: Mapped[str] = mapped_column(String(120), index=True)
    auth_username: Mapped[str | None] = mapped_column(String(120))
    password_ciphertext: Mapped[str | None] = mapped_column(Text)
    transport: Mapped[str] = mapped_column(String(16), default="udp")
    sip_proxy: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(120), default="")
    caller_id: Mapped[str | None] = mapped_column(String(64))
    stun_server: Mapped[str | None] = mapped_column(String(255))
    public_ip: Mapped[str | None] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    register_on_startup: Mapped[bool] = mapped_column(Boolean, default=True)
    max_concurrent_calls: Mapped[int] = mapped_column(Integer, default=1)
    ring_delay_seconds: Mapped[float] = mapped_column(Float, default=4.0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    agents: Mapped[list["Agent"]] = relationship(back_populates="sip_account")


class Agent(TimestampMixin, Base):
    __tablename__ = "agents"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    prompt: Mapped[str] = mapped_column(Text, default="")
    model_provider: Mapped[str] = mapped_column(String(32), default="openai")
    model_name: Mapped[str] = mapped_column(String(120), default="gpt-5.6-terra")
    llm_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("llm_profiles.id", ondelete="SET NULL")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    telegram_account_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_accounts.id", ondelete="SET NULL"))
    sip_account_id: Mapped[int | None] = mapped_column(ForeignKey("sip_accounts.id", ondelete="SET NULL"))
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    telegram_account: Mapped[TelegramAccount | None] = relationship(back_populates="agents")
    sip_account: Mapped[SipAccount | None] = relationship(back_populates="agents")
    llm_profile: Mapped[LlmProfile | None] = relationship(back_populates="agents")
    mcp_servers: Mapped[list["McpServer"]] = relationship(secondary=agent_mcp_servers, back_populates="agents")


class SipCall(TimestampMixin, Base):
    __tablename__ = "sip_calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"), index=True)
    sip_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("sip_accounts.id", ondelete="SET NULL"), index=True
    )
    direction: Mapped[str] = mapped_column(String(16), default="outbound")  # inbound|outbound
    remote_number: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="initiated", index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hangup_cause: Mapped[str | None] = mapped_column(String(500))
    transcript: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AgentLink(TimestampMixin, Base):
    __tablename__ = "agent_links"
    __table_args__ = (UniqueConstraint("source_agent_id", "target_agent_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"))
    target_agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"))
    can_delegate: Mapped[bool] = mapped_column(Boolean, default=True)
    can_message: Mapped[bool] = mapped_column(Boolean, default=True)
    permissions: Mapped[list[str]] = mapped_column(JSON, default=list)


class McpServer(TimestampMixin, Base):
    __tablename__ = "mcp_servers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    transport: Mapped[str] = mapped_column(String(32), default="stdio")
    command: Mapped[str | None] = mapped_column(String(512))
    args: Mapped[list[str]] = mapped_column(JSON, default=list)
    url: Mapped[str | None] = mapped_column(String(1024))
    env: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    env_ciphertext: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    agents: Mapped[list[Agent]] = relationship(secondary=agent_mcp_servers, back_populates="mcp_servers")


class CronJob(TimestampMixin, Base):
    __tablename__ = "cron_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"))
    cron: Mapped[str] = mapped_column(String(120))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AdminSettings(TimestampMixin, Base):
    __tablename__ = "admin_settings"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    telegram_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class RuntimeSettings(TimestampMixin, Base):
    __tablename__ = "runtime_settings"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    search_provider: Mapped[str] = mapped_column(String(16), default="ddg")
    searxng_url: Mapped[str | None] = mapped_column(String(1024))
    tavily_api_key_ciphertext: Mapped[str | None] = mapped_column(Text)
    tavily_http_proxy: Mapped[str | None] = mapped_column(String(1024))
    memory_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    memory_backend: Mapped[str] = mapped_column(String(16), default="local")
    mem0_api_key_ciphertext: Mapped[str | None] = mapped_column(Text)
    qdrant_url: Mapped[str | None] = mapped_column(String(1024))
    memory_llm_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("llm_profiles.id", ondelete="SET NULL")
    )
    typing_min_seconds: Mapped[float] = mapped_column(Float, default=0.4)
    typing_max_seconds: Mapped[float] = mapped_column(Float, default=2.5)
    typing_jitter_seconds: Mapped[float] = mapped_column(Float, default=0.35)
    typing_chunk_size: Mapped[int] = mapped_column(Integer, default=3800)
    typing_presence: Mapped[bool] = mapped_column(Boolean, default=False)
    task_workers: Mapped[int] = mapped_column(Integer, default=1)
    max_tool_rounds: Mapped[int] = mapped_column(Integer, default=8)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    telegram_history_limit: Mapped[int] = mapped_column(Integer, default=100)
    recent_context_messages: Mapped[int] = mapped_column(Integer, default=30)
    context_max_chars: Mapped[int] = mapped_column(Integer, default=30000)
    summarization_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    summarize_after_messages: Mapped[int] = mapped_column(Integer, default=80)


class MessageLog(Base):
    __tablename__ = "message_logs"
    __table_args__ = (
        UniqueConstraint(
            "agent_id",
            "account_id",
            "chat_id",
            "message_id",
            name="uq_message_logs_telegram_message",
        ),
        Index(
            "ix_message_logs_conversation_time",
            "agent_id",
            "account_id",
            "chat_id",
            "user_id",
            "message_at",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    account_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_accounts.id", ondelete="SET NULL"))
    direction: Mapped[str] = mapped_column(String(16))
    chat_id: Mapped[str | None] = mapped_column(String(64), index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    sender_id: Mapped[str | None] = mapped_column(String(64), index=True)
    message_id: Mapped[str | None] = mapped_column(String(64), index=True)
    message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    text: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ConversationState(TimestampMixin, Base):
    __tablename__ = "conversation_states"
    __table_args__ = (
        UniqueConstraint(
            "agent_id",
            "account_id",
            "chat_id",
            "user_id",
            name="uq_conversation_states_identity",
        ),
        Index("ix_conversation_states_last_message_at", "last_message_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_accounts.id", ondelete="CASCADE"), index=True
    )
    chat_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    rolling_summary: Mapped[str] = mapped_column(Text, default="")
    summary_through_message_id: Mapped[str | None] = mapped_column(String(64))
    summary_through_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_id: Mapped[str | None] = mapped_column(String(64))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_user_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_agent_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AgentTask(TimestampMixin, Base):
    __tablename__ = "agent_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    target_agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    input: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)


class EmployeeProfile(TimestampMixin, Base):
    __tablename__ = "employee_profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), unique=True, index=True)
    autonomy_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    heartbeat_minutes: Mapped[int] = mapped_column(Integer, default=15)
    workday_start: Mapped[str] = mapped_column(String(8), default="09:00")
    workday_end: Mapped[str] = mapped_column(String(8), default="18:00")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    budget_ticks_per_day: Mapped[int] = mapped_column(Integer, default=48)
    ticks_used_today: Mapped[int] = mapped_column(Integer, default=0)
    ticks_day: Mapped[str | None] = mapped_column(String(16))  # YYYY-MM-DD in employee tz
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_digest_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    role_title: Mapped[str] = mapped_column(String(200), default="")
    mission: Mapped[str] = mapped_column(Text, default="")


class PromptSection(TimestampMixin, Base):
    __tablename__ = "prompt_sections"
    __table_args__ = (UniqueConstraint("agent_id", "key", name="uq_prompt_sections_agent_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(64))  # identity|role|rules|skills|tone|self_notes
    content: Mapped[str] = mapped_column(Text, default="")


class EmployeePlan(TimestampMixin, Base):
    __tablename__ = "employee_plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    horizon: Mapped[str] = mapped_column(String(16), index=True)  # hour|day|week|month
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    body: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {steps: [...]}
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)


class Consultation(TimestampMixin, Base):
    __tablename__ = "consultations"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    question: Mapped[str] = mapped_column(Text, default="")
    context: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="open", index=True)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    action_name: Mapped[str | None] = mapped_column(String(120))  # dangerous tool to unlock
    telegram_message_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    answer_text: Mapped[str | None] = mapped_column(Text)
    answered_by: Mapped[str | None] = mapped_column(String(64))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmployeeNeed(TimestampMixin, Base):
    __tablename__ = "employee_needs"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32), default="info")  # info|access|decision|resource|rest
    title: Mapped[str] = mapped_column(String(300), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[int] = mapped_column(Integer, default=5)
    status: Mapped[str] = mapped_column(String(24), default="open", index=True)
    consultation_id: Mapped[int | None] = mapped_column(
        ForeignKey("consultations.id", ondelete="SET NULL"), index=True
    )


settings = get_settings()
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def create_schema() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        # create_all does not add columns to existing tables
        for statement in (
            "ALTER TABLE llm_profiles ADD COLUMN IF NOT EXISTS http_proxy VARCHAR(1024)",
            "ALTER TABLE telegram_accounts ADD COLUMN IF NOT EXISTS socks5_host VARCHAR(255)",
            "ALTER TABLE telegram_accounts ADD COLUMN IF NOT EXISTS socks5_port INTEGER",
            "ALTER TABLE telegram_accounts ADD COLUMN IF NOT EXISTS socks5_username VARCHAR(255)",
            "ALTER TABLE telegram_accounts ADD COLUMN IF NOT EXISTS socks5_password_ciphertext TEXT",
            "ALTER TABLE telegram_accounts ADD COLUMN IF NOT EXISTS http_proxy VARCHAR(1024)",
            "ALTER TABLE telegram_accounts ADD COLUMN IF NOT EXISTS mtproto_host VARCHAR(255)",
            "ALTER TABLE telegram_accounts ADD COLUMN IF NOT EXISTS mtproto_port INTEGER",
            "ALTER TABLE telegram_accounts ADD COLUMN IF NOT EXISTS mtproto_dc_id INTEGER",
            "ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS env_ciphertext TEXT",
            "ALTER TABLE mcp_servers ALTER COLUMN transport TYPE VARCHAR(32)",
            "ALTER TABLE runtime_settings ADD COLUMN IF NOT EXISTS tavily_api_key_ciphertext TEXT",
            "ALTER TABLE runtime_settings ADD COLUMN IF NOT EXISTS tavily_http_proxy VARCHAR(1024)",
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS sip_account_id INTEGER",
            "ALTER TABLE sip_accounts ADD COLUMN IF NOT EXISTS ring_delay_seconds FLOAT",
            "ALTER TABLE sip_calls ALTER COLUMN hangup_cause TYPE VARCHAR(500)",
        ):
            try:
                async with connection.begin_nested():
                    await connection.execute(text(statement))
            except Exception:
                # SQLite / already-migrated dialects may reject some ALTERs
                pass
