"""Task 영속 모델. 설계 원본은 docs/DATABASE_SCHEMA.md."""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.tasks.values import (
    FIELD_ORIGINS,
    LINK_RELATIONS,
    SUGGESTION_KINDS,
    SUGGESTION_STATUSES,
    TASK_PRIORITIES,
    TASK_STATUSES,
)
from app.persistence.base import Base


def _in_list(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("owner_id", "request_id"),
        CheckConstraint(_in_list("status", TASK_STATUSES), name="status"),
        CheckConstraint(_in_list("priority", TASK_PRIORITIES), name="priority"),
        CheckConstraint(
            "estimated_minutes IS NULL OR estimated_minutes > 0", name="estimated_minutes_positive"
        ),
        Index("idx_tasks_owner_status_due", "owner_id", "status", "due_date"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[str] = mapped_column(Text, nullable=False)
    # 인증된 요청 envelope에서 온 값만 넣는다. LLM 출력에서 만들지 않는다.
    request_id: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    project: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="todo")
    priority: Mapped[str] = mapped_column(Text, nullable=False, server_default="medium")
    # 날짜만 정해진 마감은 due_date, 시각까지 정해진 마감만 due_at. 23:59를 지어내지 않는다.
    due_date: Mapped[date | None] = mapped_column(Date)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estimated_minutes: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="natural_language")
    # 낙관적 동시성 제어용. UPDATE ... WHERE version = :expected 후 증가시킨다.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class TaskEvent(Base):
    __tablename__ = "task_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    request_id: Mapped[str | None] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    before_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


Index("idx_task_events_task_created", TaskEvent.task_id, TaskEvent.created_at.desc())


class TaskFieldEvidence(Base):
    __tablename__ = "task_field_evidence"
    __table_args__ = (CheckConstraint(_in_list("origin", FIELD_ORIGINS), name="origin"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    field_name: Mapped[str] = mapped_column(Text, nullable=False)
    field_value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    source_reference: Mapped[str | None] = mapped_column(Text)
    explanation: Mapped[str | None] = mapped_column(Text)
    confirmed: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = _created_at()


class TaskLink(Base):
    __tablename__ = "task_links"
    __table_args__ = (
        CheckConstraint(_in_list("relation", LINK_RELATIONS), name="relation"),
        CheckConstraint("parent_task_id <> child_task_id", name="no_self_link"),
    )

    parent_task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    child_task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    relation: Mapped[str] = mapped_column(Text, primary_key=True)


class TaskSuggestion(Base):
    """수집기가 낸 제안. 사용자가 확인해야 Task에 반영된다."""

    __tablename__ = "task_suggestions"
    __table_args__ = (
        CheckConstraint(_in_list("kind", SUGGESTION_KINDS), name="kind"),
        CheckConstraint(_in_list("status", SUGGESTION_STATUSES), name="status"),
        UniqueConstraint("task_id", "kind", "source_reference"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_reference: Mapped[str] = mapped_column(Text, nullable=False)
    explanation: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    created_at: Mapped[datetime] = _created_at()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TrackerRun(Base):
    """수집기 실행 기록. 실패를 브리핑에서 보여 주기 위한 것이다."""

    __tablename__ = "tracker_runs"
    __table_args__ = (Index("idx_tracker_runs_name_at", "name", "finished_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    ok: Mapped[bool] = mapped_column(nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime] = _created_at()


class ActivitySession(Base):
    """수집기가 어디까지 읽었는지. 원문은 저장하지 않고 요약만 남긴다."""

    __tablename__ = "activity_sessions"
    __table_args__ = (UniqueConstraint("owner_id", "source", "external_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    workspace: Mapped[str | None] = mapped_column(Text)
    processed_lines: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
