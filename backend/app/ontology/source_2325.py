"""2325 DB에서 온톨로지 사례를 읽는다. 읽기 전용 세션이고 글은 짧게만 가져온다.

전용 터널(pao-2325-tunnel)로 붙는다. user 표는 id와 이름만 읽는다.
"""

import json
from pathlib import Path
from typing import Any

import pymysql
from dotenv import dotenv_values

from app.trackers.maintenance import connect

TEXT = 240

QUERIES = {
    "users": "SELECT id, name FROM user",
    "cases": f"""
        SELECT item_key, human_id, brand_id, db_name, company, status_group,
               LEFT(feedback, {TEXT}) AS feedback FROM wi_ax_item""",
    "adhoc": f"""
        SELECT item_key, brand, company, LEFT(issue, {TEXT}) AS issue FROM rnd_adhoc_request""",
    "assignments": """
        SELECT m.item_key, a.name FROM maintenance_assignment m
        JOIN assignee a ON a.id = m.assignee_id""",
    "runs": "SELECT run_id, item_key, brand, kind, agent, ran_at FROM rnd_run",
    "diagnoses": f"""
        SELECT run_id, LEFT(body, {TEXT}) AS body FROM rnd_output WHERE part = 'diagnosis'""",
    "plans": "SELECT DISTINCT run_id FROM rnd_output WHERE part = 'plan'",
    "rounds": "SELECT run_id, body FROM rnd_output WHERE part = 'rounds'",
    "notes": "SELECT run_id, body FROM rnd_output WHERE part = 'brand_updates'",
    "changes": "SELECT run_id, body FROM rnd_output WHERE part = 'changes'",
    "snapshots": "SELECT id, run_id, target, target_ref, channel FROM rnd_snapshot",
    "reviews": "SELECT run_id, axis, verdict, reviewer FROM rnd_review",
    "promotions": "SELECT id, run_id, item_key, action, status FROM rnd_promotion",
    "fixes": f"""
        SELECT id, item_key, date, company, source_run_id, author,
               LEFT(misresponse_cause, {TEXT}) AS cause FROM maintenance_context""",
    "events": "SELECT label, title, kind, state FROM period_event",
    "event_brands": "SELECT label, brand, company_id FROM period_event_brand",
}


def _plain(row: dict[str, Any]) -> dict[str, Any]:
    """날짜를 글로 바꿔 JSON으로 보관한다."""
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}


def _slim_body(name: str, row: dict[str, Any]) -> dict[str, Any]:
    """큰 JSON 본문은 그래프에 쓰는 칸만 남긴다."""
    if name not in ("rounds", "notes", "changes"):
        return row
    try:
        body = json.loads(row.pop("body"))
    except (TypeError, ValueError):
        return row | {"parsed": None}
    if name == "rounds":
        parsed: Any = [
            {k: r.get(k) for k in ("round", "verdict", "한줄")} for r in body if isinstance(r, dict)
        ]
    elif name == "notes":
        parsed = [
            {k: str(i.get(k) or "")[:TEXT] for k in ("id", "brand", "kind", "about", "text")}
            for i in body.get("items") or []
        ]
    else:
        parsed = {
            "company_id": body.get("company_id"),
            "categories": [
                {"name": c.get("orig_name"), "id": c.get("orig_id")}
                for c in body.get("categories") or []
                if isinstance(c, dict)
            ],
        }
    return row | {"parsed": parsed}


def fetch_rows(env_file: Path, port: int) -> dict[str, list[dict[str, Any]]]:
    conn = connect(dotenv_values(env_file), port)
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            rows: dict[str, list[dict[str, Any]]] = {}
            for name, sql in QUERIES.items():
                cur.execute(sql)
                rows[name] = [_slim_body(name, _plain(dict(r))) for r in cur.fetchall()]
    finally:
        conn.close()
    return rows
