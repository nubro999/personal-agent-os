"""채팅 DB에서 건과 연결된 대화의 구조만 읽는다. 읽기 전용 세션이다.

오아는 턴이 100만 행이 넘어서 건의 chat_id로 찾은 방만 읽는다.
고객 질문, 답변, 함수 인자에는 이름과 전화번호가 들어 있어 저장하지 않는다. 글은 문제 턴을
찾는 동안 메모리에서만 쓴다. 남기는 것은 id, 시각, 상태, 카테고리, 정보 태그, 함수 이름이다.
"""

import json
import logging
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pymysql
from dotenv import dotenv_values

from app.trackers.maintenance import connect

log = logging.getLogger(__name__)

BATCH = 200
STEP_RE = re.compile(r"step\s?(\d+)", re.I)
Finder = Callable[..., dict[str, Any]]

TURNS_SQL = """
SELECT id, chat_room_id, status, agent_name, `time`, question, ai_answer, manual_usage, ai_cots,
       JSON_EXTRACT(qna_dtos, '$[*].tag') AS tags,
       JSON_EXTRACT(tool_call_requests, '$[*].tool_call_requests[*].function.name') AS functions
FROM conversation WHERE role = 'user' AND chat_room_id IN ({marks}) ORDER BY id
"""
ROOMS_SQL = """
SELECT id, user_chat_id, platform, handoff_type, handoff_reason
FROM chat_room WHERE user_chat_id IN ({marks})
"""


def load_finder(auto_cs_dir: Path) -> Finder | None:
    """회사 코드의 문제 턴 찾기를 가져온다. 없으면 문제 턴 없이 진행한다."""
    if str(auto_cs_dir) not in sys.path:
        sys.path.insert(0, str(auto_cs_dir))
    try:
        from tools.chat.target import find  # type: ignore[import-not-found]
    except ImportError as exc:
        log.warning("problem turn finder unavailable: %s", exc)
        return None
    finder: Finder = find
    return finder


def _json(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _flat(value: Any) -> list[str]:
    """JSON_EXTRACT 결과에서 빈 값을 빼고 글만 남긴다."""
    items = _json(value)
    if not isinstance(items, list):
        return []
    return [str(i) for i in items if i]


def selected_manuals(manual_usage: Any) -> list[dict[str, Any]]:
    """판정기가 쓰였다고 본 매뉴얼. 판정 기록이 없는 턴은 비어 있다."""
    usage = _json(manual_usage)
    if not isinstance(usage, dict):
        return []
    return [
        {"category": m.get("category"), "id": m.get("category_id"), "p": m.get("p")}
        for m in usage.get("manuals") or []
        if isinstance(m, dict) and m.get("used") and m.get("category")
    ]


def visited_steps(ai_cots: Any) -> list[int]:
    """CoT 글에 나온 step 번호. 글은 버리고 번호만 남긴다."""
    text = json.dumps(_json(ai_cots), ensure_ascii=False) if ai_cots else ""
    seen: list[int] = []
    for number in STEP_RE.findall(text):
        value = int(number)
        if value not in seen:
            seen.append(value)
    return seen


def slim_turn(db: str, row: dict[str, Any]) -> dict[str, Any]:
    """저장할 칸만 남긴다. question, ai_answer, ai_cots 원문은 여기서 버린다."""
    return {
        "db": db,
        "room": row["chat_room_id"],
        "id": row["id"],
        "status": row.get("status"),
        "agent": row.get("agent_name"),
        "time": str(row.get("time") or ""),
        "selected": selected_manuals(row.get("manual_usage")),
        "tags": _flat(row.get("tags")),
        "functions": _flat(row.get("functions")),
        "steps": visited_steps(row.get("ai_cots")),
    }


def _chunks(items: list[Any], size: int = BATCH) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _fetch_many(cur: Any, sql: str, values: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for chunk in _chunks(values):
        cur.execute(sql.format(marks=",".join(["%s"] * len(chunk))), chunk)
        rows += list(cur.fetchall())
    return rows


def read_db(
    conn: Any, db: str, cases: list[dict[str, Any]], finder: Finder | None
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {"conversations": [], "turns": [], "problems": []}
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute("SET SESSION TRANSACTION READ ONLY")
        chat_ids = list({c["chat_id"] for c in cases})
        rooms = {r["user_chat_id"]: r for r in _fetch_many(cur, ROOMS_SQL, chat_ids)}
        room_ids = [r["id"] for r in rooms.values()]
        by_room: dict[Any, list[dict[str, Any]]] = {}
        if room_ids:
            for row in _fetch_many(cur, TURNS_SQL, room_ids):
                by_room.setdefault(row["chat_room_id"], []).append(row)
    for room in rooms.values():
        out["conversations"].append(
            {
                "db": db,
                "room": room["id"],
                "platform": room.get("platform"),
                "handoff": room.get("handoff_type"),
            }
        )
        out["turns"] += [slim_turn(db, row) for row in by_room.get(room["id"], [])]
    for case in cases:
        case_room = rooms.get(case["chat_id"])
        if case_room is None:
            continue
        link = {"item_key": case["item_key"], "db": db, "room": case_room["id"]}
        if finder is not None:
            turns = by_room.get(case_room["id"], [])
            found = finder(case.get("feedback"), turns, case_room)
            link |= {
                "turn": found.get("conv_id"),
                "method": found.get("method"),
                "score": found.get("score"),
            }
        out["problems"].append(link)
    return out


def real_db_names(auto_cs_dir: Path) -> dict[str, str]:
    path = auto_cs_dir / "tools" / "chat" / "_chat_db_alias.json"
    try:
        alias: dict[str, str] = json.loads(path.read_text(encoding="utf-8")).get("alias") or {}
    except (OSError, ValueError):
        return {}
    return alias


def fetch_chat(env_file: Path, port: int, auto_cs_dir: Path) -> dict[str, Any]:
    env = dotenv_values(env_file)
    main = connect(env, port)
    try:
        with main.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.execute(
                "SELECT item_key, db_name, chat_id, feedback FROM wi_ax_item "
                "WHERE chat_id <> '' AND db_name <> ''"
            )
            cases: list[dict[str, Any]] = list(cur.fetchall())
    finally:
        main.close()

    by_db: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        by_db.setdefault(case["db_name"], []).append(case)
    alias = real_db_names(auto_cs_dir)
    finder = load_finder(auto_cs_dir)
    result: dict[str, Any] = {"conversations": [], "turns": [], "problems": [], "skipped": {}}
    for db_name, group in sorted(by_db.items(), key=lambda item: -len(item[1])):
        try:
            conn = connect(env, port, alias.get(db_name, db_name))
        except pymysql.err.OperationalError as exc:
            result["skipped"][db_name] = f"연결 실패 {exc.args[0] if exc.args else ''}"
            continue
        try:
            part = read_db(conn, db_name, group, finder)
        except (pymysql.err.ProgrammingError, pymysql.err.OperationalError) as exc:
            # 옛 표만 있는 DB는 지금 표가 없다.
            result["skipped"][db_name] = f"읽기 실패 {exc.args[0] if exc.args else ''}"
            continue
        finally:
            conn.close()
        for key in ("conversations", "turns", "problems"):
            result[key] += part[key]
        log.info(
            "chat %s: %d rooms, %d turns", db_name, len(part["conversations"]), len(part["turns"])
        )
    return result
