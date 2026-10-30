import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import delete, select

from app.config import get_settings
from app.domain.tasks.service import Evidence, TaskChanges, update_task
from app.persistence.db import get_sessionmaker
from app.persistence.models import ActivitySession, Task, TaskEvent, TaskFieldEvidence
from app.trackers import maintenance
from app.trackers.maintenance import MaintItem

NOW = datetime(2026, 10, 30, 12, 0)


def item(**values: Any) -> MaintItem:
    base: dict[str, Any] = {
        "item_key": "Xcs_1",
        "human_id": "CS-0004387",
        "company": "그로우어스",
        "feedback": "사은품 스케일러 120g\r\n재고소진으로 데미지 체험키트로 변경되었습니다. 답변",
        "status_group": "할 것",
        "received_at": datetime(2026, 10, 30, 9, 0),
        "stage": None,
    }
    return MaintItem(**(base | values))


@pytest.mark.parametrize(
    ("stage", "status_group", "expected"),
    [
        (None, "할 것", "in_progress"),
        (None, "끝남", "done"),
        ("fixing", "끝남", "in_progress"),
        ("communicating", "할 것", "blocked"),
        ("review", "할 것", "done"),
        ("deployed", "할 것", "done"),
        ("rejected", "할 것", "cancelled"),
    ],
)
def test_stage_wins_over_ax_status(stage: str | None, status_group: str, expected: str) -> None:
    assert item(stage=stage, status_group=status_group).status == expected


def test_title_is_human_id_and_short_feedback() -> None:
    # 줄바꿈은 공백 하나로 접고, 40자를 넘으면 자른다.
    title = item().title
    assert (
        title == "CS-0004387 사은품 스케일러 120g 재고소진으로 데미지 체험키트로 변경되었습니다.…"
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
        for model in (TaskFieldEvidence, TaskEvent):
            await session.execute(delete(model).where(model.task_id.in_(ids)))
        await session.execute(delete(Task).where(Task.owner_id == owner))
        await session.execute(delete(ActivitySession).where(ActivitySession.owner_id == owner))


async def _tasks(owner: str) -> dict[str, tuple[str, str | None]]:
    async with get_sessionmaker()() as session:
        tasks = await session.scalars(select(Task).where(Task.owner_id == owner))
        return {t.request_id or "": (t.status, t.project) for t in tasks}


async def test_creates_recent_open_items_only(owner: str) -> None:
    items = [
        item(item_key="Xcs_new"),
        item(item_key="Xcs_old", received_at=datetime(2026, 8, 1)),
        item(item_key="Xcs_done", stage="deployed"),
    ]

    stats = await maintenance.run(items, now=NOW)

    assert (stats.created, stats.skipped_old) == (1, 1)
    assert await _tasks(owner) == {"2325:Xcs_new": ("in_progress", "그로우어스")}


async def test_follows_2325_state_changes(owner: str) -> None:
    await maintenance.run([item()], now=NOW)

    await maintenance.run([item(stage="communicating")], now=NOW)
    assert await _tasks(owner) == {"2325:Xcs_1": ("blocked", "그로우어스")}

    stats = await maintenance.run([item(stage="deployed")], now=NOW)
    assert stats.updated == 1
    assert await _tasks(owner) == {"2325:Xcs_1": ("done", "그로우어스")}


async def test_unchanged_2325_state_does_not_undo_user_change(owner: str) -> None:
    await maintenance.run([item()], now=NOW)
    async with get_sessionmaker().begin() as session:
        task = await session.scalar(select(Task).where(Task.owner_id == owner))
        assert task is not None
        await update_task(
            session,
            owner_id=owner,
            task_id=task.id,
            expected_version=task.version,
            request_id="user",
            actor="t",
            changes=TaskChanges(status=Evidence("done", "explicit")),
        )

    stats = await maintenance.run([item()], now=NOW)

    assert stats.updated == 0
    assert await _tasks(owner) == {"2325:Xcs_1": ("done", "그로우어스")}
