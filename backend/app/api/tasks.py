"""보드 화면용 Task API. 127.0.0.1에만 띄운다. 인증이 붙기 전까지 외부에 열지 않는다."""

import logging
import time
import uuid
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select, update
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
from app.persistence.models import ReplyDraft, Task
from app.trackers.slack import (
    SendError,
    rewrite_draft,
    send_draft,
    sweep_sent_drafts,
    thread_messages,
)

router = APIRouter(prefix="/api")
log = logging.getLogger(__name__)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


class TaskOut(BaseModel):
    id: uuid.UUID
    version: int
    title: str
    description: str | None
    project: str | None
    status: str
    priority: str
    due_date: date | None
    source: str
    created_at: datetime
    updated_at: datetime
    suggestion: str | None = None

    @classmethod
    def of(cls, task: Task, suggestion: str | None = None) -> "TaskOut":
        return cls(
            id=task.id,
            version=task.version,
            title=task.title,
            description=task.description,
            project=task.project,
            status=task.status,
            priority=task.priority,
            due_date=task.due_date,
            source=task.source,
            created_at=task.created_at,
            updated_at=task.updated_at,
            suggestion=suggestion,
        )


class StatusChange(BaseModel):
    status: TaskStatus
    version: int
    request_id: str


class DraftOut(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID | None
    task_title: str | None
    draft: str
    slack_url: str
    notes: list[str]
    advice: list[str]
    rewriting: bool
    updated_at: datetime
    waiting_since: datetime | None
    reminders: int


class ThreadMessage(BaseModel):
    ts: str
    at: datetime
    who: str
    me: bool
    text: str


class Advice(BaseModel):
    text: str


class Outgoing(BaseModel):
    text: str


def _slack_url(team: str, channel: str, thread_ts: str | None) -> str:
    base = f"https://app.slack.com/client/{team}/{channel}"
    return f"{base}/thread/{channel}-{thread_ts}" if thread_ts else base


# 보드는 1분마다 목록을 다시 읽는다. 내가 이미 답한 초안 치우기는 그보다 자주 돌지 않는다.
SWEEP_SECONDS = 60
_last_sweep = 0.0


async def _sweep() -> None:
    try:
        await sweep_sent_drafts()
    except (OSError, KeyError, ValueError) as exc:
        log.warning("draft sweep failed: %s", exc)


@router.get("/drafts")
async def drafts(
    session: SessionDep, settings: SettingsDep, background: BackgroundTasks
) -> list[DraftOut]:
    global _last_sweep
    if time.monotonic() - _last_sweep >= SWEEP_SECONDS:
        _last_sweep = time.monotonic()
        background.add_task(_sweep)
    rows = await session.execute(
        select(ReplyDraft, Task.title)
        .outerjoin(Task, Task.id == ReplyDraft.task_id)
        .where(ReplyDraft.owner_id == settings.owner_id, ReplyDraft.status == "pending")
        .order_by(ReplyDraft.updated_at.desc())
    )
    return [
        DraftOut(
            id=draft.id,
            task_id=draft.task_id,
            task_title=title,
            draft=draft.draft,
            slack_url=_slack_url(settings.slack_team_id, draft.channel, draft.thread_ts),
            notes=draft.notes,
            advice=draft.advice,
            rewriting=draft.rewriting,
            updated_at=draft.updated_at,
            waiting_since=draft.waiting_since,
            reminders=draft.reminders,
        )
        for draft, title in rows.all()
    ]


@router.post("/drafts/{draft_id}/dismiss")
async def dismiss_draft(
    draft_id: uuid.UUID, session: SessionDep, settings: SettingsDep
) -> dict[str, bool]:
    async with session.begin():
        result = await session.execute(
            update(ReplyDraft)
            .where(ReplyDraft.id == draft_id, ReplyDraft.owner_id == settings.owner_id)
            .values(status="dismissed", updated_at=func.now())
            .returning(ReplyDraft.id)
        )
        found = result.first() is not None
    if not found:
        raise HTTPException(404, "draft not found")
    return {"dismissed": True}


@router.post("/drafts/{draft_id}/advice")
async def advise_draft(
    draft_id: uuid.UUID,
    body: Advice,
    background: BackgroundTasks,
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, bool]:
    """조언을 쌓고 초안을 다시 쓴다. 다시 쓰는 데 몇 분 걸려 응답은 먼저 돌려준다."""
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "advice is empty")
    async with session.begin():
        result = await session.execute(
            update(ReplyDraft)
            .where(
                ReplyDraft.id == draft_id,
                ReplyDraft.owner_id == settings.owner_id,
                ReplyDraft.status == "pending",
            )
            .values(
                advice=ReplyDraft.advice.op("||")(func.jsonb_build_array(text)),
                rewriting=True,
                updated_at=func.now(),
            )
            .returning(ReplyDraft.id)
        )
        found = result.first() is not None
    if not found:
        raise HTTPException(404, "draft not found")
    background.add_task(rewrite_draft, draft_id)
    return {"rewriting": True}


@router.get("/drafts/{draft_id}/thread")
async def draft_thread(draft_id: uuid.UUID) -> list[ThreadMessage]:
    """패널을 열 때 Slack에서 그때 읽는다. 저장하지 않는다."""
    try:
        return [ThreadMessage(**m) for m in await thread_messages(draft_id)]
    except SendError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (OSError, KeyError, ValueError) as exc:
        raise HTTPException(503, f"Slack 토큰을 읽지 못함: {type(exc).__name__}") from exc


@router.post("/drafts/{draft_id}/send")
async def send(draft_id: uuid.UUID, body: Outgoing) -> dict[str, str]:
    """내 이름으로 Slack에 보낸다. 보드에서 내가 확인하고 누를 때만 불린다."""
    try:
        sent = await send_draft(draft_id, body.text)
    except SendError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (OSError, KeyError, ValueError) as exc:
        raise HTTPException(503, f"Slack 토큰을 읽지 못함: {type(exc).__name__}") from exc
    return {"sent": sent}


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
