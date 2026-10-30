"""쌓인 Task 정리. 지우지 않고 완료 제안만 남긴다.

- Claude Code 수집기가 만든 Task는 2325처럼 닫아 줄 곳이 없다. 며칠 움직임이 없으면 끝났는지 묻는다.
- 출처끼리 겹친 Task는 하루 한 번 Claude에게 찾게 하고, 겹친 쪽에 제안을 남긴다.
"""

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import get_settings
from app.domain.tasks.service import list_tasks, suggest_completion
from app.llm.base import LLMError, StructuredLLM
from app.llm.factory import get_llm
from app.persistence.db import get_sessionmaker
from app.persistence.models import ActivitySession, Task
from app.trackers.prompts import DUPLICATE_PROMPT

log = logging.getLogger(__name__)

SOURCE = "housekeeping"
STALE_DAYS = 3


class DuplicatePair(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duplicate_ref: str
    keep_ref: str


class Duplicates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pairs: list[DuplicatePair] = Field(description="확실한 중복만")


async def suggest_stale(*, owner_id: str, now: datetime) -> int:
    cutoff = now - timedelta(days=STALE_DAYS)
    created = 0
    async with get_sessionmaker().begin() as session:
        stale = await session.scalars(
            select(Task).where(
                Task.owner_id == owner_id,
                Task.source == "claude_code",
                Task.status.in_(("todo", "in_progress")),
                Task.updated_at < cutoff,
            )
        )
        for task in stale.all():
            created += await suggest_completion(
                session,
                owner_id=owner_id,
                task_id=task.id,
                source=SOURCE,
                source_reference=f"stale:{task.updated_at.date().isoformat()}",
                explanation=f"{STALE_DAYS}일 넘게 움직임이 없어요",
            )
    return created


async def suggest_duplicates(*, owner_id: str, llm: StructuredLLM, today: str) -> int:
    async with get_sessionmaker()() as session:
        done_today = await session.scalar(
            select(ActivitySession.summary).where(
                ActivitySession.owner_id == owner_id,
                ActivitySession.source == SOURCE,
                ActivitySession.external_id == "duplicates",
            )
        )
        if done_today == today:
            return 0
        tasks = await list_tasks(session, owner_id=owner_id)
    refs = {f"T{i}": t for i, t in enumerate(tasks, 1)}
    created = 0
    if len(refs) > 1:
        lines = [
            f"{ref}: {t.title}" + (f" [{t.project}]" if t.project else "") + f" (출처 {t.source})"
            for ref, t in refs.items()
        ]
        result = await llm.generate(
            system=DUPLICATE_PROMPT, prompt="\n".join(lines), output=Duplicates
        )
        async with get_sessionmaker().begin() as session:
            for pair in result.pairs:
                duplicate, keep = refs.get(pair.duplicate_ref), refs.get(pair.keep_ref)
                # 2325 Task는 2325 상태를 따라가므로 여기서 닫지 않는다.
                if duplicate is None or keep is None or duplicate.source == "2325":
                    continue
                if duplicate.id == keep.id:
                    continue
                created += await suggest_completion(
                    session,
                    owner_id=owner_id,
                    task_id=duplicate.id,
                    source=SOURCE,
                    source_reference=f"dup:{keep.id}",
                    explanation=f"'{keep.title}'와 같은 일 같아요",
                )
    async with get_sessionmaker().begin() as session:
        values: dict[str, Any] = {"summary": today}
        await session.execute(
            pg_insert(ActivitySession)
            .values(owner_id=owner_id, source=SOURCE, external_id="duplicates", **values)
            .on_conflict_do_update(
                index_elements=["owner_id", "source", "external_id"], set_=values
            )
        )
    return created


async def run_once() -> str | None:
    settings = get_settings()
    now = datetime.now(ZoneInfo(settings.timezone))
    stale = await suggest_stale(owner_id=settings.owner_id, now=now)
    try:
        duplicates = await suggest_duplicates(
            owner_id=settings.owner_id, llm=get_llm(), today=now.date().isoformat()
        )
    except LLMError as exc:
        return f"중복 찾기 실패: {exc}"
    log.info("housekeeping: stale=%d duplicates=%d", stale, duplicates)
    return None
