from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAO_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://pao:pao@127.0.0.1:5433/pao"
    timezone: str = "Asia/Seoul"


@lru_cache
def get_settings() -> Settings:
    return Settings()
