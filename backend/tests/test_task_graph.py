import uuid
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from sqlalchemy import delete, select

from app.agents.task import graph as graph_module
from app.agents.task.schemas import TaskIntent
from app.config import get_settings
from app.domain.tasks.service import (
    Evidence,
    NewTask,
    create_task,
    list_pending_suggestions,
    suggest_completion,
)
from app.llm.base import LLMError, M
from app.persistence.db import get_sessionmaker
from app.persistence.models import Task, TaskEvent, TaskFieldEvidence, TaskSuggestion, TrackerRun


def intent(**values: Any) -> TaskIntent:
    base: dict[str, Any] = dict.fromkeys(TaskIntent.model_fields)
    base.update(include_done=False, reason="test", target_refs=[], ambiguous=False, statuses=[])
    base.update(values)
    return TaskIntent(**base)


class FakeLLM:
    def __init__(self, result: TaskIntent | Exception) -> None:
        self.result = result
        self.prompts: list[str] = []

    async def generate(self, *, system: str, prompt: str, output: type[M]) -> M:
        self.prompts.append(prompt)
        if isinstance(self.result, Exception):
            raise self.result
        assert output is TaskIntent
        return self.result  # type: ignore[return-value]


@pytest.fixture
def owner(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """그래프는 자체 세션으로 커밋하므로 테스트마다 owner를 나누고 끝나면 지운다."""
    owner_id = f"test-{uuid.uuid4()}"
    monkeypatch.setattr(get_settings(), "owner_id", owner_id)
    yield owner_id


@pytest.fixture(autouse=True)
async def cleanup(owner: str) -> Any:
    yield
    async with get_sessionmaker().begin() as session:
        ids = select(Task.id).where(Task.owner_id == owner).scalar_subquery()
        await session.execute(delete(TaskSuggestion).where(TaskSuggestion.task_id.in_(ids)))
        await session.execute(delete(TaskFieldEvidence).where(TaskFieldEvidence.task_id.in_(ids)))
        await session.execute(delete(TaskEvent).where(TaskEvent.task_id.in_(ids)))
        await session.execute(delete(Task).where(Task.owner_id == owner))


def use_llm(monkeypatch: pytest.MonkeyPatch, result: TaskIntent | Exception) -> FakeLLM:
    fake = FakeLLM(result)
    monkeypatch.setattr(graph_module, "get_llm", lambda: fake)
    return fake


async def run(text: str, run_id: str | None = None) -> str:
    config = {"configurable": {"run_id": run_id or str(uuid.uuid4())}}
    state = await graph_module.graph.ainvoke({"messages": [HumanMessage(text)]}, config)
    return str(state["messages"][-1].content)


async def test_create_then_read(monkeypatch: pytest.MonkeyPatch, owner: str) -> None:
    fake = use_llm(
        monkeypatch,
        intent(
            action="create",
            title="2325 Eval 실행",
            project="2325",
            project_origin="explicit",
            due_date=date(2026, 10, 31),
        ),
    )
    assert await run("내일까지 2325 Eval 돌려야 해") == (
        "등록했어요: 2325 Eval 실행 / [2325] / 마감 2026-10-31 / 우선순위 보통"
    )
    assert "사용자: 내일까지 2325 Eval 돌려야 해" in fake.prompts[0]

    use_llm(monkeypatch, intent(action="read"))
    assert await run("할 일 알려줘") == (
        "할 일 1개예요.\n1. 2325 Eval 실행 / [2325] / 마감 2026-10-31 / 우선순위 보통"
    )


async def test_same_run_does_not_create_twice(monkeypatch: pytest.MonkeyPatch, owner: str) -> None:
    use_llm(monkeypatch, intent(action="create", title="보고서"))

    await run("보고서 써야 해", run_id="same")
    second = await run("보고서 써야 해", run_id="same")

    assert second.startswith("이미 등록돼 있어요")
    async with get_sessionmaker()() as session:
        assert len((await session.scalars(select(Task).where(Task.owner_id == owner))).all()) == 1


async def test_missing_due_date_is_not_invented(
    monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    use_llm(monkeypatch, intent(action="create", title="B 고객사 건"))

    assert await run("나중에 B 고객사 건도 해야 해") == (
        "등록했어요: B 고객사 건 / 마감 없음 / 우선순위 보통"
    )


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (intent(action="unsupported"), "할 일 등록, 조회, 브리핑"),
        (intent(action="complete"), "어떤 일인지 찾지 못했어요"),
        (intent(action="complete", target_refs=["T9"]), "어떤 일인지 찾지 못했어요"),
        (intent(action="clarify", question="어떤 일을 등록할까요?"), "어떤 일을 등록할까요?"),
        (intent(action="create", title="  "), "어떤 일을 등록할까요?"),
        (LLMError("boom"), "요청을 해석하지 못했어요"),
    ],
)
async def test_non_mutating_replies(
    monkeypatch: pytest.MonkeyPatch, owner: str, result: TaskIntent | Exception, expected: str
) -> None:
    use_llm(monkeypatch, result)

    assert (await run("이 작업 배포해")).startswith(expected)
    async with get_sessionmaker()() as session:
        assert (await session.scalars(select(Task).where(Task.owner_id == owner))).all() == []


async def _seed(owner: str, *titles: str) -> None:
    async with get_sessionmaker().begin() as session:
        for title in titles:
            await create_task(
                session,
                owner_id=owner,
                request_id=f"seed-{title}",
                actor="test",
                new=NewTask(title=Evidence(title, "inferred")),
            )


async def _statuses(owner: str) -> dict[str, tuple[str, date | None]]:
    async with get_sessionmaker()() as session:
        tasks = await session.scalars(select(Task).where(Task.owner_id == owner))
        return {t.title: (t.status, t.due_date) for t in tasks}


async def test_complete_single_target(monkeypatch: pytest.MonkeyPatch, owner: str) -> None:
    await _seed(owner, "2325 Eval")
    fake = use_llm(monkeypatch, intent(action="complete", target_refs=["T1"]))

    reply = await run("2325 Eval 끝났어")

    assert reply == "완료 처리했어요: 2325 Eval / 마감 없음 / 우선순위 보통 / 완료"
    assert "T1: 2325 Eval / 마감 없음 / 우선순위 보통" in fake.prompts[0]
    assert (await _statuses(owner))["2325 Eval"][0] == "done"


async def test_postpone_changes_only_due_date(monkeypatch: pytest.MonkeyPatch, owner: str) -> None:
    await _seed(owner, "보고서")
    use_llm(monkeypatch, intent(action="update", target_refs=["T1"], due_date=date(2026, 11, 6)))

    assert (await run("그거 금요일까지 미뤄줘")).startswith("바꿨어요: 보고서 / 마감 2026-11-06")
    assert (await _statuses(owner))["보고서"] == ("todo", date(2026, 11, 6))


async def test_ambiguous_target_asks_instead_of_guessing(
    monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    await _seed(owner, "2325 Eval 준비", "2325 Eval 실행")
    use_llm(monkeypatch, intent(action="complete", target_refs=["T1", "T2"], ambiguous=True))

    reply = await run("2325 Eval 끝났어")

    assert reply.startswith("어느 일인가요?\n1. ")
    assert {s for s, _ in (await _statuses(owner)).values()} == {"todo"}


async def test_update_without_fields_asks(monkeypatch: pytest.MonkeyPatch, owner: str) -> None:
    await _seed(owner, "보고서")
    use_llm(monkeypatch, intent(action="update", target_refs=["T1"]))

    assert await run("보고서 바꿔줘") == "무엇을 바꿀지 알려 주세요."


async def test_same_run_does_not_complete_twice(
    monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    await _seed(owner, "보고서")
    use_llm(monkeypatch, intent(action="complete", target_refs=["T1"]))
    await run("보고서 끝", run_id="same")

    # 재시도 때는 후보 목록에 이미 완료된 Task가 새 version으로 보인다.
    second = await run("보고서 끝", run_id="same")

    # 이미 완료된 Task라 끝난 일 보호가 먼저 걸린다. 어느 쪽이든 다시 반영하지 않는다.
    assert second.startswith("이미 끝난 일이에요")
    async with get_sessionmaker()() as session:
        events = await session.scalars(
            select(TaskEvent.event_type).join(Task).where(Task.owner_id == owner)
        )
        assert sorted(events) == ["completed", "created"]


async def test_closed_task_is_not_updated(monkeypatch: pytest.MonkeyPatch, owner: str) -> None:
    await _seed(owner, "보고서")
    use_llm(monkeypatch, intent(action="complete", target_refs=["T1"]))
    await run("보고서 끝")
    use_llm(monkeypatch, intent(action="update", target_refs=["T1"], due_date=date(2026, 11, 6)))

    assert (await run("그거 금요일로 미뤄줘")).startswith("이미 끝난 일이에요")
    assert (await _statuses(owner))["보고서"] == ("done", None)

    use_llm(monkeypatch, intent(action="reopen", target_refs=["T1"]))
    assert (await run("보고서 다시 해야 해")).startswith("다시 열었어요: 보고서")


async def test_read_filters_by_status_and_project(
    monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    await _seed(owner, "A 일", "B 일")
    async with get_sessionmaker().begin() as session:
        tasks = await session.scalars(select(Task).where(Task.owner_id == owner))
        for task in tasks:
            if task.title == "A 일":
                task.status = "blocked"
                task.project = "2325 반영기"

    use_llm(monkeypatch, intent(action="read", statuses=["blocked"]))
    assert (await run("막힌 것만")).startswith("할 일 1개예요.\n1. A 일")

    use_llm(monkeypatch, intent(action="read", project="2325"))
    assert (await run("2325 할 일")).startswith("할 일 1개예요.\n1. A 일")


async def test_dismiss_removes_suggestion_but_keeps_task(
    monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    await _seed(owner, "보고서")
    async with get_sessionmaker().begin() as session:
        task = await session.scalar(select(Task).where(Task.owner_id == owner))
        assert task is not None
        await suggest_completion(
            session,
            owner_id=owner,
            task_id=task.id,
            source="slack",
            source_reference="r",
            explanation="보고함",
        )
    fake = use_llm(monkeypatch, intent(action="dismiss", target_refs=["T1"]))

    reply = await run("보고서 아직 안 끝났어")

    assert reply.startswith("제안을 내렸어요: 보고서")
    assert "완료 제안 있음" in fake.prompts[0]
    assert (await _statuses(owner))["보고서"][0] == "todo"
    async with get_sessionmaker()() as session:
        assert await list_pending_suggestions(session, owner_id=owner) == []


async def test_briefing_lists_due_suggestions_and_failures(
    monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    today = graph_module._today()
    async with get_sessionmaker().begin() as session:
        for title, due in (("지난 일", date(2020, 1, 1)), ("오늘 일", today)):
            await create_task(
                session,
                owner_id=owner,
                request_id=title,
                actor="t",
                new=NewTask(title=Evidence(title, "explicit"), due_date=Evidence(due, "explicit")),
            )
        session.add(TrackerRun(name="test-slack", ok=False, detail="토큰 만료"))
    use_llm(monkeypatch, intent(action="briefing"))

    text = await run("브리핑")

    assert "마감 지난 일\n- 지난 일" in text and "오늘 마감\n- 오늘 일" in text
    assert "수집기 문제\n- test-slack: 토큰 만료" in text
    async with get_sessionmaker().begin() as session:
        await session.execute(delete(TrackerRun).where(TrackerRun.name == "test-slack"))
