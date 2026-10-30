"""Slack에서 나를 멘션했는데 아직 답하지 않은 스레드를 Task로 맞춘다.

스레드 단위로 본다. 루트에 CS 번호가 있으면 2325 Task에 잇는다. 내 2325 Task가 없는 CS 스레드는
남의 담당이거나 이미 끝난 건이라 건너뛴다(여러 명을 같이 부르는 리마인드가 대부분이다).
- 요청인데 2325 Task가 막힘(고객사 소통중)이면 진행 중으로 바꾼다. 내 차례가 왔다는 뜻이다.
- 해결 보고면 완료 제안만 남긴다.
CS 번호가 없는 요청은 새 Task로 만들고, 나중에 내가 그 스레드에 답하면 완료 제안을 남긴다.

실행: uv run python -m app.trackers.slack
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.task.prompts import calendar_lines
from app.config import get_settings
from app.domain.tasks.service import (
    Evidence,
    NewTask,
    TaskChanges,
    VersionConflict,
    create_task,
    suggest_completion,
    update_task,
)
from app.llm.base import LLMError, StructuredLLM
from app.llm.factory import get_llm
from app.persistence.db import get_sessionmaker
from app.persistence.models import ActivitySession, Task
from app.trackers.prompts import SLACK_PROMPT

log = logging.getLogger(__name__)

SOURCE = "slack"
CS_PATTERN = re.compile(r"CS-\d{7}")
CONTEXT_MESSAGES = 6
MESSAGE_TEXT_LIMIT = 300
SEARCH_PAGES = 3


class SlackClient(Protocol):
    async def search_mentions(self, user_id: str, after: date) -> list[dict[str, Any]]: ...

    async def replies(self, channel: str, ts: str) -> list[dict[str, Any]]: ...


class HttpSlackClient:
    def __init__(self, token: str) -> None:
        self._http = httpx.AsyncClient(
            base_url="https://slack.com/api/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )

    async def _get(self, method: str, **params: Any) -> dict[str, Any]:
        response = await self._http.get(method, params=params)
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"slack {method} failed: {data.get('error')}")
        return data

    async def search_mentions(self, user_id: str, after: date) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for page in range(1, SEARCH_PAGES + 1):
            data = await self._get(
                "search.messages",
                query=f"<@{user_id}> after:{(after - timedelta(days=1)).isoformat()}",
                count=100,
                page=page,
                sort="timestamp",
            )
            matches += data["messages"]["matches"]
            if page >= data["messages"]["paging"]["pages"]:
                break
        return matches

    async def replies(self, channel: str, ts: str) -> list[dict[str, Any]]:
        data = await self._get("conversations.replies", channel=channel, ts=ts, limit=200)
        messages: list[dict[str, Any]] = data["messages"]
        return messages

    async def aclose(self) -> None:
        await self._http.aclose()


def thread_key(match: dict[str, Any]) -> tuple[str, str]:
    """검색 결과 하나가 속한 (채널, 스레드 루트 ts)."""
    permalink = match.get("permalink") or ""
    found = re.search(r"thread_ts=([0-9.]+)", permalink)
    return match["channel"]["id"], found.group(1) if found else match["ts"]


@dataclass
class ThreadState:
    channel: str
    thread_ts: str
    root_text: str
    cs_id: str | None
    pending: bool
    marker: str
    context: list[str] = field(default_factory=list)

    @property
    def reference(self) -> str:
        return f"{SOURCE}:{self.channel}:{self.thread_ts}"


def analyze_thread(
    channel: str, thread_ts: str, messages: list[dict[str, Any]], me: str
) -> ThreadState:
    """나를 멘션한 마지막 메시지가 내 마지막 메시지보다 뒤면 내 차례(pending)다."""
    mention = f"<@{me}>"
    my_last = max((float(m["ts"]) for m in messages if m.get("user") == me), default=0.0)
    mention_last = max(
        (
            float(m["ts"])
            for m in messages
            if m.get("user") != me and mention in (m.get("text") or "")
        ),
        default=0.0,
    )
    root_text = (messages[0].get("text") or "") if messages else ""
    found = CS_PATTERN.search(root_text)
    context = [
        f"{'나' if m.get('user') == me else '상대'}: "
        + re.sub(r"\s+", " ", (m.get("text") or "").replace(mention, "@나"))[:MESSAGE_TEXT_LIMIT]
        for m in messages[1:][-CONTEXT_MESSAGES:]
        if (m.get("text") or "").strip()
    ]
    return ThreadState(
        channel=channel,
        thread_ts=thread_ts,
        root_text=root_text[:MESSAGE_TEXT_LIMIT],
        cs_id=found.group(0) if found else None,
        pending=mention_last > my_last,
        marker=f"{max(my_last, mention_last):.6f}",
        context=context,
    )


class ThreadTriage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str
    kind: Literal["request", "done_report", "fyi"]
    title: str | None
    project: str | None
    due_date: date | None
    evidence: str


class SlackTriage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    threads: list[ThreadTriage] = Field(description="입력한 스레드마다 하나")


def _triage_prompt(today: date, threads: dict[str, ThreadState]) -> str:
    lines = [*calendar_lines(today), ""]
    for ref, t in threads.items():
        lines += [f"[{ref}] 루트: {t.root_text}", *t.context, ""]
    return "\n".join(lines)


@dataclass
class SlackStats:
    threads: int = 0
    created: int = 0
    started: int = 0
    suggested: int = 0


async def _cs_task(session: AsyncSession, owner_id: str, cs_id: str) -> Task | None:
    task: Task | None = await session.scalar(
        select(Task)
        .where(Task.owner_id == owner_id, Task.title.startswith(f"{cs_id} "))
        .order_by(Task.created_at.desc())
        .limit(1)
    )
    return task


async def _apply(
    session: AsyncSession,
    *,
    owner_id: str,
    thread: ThreadState,
    triage: ThreadTriage | None,
    stats: SlackStats,
) -> None:
    reference = thread.reference
    own = await session.scalar(
        select(Task).where(Task.owner_id == owner_id, Task.request_id == reference)
    )
    if not thread.pending:
        # 내가 답했다. 이 스레드로 만든 Task가 열려 있으면 끝났는지 묻는다.
        if own is not None and own.status not in ("done", "cancelled"):
            stats.suggested += await suggest_completion(
                session,
                owner_id=owner_id,
                task_id=own.id,
                source=SOURCE,
                source_reference=f"{reference}:{thread.marker}",
                explanation="Slack 스레드에 답했어요",
            )
        return
    if triage is None or triage.kind == "fyi":
        return

    linked = own or (await _cs_task(session, owner_id, thread.cs_id) if thread.cs_id else None)
    if linked is None and thread.cs_id:
        return
    if linked is not None:
        if linked.status in ("done", "cancelled"):
            return
        if triage.kind == "done_report":
            stats.suggested += await suggest_completion(
                session,
                owner_id=owner_id,
                task_id=linked.id,
                source=SOURCE,
                source_reference=f"{reference}:{thread.marker}",
                explanation=triage.evidence,
            )
        elif linked.status in ("blocked", "todo"):
            try:
                await update_task(
                    session,
                    owner_id=owner_id,
                    task_id=linked.id,
                    expected_version=linked.version,
                    request_id=f"{reference}:{thread.marker}",
                    actor="slack_tracker",
                    changes=TaskChanges(
                        status=Evidence("in_progress", "external", triage.evidence, reference)
                    ),
                    reason=triage.evidence,
                )
                stats.started += 1
            except VersionConflict:
                pass
        return

    if triage.kind == "request" and triage.title:
        result = await create_task(
            session,
            owner_id=owner_id,
            request_id=reference,
            actor="slack_tracker",
            new=NewTask(
                title=Evidence(triage.title, "external", triage.evidence, reference),
                project=(
                    Evidence(triage.project, "external", None, reference)
                    if triage.project
                    else None
                ),
                due_date=(
                    Evidence(triage.due_date, "external", triage.evidence, reference)
                    if triage.due_date
                    else None
                ),
                source=SOURCE,
            ),
        )
        stats.created += result.created


async def run(client: SlackClient, llm: StructuredLLM, now: datetime | None = None) -> SlackStats:
    settings = get_settings()
    now = now or datetime.now(ZoneInfo(settings.timezone))
    owner_id = settings.owner_id
    stats = SlackStats()

    matches = await client.search_mentions(
        settings.slack_user_id, now.date() - timedelta(days=settings.slack_lookback_days)
    )
    keys = dict.fromkeys(thread_key(m) for m in matches)
    async with get_sessionmaker()() as session:
        # 이 스레드로 만든 열린 Task는 내가 답했는지 보러 다시 읽는다.
        rows = await session.scalars(
            select(Task.request_id).where(
                Task.owner_id == owner_id,
                Task.source == SOURCE,
                Task.status.not_in(("done", "cancelled")),
            )
        )
        for request_id in rows:
            _, channel, ts = (request_id or "").split(":", 2)
            keys.setdefault((channel, ts))
        synced_rows = await session.execute(
            select(ActivitySession.external_id, ActivitySession.summary).where(
                ActivitySession.owner_id == owner_id, ActivitySession.source == SOURCE
            )
        )
        synced = {external_id: summary for external_id, summary in synced_rows.all()}

    threads: list[ThreadState] = []
    for channel, ts in keys:
        try:
            messages = await client.replies(channel, ts)
        except RuntimeError as exc:
            log.warning("slack thread %s %s skipped: %s", channel, ts, exc)
            continue
        thread = analyze_thread(channel, ts, messages, settings.slack_user_id)
        if synced.get(f"{channel}:{ts}") != thread.marker:
            threads.append(thread)

    pending = {f"S{i}": t for i, t in enumerate((t for t in threads if t.pending), 1)}
    triage: dict[str, ThreadTriage] = {}
    if pending:
        result = await llm.generate(
            system=SLACK_PROMPT, prompt=_triage_prompt(now.date(), pending), output=SlackTriage
        )
        by_ref = {t.ref: t for t in result.threads}
        triage = {
            pending_ref: by_ref[pending_ref] for pending_ref in pending if pending_ref in by_ref
        }
    ref_of = {id(t): ref for ref, t in pending.items()}

    for thread in threads:
        stats.threads += 1
        async with get_sessionmaker().begin() as session:
            await _apply(
                session,
                owner_id=owner_id,
                thread=thread,
                triage=triage.get(ref_of.get(id(thread), "")),
                stats=stats,
            )
            await session.execute(
                pg_insert(ActivitySession)
                .values(
                    owner_id=owner_id,
                    source=SOURCE,
                    external_id=f"{thread.channel}:{thread.thread_ts}",
                    summary=thread.marker,
                )
                .on_conflict_do_update(
                    index_elements=["owner_id", "source", "external_id"],
                    set_={"summary": thread.marker, "updated_at": now},
                )
            )
    return stats


def _load_token(path: Path, key: str) -> str:
    token = json.loads(path.read_text())[key]
    if not isinstance(token, str) or not token:
        raise KeyError(key)
    return token


async def run_once() -> None:
    settings = get_settings()
    try:
        token = await asyncio.to_thread(
            _load_token, settings.slack_token_file, settings.slack_token_key
        )
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        log.warning("slack tracker skipped: %s", exc)
        return
    client = HttpSlackClient(token)
    try:
        stats = await run(client, get_llm())
    except (httpx.HTTPError, RuntimeError, LLMError) as exc:
        # 읽은 위치를 올리지 않았으니 다음 실행에서 다시 본다.
        log.warning("slack tracker failed: %s", exc)
        return
    finally:
        await client.aclose()
    log.info(
        "slack tracker: threads=%d created=%d started=%d suggested=%d",
        stats.threads,
        stats.created,
        stats.started,
        stats.suggested,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run_once())


if __name__ == "__main__":
    main()
