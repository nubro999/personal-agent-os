"""채팅 DB에서 턴과 대화를 그때그때 읽는다. 저장하지 않는다.

온톨로지에는 턴의 구조만 있다. 고객 질문, 봇 답변, 함수 인자와 반환값은 양이 많고 그래프에
필요 없어서, 에이전트가 원인을 가릴 때만 이 함수로 읽는다. 읽기 전용 세션이다.
"""

import json
from functools import lru_cache
from typing import Any

import pymysql
from dotenv import dotenv_values

from app.config import get_settings
from app.ontology.source_chat import real_db_names
from app.trackers.maintenance import connect

RESULT_LIMIT = 8000
TEXT_LIMIT = 1500
CONVERSATION_LIMIT = 60


@lru_cache(maxsize=1)
def _aliases() -> dict[str, str]:
    return real_db_names(get_settings().auto_cs_dir)


def _parse(node_id: str, prefix: str) -> tuple[str, int]:
    """turn:<db>:<id>, conversation:<db>:<room> → (db, 번호)."""
    kind, db, number = node_id.split(":", 2)
    if kind != prefix or not number.isdigit():
        raise ValueError(f"{prefix} id가 아니다: {node_id}")
    return db, int(number)


def _clip(value: Any, limit: int) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + f"…(이하 {len(text) - limit}자 생략)"


def _query(db: str, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    settings = get_settings()
    conn = connect(
        dotenv_values(settings.maint_env_file), settings.maint_db_port, _aliases().get(db, db)
    )
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.execute(sql, params)
            return list(cur.fetchall())
    finally:
        conn.close()


def read_turn(node_id: str) -> dict[str, Any]:
    db, turn_id = _parse(node_id, "turn")
    rows = _query(
        db,
        "SELECT id, chat_room_id, status, `time`, question, ai_answer, "
        "tool_call_requests, tool_call_results FROM conversation WHERE id = %s",
        (turn_id,),
    )
    if not rows:
        return {"error": f"없는 턴: {node_id}"}
    row = rows[0]
    return {
        "id": node_id,
        "conversation": f"conversation:{db}:{row['chat_room_id']}",
        "status": row.get("status"),
        "time": str(row.get("time") or ""),
        "question": _clip(row.get("question"), TEXT_LIMIT),
        "answer": _clip(row.get("ai_answer"), TEXT_LIMIT),
        "tool_calls": _clip(row.get("tool_call_requests"), RESULT_LIMIT),
        "tool_results": _clip(row.get("tool_call_results"), RESULT_LIMIT),
    }


def read_conversation(node_id: str) -> dict[str, Any]:
    db, room = _parse(node_id, "conversation")
    rows = _query(
        db,
        "SELECT id, role, status, `time`, question, ai_answer FROM conversation "
        "WHERE chat_room_id = %s ORDER BY id LIMIT %s",
        (room, CONVERSATION_LIMIT),
    )
    return {
        "id": node_id,
        "turns": [
            {
                "turn": f"turn:{db}:{row['id']}",
                "role": row.get("role"),
                "status": row.get("status"),
                "time": str(row.get("time") or ""),
                "question": _clip(row.get("question"), TEXT_LIMIT),
                "answer": _clip(row.get("ai_answer"), TEXT_LIMIT),
            }
            for row in rows
        ],
    }
