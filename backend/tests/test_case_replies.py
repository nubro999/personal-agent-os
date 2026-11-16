import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, select

from app.config import get_settings
from app.domain.tasks.service import Evidence, NewTask, create_task
from app.persistence.db import get_sessionmaker
from app.persistence.models import (
    ActivitySession,
    ReplyDraft,
    Task,
    TaskEvent,
    TaskFieldEvidence,
    TaskSuggestion,
)
from app.reply.draft import ReplyDraft as Written
from app.trackers import case_replies, slack
from app.trackers.case_replies import AgentRun

ME = "UME"
KST = ZoneInfo("Asia/Seoul")
RAN = datetime(2026, 11, 3, 10, 0)  # KST naive, 2325 그대로
RAN_TS = RAN.replace(tzinfo=KST).timestamp()


def msg(ts: float, user: str, text: str) -> dict[str, Any]:
    return {"ts": f"{ts:.6f}", "user": user, "text": text}


class FakeSlack:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages
        self.posted: list[tuple[str, str, str | None]] = []

    async def replies(self, channel: str, ts: str) -> list[dict[str, Any]]:
        return self.messages

    async def post(self, channel: str, text: str, thread_ts: str | None) -> str:
        self.posted.append((channel, text, thread_ts))
        self.messages.append(msg(RAN_TS + 9999, ME, text))
        return "9.0"


class Writer:
    def __init__(self) -> None:
        self.cases: list[str] = []

    async def __call__(self, cs: str) -> Written:
        self.cases.append(cs)
        return Written(draft="<@UA> 사유는 이렇습니다.", answers=[], check=["반영 전"])


@pytest.fixture
def owner(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    owner_id = f"test-{uuid.uuid4()}"
    monkeypatch.setattr(get_settings(), "owner_id", owner_id)
    monkeypatch.setattr(get_settings(), "slack_user_id", ME)
    monkeypatch.setattr(get_settings(), "slack_cs_channel", "C1")
    monkeypatch.setattr(get_settings(), "slack_cs_contact", "UCONTACT")
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


async def _seed(owner: str, key: str, status: str = "in_progress") -> None:
    async with get_sessionmaker().begin() as session:
        await create_task(
            session,
            owner_id=owner,
            request_id=f"2325:{key}",
            actor="t",
            new=NewTask(
                title=Evidence(f"{key} 건", "external"),
                status=Evidence(status, "external"),  # type: ignore[arg-type]
                source="2325",
            ),
        )


def runs_for(*keys: str, brand: str = "hdex") -> case_replies.Runs:
    def runs(item_keys: list[str]) -> dict[str, AgentRun]:
        return {k: AgentRun(k, f"CS-{k}", brand, f"run-{k}", RAN) for k in keys if k in item_keys}

    return runs


def card(cs: str) -> tuple[str, str]:
    return "C1", "1.000000"


async def _drafts(owner: str) -> list[ReplyDraft]:
    async with get_sessionmaker()() as session:
        return list(await session.scalars(select(ReplyDraft).where(ReplyDraft.owner_id == owner)))


async def test_drafts_once_for_my_fixing_cases_with_a_result(owner: str) -> None:
    await _seed(owner, "A")
    await _seed(owner, "B", status="blocked")  # 고객 답 대기는 수정 중이 아니다
    client = FakeSlack([msg(1.0, "BOT", "Hdex (CS-A)"), msg(RAN_TS - 60, ME, "확인하겠습니다")])
    writer = Writer()

    stats = await case_replies.run(client, runs=runs_for("A", "B"), card=card, writer=writer)

    assert (stats.drafted, writer.cases) == (1, ["CS-A"])
    [draft] = await _drafts(owner)
    assert (draft.origin, draft.draft, draft.notes) == ("case", "사유는 이렇습니다.", ["반영 전"])

    again = await case_replies.run(client, runs=runs_for("A"), card=card, writer=writer)
    assert (again.candidates, writer.cases) == (0, ["CS-A"])


async def test_waits_for_a_card_that_is_not_up_yet(owner: str) -> None:
    await _seed(owner, "A")
    client = FakeSlack([msg(1.0, "BOT", "Hdex (CS-A)")])
    writer = Writer()

    def no_card(cs: str) -> None:
        return None

    stats = await case_replies.run(client, runs=runs_for("A"), card=no_card, writer=writer)
    assert (stats.no_card, stats.drafted, writer.cases) == (1, 0, [])

    # 카드가 나중에 서면 같은 실행으로 초안을 쓴다.
    later = await case_replies.run(client, runs=runs_for("A"), card=card, writer=writer)
    assert (later.drafted, writer.cases) == (1, ["CS-A"])


async def test_skips_when_i_already_answered_after_the_run(owner: str) -> None:
    await _seed(owner, "A")
    client = FakeSlack([msg(1.0, "BOT", "Hdex (CS-A)"), msg(RAN_TS + 60, ME, "수정하였습니다")])
    writer = Writer()

    stats = await case_replies.run(client, runs=runs_for("A"), card=card, writer=writer)

    assert (stats.drafted, stats.answered, writer.cases) == (0, 1, [])
    assert await _drafts(owner) == []


async def test_case_draft_survives_the_sweep_and_sends_to_the_contact(owner: str) -> None:
    await _seed(owner, "A")
    client = FakeSlack([msg(1.0, "BOT", "Hdex (CS-A)"), msg(RAN_TS - 60, ME, "확인하겠습니다")])
    await case_replies.run(client, runs=runs_for("A"), card=card, writer=Writer())

    # 아무도 나를 부르지 않았지만, 미리 쓴 초안은 내가 답하기 전까지 남는다.
    assert await slack.sweep_sent_drafts(client) == 0
    [draft] = await _drafts(owner)
    assert draft.status == "pending"

    sent = await slack.send_draft(draft.id, draft.draft, client)

    assert sent == "<@UCONTACT> 사유는 이렇습니다."
    assert client.posted == [("C1", sent, "1.000000")]
    [after] = await _drafts(owner)
    assert after.status == "used"


async def test_other_brands_get_the_default_line_without_the_agent(owner: str) -> None:
    await _seed(owner, "A")
    client = FakeSlack([msg(1.0, "BOT", "쿤달 (CS-A)")])
    writer = Writer()

    stats = await case_replies.run(
        client, runs=runs_for("A", brand="kundalKR"), card=card, writer=writer
    )

    assert (stats.drafted, writer.cases) == (1, [])
    [draft] = await _drafts(owner)
    assert (draft.draft, draft.origin) == ("수정완료했습니다", "case")
