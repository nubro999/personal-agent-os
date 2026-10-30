"""Claude Code 세션 기록에서 내가 한 일을 찾아 Task로 남긴다.

~/.claude/projects/*/*.jsonl 중 사람이 직접 쓴 세션(Claude Desktop, CLI)만 읽는다.
새로 보이는 일은 Task로 만들고, 기존 Task와 이어지면 진행 중으로 바꾸고,
끝난 것 같으면 완료 제안만 남긴다. 완료 처리는 사용자가 확인한다.
원문은 DB에 저장하지 않고 세션 요약과 근거 한 문장만 남긴다.

실행: uv run python -m app.trackers.claude_code
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.domain.tasks.service import (
    Evidence,
    NewTask,
    TaskChanges,
    create_task,
    list_recent_candidates,
    suggest_completion,
    update_task,
)
from app.llm.base import LLMError, StructuredLLM
from app.llm.factory import get_llm
from app.persistence.db import get_sessionmaker
from app.persistence.models import ActivitySession, Task
from app.trackers.prompts import SYSTEM_PROMPT

log = logging.getLogger(__name__)

SOURCE = "claude_code"
HUMAN_ENTRYPOINTS = frozenset({"claude-desktop", "cli"})
USER_TEXT_LIMIT = 1500
ASSISTANT_TEXT_LIMIT = 400
CHUNK_CHAR_LIMIT = 12000
_SKIP_PREFIXES = ("<command-", "<local-command", "<system-reminder>", "Caveat:")


@dataclass
class SessionChunk:
    session_id: str
    workspace: str | None
    total_lines: int
    last_activity_at: datetime | None
    turns: list[tuple[str, str]] = field(default_factory=list)


def _user_text(content: Any) -> str:
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        # tool_result 블록은 사람이 쓴 말이 아니다.
        text = "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
    else:
        return ""
    if not text or text.startswith(_SKIP_PREFIXES):
        return ""
    return text[:USER_TEXT_LIMIT]


def _assistant_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    text = "\n".join(
        b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
    ).strip()
    return text[:ASSISTANT_TEXT_LIMIT]


def parse_session(path: Path, start_line: int = 0) -> SessionChunk | None:
    """사람이 쓴 세션이 아니면 None. start_line 이후의 대화만 turns에 담는다."""
    chunk = SessionChunk(session_id=path.stem, workspace=None, total_lines=0, last_activity_at=None)
    with path.open(encoding="utf-8", errors="replace") as f:
        for index, line in enumerate(f):
            chunk.total_lines = index + 1
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = entry.get("type")
            if kind not in ("user", "assistant") or entry.get("isSidechain"):
                continue
            entrypoint = entry.get("entrypoint")
            if kind == "user" and entrypoint is not None and entrypoint not in HUMAN_ENTRYPOINTS:
                return None
            chunk.workspace = entry.get("cwd") or chunk.workspace
            if ts := entry.get("timestamp"):
                chunk.last_activity_at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if index < start_line or entry.get("isMeta"):
                continue
            content = (entry.get("message") or {}).get("content")
            if kind == "user" and (text := _user_text(content)):
                chunk.turns.append(("사용자", text))
            elif kind == "assistant" and (text := _assistant_text(content)):
                chunk.turns.append(("Claude", text))
    return chunk


def render_turns(turns: list[tuple[str, str]], limit: int = CHUNK_CHAR_LIMIT) -> str:
    """길면 처음 요청 두 개와 마지막 부분을 남긴다. 무엇을 하려 했고 어떻게 끝났는지가 중요하다."""
    lines = [f"{role}: {text}" for role, text in turns]
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    head = "\n".join(lines[:2])[: limit // 3]
    tail = text[-(limit - len(head)) :]
    return f"{head}\n...(중략)...\n{tail}"


class WorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(description="한 일의 짧은 제목")
    project: str | None = Field(description="프로젝트나 고객사 이름")
    state: Literal["in_progress", "done", "unclear"]
    match_ref: str | None = Field(description="같은 일인 기존 Task의 ref. 확실할 때만")
    evidence: str = Field(description="그렇게 판단한 근거 한 문장")


class SessionWork(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(description="이 세션에서 지금까지 한 일 1~2문장")
    items: list[WorkItem] = Field(description="업무 단위 최대 3개")


def _build_prompt(
    chunk: SessionChunk, previous_summary: str | None, candidates: dict[str, Task]
) -> str:
    task_lines = [
        f"{ref}: {t.title}" + (f" [{t.project}]" if t.project else "") + f" ({t.status})"
        for ref, t in candidates.items()
    ]
    return "\n".join(
        [
            f"작업 폴더: {chunk.workspace or '알 수 없음'}",
            f"이전까지 요약: {previous_summary or '없음'}",
            "",
            "기존 Task:",
            *(task_lines or ["(없음)"]),
            "",
            "대화 기록 (새로 추가된 부분):",
            render_turns(chunk.turns),
        ]
    )


@dataclass
class TrackStats:
    sessions: int = 0
    created: int = 0
    started: int = 0
    suggested: int = 0
    failed: int = 0


async def _apply(
    session: AsyncSession,
    *,
    owner_id: str,
    chunk: SessionChunk,
    work: SessionWork,
    candidates: dict[str, Task],
    stats: TrackStats,
) -> None:
    reference = f"{SOURCE}:{chunk.session_id}"
    for i, item in enumerate(work.items[:3]):
        request_id = f"{reference}:{chunk.total_lines}:{i}"
        task = candidates.get(item.match_ref or "")
        if task is not None:
            if task.status in ("done", "cancelled"):
                continue
            if item.state == "done":
                stats.suggested += await suggest_completion(
                    session,
                    owner_id=owner_id,
                    task_id=task.id,
                    source=SOURCE,
                    source_reference=reference,
                    explanation=item.evidence,
                )
            elif item.state == "in_progress" and task.status == "todo":
                await update_task(
                    session,
                    owner_id=owner_id,
                    task_id=task.id,
                    expected_version=task.version,
                    request_id=request_id,
                    actor="claude_code_tracker",
                    changes=TaskChanges(
                        status=Evidence("in_progress", "external", item.evidence, reference)
                    ),
                    reason=item.evidence,
                )
                stats.started += 1
        elif item.state in ("in_progress", "done"):
            result = await create_task(
                session,
                owner_id=owner_id,
                request_id=request_id,
                actor="claude_code_tracker",
                new=NewTask(
                    title=Evidence(item.title, "external", item.evidence, reference),
                    project=(
                        Evidence(item.project, "external", None, reference)
                        if item.project
                        else None
                    ),
                    status=Evidence(item.state, "external", item.evidence, reference),
                    source=SOURCE,
                ),
            )
            stats.created += result.created


async def track_session(
    path: Path, *, owner_id: str, llm: StructuredLLM, now: datetime, stats: TrackStats
) -> None:
    async with get_sessionmaker().begin() as session:
        state = await session.scalar(
            select(ActivitySession).where(
                ActivitySession.owner_id == owner_id,
                ActivitySession.source == SOURCE,
                ActivitySession.external_id == path.stem,
            )
        )
        start = state.processed_lines if state else 0
        chunk = parse_session(path, start)
        if chunk is None or chunk.total_lines <= start:
            return
        idle = timedelta(minutes=get_settings().tracker_idle_minutes)
        if chunk.last_activity_at and now - chunk.last_activity_at < idle:
            return

        summary = state.summary if state else None
        if chunk.turns:
            stats.sessions += 1
            candidates = {
                f"T{i}": t
                for i, t in enumerate(await list_recent_candidates(session, owner_id=owner_id), 1)
            }
            work = await llm.generate(
                system=SYSTEM_PROMPT,
                prompt=_build_prompt(chunk, summary, candidates),
                output=SessionWork,
            )
            await _apply(
                session,
                owner_id=owner_id,
                chunk=chunk,
                work=work,
                candidates=candidates,
                stats=stats,
            )
            summary = work.summary

        await session.execute(
            pg_insert(ActivitySession)
            .values(
                owner_id=owner_id,
                source=SOURCE,
                external_id=path.stem,
                workspace=chunk.workspace,
                processed_lines=chunk.total_lines,
                last_activity_at=chunk.last_activity_at,
                summary=summary,
            )
            .on_conflict_do_update(
                index_elements=["owner_id", "source", "external_id"],
                set_={
                    "workspace": chunk.workspace,
                    "processed_lines": chunk.total_lines,
                    "last_activity_at": chunk.last_activity_at,
                    "summary": summary,
                    "updated_at": now,
                },
            )
        )


def session_files(root: Path, since: datetime) -> list[Path]:
    files = [p for p in root.glob("*/*.jsonl") if p.stat().st_mtime >= since.timestamp()]
    return sorted(files, key=lambda p: p.stat().st_mtime)


async def run(
    root: Path, now: datetime | None = None, llm: StructuredLLM | None = None
) -> TrackStats:
    settings = get_settings()
    now = now or datetime.now(UTC)
    llm = llm or get_llm()
    since = now - timedelta(hours=settings.tracker_lookback_hours)
    stats = TrackStats()
    for path in session_files(root, since):
        try:
            await track_session(path, owner_id=settings.owner_id, llm=llm, now=now, stats=stats)
        except LLMError:
            # 읽은 위치를 올리지 않았으니 다음 실행에서 다시 시도한다.
            log.exception("tracker failed on %s", path)
            stats.failed += 1
    return stats


async def run_once() -> str | None:
    stats = await run(get_settings().claude_projects_dir)
    log.info(
        "claude_code tracker: sessions=%d created=%d started=%d suggested=%d failed=%d",
        stats.sessions,
        stats.created,
        stats.started,
        stats.suggested,
        stats.failed,
    )
    return f"세션 {stats.failed}개 분석 실패" if stats.failed else None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run_once())


if __name__ == "__main__":
    main()
