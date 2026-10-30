"""모든 수집기를 차례로 돌리고 결과를 기록한다. 하나가 실패해도 나머지는 돈다.

DB 엔진이 이벤트 루프에 묶이므로 수집기들을 한 루프 안에서 돌린다.

실행: uv run python -m app.trackers
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, insert

from app.persistence.db import get_engine, get_sessionmaker
from app.persistence.models import TrackerRun
from app.trackers import claude_code, housekeeping, maintenance, slack

log = logging.getLogger(__name__)

RUN_RETENTION = timedelta(days=14)


async def run_all() -> None:
    # 2325 건이 먼저 Task가 돼 있어야 Slack 스레드와 Claude Code 세션을 CS 번호로 이을 수 있다.
    for name, tracker in (
        ("2325", maintenance),
        ("Slack", slack),
        ("Claude Code", claude_code),
        ("정리", housekeeping),
    ):
        try:
            problem = await tracker.run_once()
        except Exception as exc:
            log.exception("%s tracker crashed", name)
            problem = f"{type(exc).__name__}: {exc}"[:200]
        async with get_sessionmaker().begin() as session:
            await session.execute(
                insert(TrackerRun).values(name=name, ok=problem is None, detail=problem)
            )
            await session.execute(
                delete(TrackerRun).where(TrackerRun.finished_at < datetime.now(UTC) - RUN_RETENTION)
            )
    await get_engine().dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run_all())


if __name__ == "__main__":
    main()
