from datetime import date
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.tasks.service import (
    Evidence,
    NewTask,
    TaskChanges,
    TaskNotFound,
    VersionConflict,
    create_task,
    list_tasks,
    update_task,
)
from app.persistence.models import TaskEvent, TaskFieldEvidence


async def _create(session: AsyncSession, request_id: str, new: NewTask) -> None:
    await create_task(session, owner_id="me", request_id=request_id, actor="test", new=new)


async def test_create_records_event_and_field_origins(session: AsyncSession) -> None:
    result = await create_task(
        session,
        owner_id="me",
        request_id="req-1",
        actor="test",
        new=NewTask(
            title=Evidence("2325 Eval 실행", "inferred"),
            project=Evidence("2325", "explicit"),
            due_date=Evidence(date(2026, 10, 31), "explicit"),
        ),
    )

    assert result.created
    assert (result.task.priority, result.task.due_date) == ("medium", date(2026, 10, 31))
    events = (await session.scalars(select(TaskEvent))).all()
    assert [(e.event_type, e.after_data and e.after_data["due_date"]) for e in events] == [
        ("created", "2026-10-31")
    ]
    evidence = {
        e.field_name: (e.field_value, e.origin, e.confirmed)
        for e in await session.scalars(
            select(TaskFieldEvidence).where(TaskFieldEvidence.task_id == result.task.id)
        )
    }
    assert evidence == {
        "title": ("2325 Eval 실행", "inferred", False),
        "project": ("2325", "explicit", True),
        "due_date": ("2026-10-31", "explicit", True),
        "priority": ("medium", "default", False),
    }


async def test_same_request_returns_existing_task_without_new_event(session: AsyncSession) -> None:
    new = NewTask(title=Evidence("보고서", "inferred"))
    first = await create_task(session, owner_id="me", request_id="req-1", actor="t", new=new)
    second = await create_task(session, owner_id="me", request_id="req-1", actor="t", new=new)

    assert (first.created, second.created) == (True, False)
    assert second.task.id == first.task.id
    assert len((await session.scalars(select(TaskEvent))).all()) == 1


async def test_list_filters_by_due_range_and_hides_done(session: AsyncSession) -> None:
    await _create(
        session,
        "a",
        NewTask(
            title=Evidence("이번 주", "inferred"), due_date=Evidence(date(2026, 10, 30), "explicit")
        ),
    )
    await _create(
        session,
        "b",
        NewTask(
            title=Evidence("다음 주", "inferred"), due_date=Evidence(date(2026, 11, 3), "explicit")
        ),
    )
    await _create(session, "c", NewTask(title=Evidence("마감 없음", "inferred")))
    done = await create_task(
        session,
        owner_id="me",
        request_id="d",
        actor="t",
        new=NewTask(
            title=Evidence("끝난 일", "inferred"), due_date=Evidence(date(2026, 10, 29), "explicit")
        ),
    )
    done.task.status = "done"
    await session.flush()

    week = await list_tasks(
        session, owner_id="me", due_from=date(2026, 10, 26), due_to=date(2026, 11, 1)
    )
    everything = await list_tasks(session, owner_id="me")

    assert [t.title for t in week] == ["이번 주"]
    assert [t.title for t in everything] == ["이번 주", "다음 주", "마감 없음"]
    assert await list_tasks(session, owner_id="other") == []


async def _new(session: AsyncSession, title: str = "보고서") -> tuple[Any, int]:
    result = await create_task(
        session,
        owner_id="me",
        request_id=f"create-{title}",
        actor="t",
        new=NewTask(title=Evidence(title, "inferred")),
    )
    return result.task.id, result.task.version


async def test_complete_sets_completed_at_and_reopen_clears_it(session: AsyncSession) -> None:
    task_id, version = await _new(session)

    done = await update_task(
        session,
        owner_id="me",
        task_id=task_id,
        expected_version=version,
        request_id="r1",
        actor="t",
        changes=TaskChanges(status=Evidence("done", "explicit")),
    )
    assert done.applied and done.task.status == "done" and done.task.completed_at is not None
    assert done.task.version == version + 1

    reopened = await update_task(
        session,
        owner_id="me",
        task_id=task_id,
        expected_version=done.task.version,
        request_id="r2",
        actor="t",
        changes=TaskChanges(status=Evidence("todo", "explicit")),
    )
    assert reopened.task.completed_at is None
    events = (
        await session.scalars(
            select(TaskEvent.event_type)
            .where(TaskEvent.task_id == task_id)
            .order_by(TaskEvent.created_at, TaskEvent.event_type)
        )
    ).all()
    assert sorted(events) == ["completed", "created", "reopened"]


async def test_postpone_records_before_after_and_evidence(session: AsyncSession) -> None:
    task_id, version = await _new(session)

    result = await update_task(
        session,
        owner_id="me",
        task_id=task_id,
        expected_version=version,
        request_id="r1",
        actor="t",
        changes=TaskChanges(due_date=Evidence(date(2026, 11, 6), "explicit")),
        reason="금요일까지 미뤄줘",
    )

    assert result.changed == ("due_date",)
    event = await session.scalar(
        select(TaskEvent).where(TaskEvent.task_id == task_id, TaskEvent.event_type == "updated")
    )
    assert event is not None and event.reason == "금요일까지 미뤄줘"
    assert (event.before_data or {})["due_date"] is None
    assert (event.after_data or {})["due_date"] == "2026-11-06"


async def test_same_request_is_not_applied_twice(session: AsyncSession) -> None:
    task_id, version = await _new(session)
    changes = TaskChanges(priority=Evidence("high", "explicit"))

    first = await update_task(
        session,
        owner_id="me",
        task_id=task_id,
        expected_version=version,
        request_id="r1",
        actor="t",
        changes=changes,
    )
    second = await update_task(
        session,
        owner_id="me",
        task_id=task_id,
        expected_version=version,
        request_id="r1",
        actor="t",
        changes=changes,
    )

    assert (first.applied, second.applied) == (True, False)
    assert second.task.version == version + 1


async def test_stale_version_conflicts(session: AsyncSession) -> None:
    task_id, version = await _new(session)
    await update_task(
        session,
        owner_id="me",
        task_id=task_id,
        expected_version=version,
        request_id="r1",
        actor="t",
        changes=TaskChanges(priority=Evidence("high", "explicit")),
    )

    with pytest.raises(VersionConflict):
        await update_task(
            session,
            owner_id="me",
            task_id=task_id,
            expected_version=version,
            request_id="r2",
            actor="t",
            changes=TaskChanges(priority=Evidence("low", "explicit")),
        )


async def test_other_owner_cannot_update(session: AsyncSession) -> None:
    task_id, version = await _new(session)

    with pytest.raises(TaskNotFound):
        await update_task(
            session,
            owner_id="other",
            task_id=task_id,
            expected_version=version,
            request_id="r1",
            actor="t",
            changes=TaskChanges(status=Evidence("done", "explicit")),
        )


async def test_no_op_change_does_not_bump_version(session: AsyncSession) -> None:
    task_id, version = await _new(session)

    result = await update_task(
        session,
        owner_id="me",
        task_id=task_id,
        expected_version=version,
        request_id="r1",
        actor="t",
        changes=TaskChanges(status=Evidence("todo", "explicit")),
    )

    assert not result.applied and result.task.version == version
