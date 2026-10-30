from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAO_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://pao:pao@127.0.0.1:5433/pao"
    timezone: str = "Asia/Seoul"
    # 인증이 붙기 전까지 로컬 단일 사용자. 클라이언트 입력이나 LLM 출력에서 받지 않는다.
    owner_id: str = "me"
    llm_model: str = "claude-sonnet-5-5"
    claude_projects_dir: str = "~/.claude/projects"
    # 처음 돌릴 때 이만큼 지난 세션까지만 본다. 이후에는 읽은 위치부터 이어서 본다.
    tracker_lookback_hours: int = 24
    # 마지막 활동 뒤 이만큼 조용한 세션만 읽는다. 진행 중인 대화를 반쯤 읽지 않기 위해서다.
    tracker_idle_minutes: int = 20


@lru_cache
def get_settings() -> Settings:
    return Settings()
