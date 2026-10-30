from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.tasks.values import Priority, TaskStatus

Origin = Literal["explicit", "inferred"]
Action = Literal[
    "create",
    "read",
    "briefing",
    "complete",
    "update",
    "cancel",
    "reopen",
    "dismiss",
    "clarify",
    "unsupported",
]
MUTATIONS: tuple[Action, ...] = ("complete", "update", "cancel", "reopen", "dismiss")


class TaskIntent(BaseModel):
    """LLM 구조화 출력. 값은 제안일 뿐이고 그래프가 검증한 뒤에 저장한다."""

    # structured output은 모든 필드가 required이고 추가 필드가 없어야 한다.
    model_config = ConfigDict(extra="forbid")

    action: Action
    target_refs: list[str] = Field(
        description="complete/update/cancel/reopen/dismiss: 대상 Task의 ref. 후보가 여럿이면 모두"
    )
    ambiguous: bool = Field(description="대상을 하나로 정할 수 없으면 true")
    title: str | None = Field(description="create: 제목. update: 바꿀 제목")
    description: str | None
    project: str | None = Field(
        description="create: 프로젝트. update: 바꿀 프로젝트. read: 이 프로젝트만 볼 때"
    )
    project_origin: Origin | None
    priority: Priority | None = Field(description="create: 우선순위. update: 바꿀 우선순위")
    priority_origin: Origin | None
    due_date: date | None = Field(description="create: 사용자가 말한 마감일. update: 새 마감일")
    due_from: date | None = Field(description="read: 조회 기간 시작일")
    due_to: date | None = Field(description="read: 조회 기간 종료일")
    include_done: bool = Field(description="read: 완료된 Task도 포함할지")
    statuses: list[TaskStatus] = Field(description="read: 이 상태만 볼 때. 아니면 빈 배열")
    question: str | None = Field(description="clarify: 사용자에게 물을 한 문장")
    reason: str = Field(description="판단 근거 한 문장")
