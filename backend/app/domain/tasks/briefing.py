"""브리핑. 채팅과 낮 12시 알림이 같은 내용을 쓴다."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.tasks.service import list_pending_suggestions, list_tasks
from app.persistence.models import ReplyDraft, Task, TrackerRun

RECENT_FAILURE_WINDOW = timedelta(hours=24)
NEW_TASK_WINDOW = timedelta(hours=24)
NEW_TASK_LIMIT = 10


@dataclass(frozen=True)
class Briefing:
    text: str
    summary: str


def _line(task: Task) -> str:
    parts = [task.title]
    if task.project:
        parts.append(f"[{task.project}]")
    if task.due_date:
        parts.append(f"마감 {task.due_date.isoformat()}")
    return " ".join(parts)


async def failed_trackers(session: AsyncSession, now: datetime) -> list[str]:
    """각 수집기의 마지막 실행이 실패면 사유를 돌려준다."""
    rows = await session.execute(
        select(TrackerRun.name, TrackerRun.ok, TrackerRun.detail, TrackerRun.finished_at)
        .where(TrackerRun.finished_at >= now - RECENT_FAILURE_WINDOW)
        .order_by(TrackerRun.finished_at.desc())
    )
    latest: dict[str, str | None] = {}
    for name, ok, detail, _ in rows.all():
        latest.setdefault(name, None if ok else (detail or "원인 불명"))
    return [f"{name}: {detail}" for name, detail in latest.items() if detail]


async def build_briefing(
    session: AsyncSession, *, owner_id: str, today: date, now: datetime
) -> Briefing:
    open_tasks = await list_tasks(session, owner_id=owner_id)
    overdue = [t for t in open_tasks if t.due_date and t.due_date < today]
    due_today = [t for t in open_tasks if t.due_date == today]
    in_progress = [t for t in open_tasks if t.status == "in_progress"]
    blocked = [t for t in open_tasks if t.status == "blocked"]
    new = [
        t
        for t in open_tasks
        if t.source != "natural_language" and t.created_at >= now - NEW_TASK_WINDOW
    ]
    suggestions = await list_pending_suggestions(session, owner_id=owner_id)
    failed = await failed_trackers(session, now)
    drafts = (
        await session.scalar(
            select(func.count())
            .select_from(ReplyDraft)
            .where(ReplyDraft.owner_id == owner_id, ReplyDraft.status == "pending")
        )
        or 0
    )

    sections: list[str] = []

    def add(title: str, lines: list[str]) -> None:
        if lines:
            sections.append(f"{title}\n" + "\n".join(f"- {line}" for line in lines))

    add("마감 지난 일", [_line(t) for t in overdue])
    add("오늘 마감", [_line(t) for t in due_today])
    add(
        "끝난 것 같은 일 (맞으면 끝났다고, 아니면 아니라고 말해 주세요)",
        [f"{task.title}: {s.explanation or '근거 없음'}" for s, task in suggestions],
    )
    new_lines = [_line(t) for t in new[:NEW_TASK_LIMIT]]
    if len(new) > NEW_TASK_LIMIT:
        new_lines.append(f"외 {len(new) - NEW_TASK_LIMIT}개")
    if drafts:
        sections.append(f"답변 초안 {drafts}개가 준비돼 있어요. 보드에서 볼 수 있어요.")
    add("새로 들어온 일", new_lines)
    add("수집기 문제", failed)
    sections.append(f"진행 중 {len(in_progress)}개, 고객 답 대기 {len(blocked)}개")

    summary = (
        f"마감 지남 {len(overdue)}, 오늘 마감 {len(due_today)}, "
        f"끝난 것 같은 일 {len(suggestions)}, 답변 초안 {drafts}, 새 일 {len(new)}"
    )
    if failed:
        summary += f", 수집기 문제 {len(failed)}"
    return Briefing(text="\n\n".join(sections), summary=summary)
