"""ai-admin 설정을 config_tree JSON으로 받는다. `admin export`(읽기 전용)를 부른다.

봇 구성(company_id)마다 운영본(--env prod) 하나씩 받는다. 실제 챗봇이 읽는 것이 운영본이다.
config_tree에는 카테고리, 매뉴얼, step, 정보, 멘트, 함수가 있고 AI 설정, 회사 프롬프트,
채널 설정, 이벤트, 태그는 없다. 옛 형식인 번들(export-bundle)은 쓰지 않는다.

회사 프롬프트(company_prompts.main_prompt)에서는 기본 매뉴얼("그 외 문의 응대 매뉴얼") 절만
읽기 전용 쿼리로 잘라 둔다. 카테고리 테이블에 없지만 봇이 실제로 쓰는 매뉴얼이다.

실행: uv run python -m app.ontology.source_admin [--fallback-only]
"""

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import pymysql
from dotenv import dotenv_values

from app.config import get_settings
from app.trackers.maintenance import connect

log = logging.getLogger(__name__)

EXPORT_TIMEOUT = 300
FALLBACK_HEADER = "## 그 외 문의 응대 매뉴얼"
FALLBACK_FILE = "fallback_manual.json"


def company_ids(instances: Path) -> dict[int, str]:
    """지금 온톨로지의 봇 구성에서 company_id와 이름을 얻는다."""
    data = json.loads(instances.read_text(encoding="utf-8"))
    ids: dict[int, str] = {}
    for node in data["nodes"]:
        key = node["id"].removeprefix("bot_config:")
        if node["concept"] == "bot_config" and key.isdigit():
            ids[int(key)] = node["label"]
    return ids


def export(company_id: int, out: Path) -> bool:
    result = subprocess.run(
        ["admin", "--env", "prod", "export", str(company_id), "--out", str(out)],
        capture_output=True,
        text=True,
        timeout=EXPORT_TIMEOUT,
        check=False,
    )
    if result.returncode != 0 or not out.is_file():
        log.warning("export %s failed: %s", company_id, result.stderr.strip()[-200:])
        return False
    return True


def fallback_section(main_prompt: str | None) -> str | None:
    """회사 프롬프트에서 기본 매뉴얼 절만. 다음 ## 머리글 앞까지다."""
    if not main_prompt or FALLBACK_HEADER not in main_prompt:
        return None
    start = main_prompt.index(FALLBACK_HEADER)
    end = main_prompt.find("\n## ", start + len(FALLBACK_HEADER))
    return main_prompt[start : end if end >= 0 else None].strip()


def fetch_fallback(env_file: Path, port: int) -> dict[str, str]:
    """company_id -> 운영 기본 매뉴얼 절. 프롬프트 전문은 저장하지 않는다."""
    conn = connect(dotenv_values(env_file), port, "ai_admin")
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.execute(
                "SELECT company_id, main_prompt FROM company_prompts WHERE environment = 'prod'"
            )
            rows: list[dict[str, Any]] = list(cur.fetchall())
    finally:
        conn.close()
    sections = {str(r["company_id"]): fallback_section(r["main_prompt"]) for r in rows}
    missing = sorted(k for k, v in sections.items() if v is None)
    log.info("fallback manual: %d companies, missing %s", len(rows), missing)
    return {k: v for k, v in sections.items() if v}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    out_dir = settings.ontology_dir / "source" / "admin"
    out_dir.mkdir(parents=True, exist_ok=True)
    fallback = fetch_fallback(settings.maint_env_file, settings.maint_db_port)
    (out_dir / FALLBACK_FILE).write_text(
        json.dumps(fallback, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    if "--fallback-only" in sys.argv:
        return
    ids = company_ids(settings.ontology_dir / "build" / "instances.json")
    done = 0
    for company_id, label in sorted(ids.items()):
        done += export(company_id, out_dir / f"{company_id}_prod.json")
        log.info("export %s %s (%d/%d)", company_id, label, done, len(ids))


if __name__ == "__main__":
    main()
