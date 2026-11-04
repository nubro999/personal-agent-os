"""에이전트 결과가 나온 내 2325 건은 아무도 부르지 않아도 회신 초안을 미리 쓴다.

대상은 둘을 다 만족하는 건이다.
- 보드의 2325 일이 진행 중이다. 나에게 할당됐고 2325에서 수정 중이다(끝남, 고객 답 대기가 아님).
- 진단이 있는 에이전트 실행이 있다.

브랜드(2325 db_name)가 case_reply_brands(hdex, mommycare)면 회신 에이전트가 쓴다. 다른 브랜드는
에이전트를 돌리지 않고 case_reply_default("수정완료했습니다")를 띄운다.

실행 하나에 초안 하나다. 그 실행 뒤에 내가 카드 스레드에 이미 답했으면 쓰지 않는다. 본 실행은
activity_sessions(source=case_reply)에 남겨 다시 보지 않는다. 같은 스레드에 누가 나를 불러 만든
초안이 이미 있으면 그것을 둔다.

실행: uv run python -m app.trackers.case_replies
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pymysql
from dotenv import dotenv_values
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import get_settings
from app.persistence.db import get_sessionmaker
from app.persistence.models import ActivitySession, ReplyDraft, Task
from app.trackers import slack
from app.trackers.maintenance import connect

log = logging.getLogger(__name__)

SOURCE = "case_reply"
KST = ZoneInfo("Asia/Seoul")
CARD_LINK = re.compile(r"archives/(C[A-Z0-9]+)/p(\d{10})(\d{6})")


@dataclass(frozen=True)
class AgentRun:
    item_key: str
    cs: str
    brand: str
    run_id: str
    ran_at: datetime  # 2325 rnd_run.ran_at은 KST naive 값이다

    @property
    def ran_ts(self) -> float:
        return self.ran_at.replace(tzinfo=KST).timestamp()


def latest_runs(item_keys: list[str]) -> dict[str, AgentRun]:
    """건마다 진단이 있는 가장 늦은 실행."""
    if not item_keys:
        return {}
    settings = get_settings()
    conn = connect(dotenv_values(settings.maint_env_file), settings.maint_db_port)
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            keys = ",".join(["%s"] * len(item_keys))
            cur.execute(
                "SELECT x.item_key, x.human_id, x.db_name, r.run_id, r.ran_at FROM wi_ax_item x "
                "JOIN rnd_run r ON r.item_key = x.item_key "
                f"WHERE x.item_key IN ({keys}) AND r.ran_at IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM rnd_output o "
                "WHERE o.run_id = r.run_id AND o.part = 'diagnosis') "
                "ORDER BY r.ran_at",
                item_keys,
            )
            rows: list[dict[str, Any]] = list(cur.fetchall())
    finally:
        conn.close()
    return {
        r["item_key"]: AgentRun(
            r["item_key"], r["human_id"] or "", r["db_name"] or "", r["run_id"], r["ran_at"]
        )
        for r in rows
    }


def card_thread(cs: str) -> tuple[str, str] | None:
    """(채널, 스레드 ts). 팀이 바뀌면 카드가 다시 서므로 마지막 카드다."""
    from app.reply import sources

    thread = sources.load_thread(cs)
    found = CARD_LINK.search(thread.link) if thread else None
    return (found[1], f"{found[2]}.{found[3]}") if found else None


@dataclass
class CaseReplyStats:
    candidates: int = 0
    drafted: int = 0
    answered: int = 0


Writer = Callable[[str], Awaitable[Any]]
Runs = Callable[[list[str]], dict[str, AgentRun]]
Card = Callable[[str], tuple[str, str] | None]


async def _default_writer(cs: str) -> Any:
    from app.reply.draft import draft_for_board

    return await draft_for_board(cs, [], "")


async def _mark(owner_id: str, run_id: str, what: str) -> None:
    async with get_sessionmaker().begin() as session:
        await session.execute(
            pg_insert(ActivitySession)
            .values(owner_id=owner_id, source=SOURCE, external_id=run_id, summary=what)
            .on_conflict_do_update(
                index_elements=["owner_id", "source", "external_id"],
                set_={"summary": what, "updated_at": datetime.now(UTC)},
            )
        )


async def run(
    client: slack.SlackClient,
    *,
    runs: Runs = latest_runs,
    card: Card = card_thread,
    writer: Writer = _default_writer,
) -> CaseReplyStats:
    settings = get_settings()
    owner_id = settings.owner_id
    stats = CaseReplyStats()
    async with get_sessionmaker()() as session:
        tasks = list(
            await session.scalars(
                select(Task).where(
                    Task.owner_id == owner_id,
                    Task.source == "2325",
                    Task.status == "in_progress",
                )
            )
        )
        seen = set(
            await session.scalars(
                select(ActivitySession.external_id).where(
                    ActivitySession.owner_id == owner_id, ActivitySession.source == SOURCE
                )
            )
        )
    by_key = {(t.request_id or "").removeprefix("2325:"): t for t in tasks}
    found = await asyncio.to_thread(runs, list(by_key))
    todo = sorted(
        (r for r in found.values() if r.run_id not in seen), key=lambda r: r.ran_at, reverse=True
    )
    stats.candidates = len(todo)
    written = 0
    for agent_run in todo:
        by_agent = agent_run.brand in settings.case_reply_brands
        # 에이전트로 쓰는 건만 한 번에 몇 개로 묶는다. 기본 문구는 바로 띄운다.
        if by_agent and written >= settings.case_reply_limit:
            continue
        place = await asyncio.to_thread(card, agent_run.cs)
        if place is None:
            await _mark(owner_id, agent_run.run_id, "카드 없음")
            continue
        channel, thread_ts = place
        thread = slack.analyze_thread(
            channel, thread_ts, await client.replies(channel, thread_ts), settings.slack_user_id
        )
        if thread.my_last > agent_run.ran_ts:
            stats.answered += 1
            await _mark(owner_id, agent_run.run_id, "이미 답함")
            continue
        async with get_sessionmaker()() as session:
            existing = await session.scalar(
                select(ReplyDraft.id).where(
                    ReplyDraft.owner_id == owner_id,
                    ReplyDraft.reference == thread.reference,
                    ReplyDraft.status == "pending",
                )
            )
        if existing is not None:
            await _mark(owner_id, agent_run.run_id, "초안 있음")
            continue
        if by_agent:
            result = await writer(agent_run.cs)
            written += 1
            if result is None:
                # 다음 실행에서 다시 쓴다.
                log.warning("case reply %s: no draft", agent_run.cs)
                continue
            text = slack.LEADING_MENTION.sub("", result.draft).strip()
            notes = list(result.check)
        else:
            text = settings.case_reply_default
            notes = ["기본 문구예요. 실제로 반영했는지 확인하고 보내세요."]
        async with get_sessionmaker().begin() as session:
            await slack.save_draft(
                session,
                owner_id=owner_id,
                thread=thread,
                task=by_key[agent_run.item_key],
                draft=text,
                notes=notes,
                origin="case",
            )
        await _mark(owner_id, agent_run.run_id, "초안 씀")
        stats.drafted += 1
    return stats


async def run_once() -> str | None:
    settings = get_settings()
    try:
        token = await asyncio.to_thread(
            slack._load_token, settings.slack_token_file, settings.slack_token_key
        )
    except (OSError, KeyError, ValueError) as exc:
        # 토큰이 없으면 아무것도 쓰지 못한다. 로그에도 남겨야 조용히 멈춘 줄 안다.
        log.warning("case reply tracker skipped: %s %r", settings.slack_token_key, exc)
        return f"Slack 토큰을 읽지 못함: {settings.slack_token_key} {type(exc).__name__}"
    client = slack.HttpSlackClient(token)
    try:
        stats = await run(client)
    except (pymysql.err.OperationalError, RuntimeError) as exc:
        log.warning("case reply tracker failed: %s", exc)
        return f"미리 쓰는 회신 실패: {exc}"[:200]
    finally:
        await client.aclose()
    log.info(
        "case reply tracker: candidates=%d drafted=%d answered=%d",
        stats.candidates,
        stats.drafted,
        stats.answered,
    )
    return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run_once())


if __name__ == "__main__":
    main()
