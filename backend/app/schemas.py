from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TelegramAccountIn(BaseModel):
    phone: str
    name: str = ""
    api_id: int | None = None
    api_hash: str = ""
    clear_api_hash: bool = False
    enabled: bool = True
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class TelegramAccountOut(ORMModel):
    id: int
    phone: str
    name: str
    api_id: int | None
    enabled: bool
    metadata_json: dict[str, Any]
    session_path: str
    authorized: bool
    created_at: datetime
    has_api_hash: bool


class AgentIn(BaseModel):
    name: str
    prompt: str = ""
    model_provider: Literal["openai", "deepseek"] = "openai"
    model_name: str = "gpt-5.5"
    llm_profile_id: int | None = None
    enabled: bool = True
    telegram_account_id: int | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class AgentPatch(BaseModel):
    name: str | None = None
    prompt: str | None = None
    model_provider: Literal["openai", "deepseek"] | None = None
    model_name: str | None = None
    llm_profile_id: int | None = None
    enabled: bool | None = None
    telegram_account_id: int | None = None
    config: dict[str, Any] | None = None


class AgentOut(ORMModel, AgentIn):
    id: int
    created_at: datetime
    updated_at: datetime


class AgentLinkIn(BaseModel):
    source_agent_id: int
    target_agent_id: int
    can_delegate: bool = True
    can_message: bool = True
    permissions: list[str] = Field(default_factory=list)


class AgentLinkOut(ORMModel, AgentLinkIn):
    id: int


class McpServerIn(BaseModel):
    name: str
    transport: Literal["stdio", "sse"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class McpServerOut(ORMModel, McpServerIn):
    id: int


class CronJobIn(BaseModel):
    name: str
    agent_id: int
    cron: str
    payload: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class CronJobOut(ORMModel, CronJobIn):
    id: int
    last_run_at: datetime | None


class AdminSettingsIn(BaseModel):
    telegram_ids: list[str] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)


class AdminSettingsOut(ORMModel, AdminSettingsIn):
    id: int


class MessageLogIn(BaseModel):
    agent_id: int | None = None
    account_id: int | None = None
    direction: str
    chat_id: str | None = None
    user_id: str | None = None
    sender_id: str | None = None
    message_id: str | None = None
    message_at: datetime | None = None
    text: str
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class MessageLogOut(ORMModel, MessageLogIn):
    id: int
    created_at: datetime


class AgentTaskIn(BaseModel):
    source_agent_id: int | None = None
    target_agent_id: int
    input: dict[str, Any] = Field(default_factory=dict)


class AgentTaskOut(ORMModel, AgentTaskIn):
    id: int
    status: str
    output: dict[str, Any] | None
    error: str | None
    created_at: datetime


class LoginRequest(BaseModel):
    password: str


class TokenResponse(BaseModel):
    token: str
    expires_at: datetime


class TelegramCodeRequest(BaseModel):
    phone: str


class TelegramCodeVerify(BaseModel):
    phone: str
    code: str
    password: str | None = None


class RunRequest(BaseModel):
    message: str
    context: dict[str, Any] = Field(default_factory=dict)


class LlmProfileBody(BaseModel):
    name: str
    provider: Literal["openai", "deepseek", "custom-openai-compatible"]
    base_url: str | None = None
    api_key: str = ""
    clear_api_key: bool = False
    default_model: str
    enabled: bool = True


class RuntimeSettingsBody(BaseModel):
    search_provider: Literal["searxng", "ddg"] = "ddg"
    searxng_url: str | None = None
    memory_enabled: bool = False
    memory_backend: Literal["local", "platform"] = "local"
    mem0_api_key: str = ""
    clear_mem0_api_key: bool = False
    qdrant_url: str | None = None
    memory_llm_profile_id: int | None = None
    typing_min_seconds: float = Field(0.4, ge=0, le=60)
    typing_max_seconds: float = Field(2.5, ge=0, le=60)
    typing_jitter_seconds: float = Field(0.35, ge=0, le=60)
    typing_chunk_size: int = Field(3800, ge=256, le=4096)
    typing_presence: bool = False
    task_workers: int = Field(1, ge=0, le=64)
    max_tool_rounds: int = Field(8, ge=1, le=100)
    timezone: str = "UTC"
    telegram_history_limit: int = Field(100, ge=1, le=500)
    recent_context_messages: int = Field(30, ge=1, le=500)
    context_max_chars: int = Field(30000, ge=1000, le=200000)
    summarization_enabled: bool = True
    summarize_after_messages: int = Field(80, ge=2, le=5000)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value
