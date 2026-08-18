from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ICE_", env_file=".env", extra="ignore")

    app_name: str = "Ice.agent"
    environment: str = "development"
    database_url: str = "sqlite+aiosqlite:///./ice-agent.db"
    secret_key: SecretStr = SecretStr("change-me-in-production")
    access_token_hours: int = 24
    admin_password: SecretStr = SecretStr("admin")
    admin_telegram_ids: str = ""
    session_dir: Path = Path("./data/sessions")
    backup_dir: Path = Path("./data/backups")
    # Public URL of this ice.agent API as seen from the Cursor PC (for customer file downloads).
    public_base_url: str = ""
    # SIP UA (pjsua2) — local bind + RTP pool for parallel calls
    sip_bind_port: int = 5060
    sip_rtp_port_min: int = 10000
    sip_rtp_port_max: int = 10199
    sip_stun_server: str = ""
    sip_public_ip: str = ""
    sip_ring_delay_seconds: float = 4.0
    sip_wait_first_rtp_seconds: float = 5.0
    # Fallback HTTP(S) proxy for OpenAI Realtime WSS when LLM profile has no http_proxy
    openai_http_proxy: str = ""

    @property
    def admin_ids(self) -> set[int]:
        return {int(value.strip()) for value in self.admin_telegram_ids.split(",") if value.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
