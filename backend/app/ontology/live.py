"""채팅 DB와 함수 로그를 그때그때 읽는다. 저장하지 않는다.

온톨로지에는 턴의 구조만 있다. 고객 질문, 봇 답변, 생각 과정, 서빙 프롬프트, 함수 반환값과
실행 로그는 양이 많고 그래프에 필요 없어서, 에이전트가 원인을 가릴 때만 이 함수로 읽는다.
읽기는 회사 도구 `tools/run`(auto_cs_maintenance)에 맡긴다. 같은 일을 하는 코드를 두지 않는다.
"""

import json
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

import pymysql
from dotenv import dotenv_values

from app.config import get_settings
from app.ontology import auto_cs
from app.ontology.source_chat import real_db_names
from app.trackers.maintenance import connect

RESULT_LIMIT = 8000
TEXT_LIMIT = 1500
SECTION_LIMIT = 20000
LOG_LINE_LIMIT = 1500
CONVERSATION_LIMIT = 60


@lru_cache(maxsize=1)
def _aliases() -> dict[str, str]:
    return real_db_names(get_settings().auto_cs_dir)


def _parse(node_id: str, prefix: str) -> tuple[str, str]:
    """turn:<db>:<id>, conversation:<db>:<room>, function:<db>:<이름> → (db, 나머지)."""
    kind, db, rest = node_id.split(":", 2)
    if kind != prefix or not rest:
        raise ValueError(f"{prefix} id가 아니다: {node_id}")
    if prefix in ("turn", "conversation") and not rest.isdigit():
        raise ValueError(f"{prefix} 번호가 아니다: {node_id}")
    return db, rest


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


def _run(name: str, *args: str) -> dict[str, Any]:
    """회사 함수의 data. 실패하면 {"error": ...}."""
    result = auto_cs.run(get_settings().auto_cs_dir, name, *args)
    if "error" in result:
        return {"error": result["error"]}
    data: dict[str, Any] = result.get("data") or {}
    return data


def _chat_id(db: str, room: int) -> str | None:
    rows = _query(db, "SELECT user_chat_id FROM chat_room WHERE id = %s", (room,))
    return rows[0]["user_chat_id"] if rows else None


def read_turn(node_id: str) -> dict[str, Any]:
    """chat.turn: 질문, 답변, 생각 과정(고른 매뉴얼, step 진행), 실린 정보, 함수 호출과 반환값."""
    db, turn = _parse(node_id, "turn")
    data = _run("chat.turn", db, turn)
    if "error" in data:
        return data
    data.pop("tokens", None)
    for call in data.get("tools") or []:
        call["result"] = _clip(call.get("result"), RESULT_LIMIT)
    for info in data.get("info") or []:
        info["content"] = _clip(info.get("content"), TEXT_LIMIT)
    data["conversation"] = f"conversation:{db}:{data.get('room_id')}"
    return {"id": node_id, **data}


def read_conversation(node_id: str) -> dict[str, Any]:
    """chat.conversation: 그 방의 턴을 순서대로. 질문, 답변, 고른 매뉴얼 이름, 부른 함수 이름."""
    db, room = _parse(node_id, "conversation")
    chat_id = _chat_id(db, int(room))
    if not chat_id:
        return {"error": f"없는 대화: {node_id}"}
    data = _run("chat.conversation", db, chat_id)
    if "error" in data:
        return data
    turns = [t for t in data.get("turns") or [] if str(t.get("room_id")) == room]
    for t in turns:
        t["turn"] = f"turn:{db}:{t.get('id')}"
        t["question"] = _clip(t.get("question"), TEXT_LIMIT)
        t["answer"] = _clip(t.get("answer"), TEXT_LIMIT)
    rooms = [r for r in data.get("rooms") or [] if str(r.get("id")) == room]
    return {"id": node_id, "room": rooms[0] if rooms else None, "turns": turns[:CONVERSATION_LIMIT]}


def _prompt(db: str, turn: str, out: Path) -> dict[str, Any]:
    return _run("chat.prompt", db, turn, "--out", str(out))


def read_prompt(node_id: str, section: int = 0, offset: int = 0) -> dict[str, Any]:
    """chat.prompt: 그 턴에 봇이 실제로 받은 서빙 프롬프트.

    section 0이면 목차, 실린 정보 태그, 함수 세 층(번들, 매뉴얼, 실제 호출)을 준다.
    section n이면 목차 n번 섹션 본문을 offset부터 SECTION_LIMIT자까지 준다.
    """
    db, turn = _parse(node_id, "turn")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        data = _prompt(db, turn, out)
        if "error" in data:
            return data
        toc = data.get("toc") or []
        if section == 0:
            return {
                "id": node_id,
                "chars": data.get("chars"),
                "toc": [{k: v for k, v in s.items() if k != "file"} for s in toc],
                "info_tags": data.get("info_tags"),
                "functions": (data.get("tools") or {}).get("layers"),
                "notes": data.get("notes"),
            }
        found = next((s for s in toc if s.get("n") == section), None)
        if not found or not found.get("file"):
            return {"error": f"없는 섹션: {section}"}
        body = (out / found["file"]).read_text(encoding="utf-8")
    end = offset + SECTION_LIMIT
    return {
        "id": node_id,
        "section": section,
        "title": found.get("title"),
        "chars": len(body),
        "offset": offset,
        "text": body[offset:end],
        "more": end < len(body),
    }


def read_tool_logs(node_id: str) -> dict[str, Any]:
    """logs.tool_calls: 그 턴의 함수가 서버에서 실제로 무엇을 조회했나(CloudWatch).

    인자, 외부 API 조회 범위와 건수, 중간 분기가 나온다. this_chat이 false인 호출은 다른 고객 것일
    수 있어 뺀다.
    """
    db, turn = _parse(node_id, "turn")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        prompt = _prompt(db, turn, out / "prompt")
        if "error" in prompt:
            return prompt
        calls = (prompt.get("tools") or {}).get("calls") or []
        if not calls:
            return {"id": node_id, "calls": [], "note": "이 턴에서 부른 함수가 없다"}
        rows = _query(db, "SELECT chat_room_id FROM conversation WHERE id = %s", (int(turn),))
        chat_id = _chat_id(db, rows[0]["chat_room_id"]) if rows else None
        if not chat_id:
            return {"error": f"대화 id를 못 찾았다: {node_id}"}
        path = out / "calls.json"
        path.write_text(json.dumps(calls, ensure_ascii=False), encoding="utf-8")
        data = _run("logs.tool_calls", db, chat_id, "--calls-json", str(path))
    if "error" in data:
        return data
    kept = [c for c in data.get("calls") or [] if c.get("this_chat") is not False]
    for call in kept:
        call["lines"] = [_clip(line.get("msg"), LOG_LINE_LIMIT) for line in call.get("lines") or []]
    return {"id": node_id, "calls": kept, "no_time": data.get("no_time"), "note": data.get("note")}


def read_api_spec(node_id: str) -> dict[str, Any]:
    """api.spec: 함수가 낼 수 있는 응답(코드, 메시지). 배포된 genlab-tools openapi에서."""
    db, name = _parse(node_id, "function")
    data = _run("api.spec", db, "--only", name)
    if "error" in data:
        return data
    return {"id": node_id, "functions": data.get("functions"), "found": data.get("found", True)}


def read_function_spec(
    node_id: str, section: str = "", pattern: str = "", file: str = ""
) -> dict[str, Any]:
    """genlab-tools 스펙 문서. 호출 흐름, 비즈니스 로직, 바깥 시스템 칸의 뜻이 여기에만 있다."""
    db, name = _parse(node_id, "function")
    args = ["--brand", db, "--fn", name]
    if pattern:
        data = auto_cs.toolspec(
            get_settings().auto_cs_dir, "grep", *args, "--pattern", pattern, "--limit", "30"
        )
    else:
        if file:
            args += ["--file", file]
        if section:
            args += ["--section", section]
        data = auto_cs.toolspec(get_settings().auto_cs_dir, "fn", *args)
    if "error" in data:
        return {"error": data["error"]}
    for key in ("본문", "전문"):
        if key in data:
            data[key] = _clip(data[key], SECTION_LIMIT)
    return {"id": node_id, **data}
