import uuid
from collections.abc import Iterator
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, select

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
from app.trackers import slack
from app.trackers.slack import SlackTriage, ThreadTriage, analyze_thread, thread_key

ME = "UME"
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=ZoneInfo("Asia/Seoul"))


def msg(ts: str, user: str, text: str) -> dict[str, Any]:
    return {"ts": ts, "user": user, "text": text}


def test_thread_key_uses_thread_ts_from_permalink() -> None:
    match = {"channel": {"id": "C1"}, "ts": "2.0", "permalink": "https://x/p2?thread_ts=1.0&cid=C1"}
    root = {"channel": {"id": "C1"}, "ts": "3.0", "permalink": "https://x/p3"}

    assert (thread_key(match), thread_key(root)) == (("C1", "1.0"), ("C1", "3.0"))


def test_pending_when_mentioned_after_my_last_reply() -> None:
    messages = [
        msg("1.0", "BOT", "에이치덱스 (CS-0004260)"),
        msg("2.0", ME, "답변드렸어요"),
        msg("3.0", "UADMIN", f"<@{ME}> 확인 부탁드립니다!"),
    ]

    thread = analyze_thread("C1", "1.0", messages, ME)

    assert (thread.pending, thread.cs_id, thread.marker) == (True, "CS-0004260", "3.000000")
    assert thread.context == ["나: 답변드렸어요", "상대: @나 확인 부탁드립니다!"]


def test_not_pending_after_i_reply() -> None:
    messages = [msg("1.0", "BOT", "루트"), msg("2.0", "U2", f"<@{ME}> 봐줘"), msg("3.0", ME, "넵")]

    assert not analyze_thread("C1", "1.0", messages, ME).pending


class FakeSlack:
    def __init__(self, threads: dict[tuple[str, str], list[dict[str, Any]]]) -> None:
        self.threads = threads

    async def search_mentions(self, user_id: str, after: date) -> list[dict[str, Any]]:
        return [
            {"channel": {"id": ch}, "ts": msgs[-1]["ts"], "permalink": f"x?thread_ts={ts}"}
            for (ch, ts), msgs in self.threads.items()
        ]

    async def replies(self, channel: str, ts: str) -> list[dict[str, Any]]:
        return self.threads[(channel, ts)]


class FakeLLM:
    def __init__(self, *triage: ThreadTriage) -> None:
        self.triage = list(triage)
        self.prompts: list[str] = []

    async def generate(self, *, system: str, prompt: str, output: type[M]) -> M:
        self.prompts.append(prompt)
        return SlackTriage(threads=self.triage)  # type: ignore[return-value]


def triage(**values: Any) -> ThreadTriage:
    base: dict[str, Any] = {
        "ref": "S1",
        "kind": "request",
        "title": "쿠폰 안내 확인",
        "project": "에이치덱스",
        "due_date": None,
        "evidence": "확인 요청",
    }
    return ThreadTriage(**(base | values))


@pytest.fixture
def owner(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    owner_id = f"test-{uuid.uuid4()}"
    monkeypatch.setattr(get_settings(), "owner_id", owner_id)
    monkeypatch.setattr(get_settings(), "slack_user_id", ME)
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


async def _seed(owner: str, title: str, status: str = "blocked") -> None:
    async with get_sessionmaker().begin() as session:
        await create_task(
            session,
            owner_id=owner,
            request_id=f"seed-{title}",
            actor="t",
            new=NewTask(title=Evidence(title, "external"), status=Evidence(status, "external")),  # type: ignore[arg-type]
        )


async def _tasks(owner: str) -> dict[str, str]:
    async with get_sessionmaker()() as session:
        return {
            t.title: t.status
            for t in await session.scalars(select(Task).where(Task.owner_id == owner))
        }


async def test_request_without_cs_creates_task_once(owner: str) -> None:
    client = FakeSlack({("C1", "1.0"): [msg("1.0", "U2", f"<@{ME}> 쿠폰 안내 확인해 주세요")]})
    llm = FakeLLM(triage(due_date=date(2026, 10, 31)))

    first = await slack.run(client, llm, now=NOW)
    second = await slack.run(client, llm, now=NOW)

    assert (first.created, second.threads) == (1, 0)
    assert await _tasks(owner) == {"쿠폰 안내 확인": "todo"}
    assert "[S1] 루트: @" not in llm.prompts[0] and "오늘: 2026-10-30" in llm.prompts[0]


async def test_request_on_blocked_cs_task_moves_it_to_in_progress(owner: str) -> None:
    await _seed(owner, "CS-0004260 쿠폰 문의")
    client = FakeSlack(
        {
            ("C1", "1.0"): [
                msg("1.0", "BOT", "에이치덱스 (CS-0004260)"),
                msg("2.0", "U2", f"<@{ME}> 리마인드"),
            ]
        }
    )

    stats = await slack.run(client, FakeLLM(triage()), now=NOW)

    assert (stats.created, stats.started) == (0, 1)
    assert await _tasks(owner) == {"CS-0004260 쿠폰 문의": "in_progress"}


async def test_done_report_only_suggests_completion(owner: str) -> None:
    await _seed(owner, "CS-0004323 사은품 조회", status="in_progress")
    client = FakeSlack(
        {
            ("C1", "1.0"): [
                msg("1.0", "BOT", "Hdex (CS-0004323)"),
                msg("2.0", "U2", f"<@{ME}> 반영 완료"),
            ]
        }
    )

    stats = await slack.run(client, FakeLLM(triage(kind="done_report", title=None)), now=NOW)

    assert stats.suggested == 1
    assert await _tasks(owner) == {"CS-0004323 사은품 조회": "in_progress"}


async def test_my_reply_suggests_completing_slack_task(owner: str) -> None:
    thread = [msg("1.0", "U2", f"<@{ME}> 쿠폰 안내 확인해 주세요")]
    client = FakeSlack({("C1", "1.0"): thread})
    await slack.run(client, FakeLLM(triage()), now=NOW)

    thread.append(msg("2.0", ME, "확인했습니다"))
    stats = await slack.run(client, FakeLLM(), now=NOW)

    assert stats.suggested == 1
    async with get_sessionmaker()() as session:
        [(suggestion, task)] = await list_pending_suggestions(session, owner_id=owner)
    assert (task.title, suggestion.explanation) == ("쿠폰 안내 확인", "Slack 스레드에 답했어요")


async def test_fyi_is_ignored(owner: str) -> None:
    client = FakeSlack({("C1", "1.0"): [msg("1.0", "U2", f"<@{ME}> 감사합니다")]})

    stats = await slack.run(client, FakeLLM(triage(kind="fyi", title=None)), now=NOW)

    assert (stats.threads, stats.created) == (1, 0)
    assert await _tasks(owner) == {}


async def test_cs_thread_without_my_task_is_skipped(owner: str) -> None:
    client = FakeSlack(
        {
            ("C1", "1.0"): [
                msg("1.0", "BOT", "루미르 (CS-0004264)"),
                msg("2.0", "UADMIN", f"<@U2> <@{ME}> 리마인드욤"),
            ]
        }
    )

    stats = await slack.run(client, FakeLLM(triage()), now=NOW)

    assert (stats.threads, stats.created) == (1, 0)
    assert await _tasks(owner) == {}
