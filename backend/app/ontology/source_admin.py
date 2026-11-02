"""ai-admin 설정을 config_tree JSON으로 받는다. `admin export`(읽기 전용)를 부른다.

봇 구성(company_id)마다 운영본(--env prod) 하나씩 받는다. 실제 챗봇이 읽는 것이 운영본이다.
config_tree에는 카테고리, 매뉴얼, step, 정보, 멘트, 함수가 있고 AI 설정, 회사 프롬프트,
채널 설정, 이벤트, 태그는 없다. 옛 형식인 번들(export-bundle)은 쓰지 않는다.

실행: uv run python -m app.ontology.source_admin
"""

import json
import logging
import subprocess
from pathlib import Path

from app.config import get_settings

log = logging.getLogger(__name__)

EXPORT_TIMEOUT = 300


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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    out_dir = settings.ontology_dir / "source" / "admin"
    out_dir.mkdir(parents=True, exist_ok=True)
    ids = company_ids(settings.ontology_dir / "build" / "instances.json")
    done = 0
    for company_id, label in sorted(ids.items()):
        done += export(company_id, out_dir / f"{company_id}_prod.json")
        log.info("export %s %s (%d/%d)", company_id, label, done, len(ids))


if __name__ == "__main__":
    main()
