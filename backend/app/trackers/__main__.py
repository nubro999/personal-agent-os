"""모든 수집기를 차례로 돌린다. 하나가 실패해도 나머지는 돈다.

DB 엔진이 이벤트 루프에 묶이므로 수집기들을 한 루프 안에서 돌린다.

실행: uv run python -m app.trackers
"""

import asyncio
import logging

from app.persistence.db import get_engine
from app.trackers import claude_code, maintenance

log = logging.getLogger(__name__)


async def run_all() -> None:
    # 2325 건이 먼저 Task가 돼 있어야 Claude Code 세션을 CS 번호로 그 Task에 이을 수 있다.
    for tracker in (maintenance, claude_code):
        try:
            await tracker.run_once()
        except Exception:
            log.exception("%s tracker crashed", tracker.__name__)
    await get_engine().dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run_all())


if __name__ == "__main__":
    main()
