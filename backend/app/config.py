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
    # 2325(maintenance_list). 계정은 그 저장소의 .env를 실행할 때 읽고,
    # 접속은 전용 터널(pao-2325-tunnel)로 한다. run.sh가 꺼져 있어도 된다.
    maint_db_port: int = 13316
    maint_env_file: Path = Path("~/glab/glab_mt/maintenance_list/.env")
    maint_assignee_name: str = "신형섭"
    # 처음 가져올 때 이 기간 안에 접수된 열린 건만 Task로 만든다.
    maint_lookback_days: int = 30
    # Slack. 내 계정 user token이라 내 채널, DM, 그룹 DM이 보인다. 쓰기 API는 부르지 않는다.
    slack_user_id: str = "U0BD9BYJXB8"
    slack_token_file: Path = Path("~/.config/genlab-overmind/secrets.json")
    slack_token_key: str = "SLACK_USER_TOKEN"
    slack_lookback_days: int = 3
    slack_team_id: str = "T05NS1KHKUH"
    # 건 카드 스레드가 서는 채널(유지보수-고객성공팀). 이 채널의 건 초안은 건 회신 에이전트가 쓴다.
    slack_cs_channel: str = "C0A1CV8572S"
    # 건 카드 스레드에서 나를 부른 사람이 없을 때 멘션할 고객사 소통 담당자
    slack_cs_contact: str = "U0AMDBKA1T6"
    # 에이전트 결과가 나오면 부르지 않아도 회신 초안을 미리 쓰는 브랜드(2325 db_name)
    case_reply_brands: tuple[str, ...] = ("hdex", "mommycare")
    # 수집기 한 번에 미리 쓰는 초안 수. 건마다 Agent SDK로 30초~2분 걸린다.
    case_reply_limit: int = 3
    # 다른 브랜드는 에이전트를 돌리지 않고 이 문구를 미리 띄운다
    case_reply_default: str = "수정완료했습니다"
    # 한 번 실행에 만드는 답변 초안 수. Claude 호출 수를 묶어 둔다.
    slack_draft_limit: int = 5
    briefing_dir: Path = Path("~/.local/state/personal-agent-os/briefings")
    # 온톨로지. 회사 자료라 이 저장소가 아니라 glab_mt 쪽에 둔다.
    ontology_dir: Path = Path("~/glab/glab_mt/_ontology")
    brand_context_dir: Path = Path("~/glab/glab_mt/auto_cs_maintenance/brands/context")
    # 문제 턴을 찾는 회사 코드(tools.chat.target.find)를 그대로 쓴다.
    auto_cs_dir: Path = Path("~/glab/glab_mt/auto_cs_maintenance")
    web_dist_dir: Path = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    toast_script: str = (
        r"\\wsl.localhost\Ubuntu\home\nubroo\projects\personal-agent-os"
        r"\infra\windows\toast.ps1"
    )
    powershell_path: str = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

    @field_validator(
        "ontology_dir",
        "brand_context_dir",
        "auto_cs_dir",
        "claude_projects_dir",
        "maint_env_file",
        "slack_token_file",
        "briefing_dir",
        mode="after",
    )
    @classmethod
    def _expand_home(cls, value: Path) -> Path:
        return value.expanduser()


@lru_cache
def get_settings() -> Settings:
    return Settings()
