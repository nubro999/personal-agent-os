"""브리핑을 만들어 파일로 남기고 Windows 알림을 띄운다.

Slack 봇을 붙이기 전까지의 전달 경로다. 채팅에서 "브리핑"이라고 해도 같은 내용이 나온다.

실행: uv run python -m app.briefing
"""

import asyncio
import logging
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.domain.tasks.briefing import Briefing, build_briefing
from app.persistence.db import get_engine, get_sessionmaker

log = logging.getLogger(__name__)


async def _build(now: datetime) -> Briefing:
    async with get_sessionmaker()() as session:
        briefing = await build_briefing(
            session, owner_id=get_settings().owner_id, today=now.date(), now=now
        )
    await get_engine().dispose()
    return briefing


def _notify(summary: str) -> None:
    settings = get_settings()
    try:
        subprocess.run(
            [
                settings.powershell_path,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                settings.toast_script,
                "-Title",
                "오늘 브리핑",
                "-Body",
                summary,
            ],
            check=True,
            timeout=30,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # 알림이 실패해도 브리핑 파일과 채팅으로 볼 수 있다.
        log.warning("toast failed: %s", exc)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    now = datetime.now(ZoneInfo(settings.timezone))
    briefing = asyncio.run(_build(now))
    settings.briefing_dir.mkdir(parents=True, exist_ok=True)
    path = settings.briefing_dir / f"{now.date().isoformat()}.md"
    path.write_text(briefing.text + "\n", encoding="utf-8")
    _notify(briefing.summary)
    log.info("briefing: %s (%s)", briefing.summary, path)


if __name__ == "__main__":
    main()
