"""함수 응답 칸의 뜻을 회사 도구 `tools/run api.fields`로 뽑는다.

api.fields는 genlab-tools(origin/main)의 스펙과 코드에서 칸마다 타입, 설명, 스펙 문장, 코드가
넣는 값을 찾는다. 읽기만 하고 6시간 캐시를 쓴다. GitHub 회사 계정 인증이 필요하다.

실행: uv run python -m app.ontology.source_fields
결과: <PAO_ONTOLOGY_DIR>/source/fields/<db_name>.json = {함수: [칸, ...]}
"""

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from app.config import get_settings

log = logging.getLogger(__name__)

TIMEOUT = 300


def run_fields(auto_cs_dir: Path, *args: str) -> dict[str, Any] | None:
    result = subprocess.run(
        ["tools/run", "api.fields", *args],
        cwd=auto_cs_dir,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )
    try:
        data: dict[str, Any] = json.loads(result.stdout).get("data") or {}
    except ValueError:
        log.warning("api.fields %s: not json", args)
        return None
    return data or None


def db_names(brand_context: Path) -> list[str]:
    names = {
        json.loads(path.read_text(encoding="utf-8")).get("db_name") or path.parent.name
        for path in brand_context.glob("_*/*/정책.json")
    }
    return sorted(names)


def fetch_brand(auto_cs_dir: Path, db_name: str) -> dict[str, list[dict[str, Any]]]:
    listing = run_fields(auto_cs_dir, db_name)
    if listing is None:
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for function in listing.get("functions") or []:
        if not function.get("response_fields"):
            continue
        detail = run_fields(auto_cs_dir, db_name, "--fn", function["name"], "--limit", "4")
        out[function["name"]] = (detail or {}).get("fields") or []
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    out_dir = settings.ontology_dir / "source" / "fields"
    out_dir.mkdir(parents=True, exist_ok=True)
    for db_name in db_names(settings.brand_context_dir):
        fields = fetch_brand(settings.auto_cs_dir, db_name)
        (out_dir / f"{db_name}.json").write_text(
            json.dumps(fields, ensure_ascii=False), encoding="utf-8"
        )
        log.info(
            "fields %s: %d functions, %d fields",
            db_name,
            len(fields),
            sum(map(len, fields.values())),
        )


if __name__ == "__main__":
    main()
