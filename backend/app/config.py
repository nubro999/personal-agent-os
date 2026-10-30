from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAO_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://pao:pao@127.0.0.1:5433/pao"
    timezone: str = "Asia/Seoul"
    # 인증이 붙기 전까지 로컬 단일 사용자. 클라이언트 입력이나 LLM 출력에서 받지 않는다.
    owner_id: str = "me"
    llm_model: str = "claude-sonnet-5-5"
    claude_projects_dir: Path = Path("~/.claude/projects")
    # 처음 돌릴 때 이만큼 지난 세션까지만 본다. 이후에는 읽은 위치부터 이어서 본다.
    tracker_lookback_hours: int = 24
    # 마지막 활동 뒤 이만큼 조용한 세션만 읽는다. 진행 중인 대화를 반쯤 읽지 않기 위해서다.
    tracker_idle_minutes: int = 20
    # 2325(maintenance_list). DB 접속 정보는 그 저장소의 .env를 실행할 때 읽는다.
    maint_env_file: Path = Path("~/glab/glab_mt/maintenance_list/.env")
    maint_assignee_name: str = "신형섭"
    # 처음 가져올 때 이 기간 안에 접수된 열린 건만 Task로 만든다.
    maint_lookback_days: int = 30

    @field_validator("claude_projects_dir", "maint_env_file", mode="after")
    @classmethod
    def _expand_home(cls, value: Path) -> Path:
        return value.expanduser()


@lru_cache
def get_settings() -> Settings:
    return Settings()
