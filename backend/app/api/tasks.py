"""보드 화면용 Task API. 127.0.0.1에만 띄운다. 인증이 붙기 전까지 외부에 열지 않는다."""

import uuid
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.domain.tasks.service import (
    Evidence,
    TaskChanges,
    TaskNotFound,
    VersionConflict,
    dismiss_suggestions,
    list_pending_suggestions,
    list_tasks,
    update_task,
)
from app.domain.tasks.values import TaskStatus
from app.persistence.db import get_session
from app.persistence.models import Task

router = APIRouter(prefix="/api")

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


class TaskOut(BaseModel):
    id: uuid.UUID
    version: int
    title: str
    project: str | None
    status: str
    priority: str
    due_date: date | None
    source: str
    updated_at: datetime
    suggestion: str | None = None

    @classmethod
    def of(cls, task: Task, suggestion: str | None = None) -> "TaskOut":
        return cls(
            id=task.id,
            version=task.version,
            title=task.title,
            project=task.project,
            status=task.status,
            priority=task.priority,
            due_date=task.due_date,
            source=task.source,
            updated_at=task.updated_at,
            suggestion=suggestion,
        )


class StatusChange(BaseModel):
    status: TaskStatus
    version: int
    request_id: str


@router.get("/tasks")
async def tasks(session: SessionDep, settings: SettingsDep) -> list[TaskOut]:
    owner_id = settings.owner_id
    suggestions = {
        task.id: s.explanation or "근거 없음"
        for s, task in await list_pending_suggestions(session, owner_id=owner_id)
    }
    open_tasks = await list_tasks(session, owner_id=owner_id)
    return [TaskOut.of(t, suggestions.get(t.id)) for t in open_tasks]


@router.post("/tasks/{task_id}/status")
async def change_status(
    task_id: uuid.UUID, change: StatusChange, session: SessionDep, settings: SettingsDep
) -> TaskOut:
    try:
        async with session.begin():
            result = await update_task(
                session,
                owner_id=settings.owner_id,
                task_id=task_id,
                expected_version=change.version,
                request_id=f"web:{change.request_id}",
                actor="board",
                changes=TaskChanges(status=Evidence(change.status, "explicit")),
            )
    except TaskNotFound as exc:
        raise HTTPException(404, "task not found") from exc
    except VersionConflict as exc:
        raise HTTPException(409, "task changed, reload") from exc
    return TaskOut.of(result.task)


@router.post("/tasks/{task_id}/suggestion/dismiss")
async def dismiss(task_id: uuid.UUID, session: SessionDep, settings: SettingsDep) -> dict[str, int]:
    async with session.begin():
        count = await dismiss_suggestions(session, owner_id=settings.owner_id, task_id=task_id)
    return {"dismissed": count}
