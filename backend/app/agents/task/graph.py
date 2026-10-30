"""자연어 Task Agent 그래프.

parse(LLM) -> create | read | mutate | reply. LLM은 의도, 필드, 대상 후보만 제안하고
대상 확정과 저장은 결정적 코드와 도메인 서비스가 한다.
"""

import uuid
from datetime import date, datetime
from typing import Any, Literal, TypedDict
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, MessagesState, StateGraph

from app.agents.task.prompts import SYSTEM_PROMPT, build_prompt
from app.agents.task.schemas import MUTATIONS, TaskIntent
from app.config import get_settings
from app.domain.tasks.service import (
    Evidence,
    NewTask,
    TaskChanges,
    TaskNotFound,
    VersionConflict,
    create_task,
    list_pending_suggestions,
    list_recent_candidates,
    list_tasks,
    update_task,
)
from app.domain.tasks.values import TaskStatus
from app.llm.base import LLMError
from app.llm.factory import get_llm
from app.persistence.db import get_sessionmaker
from app.persistence.models import Task

TRANSCRIPT_LIMIT = 10
_PRIORITY_LABEL = {"urgent": "긴급", "high": "높음", "medium": "보통", "low": "낮음"}
_STATUS_LABEL = {"in_progress": "진행 중", "blocked": "막힘", "done": "완료", "cancelled": "취소"}
_STATUS_FOR: dict[str, TaskStatus] = {"complete": "done", "cancel": "cancelled", "reopen": "todo"}


class Target(TypedDict):
    id: str
    version: int
    label: str
    closed: bool


class TaskAgentState(MessagesState):
    intent: TaskIntent | None
    targets: list[Target]


def _text(message: AnyMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(
        block.get("text", "") if isinstance(block, dict) else str(block) for block in content
    )


def _today() -> date:
    return datetime.now(ZoneInfo(get_settings().timezone)).date()


def _request_id(config: RunnableConfig) -> str:
    # 같은 run 안에서 노드가 재시도돼도 같은 값이라 Task 생성이나 변경이 두 번 반영되지 않는다.
    run_id = config.get("configurable", {}).get("run_id") or config.get("metadata", {}).get(
        "run_id"
    )
    return f"run:{run_id}" if run_id else f"local:{uuid.uuid4()}"


def _describe(task: Task) -> str:
    parts = [task.title]
    if task.project:
        parts.append(f"[{task.project}]")
    parts.append(f"마감 {task.due_date.isoformat()}" if task.due_date else "마감 없음")
    parts.append(f"우선순위 {_PRIORITY_LABEL.get(task.priority, task.priority)}")
    if task.status in _STATUS_LABEL:
        parts.append(_STATUS_LABEL[task.status])
    return " / ".join(parts)


def _last_user_text(state: TaskAgentState) -> str:
    return next((_text(m) for m in reversed(state["messages"]) if m.type == "human"), "")


async def parse(state: TaskAgentState) -> dict[str, Any]:
    transcript = [
        ("사용자" if m.type == "human" else "비서", _text(m))
        for m in state["messages"][-TRANSCRIPT_LIMIT:]
        if m.type in ("human", "ai")
    ]
    async with get_sessionmaker()() as session:
        candidates = await list_recent_candidates(session, owner_id=get_settings().owner_id)
    refs = {f"T{i}": task for i, task in enumerate(candidates, 1)}
    try:
        intent = await get_llm().generate(
            system=SYSTEM_PROMPT,
            prompt=build_prompt(
                _today(), transcript, [f"{ref}: {_describe(t)}" for ref, t in refs.items()]
            ),
            output=TaskIntent,
        )
    except LLMError:
        return {
            "intent": None,
            "targets": [],
            "messages": [AIMessage("요청을 해석하지 못했어요. 다시 말해 주세요.")],
        }
    # LLM이 만든 ref는 후보 목록에 있는 것만 받는다.
    targets: list[Target] = [
        {
            "id": str(refs[r].id),
            "version": refs[r].version,
            "label": _describe(refs[r]),
            "closed": refs[r].status in ("done", "cancelled"),
        }
        for r in dict.fromkeys(intent.target_refs)
        if r in refs
    ]
    return {"intent": intent, "targets": targets}


def route(state: TaskAgentState) -> Literal["create", "read", "mutate", "reply", "__end__"]:
    intent = state.get("intent")
    if intent is None:
        return "__end__"
    if intent.action == "create":
        return "create" if (intent.title or "").strip() else "reply"
    if intent.action == "read":
        return "read"
    if intent.action in MUTATIONS and state["targets"] and not intent.ambiguous:
        return "mutate"
    return "reply"


def _new_task(intent: TaskIntent) -> NewTask:
    assert intent.title is not None
    return NewTask(
        title=Evidence(intent.title.strip(), "inferred", intent.reason),
        description=Evidence(intent.description, "inferred") if intent.description else None,
        project=(
            Evidence(intent.project, intent.project_origin or "inferred")
            if intent.project
            else None
        ),
        priority=(
            Evidence(intent.priority, intent.priority_origin or "inferred")
            if intent.priority
            else None
        ),
        # 마감일은 사용자가 말한 경우에만 LLM이 채운다. 상대 날짜도 사용자가 말한 값이다.
        due_date=Evidence(intent.due_date, "explicit") if intent.due_date else None,
    )


def _changes(intent: TaskIntent) -> TaskChanges:
    if intent.action in _STATUS_FOR:
        return TaskChanges(status=Evidence(_STATUS_FOR[intent.action], "explicit"))
    # update는 사용자가 바꾸라고 한 값만 온다.
    return TaskChanges(
        title=Evidence(intent.title, "explicit") if intent.title else None,
        project=Evidence(intent.project, "explicit") if intent.project else None,
        priority=Evidence(intent.priority, "explicit") if intent.priority else None,
        due_date=Evidence(intent.due_date, "explicit") if intent.due_date else None,
    )


async def create(state: TaskAgentState, config: RunnableConfig) -> dict[str, Any]:
    intent = state["intent"]
    assert intent is not None
    async with get_sessionmaker().begin() as session:
        result = await create_task(
            session,
            owner_id=get_settings().owner_id,
            request_id=_request_id(config),
            actor="task_agent",
            new=_new_task(intent),
        )
    verb = "등록했어요" if result.created else "이미 등록돼 있어요"
    return {"messages": [AIMessage(f"{verb}: {_describe(result.task)}")]}


async def read(state: TaskAgentState) -> dict[str, Any]:
    intent = state["intent"]
    assert intent is not None
    owner_id = get_settings().owner_id
    async with get_sessionmaker()() as session:
        tasks = await list_tasks(
            session,
            owner_id=owner_id,
            due_from=intent.due_from,
            due_to=intent.due_to,
            include_done=intent.include_done,
        )
        suggestions = await list_pending_suggestions(session, owner_id=owner_id)
    if tasks:
        lines = [f"{i}. {_describe(t)}" for i, t in enumerate(tasks, 1)]
        text = f"할 일 {len(tasks)}개예요.\n" + "\n".join(lines)
    else:
        text = "해당하는 할 일이 없어요."
    if suggestions:
        # 수집기가 낸 완료 제안. "OO 끝났어"라고 하면 complete 경로로 정리된다.
        hints = [f"- {task.title}: {s.explanation or '근거 없음'}" for s, task in suggestions]
        text += "\n\n끝난 것 같은 일이 있어요. 맞으면 끝났다고 말해 주세요.\n" + "\n".join(hints)
    return {"messages": [AIMessage(text)]}


_DONE_VERB = {"complete": "완료 처리했어요", "cancel": "취소했어요", "reopen": "다시 열었어요"}


async def mutate(state: TaskAgentState, config: RunnableConfig) -> dict[str, Any]:
    intent = state["intent"]
    assert intent is not None
    changes = _changes(intent)
    if changes == TaskChanges():
        return {"messages": [AIMessage("무엇을 바꿀지 알려 주세요.")]}

    lines: list[str] = []
    for target in state["targets"]:
        # 끝난 일을 조용히 고치지 않는다. 다시 열지는 사용자가 정한다.
        if target["closed"] and intent.action in ("update", "complete"):
            lines.append(
                f"이미 끝난 일이에요. 다시 하려면 다시 열어 달라고 해 주세요: {target['label']}"
            )
            continue
        try:
            async with get_sessionmaker().begin() as session:
                result = await update_task(
                    session,
                    owner_id=get_settings().owner_id,
                    task_id=uuid.UUID(target["id"]),
                    expected_version=target["version"],
                    request_id=_request_id(config),
                    actor="task_agent",
                    changes=changes,
                    reason=_last_user_text(state),
                )
        except VersionConflict:
            lines.append(f"그사이 바뀌어서 반영하지 않았어요. 다시 말해 주세요: {target['label']}")
            continue
        except TaskNotFound:
            lines.append(f"찾을 수 없어요: {target['label']}")
            continue
        if not result.applied:
            lines.append(f"바뀐 게 없어요: {_describe(result.task)}")
        else:
            verb = _DONE_VERB.get(intent.action, "바꿨어요")
            lines.append(f"{verb}: {_describe(result.task)}")
    return {"messages": [AIMessage("\n".join(lines))]}


async def reply(state: TaskAgentState) -> dict[str, Any]:
    intent = state["intent"]
    assert intent is not None
    if intent.action in MUTATIONS:
        if not state["targets"]:
            text = "어떤 일인지 찾지 못했어요. 제목을 조금 더 구체적으로 말해 주세요."
        else:
            options = "\n".join(f"{i}. {t['label']}" for i, t in enumerate(state["targets"], 1))
            text = f"어느 일인가요?\n{options}"
    elif intent.action in ("clarify", "create"):
        text = intent.question or "어떤 일을 등록할까요?"
    else:
        text = "할 일 등록, 조회, 완료, 수정, 미루기, 취소를 할 수 있어요."
    return {"messages": [AIMessage(text)]}


def build_graph() -> Any:
    builder = StateGraph(TaskAgentState)
    builder.add_node("parse", parse)
    builder.add_node("create", create)
    builder.add_node("read", read)
    builder.add_node("mutate", mutate)
    builder.add_node("reply", reply)
    builder.add_edge(START, "parse")
    builder.add_conditional_edges("parse", route)
    for node in ("create", "read", "mutate", "reply"):
        builder.add_edge(node, END)
    return builder.compile()


graph = build_graph()
