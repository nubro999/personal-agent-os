"""Task 생성, 조회, 변경. 변경 함수는 커밋하지 않는다. 호출하는 쪽이 한 트랜잭션으로 감싼다."""

import uuid
from dataclasses import dataclass, fields
from datetime import date, timedelta
from typing import Any

from sqlalchemy import case, exists, func, insert, nulls_last, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.tasks.values import FieldOrigin, Priority, TaskStatus
from app.persistence.models import Task, TaskEvent, TaskFieldEvidence, TaskSuggestion


class TaskNotFound(LookupError):
    pass


class VersionConflict(RuntimeError):
    """읽은 뒤에 다른 변경이 먼저 반영됐다. 다시 읽고 판단해야 한다."""


@dataclass(frozen=True)
class Evidence[T]:
    value: T
    origin: FieldOrigin
    explanation: str | None = None
    source_reference: str | None = None


@dataclass(frozen=True)
class NewTask:
    title: Evidence[str]
    description: Evidence[str] | None = None
    project: Evidence[str] | None = None
    priority: Evidence[Priority] | None = None
    due_date: Evidence[date] | None = None
    status: Evidence[TaskStatus] | None = None
    source: str = "natural_language"


@dataclass(frozen=True)
class TaskChanges:
    """None인 필드는 바꾸지 않는다."""

    title: Evidence[str] | None = None
    description: Evidence[str] | None = None
    project: Evidence[str] | None = None
    priority: Evidence[Priority] | None = None
    due_date: Evidence[date] | None = None
    status: Evidence[TaskStatus] | None = None


@dataclass(frozen=True)
class CreateResult:
    task: Task
    created: bool


@dataclass(frozen=True)
class UpdateResult:
    task: Task
    applied: bool
    changed: tuple[str, ...] = ()


def _json_value(value: Any) -> Any:
    return value.isoformat() if isinstance(value, date) else value


def _snapshot(task: Task) -> dict[str, Any]:
    names = ("title", "description", "project", "status", "priority", "due_date", "due_at")
    return {name: _json_value(getattr(task, name)) for name in names}


async def _record(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    request_id: str,
    actor: str,
    event_type: str,
    before: dict[str, Any] | None,
    after: dict[str, Any],
    evidence: dict[str, Evidence[Any]],
    reason: str | None = None,
) -> None:
    await session.execute(
        insert(TaskEvent).values(
            task_id=task_id,
            request_id=request_id,
            event_type=event_type,
            actor=actor,
            before_data=before,
            after_data=after,
            reason=reason,
        )
    )
    if evidence:
        await session.execute(
            insert(TaskFieldEvidence),
            [
                {
                    "task_id": task_id,
                    "field_name": name,
                    "field_value": _json_value(field.value),
                    "origin": field.origin,
                    "source_reference": field.source_reference,
                    "explanation": field.explanation,
                    "confirmed": field.origin == "explicit",
                }
                for name, field in evidence.items()
            ],
        )


async def create_task(
    session: AsyncSession, *, owner_id: str, request_id: str, actor: str, new: NewTask
) -> CreateResult:
    """같은 owner의 같은 request_id는 한 번만 만든다. 다시 오면 기존 Task를 돌려준다."""
    priority = new.priority or Evidence[Priority]("medium", "default")
    values: dict[str, Any] = {"title": new.title.value, "priority": priority.value}
    evidence: dict[str, Evidence[Any]] = {"title": new.title, "priority": priority}
    for name in ("description", "project", "due_date", "status"):
        field: Evidence[Any] | None = getattr(new, name)
        if field is not None:
            values[name] = field.value
            evidence[name] = field
    if values.get("status") == "done":
        values["completed_at"] = func.now()

    task_id = await session.scalar(
        pg_insert(Task)
        .values(owner_id=owner_id, request_id=request_id, source=new.source, **values)
        .on_conflict_do_nothing(index_elements=["owner_id", "request_id"])
        .returning(Task.id)
    )
    if task_id is None:
        existing = await session.scalar(
            select(Task).where(Task.owner_id == owner_id, Task.request_id == request_id)
        )
        assert existing is not None
        return CreateResult(task=existing, created=False)

    task = await session.get_one(Task, task_id)
    await _record(
        session,
        task_id=task_id,
        request_id=request_id,
        actor=actor,
        event_type="created",
        before=None,
        after=_snapshot(task),
        evidence=evidence,
    )
    return CreateResult(task=task, created=True)


async def get_task(session: AsyncSession, *, owner_id: str, task_id: uuid.UUID) -> Task:
    task = await session.scalar(select(Task).where(Task.id == task_id, Task.owner_id == owner_id))
    if task is None:
        raise TaskNotFound(task_id)
    return task


def _event_type(before_status: str, new_status: str | None) -> str:
    if new_status == "done":
        return "completed"
    if new_status == "cancelled":
        return "cancelled"
    if new_status is not None and before_status in ("done", "cancelled"):
        return "reopened"
    return "updated"


async def update_task(
    session: AsyncSession,
    *,
    owner_id: str,
    task_id: uuid.UUID,
    expected_version: int,
    request_id: str,
    actor: str,
    changes: TaskChanges,
    reason: str | None = None,
) -> UpdateResult:
    """expected_version이 맞을 때만 반영한다. 같은 request_id로 다시 오면 다시 반영하지 않는다."""
    task = await get_task(session, owner_id=owner_id, task_id=task_id)
    already = await session.scalar(
        select(exists().where(TaskEvent.task_id == task_id, TaskEvent.request_id == request_id))
    )
    if already:
        return UpdateResult(task=task, applied=False)
    if task.version != expected_version:
        raise VersionConflict(task_id)

    values: dict[str, Any] = {}
    evidence: dict[str, Evidence[Any]] = {}
    for f in fields(changes):
        field: Evidence[Any] | None = getattr(changes, f.name)
        if field is not None and getattr(task, f.name) != field.value:
            values[f.name] = field.value
            evidence[f.name] = field
    if not values:
        return UpdateResult(task=task, applied=False)

    if "status" in values:
        values["completed_at"] = func.now() if values["status"] == "done" else None

    before = _snapshot(task)
    updated = await session.scalar(
        update(Task)
        .where(Task.id == task_id, Task.owner_id == owner_id, Task.version == expected_version)
        .values(**values, version=Task.version + 1, updated_at=func.now())
        .returning(Task.id)
        .execution_options(synchronize_session=False)
    )
    if updated is None:
        raise VersionConflict(task_id)
    await session.refresh(task)
    if values.get("status") in ("done", "cancelled"):
        # 끝났으니 남아 있던 완료 제안은 정리한다.
        await session.execute(
            update(TaskSuggestion)
            .where(TaskSuggestion.task_id == task_id, TaskSuggestion.status == "pending")
            .values(status="accepted", decided_at=func.now())
        )

    await _record(
        session,
        task_id=task_id,
        request_id=request_id,
        actor=actor,
        event_type=_event_type(before["status"], values.get("status")),
        before=before,
        after=_snapshot(task),
        evidence=evidence,
        reason=reason,
    )
    return UpdateResult(task=task, applied=True, changed=tuple(evidence))


_PRIORITY_RANK = case({"urgent": 0, "high": 1, "medium": 2, "low": 3}, value=Task.priority, else_=4)


async def list_tasks(
    session: AsyncSession,
    *,
    owner_id: str,
    due_from: date | None = None,
    due_to: date | None = None,
    include_done: bool = False,
) -> list[Task]:
    """기간을 주면 그 안에 마감이 있는 Task만, 안 주면 전부. 마감 빠른 순, 마감 없는 건 뒤로."""
    stmt = select(Task).where(Task.owner_id == owner_id)
    if not include_done:
        stmt = stmt.where(Task.status.not_in(("done", "cancelled")))
    if due_from is not None:
        stmt = stmt.where(Task.due_date >= due_from)
    if due_to is not None:
        stmt = stmt.where(Task.due_date <= due_to)
    stmt = stmt.order_by(nulls_last(Task.due_date.asc()), _PRIORITY_RANK, Task.created_at)
    return list((await session.scalars(stmt)).all())


async def list_recent_candidates(
    session: AsyncSession, *, owner_id: str, closed_within_days: int = 14, limit: int = 50
) -> list[Task]:
    """대상 찾기용 후보. 열린 Task 전부와 최근에 끝내거나 취소한 Task."""
    recent_closed = Task.updated_at >= func.now() - timedelta(days=closed_within_days)
    stmt = (
        select(Task)
        .where(Task.owner_id == owner_id)
        .where(Task.status.not_in(("done", "cancelled")) | recent_closed)
        .order_by(Task.updated_at.desc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


async def suggest_completion(
    session: AsyncSession,
    *,
    owner_id: str,
    task_id: uuid.UUID,
    source: str,
    source_reference: str,
    explanation: str | None,
) -> bool:
    """완료된 것 같다는 제안을 남긴다. 같은 출처로 같은 제안은 한 번만. 새로 남겼으면 True."""
    created = await session.scalar(
        pg_insert(TaskSuggestion)
        .values(
            owner_id=owner_id,
            task_id=task_id,
            kind="complete",
            source=source,
            source_reference=source_reference,
            explanation=explanation,
        )
        .on_conflict_do_nothing(index_elements=["task_id", "kind", "source_reference"])
        .returning(TaskSuggestion.id)
    )
    return created is not None


async def list_pending_suggestions(
    session: AsyncSession, *, owner_id: str
) -> list[tuple[TaskSuggestion, Task]]:
    stmt = (
        select(TaskSuggestion, Task)
        .join(Task, Task.id == TaskSuggestion.task_id)
        .where(
            TaskSuggestion.owner_id == owner_id,
            TaskSuggestion.status == "pending",
            Task.status.not_in(("done", "cancelled")),
        )
        .order_by(TaskSuggestion.created_at)
    )
    return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]
