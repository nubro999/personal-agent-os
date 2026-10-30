"""Task 값 집합. DB CHECK 제약과 도메인 검증이 같은 목록을 쓴다."""

from typing import Final, Literal

TASK_STATUSES: Final = ("todo", "in_progress", "blocked", "done", "cancelled")
TASK_PRIORITIES: Final = ("low", "medium", "high", "urgent")
FIELD_ORIGINS: Final = ("explicit", "inferred", "default", "external")
LINK_RELATIONS: Final = ("subtask", "blocks", "related")
SUGGESTION_KINDS: Final = ("complete",)
SUGGESTION_STATUSES: Final = ("pending", "accepted", "dismissed")
DRAFT_STATUSES: Final = ("pending", "used", "dismissed")

TaskStatus = Literal["todo", "in_progress", "blocked", "done", "cancelled"]
Priority = Literal["low", "medium", "high", "urgent"]
FieldOrigin = Literal["explicit", "inferred", "default", "external"]
