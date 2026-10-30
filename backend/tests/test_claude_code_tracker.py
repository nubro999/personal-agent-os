import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, select

from app.config import get_settings
from app.domain.tasks.service import (
    Evidence,
    NewTask,
    TaskChanges,
    create_task,
    list_pending_suggestions,
    update_task,
)
from app.llm.base import LLMError, M
from app.persistence.db import get_sessionmaker
from app.persistence.models import (
    ActivitySession,
    Task,
    TaskEvent,
    TaskFieldEvidence,
    TaskSuggestion,
)
from app.trackers import claude_code
from app.trackers.claude_code import SessionWork, WorkItem, parse_session, render_turns

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def _entry(kind: str, content: Any, *, minutes_ago: int = 60, **extra: Any) -> dict[str, Any]:
    ts = (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")
    return {
        "type": kind,
        "entrypoint": "claude-desktop",
        "cwd": "/home/nubroo/glab/dl",
        "isSidechain": False,
        "timestamp": ts,
        "message": {"role": kind, "content": content},
        **extra,
    }


def _write(path: Path, entries: list[dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n")
    return path


def test_parse_keeps_human_and_assistant_text_only(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "s1.jsonl",
        [
            {"type": "queue-operation"},
            _entry("user", "디엘마케팅 반품 기능 개발할거야"),
            _entry("user", [{"type": "tool_result", "content": "secret output"}]),
            _entry("user", "<system-reminder>ignore</system-reminder>"),
            _entry("user", "meta", isMeta=True),
            _entry("assistant", [{"type": "tool_use", "name": "Bash"}]),
            _entry("assistant", [{"type": "text", "text": "배포까지 끝냈어요."}]),
            _entry("user", "sub", isSidechain=True),
        ],
    )

    chunk = parse_session(path)

    assert chunk is not None
    assert chunk.turns == [
        ("사용자", "디엘마케팅 반품 기능 개발할거야"),
        ("Claude", "배포까지 끝냈어요."),
    ]
    assert (chunk.total_lines, chunk.workspace) == (8, "/home/nubroo/glab/dl")
    assert chunk.last_activity_at == NOW - timedelta(minutes=60)


def test_parse_skips_automated_sessions(tmp_path: Path) -> None:
    path = _write(tmp_path / "s.jsonl", [_entry("user", "prompt", entrypoint="sdk-py")])

    assert parse_session(path) is None


def test_parse_reads_only_new_lines(tmp_path: Path) -> None:
    path = _write(tmp_path / "s.jsonl", [_entry("user", "첫 요청"), _entry("user", "두 번째")])

    chunk = parse_session(path, start_line=1)

    assert chunk is not None and chunk.turns == [("사용자", "두 번째")]


def test_render_keeps_first_requests_and_ending() -> None:
    turns = [("사용자", "처음 요청")] + [("Claude", "x" * 300)] * 100 + [("사용자", "배포 완료")]

    text = render_turns(turns, limit=2000)

    assert text.startswith("사용자: 처음 요청") and text.endswith("사용자: 배포 완료")
    assert len(text) <= 2100


class FakeLLM:
    def __init__(self, result: SessionWork | Exception) -> None:
        self.result = result
        self.prompts: list[str] = []

    async def generate(self, *, system: str, prompt: str, output: type[M]) -> M:
        self.prompts.append(prompt)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result  # type: ignore[return-value]


def _item(**values: Any) -> WorkItem:
    base: dict[str, Any] = {
        "title": "반품 기능 개발",
        "project": "디엘마케팅",
        "state": "in_progress",
        "match_ref": None,
        "evidence": "근거",
    }
    return WorkItem(**(base | values))


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


async def _tasks(owner: str) -> list[Task]:
    async with get_sessionmaker()() as session:
        return list((await session.scalars(select(Task).where(Task.owner_id == owner))).all())


async def test_new_work_becomes_task_and_session_is_not_reprocessed(
    tmp_path: Path, owner: str
) -> None:
    projects = tmp_path / "projects" / "-home-nubroo-glab"
    projects.mkdir(parents=True)
    _write(projects / "s1.jsonl", [_entry("user", "반품 기능 개발할거야")])
    llm = FakeLLM(SessionWork(summary="반품 기능 개발 시작", items=[_item()]))

    first = await claude_code.run(tmp_path / "projects", now=NOW, llm=llm)
    second = await claude_code.run(tmp_path / "projects", now=NOW, llm=llm)

    assert (first.sessions, first.created, second.sessions) == (1, 1, 0)
    [task] = await _tasks(owner)
    assert (task.title, task.project, task.status, task.source) == (
        "반품 기능 개발",
        "디엘마케팅",
        "in_progress",
        "claude_code",
    )
    assert "작업 폴더: /home/nubroo/glab/dl" in llm.prompts[0]


async def test_active_session_waits_until_idle(tmp_path: Path, owner: str) -> None:
    projects = tmp_path / "p" / "x"
    projects.mkdir(parents=True)
    _write(projects / "s1.jsonl", [_entry("user", "작업 중", minutes_ago=5)])
    llm = FakeLLM(SessionWork(summary="", items=[_item()]))

    stats = await claude_code.run(tmp_path / "p", now=NOW, llm=llm)

    assert stats.sessions == 0 and llm.prompts == []


async def test_matched_done_work_only_suggests_completion(tmp_path: Path, owner: str) -> None:
    async with get_sessionmaker().begin() as session:
        await create_task(
            session,
            owner_id=owner,
            request_id="seed",
            actor="t",
            new=NewTask(title=Evidence("반품 기능 개발", "explicit")),
        )
    projects = tmp_path / "p" / "x"
    projects.mkdir(parents=True)
    _write(projects / "s1.jsonl", [_entry("user", "배포했어")])
    llm = FakeLLM(
        SessionWork(summary="배포", items=[_item(state="done", match_ref="T1", evidence="배포함")])
    )

    stats = await claude_code.run(tmp_path / "p", now=NOW, llm=llm)

    assert (stats.created, stats.suggested) == (0, 1)
    [task] = await _tasks(owner)
    assert task.status == "todo"
    async with get_sessionmaker()() as session:
        [(suggestion, _)] = await list_pending_suggestions(session, owner_id=owner)
    assert suggestion.explanation == "배포함"

    # 사용자가 완료하면 제안은 정리된다.
    async with get_sessionmaker().begin() as session:
        await update_task(
            session,
            owner_id=owner,
            task_id=task.id,
            expected_version=task.version,
            request_id="r1",
            actor="t",
            changes=TaskChanges(status=Evidence("done", "explicit")),
        )
    async with get_sessionmaker()() as session:
        assert await list_pending_suggestions(session, owner_id=owner) == []


async def test_matched_in_progress_work_starts_todo_task(tmp_path: Path, owner: str) -> None:
    async with get_sessionmaker().begin() as session:
        await create_task(
            session,
            owner_id=owner,
            request_id="seed",
            actor="t",
            new=NewTask(title=Evidence("반품 기능 개발", "explicit")),
        )
    projects = tmp_path / "p" / "x"
    projects.mkdir(parents=True)
    _write(projects / "s1.jsonl", [_entry("user", "이어서 하자")])

    stats = await claude_code.run(
        tmp_path / "p", now=NOW, llm=FakeLLM(SessionWork(summary="", items=[_item(match_ref="T1")]))
    )

    assert stats.started == 1
    [task] = await _tasks(owner)
    assert task.status == "in_progress"


async def test_llm_failure_leaves_session_for_next_run(tmp_path: Path, owner: str) -> None:
    projects = tmp_path / "p" / "x"
    projects.mkdir(parents=True)
    _write(projects / "s1.jsonl", [_entry("user", "작업")])

    failed = await claude_code.run(tmp_path / "p", now=NOW, llm=FakeLLM(LLMError("boom")))
    retried = await claude_code.run(
        tmp_path / "p", now=NOW, llm=FakeLLM(SessionWork(summary="", items=[_item()]))
    )

    assert (failed.failed, retried.created) == (1, 1)
