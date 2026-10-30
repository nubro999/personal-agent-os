import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select, update

from app.config import get_settings
from app.domain.tasks.service import Evidence, NewTask, create_task, list_pending_suggestions
from app.llm.base import M
from app.persistence.db import get_sessionmaker
from app.persistence.models import (
    ActivitySession,
    Task,
    TaskEvent,
    TaskFieldEvidence,
    TaskSuggestion,
)
from app.trackers.housekeeping import (
    DuplicatePair,
    Duplicates,
    suggest_duplicates,
    suggest_stale,
)


@pytest.fixture
def owner(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    owner_id = f"test-{uuid.uuid4()}"
    monkeypatch.setattr(get_settings(), "owner_id", owner_id)
    yield owner_id


@pytest.fixture(autouse=True)
async def cleanup(owner: str) -> Any:
    yield
    async with get_sessionmaker().begin() as session:
        ids = select(Task.id).where(Task.owner_id == owner).scalar_subquery()
        for model in (TaskSuggestion, TaskFieldEvidence, TaskEvent):
            await session.execute(delete(model).where(model.task_id.in_(ids)))
        await session.execute(delete(Task).where(Task.owner_id == owner))
        await session.execute(delete(ActivitySession).where(ActivitySession.owner_id == owner))


async def _seed(owner: str, title: str, source: str) -> uuid.UUID:
    async with get_sessionmaker().begin() as session:
        result = await create_task(
            session,
            owner_id=owner,
            request_id=f"{source}:{title}",
            actor="t",
            new=NewTask(title=Evidence(title, "external"), source=source),
        )
    return result.task.id


async def _suggested(owner: str) -> dict[str, str | None]:
    async with get_sessionmaker()() as session:
        rows = await list_pending_suggestions(session, owner_id=owner)
    return {task.title: s.explanation for s, task in rows}


class FakeLLM:
    def __init__(self, *pairs: DuplicatePair) -> None:
        self.pairs = list(pairs)
        self.calls = 0

    async def generate(self, *, system: str, prompt: str, output: type[M]) -> M:
        self.calls += 1
        return Duplicates(pairs=self.pairs)  # type: ignore[return-value]


async def test_stale_claude_task_gets_one_suggestion(owner: str) -> None:
    old = await _seed(owner, "오래된 일", "claude_code")
    await _seed(owner, "새 일", "claude_code")
    await _seed(owner, "2325 일", "2325")
    now = datetime.now(UTC)
    async with get_sessionmaker().begin() as session:
        await session.execute(
            update(Task)
            .where(Task.owner_id == owner, Task.title != "새 일")
            .values(updated_at=now - timedelta(days=5))
        )

    first = await suggest_stale(owner_id=owner, now=now)
    second = await suggest_stale(owner_id=owner, now=now)

    assert (first, second) == (1, 0)
    assert old is not None
    assert list(await _suggested(owner)) == ["오래된 일"]


async def test_duplicates_run_once_a_day_and_never_close_2325(owner: str) -> None:
    await _seed(owner, "CS-0004390 중복 인사", "2325")
    await _seed(owner, "에이치덱스 중복 인사 분석", "claude_code")
    llm = FakeLLM(
        DuplicatePair(duplicate_ref="T1", keep_ref="T2"),
        DuplicatePair(duplicate_ref="T2", keep_ref="T1"),
    )

    created = await suggest_duplicates(owner_id=owner, llm=llm, today="2026-10-30")
    again = await suggest_duplicates(owner_id=owner, llm=llm, today="2026-10-30")

    assert (created, again, llm.calls) == (1, 0, 1)
    assert await _suggested(owner) == {
        "에이치덱스 중복 인사 분석": "'CS-0004390 중복 인사'와 같은 일 같아요"
    }
