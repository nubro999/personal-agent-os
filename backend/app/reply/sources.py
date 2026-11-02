"""회신 초안에 넣을 재료를 모은다. 모두 읽기만 한다.

- 건: 2325 wi_ax_item(요청 원문, 브랜드, 접수 시각)
- 에이전트 결과: 2325 rnd_run, rnd_output(진단, 수정안), rnd_review(판정), rnd_promotion(반영)
- 카드 스레드: 회사 도구 `slack.attachments`(같은 번호의 옛 카드를 접수 시각으로 거른다)
- 내 지난 회신: 슬랙 검색(내 계정 토큰). 말투와 멘션 대상을 배운다.

시각: 2325 실행, 판정, 반영 시각은 한국 시각이다. 접수 시각(received_at)만 UTC다.
시험에서는 내가 실제로 회신한 시각(until) 전의 재료만 쓴다.
"""

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pymysql
from dotenv import dotenv_values

from app.config import get_settings
from app.ontology import auto_cs
from app.trackers.maintenance import connect

KST = ZoneInfo("Asia/Seoul")
CHANNEL = "유지보수-고객성공팀"
DIAGNOSIS_LIMIT = 3000
PLAN_LIMIT = 2500
FEEDBACK_LIMIT = 4000
MESSAGE_LIMIT = 1500
EXAMPLE_LIMIT = 15
EXAMPLE_MIN_CHARS = 60
SEARCH_PAGES = 4
MENTION = re.compile(r"<@(U[A-Z0-9]+)>")


@dataclass
class AgentResult:
    run_id: str
    agent: str
    ran_at: str
    diagnosis: str
    plan: str
    reviews: list[str]
    applied: str  # 반영됨, 반영 실패, 되돌림, 반영 전


@dataclass
class Case:
    cs: str
    item_key: str
    brand: str
    company: str
    received_at: str
    feedback: str
    result: AgentResult | None
    other_runs: int


@dataclass
class Message:
    ts: str
    when: str
    user: str
    text: str
    files: list[str] = field(default_factory=list)


@dataclass
class Thread:
    link: str
    messages: list[Message]


@dataclass
class MyReply:
    thread_ts: str
    ts: str
    text: str


def _cursor(conn: Any) -> Any:
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute("SET SESSION TRANSACTION READ ONLY")
    return cur


def section(body: str, start: str, end: str, limit: int) -> str:
    """body에서 start 줄부터 end 줄 전까지. start가 없으면 처음부터."""
    begin = body.find(start) if start else 0
    text = body[max(begin, 0) :]
    stop = text.find(end, len(start)) if end else -1
    if stop > 0:
        text = text[:stop]
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _applied(rows: list[dict[str, Any]], run_id: str) -> str:
    mine = [r for r in rows if r["run_id"] == run_id]
    if any(r["action"] == "rollback" and r["status"] == "done" for r in mine):
        return "되돌림"
    if any(r["action"] == "approve" and r["status"] == "done" for r in mine):
        return "반영됨"
    if any(r["action"] == "approve" and r["status"] == "failed" for r in mine):
        return "반영 실패"
    return "반영 전"


def pick_run(
    runs: list[dict[str, Any]], reviews: list[dict[str, Any]], promotions: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """반영된 실행 > 진단이 O로 판정된 실행 > 가장 늦은 실행. 진단이 있는 실행만 본다."""
    with_diagnosis = [r for r in runs if r.get("has_diagnosis")]
    if not with_diagnosis:
        return None
    applied = {
        p["run_id"] for p in promotions if p["action"] == "approve" and p["status"] == "done"
    }
    good = {v["run_id"] for v in reviews if v["axis"] == "diagnosis" and v["verdict"] == "O"}

    def rank(run: dict[str, Any]) -> tuple[bool, bool, Any]:
        return run["run_id"] in applied, run["run_id"] in good, run["ran_at"]

    return max(with_diagnosis, key=rank)


def load_case(cs: str, until: datetime | None = None) -> Case:
    """until(한국 시각)이 있으면 그 전에 나온 실행, 판정, 반영만 본다."""
    settings = get_settings()
    conn = connect(dotenv_values(settings.maint_env_file), settings.maint_db_port)
    limit = until.replace(tzinfo=None) if until else datetime.max
    try:
        cur = _cursor(conn)
        cur.execute(
            "SELECT item_key, db_name, company, received_at, LEFT(feedback, %s) AS feedback "
            "FROM wi_ax_item WHERE human_id = %s ORDER BY received_at DESC LIMIT 1",
            (FEEDBACK_LIMIT, cs),
        )
        item = cur.fetchone()
        if item is None:
            raise ValueError(f"2325에 없는 건: {cs}")
        key = item["item_key"]
        cur.execute(
            "SELECT run_id, agent, ran_at, EXISTS(SELECT 1 FROM rnd_output o "
            "WHERE o.run_id = r.run_id AND o.part = 'diagnosis') AS has_diagnosis "
            "FROM rnd_run r WHERE item_key = %s",
            (key,),
        )
        runs = [r for r in cur.fetchall() if r["ran_at"] and r["ran_at"] < limit]
        cur.execute(
            "SELECT v.run_id, axis, verdict, comment, reviewed_at FROM rnd_review v "
            "JOIN rnd_run r ON r.run_id = v.run_id WHERE r.item_key = %s",
            (key,),
        )
        reviews = [v for v in cur.fetchall() if v["reviewed_at"] and v["reviewed_at"] < limit]
        cur.execute(
            "SELECT run_id, action, status, requested_at FROM rnd_promotion WHERE item_key = %s",
            (key,),
        )
        promotions = [p for p in cur.fetchall() if p["requested_at"] and p["requested_at"] < limit]
        chosen = pick_run(runs, reviews, promotions)
        result = None
        if chosen:
            cur.execute(
                "SELECT part, body FROM rnd_output WHERE run_id = %s "
                "AND part IN ('diagnosis', 'plan')",
                (chosen["run_id"],),
            )
            parts = {row["part"]: row["body"] or "" for row in cur.fetchall()}
            result = AgentResult(
                run_id=chosen["run_id"],
                agent=chosen["agent"],
                ran_at=str(chosen["ran_at"]),
                diagnosis=section(parts.get("diagnosis", ""), "", "\n요청과 판정", DIAGNOSIS_LIMIT),
                plan=section(parts.get("plan", ""), "수정 방향", "\n고칠 것", PLAN_LIMIT),
                reviews=[
                    f"{v['axis']} {v['verdict']}" + (f": {v['comment']}" if v["comment"] else "")
                    for v in reviews
                    if v["run_id"] == chosen["run_id"]
                ],
                applied=_applied(promotions, chosen["run_id"]),
            )
    finally:
        conn.close()
    received = item["received_at"]
    received_kst = received.replace(tzinfo=ZoneInfo("UTC")).astimezone(KST) if received else None
    return Case(
        cs=cs,
        item_key=key,
        brand=item["db_name"] or "",
        company=item["company"] or "",
        received_at=received_kst.strftime("%Y-%m-%d %H:%M") if received_kst else "",
        feedback=item["feedback"] or "",
        result=result,
        other_runs=max(len([r for r in runs if r.get("has_diagnosis")]) - 1, 0),
    )


def load_thread(cs: str, before: datetime | None = None) -> Thread | None:
    """그 건의 카드 스레드. before가 있으면 그 시각부터 뒤의 글은 자른다."""
    args = [cs]
    if before:
        args += ["--before", str(before.timestamp())]
    result = auto_cs.run(get_settings().auto_cs_dir, "slack.attachments", *args)
    threads = (result.get("data") or {}).get("threads") or []
    if not threads:
        return None
    # 팀이 바뀌면 카드가 다시 선다. 마지막 카드가 지금 대화다.
    last = threads[-1]
    return Thread(
        link=last.get("link") or "",
        messages=[
            Message(
                ts=m.get("ts") or "",
                when=m.get("when") or "",
                user=m.get("user") or "",
                text=(m.get("text") or "")[:MESSAGE_LIMIT],
                files=[f.get("name") or "" for f in m.get("files") or []],
            )
            for m in last.get("messages") or []
        ],
    )


def _slack_token() -> str:
    settings = get_settings()
    token = json.loads(settings.slack_token_file.read_text())[settings.slack_token_key]
    if not isinstance(token, str) or not token:
        raise ValueError(f"{settings.slack_token_key} not found")
    return token


def load_my_replies() -> list[MyReply]:
    """유지보수-고객성공팀에 내가 남긴 회신. 짧은 답("넵 전달해주세요")은 뺀다."""
    settings = get_settings()
    replies: list[MyReply] = []
    with httpx.Client(
        base_url="https://slack.com/api/",
        headers={"Authorization": f"Bearer {_slack_token()}"},
        timeout=20,
    ) as client:
        for page in range(1, SEARCH_PAGES + 1):
            response = client.get(
                "search.messages",
                params={
                    "query": f"from:<@{settings.slack_user_id}> in:#{CHANNEL}",
                    "count": 100,
                    "page": page,
                    "sort": "timestamp",
                },
            )
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(f"slack search failed: {data.get('error')}")
            for match in data["messages"]["matches"]:
                text = match.get("text") or ""
                found = re.search(r"thread_ts=([0-9.]+)", match.get("permalink") or "")
                if len(text) >= EXAMPLE_MIN_CHARS and found:
                    replies.append(MyReply(found.group(1), match["ts"], text))
            if page >= data["messages"]["paging"]["pages"]:
                break
    return replies


def first_reply(thread: Thread, my_name: str) -> Message | None:
    """스레드에서 내가 처음 남긴 회신. 시험에서 이것을 정답으로 쓴다."""
    for message in thread.messages:
        if message.user == my_name and len(message.text) >= EXAMPLE_MIN_CHARS:
            return message
    return None


def examples(replies: list[MyReply], skip_thread: str = "") -> list[str]:
    """말투 예시. 같은 스레드의 글은 뺀다(시험에서 정답이 샌다)."""
    picked = [r.text for r in replies if r.thread_ts != skip_thread]
    return picked[:EXAMPLE_LIMIT]


def usual_mention(replies: list[MyReply]) -> str | None:
    """내 회신 첫머리에 가장 많이 부른 사람. 고객사 소통 담당자다."""
    counts = Counter(found.group(1) for r in replies if (found := MENTION.match(r.text.strip())))
    return counts.most_common(1)[0][0] if counts else None
