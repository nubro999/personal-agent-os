"""2325(maintenance_list)에서 내 담당 건을 Task로 맞춘다.

2325 워크플로(stage)가 기준이다. ax 상태(status_group)는 2325와 자주 어긋나서
stage가 비어 있을 때만 끝났는지 보는 데 쓴다.
- 수정중(fixing, stage 없음) -> in_progress
- 고객사 소통중(communicating) -> blocked
- 팀장 확인중(review), 배포완료(deployed) -> done
- 반려(rejected) -> cancelled
Task는 2325 건을 따라가는 거울이라 상태가 바뀌면 그대로 반영한다. 다만 2325 쪽 상태가
바뀌었을 때만 반영해서, 사용자가 채팅으로 바꾼 상태를 매번 되돌리지 않는다.

실행: uv run python -m app.trackers.maintenance
"""

import asyncio
import hashlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pymysql
from dotenv import dotenv_values
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.domain.tasks.service import (
    Evidence,
    NewTask,
    TaskChanges,
    VersionConflict,
    create_task,
    update_task,
)
from app.domain.tasks.values import TaskStatus
from app.persistence.db import get_sessionmaker
from app.persistence.models import ActivitySession, Task
from app.trackers.remote_2325 import Remote, RemoteError, tunnel_up

log = logging.getLogger(__name__)

SOURCE = "2325"
TITLE_FEEDBACK_LIMIT = 60
DETAIL_LIMIT = 4000
# 피드백 시트 행은 "[1254행] [수정이 필요한 AI답변] ... [피드백 내용] ..." 꼴이다.
_ROW = re.compile(r"^\s*\[\d+행\]\s*")
_SECTION = re.compile(r"\[(수정이 필요한 AI답변|피드백 내용|적정 답변 예시)\]")
CONNECT_ATTEMPTS = 4
CONNECT_RETRY_SECONDS = 10
_STAGE_STATUS: dict[str, TaskStatus] = {
    "fixing": "in_progress",
    "communicating": "blocked",
    "review": "done",
    "deployed": "done",
    "rejected": "cancelled",
}

QUERY = """
SELECT m.item_key, x.human_id, x.company, x.feedback, x.status_group, x.received_at,
       s.stage, s.updated_at AS stage_updated_at
FROM maintenance_assignment m
JOIN assignee a ON a.id = m.assignee_id AND a.name = %s
JOIN wi_ax_item x ON x.item_key = m.item_key
LEFT JOIN maintenance_status s ON s.item_key = m.item_key
"""


@dataclass(frozen=True)
class MaintItem:
    item_key: str
    human_id: str
    company: str
    feedback: str
    status_group: str
    received_at: datetime | None
    stage: str | None

    @property
    def status(self) -> TaskStatus:
        if self.stage in _STAGE_STATUS:
            return _STAGE_STATUS[self.stage]
        return "done" if self.status_group == "끝남" else "in_progress"

    @property
    def state_key(self) -> str:
        """2325 쪽 상태가 바뀌었는지 비교하는 값."""
        return f"{self.stage or '-'}|{self.status_group or '-'}"

    @property
    def title(self) -> str:
        summary = feedback_summary(self.feedback)
        if len(summary) > TITLE_FEEDBACK_LIMIT:
            summary = summary[:TITLE_FEEDBACK_LIMIT].rstrip() + "…"
        return f"{self.human_id} {summary}".strip() or self.item_key

    @property
    def detail(self) -> str | None:
        text = self.feedback.replace("\r\n", "\n").strip()
        return text[:DETAIL_LIMIT] or None


def feedback_summary(feedback: str) -> str:
    """제목에 쓸 한 줄. 시트 행이면 피드백 내용을, 비었으면 고칠 AI답변을 쓴다."""
    text = _ROW.sub("", feedback)
    parts: dict[str, str] = {}
    pieces = _SECTION.split(text)
    for name, body in zip(pieces[1::2], pieces[2::2], strict=True):
        parts[name] = body
    for name in ("피드백 내용", "수정이 필요한 AI답변"):
        body = _flat(parts.get(name, "")).strip(" -")
        if len(body) > 1:
            return body
    # 대화 링크만 붙인 피드백도 있다. 링크를 빼고 남는 말이 있으면 그것을 쓴다.
    return _flat(text) or re.sub(r"\s+", " ", text).strip()


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"https?://\S+", " ", text)).strip()


def connect(
    env: dict[str, str | None], port: int, database: str | None = None
) -> "pymysql.Connection[Any]":
    """전용 터널이 재시작 중일 수 있어 몇 번 더 시도한다. 채팅 DB도 같은 서버, 같은 계정이다."""
    for attempt in range(CONNECT_ATTEMPTS):
        try:
            return pymysql.connect(
                host="127.0.0.1",
                port=port,
                user=env["DB_USER"] or "",
                password=env["DB_PASSWORD"] or "",
                database=database or env["MAINT_DB_NAME"] or "",
                connect_timeout=5,
                charset="utf8mb4",
            )
        except pymysql.err.OperationalError:
            if attempt == CONNECT_ATTEMPTS - 1:
                raise
            time.sleep(CONNECT_RETRY_SECONDS)
    raise AssertionError("unreachable")


def fetch_items(env_file: Path, assignee: str, port: int) -> list[MaintItem]:
    conn = connect(dotenv_values(env_file), port)
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(QUERY, (assignee,))
            rows: list[dict[str, Any]] = list(cur.fetchall())
    finally:
        conn.close()
    return [
        MaintItem(
            item_key=r["item_key"],
            human_id=r["human_id"] or "",
            company=r["company"] or "",
            feedback=r["feedback"] or "",
            status_group=r["status_group"] or "",
            # 2325 DB의 DATETIME은 KST 기준 naive 값이다.
            received_at=r["received_at"],
            stage=r["stage"],
        )
        for r in rows
    ]


@dataclass
class MaintStats:
    created: int = 0
    updated: int = 0
    skipped_old: int = 0


async def sync_item(
    session: AsyncSession, *, owner_id: str, item: MaintItem, since: datetime, stats: MaintStats
) -> None:
    reference = f"{SOURCE}:{item.item_key}"
    task = await session.scalar(
        select(Task).where(Task.owner_id == owner_id, Task.request_id == reference)
    )
    if task is None:
        is_open = item.status not in ("done", "cancelled")
        recent = item.received_at is not None and item.received_at >= since
        if not (is_open and recent):
            # 상태만 기록해 두고 다음부터는 2325 쪽이 바뀔 때만 다시 본다.
            stats.skipped_old += is_open
        else:
            result = await create_task(
                session,
                owner_id=owner_id,
                request_id=reference,
                actor="maintenance_tracker",
                new=NewTask(
                    title=Evidence(item.title, "external", None, reference),
                    description=Evidence(item.detail, "external", None, reference)
                    if item.detail
                    else None,
                    project=Evidence(item.company, "external", None, reference)
                    if item.company
                    else None,
                    status=Evidence(item.status, "external", item.state_key, reference),
                    source=SOURCE,
                ),
            )
            stats.created += result.created
    elif task.status != item.status:
        try:
            result_u = await update_task(
                session,
                owner_id=owner_id,
                task_id=task.id,
                expected_version=task.version,
                request_id=f"{reference}:{item.state_key}",
                actor="maintenance_tracker",
                changes=TaskChanges(
                    status=Evidence(item.status, "external", item.state_key, reference)
                ),
                reason=f"2325 상태 {item.state_key}",
            )
        except VersionConflict:
            # 다음 실행에서 다시 본다. 동기화 기록도 올리지 않는다.
            return
        stats.updated += result_u.applied

    await session.execute(
        pg_insert(ActivitySession)
        .values(owner_id=owner_id, source=SOURCE, external_id=item.item_key, summary=item.state_key)
        .on_conflict_do_update(
            index_elements=["owner_id", "source", "external_id"],
            set_={"summary": item.state_key, "updated_at": datetime.now(UTC)},
        )
    )


async def refresh_texts(items: list[MaintItem]) -> int:
    """열린 일의 제목과 원문을 2325 피드백에 맞춘다. 제목 규칙이 바뀌거나 피드백이 고쳐졌을 때."""
    settings = get_settings()
    by_reference = {f"{SOURCE}:{item.item_key}": item for item in items}
    async with get_sessionmaker()() as session:
        tasks = list(
            await session.scalars(
                select(Task).where(
                    Task.owner_id == settings.owner_id,
                    Task.source == SOURCE,
                    Task.status.not_in(("done", "cancelled")),
                )
            )
        )
    changed = 0
    for task in tasks:
        item = by_reference.get(task.request_id or "")
        if item is None or (task.title, task.description) == (item.title, item.detail):
            continue
        reference = f"{SOURCE}:{item.item_key}"
        digest = hashlib.sha1(f"{item.title}\n{item.detail}".encode()).hexdigest()[:12]
        async with get_sessionmaker().begin() as session:
            try:
                result = await update_task(
                    session,
                    owner_id=settings.owner_id,
                    task_id=task.id,
                    expected_version=task.version,
                    request_id=f"{reference}:text:{digest}",
                    actor="maintenance_tracker",
                    changes=TaskChanges(
                        title=Evidence(item.title, "external", None, reference),
                        description=Evidence(item.detail, "external", None, reference)
                        if item.detail
                        else None,
                    ),
                    reason="2325 피드백 제목",
                )
            except VersionConflict:
                continue
        changed += result.applied
    return changed


async def run(items: list[MaintItem], now: datetime | None = None) -> MaintStats:
    settings = get_settings()
    # received_at이 KST naive라 비교 기준도 같은 형태로 맞춘다.
    now = now or datetime.now()
    since = now - timedelta(days=settings.maint_lookback_days)
    stats = MaintStats()
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(ActivitySession.external_id, ActivitySession.summary).where(
                ActivitySession.owner_id == settings.owner_id, ActivitySession.source == SOURCE
            )
        )
        synced = {external_id: summary for external_id, summary in rows.all()}
    for item in items:
        if synced.get(item.item_key) == item.state_key:
            continue
        async with get_sessionmaker().begin() as session:
            await sync_item(
                session, owner_id=settings.owner_id, item=item, since=since, stats=stats
            )
    return stats


def remote_items(
    remote: Remote, assignee: str, needs_detail: Callable[[MaintItem], bool]
) -> list[MaintItem]:
    """터널이 닫혔을 때 서버 맥 2325 API 로 같은 건을 읽는다.

    목록에는 피드백 앞부분만 있다. 동기화하거나 제목을 다시 맞출 건(needs_detail)만 상세를 읽어
    전체 피드백과 접수 시각을 채운다. 담당 건이 900개가 넘어 건마다 상세를 부르지 않는다.
    휴지통으로 간 건은 목록에 없어 빠진다.
    """
    mine = remote.my_item_keys(assignee)
    stages = remote.stages()
    items: list[MaintItem] = []
    for row in remote.items():
        key = row["id"]
        if key not in mine:
            continue
        item = MaintItem(
            item_key=key,
            human_id=row.get("display_id") or "",
            company=row.get("company") or "",
            feedback=row.get("detail_preview") or "",
            status_group=row.get("status_group") or "",
            received_at=None,
            stage=stages.get(key),
        )
        if needs_detail(item):
            detail = remote.item(key)
            if detail is None:
                continue
            item = replace(
                item,
                human_id=detail.get("human_id") or item.human_id,
                company=detail.get("company") or item.company,
                feedback=detail.get("feedback") or "",
                received_at=detail["received_at"],
            )
        items.append(item)
    return items


async def _needs_detail() -> Callable[[MaintItem], bool]:
    """run이 동기화할 건(2325 상태가 바뀜)과 refresh_texts가 볼 건(열린 일)."""
    settings = get_settings()
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(ActivitySession.external_id, ActivitySession.summary).where(
                ActivitySession.owner_id == settings.owner_id, ActivitySession.source == SOURCE
            )
        )
        synced = {external_id: summary for external_id, summary in rows.all()}
        open_refs = set(
            await session.scalars(
                select(Task.request_id).where(
                    Task.owner_id == settings.owner_id,
                    Task.source == SOURCE,
                    Task.status.not_in(("done", "cancelled")),
                )
            )
        )
    return lambda item: (
        synced.get(item.item_key) != item.state_key or f"{SOURCE}:{item.item_key}" in open_refs
    )


async def load_items() -> list[MaintItem]:
    """터널이 열려 있으면 DB, 닫혀 있으면 서버 맥 2325 API."""
    settings = get_settings()
    if tunnel_up(settings.maint_db_port):
        return await asyncio.to_thread(
            fetch_items,
            settings.maint_env_file,
            settings.maint_assignee_name,
            settings.maint_db_port,
        )
    needs_detail = await _needs_detail()

    def fetch() -> list[MaintItem]:
        with Remote(settings.maint_remote_session_file, settings.maint_remote_url) as remote:
            return remote_items(remote, settings.maint_assignee_name, needs_detail)

    log.info("2325 tunnel closed; reading the server mac 2325 API")
    return await asyncio.to_thread(fetch)


async def run_once() -> str | None:
    try:
        items = await load_items()
    except (pymysql.err.OperationalError, KeyError, FileNotFoundError) as exc:
        # 전용 터널이 끊겨 있으면 이번 주기는 건너뛴다. 다음 주기에 다시 본다.
        log.warning("2325 tracker skipped: %s", exc)
        return f"2325 DB 연결 실패: {type(exc).__name__}"
    except RemoteError as exc:
        log.warning("2325 tracker skipped: %s", exc)
        return f"2325 터널이 닫혀 있고 서버 맥도 실패: {exc}"[:200]
    stats = await run(items)
    refreshed = await refresh_texts(items)
    log.info(
        "2325 tracker: items=%d created=%d updated=%d skipped_old=%d refreshed=%d",
        len(items),
        stats.created,
        stats.updated,
        stats.skipped_old,
        refreshed,
    )
    return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run_once())


if __name__ == "__main__":
    main()
