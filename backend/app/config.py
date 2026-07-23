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

    @property
    def admin_ids(self) -> set[int]:
        return {int(value.strip()) for value in self.admin_telegram_ids.split(",") if value.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
