"""Slack에서 나를 멘션했거나 DM을 보냈는데 아직 답하지 않은 대화를 Task로 맞춘다.

스레드 단위로 본다. 루트에 CS 번호가 있으면 2325 Task에 잇는다. 내 2325 Task가 없는 CS 스레드는
남의 담당이거나 이미 끝난 건이라 건너뛴다(여러 명을 같이 부르는 리마인드가 대부분이다).
- 요청인데 2325 Task가 막힘(고객사 소통중)이면 진행 중으로 바꾼다. 내 차례가 왔다는 뜻이다.
- 해결 보고면 완료 제안만 남긴다.
CS 번호가 없는 요청은 새 Task로 만들고, 나중에 내가 그 스레드에 답하면 완료 제안을 남긴다.

DM과 그룹 DM은 멘션이 없으니 내 마지막 메시지 뒤에 상대 메시지가 있으면 내 차례로 본다.
답하지 않은 메시지 묶음의 첫 메시지를 기준으로 Task 하나를 만든다. 봇과 Slackbot은 건너뛴다.
Slack에는 읽기만 하고 메시지를 보내지 않는다. 내 차례인 요청에는 답변 초안을 만들어 두고,
내가 답하면 초안을 치운다.

실행: uv run python -m app.trackers.slack
"""

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select, update
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
from app.persistence.models import ActivitySession, ReplyDraft, Task
from app.trackers.prompts import DRAFT_PROMPT, SLACK_PROMPT

log = logging.getLogger(__name__)

SOURCE = "slack"
CS_PATTERN = re.compile(r"CS-\d{7}")
CONTEXT_MESSAGES = 6
MESSAGE_TEXT_LIMIT = 300
DRAFT_CONTEXT_MESSAGES = 12
DRAFT_TEXT_LIMIT = 800
STYLE_SAMPLE_COUNT = 30
LEADING_MENTION = re.compile(r"^\s*<@[A-Z0-9]+>\s*")
SEARCH_PAGES = 3


class SlackClient(Protocol):
    async def search_mentions(self, user_id: str, after: date) -> list[dict[str, Any]]: ...

    async def replies(self, channel: str, ts: str) -> list[dict[str, Any]]: ...

    async def dm_channels(self) -> list[dict[str, Any]]: ...

    async def history(self, channel: str, oldest: float) -> list[dict[str, Any]]: ...

    async def user_name(self, user_id: str) -> str: ...

    async def my_messages(self, user_id: str) -> list[str]: ...

    async def post(self, channel: str, text: str, thread_ts: str | None) -> str: ...


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

    async def dm_channels(self) -> list[dict[str, Any]]:
        channels: list[dict[str, Any]] = []
        cursor = ""
        while True:
            data = await self._get(
                "conversations.list",
                types="im,mpim",
                exclude_archived="true",
                limit=200,
                cursor=cursor,
            )
            channels += data["channels"]
            cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                return channels

    async def history(self, channel: str, oldest: float) -> list[dict[str, Any]]:
        data = await self._get(
            "conversations.history", channel=channel, oldest=f"{oldest:.6f}", limit=200
        )
        # API는 최신순으로 준다.
        messages: list[dict[str, Any]] = data["messages"]
        return list(reversed(messages))

    async def user_name(self, user_id: str) -> str:
        data = await self._get("users.info", user=user_id)
        profile = data["user"].get("profile") or {}
        name: str = profile.get("real_name") or data["user"].get("name") or user_id
        return name

    async def my_messages(self, user_id: str) -> list[str]:
        data = await self._get(
            "search.messages", query=f"from:<@{user_id}>", count=60, sort="timestamp"
        )
        return [m.get("text") or "" for m in data["messages"]["matches"]]

    async def post(self, channel: str, text: str, thread_ts: str | None) -> str:
        """내 이름으로 보낸다. 보드의 보내기 버튼만 부른다."""
        body: dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts:
            body["thread_ts"] = thread_ts
        response = await self._http.post("chat.postMessage", json=body)
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"slack chat.postMessage failed: {data.get('error')}")
        return str(data["ts"])

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
    draft_context: list[str] = field(default_factory=list)
    is_dm: bool = False
    # 내 마지막 답 뒤에 처음 나를 부른 글의 ts와, 그 뒤로 더 부른 횟수
    waiting_since: float | None = None
    reminders: int = 0
    my_last: float = 0.0

    @property
    def waiting(self) -> dict[str, Any]:
        since = datetime.fromtimestamp(self.waiting_since, UTC) if self.waiting_since else None
        return {"waiting_since": since, "reminders": self.reminders}

    @property
    def reference(self) -> str:
        return f"{SOURCE}:{self.channel}:{self.thread_ts}"

    @property
    def sync_id(self) -> str:
        # DM은 대화방 하나에 상태 하나다. 답하지 않은 묶음은 시간이 지나며 바뀐다.
        return f"{self.channel}:dm" if self.is_dm else f"{self.channel}:{self.thread_ts}"


def _format_messages(messages: list[dict[str, Any]], me: str, count: int, limit: int) -> list[str]:
    mention = f"<@{me}>"
    return [
        f"{'나' if m.get('user') == me else '상대'}: "
        + re.sub(r"\s+", " ", (m.get("text") or "").replace(mention, "@나"))[:limit]
        for m in messages[-count:]
        if (m.get("text") or "").strip()
    ]


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
    asks = [
        float(m["ts"])
        for m in messages
        if m.get("user") != me and mention in (m.get("text") or "") and float(m["ts"]) > my_last
    ]
    return ThreadState(
        channel=channel,
        thread_ts=thread_ts,
        root_text=root_text[:MESSAGE_TEXT_LIMIT],
        cs_id=found.group(0) if found else None,
        pending=mention_last > my_last,
        marker=f"{max(my_last, mention_last):.6f}",
        context=_format_messages(messages[1:], me, CONTEXT_MESSAGES, MESSAGE_TEXT_LIMIT),
        draft_context=_format_messages(messages[1:], me, DRAFT_CONTEXT_MESSAGES, DRAFT_TEXT_LIMIT),
        waiting_since=min(asks) if asks else None,
        reminders=max(0, len(asks) - 1),
        my_last=my_last,
    )


def _is_human(message: dict[str, Any]) -> bool:
    return (
        not message.get("bot_id")
        and not message.get("subtype")
        and bool(message.get("user"))
        and message.get("user") != "USLACKBOT"
    )


def analyze_dm(channel: str, messages: list[dict[str, Any]], me: str, partner: str) -> ThreadState:
    """시간순 메시지를 받는다. 내 마지막 메시지 뒤에 상대 메시지가 있으면 내 차례(pending)다."""
    human = [m for m in messages if _is_human(m)]
    my_last = max((float(m["ts"]) for m in human if m["user"] == me), default=0.0)
    unanswered = [m for m in human if m["user"] != me and float(m["ts"]) > my_last]
    latest = max((float(m["ts"]) for m in human), default=0.0)
    texts = " ".join(m.get("text") or "" for m in human)
    found = CS_PATTERN.search(texts)
    return ThreadState(
        channel=channel,
        thread_ts=unanswered[0]["ts"] if unanswered else f"{latest:.6f}",
        root_text=f"DM ({partner})",
        cs_id=found.group(0) if found else None,
        pending=bool(unanswered),
        marker=f"{latest:.6f}",
        context=_format_messages(human, me, CONTEXT_MESSAGES, MESSAGE_TEXT_LIMIT),
        draft_context=_format_messages(human, me, DRAFT_CONTEXT_MESSAGES, DRAFT_TEXT_LIMIT),
        is_dm=True,
        waiting_since=float(unanswered[0]["ts"]) if unanswered else None,
        reminders=max(0, len(unanswered) - 1),
        my_last=my_last,
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


class DraftOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft: str = Field(description="Slack에 보낼 답변 초안")


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
    drafted: int = 0


def style_samples(messages: list[str]) -> list[str]:
    """내가 쓴 짧은 답변만 말투 샘플로 쓴다. 긴 공지나 붙여넣기는 말투가 아니다."""
    samples: list[str] = []
    for text in messages:
        cleaned = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
        if 4 <= len(cleaned) <= 200 and "```" not in cleaned:
            samples.append(re.sub(r"\s+", " ", cleaned))
    return samples[:STYLE_SAMPLE_COUNT]


def _draft_prompt(
    thread: ThreadState,
    task: Task | None,
    samples: list[str],
    advice: list[str] | None = None,
    previous: str = "",
) -> str:
    lines = ["내 말투 샘플:", *(f"- {s}" for s in samples), ""]
    if task:
        lines += [f"연결된 할 일: {task.title}", f"할 일 상태: {task.status}"]
    lines += ["", f"대화 ({thread.root_text}):", *thread.draft_context]
    if advice:
        lines += ["", "이전 초안:", previous, "", "내 조언:", *(f"- {a}" for a in advice)]
    return "\n".join(lines)


async def compose_draft(
    llm: StructuredLLM,
    thread: ThreadState,
    task: Task | None,
    samples: list[str],
    advice: list[str],
    previous: str,
) -> tuple[str, list[str]]:
    """(초안, 보내기 전에 확인할 것).

    유지보수-고객성공팀의 건 스레드는 건 회신 에이전트가 쓴다. 에이전트 결과와 그 건의 대화를
    읽어 사유를 적는다. 건 재료를 못 읽으면 일반 초안으로 쓴다.
    """
    if thread.cs_id and thread.channel == get_settings().slack_cs_channel:
        # 건 회신 에이전트는 온톨로지와 Agent SDK를 불러 무겁다. 쓸 때만 읽는다.
        from app.reply.draft import draft_for_board

        try:
            result = await draft_for_board(thread.cs_id, advice, previous)
        except Exception as exc:  # 2325, 채팅 DB, Agent SDK 어디서든 실패할 수 있다
            log.warning("case reply %s failed, using plain draft: %s", thread.cs_id, exc)
            result = None
        if result:
            # 멘션은 내가 보낼 때 붙인다. 복사한 <@U...> 글자는 Slack에서 멘션이 되지 않는다.
            return LEADING_MENTION.sub("", result.draft).strip(), result.check
    out = await llm.generate(
        system=DRAFT_PROMPT,
        prompt=_draft_prompt(thread, task, samples, advice, previous),
        output=DraftOut,
    )
    return out.draft.strip(), []


def answered(draft: ReplyDraft, thread: ThreadState) -> bool:
    """이 초안이 필요 없어졌나.

    부른 초안은 내 차례가 끝났으면, 미리 쓴 초안은 쓴 뒤에 내가 답했으면 필요 없다.
    """
    if draft.origin == "case":
        return thread.my_last > draft.created_at.timestamp()
    return not thread.pending


async def save_draft(
    session: AsyncSession,
    *,
    owner_id: str,
    thread: ThreadState,
    task: Task,
    draft: str,
    notes: list[str],
    origin: str = "mention",
) -> None:
    fields = {
        "origin": origin,
        "marker": thread.marker,
        "draft": draft,
        "notes": notes,
        "status": "pending",
        "task_id": task.id,
        "rewriting": False,
        **thread.waiting,
    }
    await session.execute(
        pg_insert(ReplyDraft)
        .values(
            owner_id=owner_id,
            reference=thread.reference,
            channel=thread.channel,
            thread_ts=None if thread.is_dm else thread.thread_ts,
            **fields,
        )
        .on_conflict_do_update(
            index_elements=["owner_id", "reference"], set_=fields | {"updated_at": func.now()}
        )
    )


async def _clear_drafts(session: AsyncSession, *, owner_id: str, thread: ThreadState) -> None:
    """내가 답했으니 그 대화의 초안은 치운다. 미리 쓴 초안은 쓴 뒤에 내가 답했을 때만 치운다."""
    replied = datetime.fromtimestamp(thread.my_last, UTC)
    stmt = update(ReplyDraft).where(
        ReplyDraft.owner_id == owner_id,
        ReplyDraft.channel == thread.channel,
        ReplyDraft.status == "pending",
        or_(ReplyDraft.origin == "mention", ReplyDraft.created_at < replied),
    )
    if not thread.is_dm:
        stmt = stmt.where(ReplyDraft.thread_ts == thread.thread_ts)
    # 조언은 그 답을 위한 것이었다. 다음 요청에는 새로 남긴다.
    await session.execute(stmt.values(status="used", advice=[], updated_at=func.now()))


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
) -> Task | None:
    """초안을 만들 Task를 돌려준다. 내 일이 아니거나 내 차례가 아니면 None."""
    reference = thread.reference
    if not thread.pending:
        await _clear_drafts(session, owner_id=owner_id, thread=thread)
    if thread.is_dm and not thread.pending:
        # 내가 답했다. 이 대화방에서 만든 열린 Task마다 끝났는지 묻는다.
        tasks = await session.scalars(
            select(Task).where(
                Task.owner_id == owner_id,
                Task.source == SOURCE,
                Task.request_id.startswith(f"{SOURCE}:{thread.channel}:"),
                Task.status.not_in(("done", "cancelled")),
            )
        )
        for task in tasks.all():
            stats.suggested += await suggest_completion(
                session,
                owner_id=owner_id,
                task_id=task.id,
                source=SOURCE,
                source_reference=f"{reference}:{thread.marker}",
                explanation="DM에 답했어요",
            )
        return None
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
        return None
    if triage is None or triage.kind == "fyi":
        return None

    linked = own or (await _cs_task(session, owner_id, thread.cs_id) if thread.cs_id else None)
    if linked is None and thread.cs_id:
        return None
    if linked is not None:
        if linked.status in ("done", "cancelled"):
            return None
        if triage.kind == "done_report":
            stats.suggested += await suggest_completion(
                session,
                owner_id=owner_id,
                task_id=linked.id,
                source=SOURCE,
                source_reference=f"{reference}:{thread.marker}",
                explanation=triage.evidence,
            )
        elif own is None and linked.status in ("blocked", "todo"):
            # 2325 Task에 내 차례가 왔다. 이 스레드로 만든 Task는 다시 불려도 그대로 둔다.
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
        return linked if triage.kind == "request" else None

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
        return result.task
    return None


async def _write_drafts(
    client: SlackClient,
    llm: StructuredLLM,
    *,
    owner_id: str,
    wanted: list[tuple[ThreadState, Task]],
    stats: SlackStats,
) -> None:
    settings = get_settings()
    samples = style_samples(await client.my_messages(settings.slack_user_id))
    for thread, task in wanted[: settings.slack_draft_limit]:
        async with get_sessionmaker()() as session:
            existing = await session.scalar(
                select(ReplyDraft).where(
                    ReplyDraft.owner_id == owner_id,
                    ReplyDraft.reference == thread.reference,
                    ReplyDraft.status == "pending",
                )
            )
        advice = list(existing.advice) if existing else []
        try:
            draft, notes = await compose_draft(
                llm, thread, task, samples, advice, existing.draft if existing else ""
            )
        except LLMError as exc:
            # 초안은 없어도 Task는 남는다. 다음 메시지가 오면 다시 만든다.
            log.warning("slack draft failed: %s", exc)
            continue
        async with get_sessionmaker().begin() as session:
            await save_draft(
                session, owner_id=owner_id, thread=thread, task=task, draft=draft, notes=notes
            )
        stats.drafted += 1


async def _current_thread(client: SlackClient, draft: ReplyDraft) -> ThreadState:
    me = get_settings().slack_user_id
    if draft.thread_ts:
        return analyze_thread(
            draft.channel, draft.thread_ts, await client.replies(draft.channel, draft.thread_ts), me
        )
    oldest = (datetime.now() - timedelta(days=get_settings().slack_lookback_days)).timestamp()
    return analyze_dm(draft.channel, await client.history(draft.channel, oldest), me, "상대")


async def sweep_sent_drafts(client: SlackClient | None = None) -> int:
    """내가 이미 답한 스레드의 초안을 치운다. 수집기(30분)를 기다리지 않게 보드가 부른다."""
    settings = get_settings()
    async with get_sessionmaker()() as session:
        drafts = list(
            await session.scalars(
                select(ReplyDraft).where(
                    ReplyDraft.owner_id == settings.owner_id, ReplyDraft.status == "pending"
                )
            )
        )
    if not drafts:
        return 0
    own = client is None
    if client is None:
        token = await asyncio.to_thread(
            _load_token, settings.slack_token_file, settings.slack_token_key
        )
        client = HttpSlackClient(token)
    cleared = 0
    try:
        for draft in drafts:
            try:
                thread = await _current_thread(client, draft)
            except (httpx.HTTPError, RuntimeError) as exc:
                log.warning("draft sweep %s skipped: %s", draft.id, exc)
                continue
            async with get_sessionmaker().begin() as session:
                if not answered(draft, thread):
                    # 리마인드가 더 왔는지 수집기(30분)보다 먼저 맞춘다.
                    await session.execute(
                        update(ReplyDraft).where(ReplyDraft.id == draft.id).values(**thread.waiting)
                    )
                    continue
                await _clear_drafts(session, owner_id=settings.owner_id, thread=thread)
            cleared += 1
    finally:
        if own and isinstance(client, HttpSlackClient):
            await client.aclose()
    return cleared


THREAD_MESSAGES = 10
THREAD_TEXT_LIMIT = 1500
_names: dict[str, str] = {}


async def thread_messages(
    draft_id: uuid.UUID, client: SlackClient | None = None
) -> list[dict[str, Any]]:
    """초안이 답하는 스레드의 마지막 글들. 보드 패널이 Slack을 열지 않고 맥락을 보게 한다."""
    settings = get_settings()
    async with get_sessionmaker()() as session:
        draft = await session.get(ReplyDraft, draft_id)
    if draft is None or draft.owner_id != settings.owner_id:
        raise SendError("초안이 없다")
    own = client is None
    if client is None:
        token = await asyncio.to_thread(
            _load_token, settings.slack_token_file, settings.slack_token_key
        )
        client = HttpSlackClient(token)
    try:
        if draft.thread_ts:
            messages = await client.replies(draft.channel, draft.thread_ts)
        else:
            oldest = (datetime.now() - timedelta(days=settings.slack_lookback_days)).timestamp()
            messages = await client.history(draft.channel, oldest)
        shown = [m for m in messages if (m.get("text") or "").strip()]
        # 스레드 루트(건 카드)는 맨 앞에 남기고 나머지는 최근 것만 보인다.
        if draft.thread_ts and len(shown) > THREAD_MESSAGES:
            shown = [shown[0], *shown[-(THREAD_MESSAGES - 1) :]]
        else:
            shown = shown[-THREAD_MESSAGES:]
        people: set[str] = {str(m["user"]) for m in shown if m.get("user")} | {
            found for m in shown for found in re.findall(r"<@([A-Z0-9]+)>", m.get("text") or "")
        }
        for user in people - _names.keys():
            try:
                _names[user] = await client.user_name(user)
            except RuntimeError:
                _names[user] = user
        out = []
        for m in shown:
            author = m.get("user")
            text = re.sub(
                r"<@([A-Z0-9]+)>", lambda found: f"@{_names.get(found[1], '')}", m.get("text") or ""
            )
            out.append(
                {
                    "ts": m["ts"],
                    "at": datetime.fromtimestamp(float(m["ts"]), UTC),
                    "who": _names.get(author or "", "") or m.get("username") or "봇",
                    "me": author == settings.slack_user_id,
                    "text": text[:THREAD_TEXT_LIMIT],
                }
            )
        return out
    except (httpx.HTTPError, RuntimeError) as exc:
        raise SendError(str(exc)) from exc
    finally:
        if own and isinstance(client, HttpSlackClient):
            await client.aclose()


class SendError(Exception):
    """보내지 않았다. 초안이 없거나, 이미 답했거나, Slack이 거절했다."""


def asker(messages: list[dict[str, Any]], me: str) -> str | None:
    """나를 마지막으로 부른 사람. 스레드 답은 그 사람을 멘션해서 보낸다."""
    mention = f"<@{me}>"
    for message in reversed(messages):
        if message.get("user") not in (None, me) and mention in (message.get("text") or ""):
            return str(message["user"])
    return None


async def send_draft(draft_id: uuid.UUID, text: str, client: SlackClient | None = None) -> str:
    """보드에서 고친 초안을 그 스레드(DM이면 그 대화)에 보내고 초안을 치운다. 보낸 글을 돌려준다."""
    settings = get_settings()
    text = text.strip()
    if not text:
        raise SendError("보낼 글이 비어 있다")
    async with get_sessionmaker()() as session:
        draft = await session.get(ReplyDraft, draft_id)
    if draft is None or draft.owner_id != settings.owner_id or draft.status != "pending":
        raise SendError("보낼 초안이 없다")
    own = client is None
    if client is None:
        token = await asyncio.to_thread(
            _load_token, settings.slack_token_file, settings.slack_token_key
        )
        client = HttpSlackClient(token)
    try:
        if draft.thread_ts:
            messages = await client.replies(draft.channel, draft.thread_ts)
            me = settings.slack_user_id
            thread = analyze_thread(draft.channel, draft.thread_ts, messages, me)
            target = asker(messages, me)
            if target is None and draft.channel == settings.slack_cs_channel:
                # 미리 쓴 회신은 아무도 부르지 않은 카드에 남긴다. 늘 부르던 소통 담당자를 부른다.
                target = settings.slack_cs_contact
            if target and not text.startswith("<@"):
                text = f"<@{target}> {text}"
        else:
            thread = await _current_thread(client, draft)
        if answered(draft, thread):
            # 다른 데서 이미 답했다. 두 번 보내지 않는다.
            async with get_sessionmaker().begin() as session:
                await _clear_drafts(session, owner_id=settings.owner_id, thread=thread)
            raise SendError("이미 답한 스레드라 보내지 않았다")
        await client.post(draft.channel, text, draft.thread_ts)
    except (httpx.HTTPError, RuntimeError) as exc:
        raise SendError(str(exc)) from exc
    finally:
        if own and isinstance(client, HttpSlackClient):
            await client.aclose()
    async with get_sessionmaker().begin() as session:
        # 방금 보낸 답은 thread에 아직 없다. 보낸 초안은 id로 치운다.
        await session.execute(
            update(ReplyDraft)
            .where(ReplyDraft.id == draft_id)
            .values(status="used", advice=[], updated_at=func.now())
        )
        await _clear_drafts(session, owner_id=settings.owner_id, thread=thread)
    return text


async def rewrite_draft(draft_id: uuid.UUID) -> None:
    """보드에서 조언을 남긴 초안을 다시 쓴다. 지금 스레드를 다시 읽는다."""
    settings = get_settings()
    async with get_sessionmaker()() as session:
        draft = await session.get(ReplyDraft, draft_id)
        task = await session.get(Task, draft.task_id) if draft and draft.task_id else None
    if draft is None:
        return
    values: dict[str, Any] = {"rewriting": False}
    try:
        token = await asyncio.to_thread(
            _load_token, settings.slack_token_file, settings.slack_token_key
        )
        client = HttpSlackClient(token)
        try:
            thread = await _current_thread(client, draft)
            if answered(draft, thread):
                # 그사이 내가 답했다. 다시 쓰지 않고 치운다.
                async with get_sessionmaker().begin() as session:
                    await _clear_drafts(session, owner_id=settings.owner_id, thread=thread)
                return
            samples = style_samples(await client.my_messages(settings.slack_user_id))
            text, notes = await compose_draft(
                get_llm(), thread, task, samples, list(draft.advice), draft.draft
            )
        finally:
            await client.aclose()
        values |= {"draft": text, "notes": notes, **thread.waiting}
    except (OSError, KeyError, ValueError, httpx.HTTPError, RuntimeError, LLMError) as exc:
        log.warning("draft rewrite %s failed: %s", draft_id, exc)
    finally:
        async with get_sessionmaker().begin() as session:
            await session.execute(
                update(ReplyDraft)
                .where(ReplyDraft.id == draft_id)
                .values(**values, updated_at=func.now())
            )


async def run(client: SlackClient, llm: StructuredLLM, now: datetime | None = None) -> SlackStats:
    settings = get_settings()
    now = now or datetime.now(ZoneInfo(settings.timezone))
    owner_id = settings.owner_id
    stats = SlackStats()

    since = now.date() - timedelta(days=settings.slack_lookback_days)
    matches = await client.search_mentions(settings.slack_user_id, since)
    # DM은 멘션이 아니라 아래 DM 경로로 본다.
    keys = dict.fromkeys(
        thread_key(m)
        for m in matches
        if not (m["channel"].get("is_im") or m["channel"].get("is_mpim"))
    )
    dm_open: set[str] = set()
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
            if channel.startswith("D") or channel.startswith("G"):
                dm_open.add(channel)
            else:
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
        if synced.get(thread.sync_id) != thread.marker:
            threads.append(thread)

    oldest = (now - timedelta(days=settings.slack_lookback_days)).timestamp()
    for dm in await client.dm_channels():
        channel = dm["id"]
        partner_id = dm.get("user")
        if partner_id in ("USLACKBOT", settings.slack_user_id) or dm.get("is_user_deleted"):
            continue
        # updated는 밀리초 단위다. 조용한 대화방은 history를 부르지 않는다.
        if channel not in dm_open and float(dm.get("updated") or 0) / 1000 < oldest:
            continue
        try:
            messages = await client.history(channel, oldest)
            partner = await client.user_name(partner_id) if partner_id else "그룹 DM"
        except RuntimeError as exc:
            log.warning("slack dm %s skipped: %s", channel, exc)
            continue
        thread = analyze_dm(channel, messages, settings.slack_user_id, partner)
        if float(thread.marker) and synced.get(thread.sync_id) != thread.marker:
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

    wanted: list[tuple[ThreadState, Task]] = []
    for thread in threads:
        stats.threads += 1
        async with get_sessionmaker().begin() as session:
            task = await _apply(
                session,
                owner_id=owner_id,
                thread=thread,
                triage=triage.get(ref_of.get(id(thread), "")),
                stats=stats,
            )
            if task is not None:
                wanted.append((thread, task))
            await session.execute(
                pg_insert(ActivitySession)
                .values(
                    owner_id=owner_id,
                    source=SOURCE,
                    external_id=thread.sync_id,
                    summary=thread.marker,
                )
                .on_conflict_do_update(
                    index_elements=["owner_id", "source", "external_id"],
                    set_={"summary": thread.marker, "updated_at": now},
                )
            )
    if wanted:
        await _write_drafts(client, llm, owner_id=owner_id, wanted=wanted, stats=stats)
    return stats


def _load_token(path: Path, key: str) -> str:
    token = json.loads(path.read_text())[key]
    if not isinstance(token, str) or not token:
        raise KeyError(key)
    return token


async def run_once() -> str | None:
    settings = get_settings()
    try:
        token = await asyncio.to_thread(
            _load_token, settings.slack_token_file, settings.slack_token_key
        )
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        log.warning("slack tracker skipped: %s", exc)
        return f"Slack 토큰을 읽지 못함: {type(exc).__name__}"
    client = HttpSlackClient(token)
    try:
        stats = await run(client, get_llm())
    except (httpx.HTTPError, RuntimeError, LLMError) as exc:
        # 읽은 위치를 올리지 않았으니 다음 실행에서 다시 본다.
        log.warning("slack tracker failed: %s", exc)
        return f"Slack 수집 실패: {exc}"
    finally:
        await client.aclose()
    log.info(
        "slack tracker: threads=%d created=%d started=%d suggested=%d drafted=%d",
        stats.threads,
        stats.created,
        stats.started,
        stats.suggested,
        stats.drafted,
    )
    return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run_once())


if __name__ == "__main__":
    main()
