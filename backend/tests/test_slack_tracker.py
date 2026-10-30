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
    ReplyDraft,
    Task,
    TaskEvent,
    TaskFieldEvidence,
    TaskSuggestion,
)
from app.trackers import slack
from app.trackers.slack import (
    DraftOut,
    SlackTriage,
    ThreadTriage,
    analyze_dm,
    analyze_thread,
    thread_key,
)

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
    def __init__(
        self,
        threads: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
        dms: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.threads = threads or {}
        self.dms = dms or {}

    async def dm_channels(self) -> list[dict[str, Any]]:
        return [{"id": d, "user": "U2", "updated": NOW.timestamp() * 1000} for d in self.dms]

    async def history(self, channel: str, oldest: float) -> list[dict[str, Any]]:
        return self.dms[channel]

    async def user_name(self, user_id: str) -> str:
        return "김재서"

    async def my_messages(self, user_id: str) -> list[str]:
        return ["<@U9> 수정완료했습니다", "넵 확인하겠습니다", "ok", "긴글" * 200]

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
        if output is DraftOut:
            return DraftOut(draft="넵 확인하겠습니다")  # type: ignore[return-value]
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
        await session.execute(delete(ReplyDraft).where(ReplyDraft.owner_id == owner))
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


def test_dm_pending_when_other_spoke_after_me() -> None:
    messages = [
        msg("1.0", "U2", "예전 얘기"),
        msg("2.0", ME, "넵"),
        msg("3.0", "U2", "CS-0004353 이거 봐주세요"),
        msg("4.0", "U2", "급해요"),
        {"ts": "5.0", "bot_id": "B1", "text": "자동 알림"},
    ]

    thread = analyze_dm("D1", messages, ME, "김재서")

    assert (thread.pending, thread.thread_ts, thread.marker) == (True, "3.0", "4.000000")
    assert (thread.cs_id, thread.root_text, thread.sync_id) == (
        "CS-0004353",
        "DM (김재서)",
        "D1:dm",
    )


def test_dm_not_pending_after_my_reply() -> None:
    assert not analyze_dm("D1", [msg("1.0", "U2", "요청"), msg("2.0", ME, "확인")], ME, "x").pending


async def test_dm_request_creates_task_then_my_reply_suggests_completion(owner: str) -> None:
    dm = [msg("1.0", "U2", "내일까지 반품 정책 정리해 주세요")]
    client = FakeSlack(dms={"D1": dm})
    llm = FakeLLM(triage(title="반품 정책 정리", project=None))

    created = await slack.run(client, llm, now=NOW)
    assert created.created == 1
    assert await _tasks(owner) == {"반품 정책 정리": "todo"}
    assert "[S1] 루트: DM (김재서)" in llm.prompts[0]

    dm.append(msg("2.0", ME, "네 정리할게요"))
    answered = await slack.run(client, FakeLLM(), now=NOW)

    assert answered.suggested == 1
    async with get_sessionmaker()() as session:
        [(suggestion, task)] = await list_pending_suggestions(session, owner_id=owner)
    assert (task.title, suggestion.explanation) == ("반품 정책 정리", "DM에 답했어요")


async def test_dm_chatter_makes_no_task(owner: str) -> None:
    client = FakeSlack(dms={"D1": [msg("1.0", "U2", "점심 뭐 먹어요?")]})

    stats = await slack.run(client, FakeLLM(triage(kind="fyi", title=None)), now=NOW)

    assert (stats.threads, stats.created) == (1, 0)
    assert await _tasks(owner) == {}


async def test_repeat_ping_keeps_slack_task_todo(owner: str) -> None:
    dm = [msg("1.0", "U2", "반품 정책 정리 부탁드려요")]
    client = FakeSlack(dms={"D1": dm})
    await slack.run(client, FakeLLM(triage()), now=NOW)

    dm.append(msg("2.0", "U2", "혹시 보셨나요?"))
    await slack.run(client, FakeLLM(triage()), now=NOW)

    assert await _tasks(owner) == {"쿠폰 안내 확인": "todo"}


async def _drafts(owner: str) -> list[tuple[str, str]]:
    async with get_sessionmaker()() as session:
        rows = await session.scalars(select(ReplyDraft).where(ReplyDraft.owner_id == owner))
        return [(d.draft, d.status) for d in rows]


def test_style_samples_keep_short_replies_only() -> None:
    samples = slack.style_samples(["<@U9> 수정완료했습니다", "ok", "x" * 300, "```code```"])

    assert samples == ["수정완료했습니다"]


async def test_request_gets_a_draft_and_my_reply_clears_it(owner: str) -> None:
    thread = [msg("1.0", "U2", f"<@{ME}> 쿠폰 안내 확인해 주세요")]
    client = FakeSlack({("C1", "1.0"): thread})
    llm = FakeLLM(triage())

    stats = await slack.run(client, llm, now=NOW)

    assert stats.drafted == 1
    assert await _drafts(owner) == [("넵 확인하겠습니다", "pending")]
    assert "내 말투 샘플:\n- 수정완료했습니다" in llm.prompts[-1]
    assert "연결된 할 일: 쿠폰 안내 확인" in llm.prompts[-1]

    thread.append(msg("2.0", ME, "확인했습니다"))
    await slack.run(client, FakeLLM(), now=NOW)

    assert await _drafts(owner) == [("넵 확인하겠습니다", "used")]


async def test_no_draft_for_fyi_or_someone_elses_cs_thread(owner: str) -> None:
    client = FakeSlack(
        {
            ("C1", "1.0"): [msg("1.0", "U2", f"<@{ME}> 감사합니다")],
            ("C2", "1.0"): [
                msg("1.0", "BOT", "루미르 (CS-0004264)"),
                msg("2.0", "U2", f"<@{ME}> 봐줘"),
            ],
        }
    )
    llm = FakeLLM(triage(kind="fyi", title=None), triage(ref="S2"))

    stats = await slack.run(client, llm, now=NOW)

    assert stats.drafted == 0
    assert await _drafts(owner) == []


async def test_cs_request_drafts_on_my_2325_task(owner: str) -> None:
    await _seed(owner, "CS-0004260 쿠폰 문의")
    client = FakeSlack(
        {
            ("C1", "1.0"): [
                msg("1.0", "BOT", "x (CS-0004260)"),
                msg("2.0", "U2", f"<@{ME}> 봐주세요"),
            ]
        }
    )

    stats = await slack.run(client, FakeLLM(triage()), now=NOW)

    assert (stats.started, stats.drafted) == (1, 1)


async def test_dm_reply_clears_draft(owner: str) -> None:
    dm = [msg("1.0", "U2", "반품 정책 정리 부탁드려요")]
    client = FakeSlack(dms={"D1": dm})
    await slack.run(client, FakeLLM(triage()), now=NOW)

    dm.append(msg("2.0", ME, "네"))
    await slack.run(client, FakeLLM(), now=NOW)

    assert await _drafts(owner) == [("넵 확인하겠습니다", "used")]
