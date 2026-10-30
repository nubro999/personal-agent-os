import uuid

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.base import Base
from app.persistence.models import Task, TaskFieldEvidence, TaskLink


async def _create_task(session: AsyncSession, **values: object) -> uuid.UUID:
    result = await session.execute(
        insert(Task).values(owner_id="me", title="2325 Eval", **values).returning(Task.id)
    )
    return result.scalar_one()


async def test_migration_matches_models(session: AsyncSession) -> None:
    def diff(sync_conn: Connection) -> list[object]:
        context = MigrationContext.configure(sync_conn)
        changes: list[object] = compare_metadata(context, Base.metadata)
        return changes

    conn = await session.connection()
    assert await conn.run_sync(diff) == []


async def test_task_defaults(session: AsyncSession) -> None:
    task_id = await _create_task(session)

    task = await session.scalar(select(Task).where(Task.id == task_id))
    assert task is not None
    assert (task.status, task.priority, task.version, task.source) == (
        "todo",
        "medium",
        1,
        "natural_language",
    )
    assert task.due_date is None and task.due_at is None


async def test_same_request_id_cannot_create_two_tasks(session: AsyncSession) -> None:
    await _create_task(session, request_id="req-1")

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await _create_task(session, request_id="req-1")


async def test_request_id_is_scoped_per_owner(session: AsyncSession) -> None:
    await _create_task(session, request_id="req-1")
    await session.execute(insert(Task).values(owner_id="other", title="x", request_id="req-1"))


@pytest.mark.parametrize(
    "values",
    [
        {"status": "archived"},
        {"priority": "critical"},
        {"estimated_minutes": 0},
    ],
)
async def test_task_check_constraints(session: AsyncSession, values: dict[str, object]) -> None:
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await _create_task(session, **values)


async def test_field_evidence_origin_is_constrained(session: AsyncSession) -> None:
    task_id = await _create_task(session)

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                insert(TaskFieldEvidence).values(
                    task_id=task_id, field_name="priority", field_value="high", origin="guessed"
                )
            )


async def test_task_cannot_link_to_itself(session: AsyncSession) -> None:
    task_id = await _create_task(session)

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                insert(TaskLink).values(
                    parent_task_id=task_id, child_task_id=task_id, relation="subtask"
                )
            )
