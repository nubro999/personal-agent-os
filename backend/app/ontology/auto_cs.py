"""회사 도구 `tools/run`(auto_cs_maintenance)을 부른다.

함수마다 stdout에 JSON 하나를 낸다.
성공은 {"data", "source", "warnings"}, 실패는 {"error", "source"}다.
읽기만 하는 함수만 부른다. 쓰는 법은 auto_cs_maintenance `tools/README.md`.
"""

import json
import subprocess
from pathlib import Path
from typing import Any

TIMEOUT = 300


def run(auto_cs_dir: Path, name: str, *args: str, timeout: int = TIMEOUT) -> dict[str, Any]:
    """함수 하나를 부르고 JSON을 그대로 돌려준다. JSON이 아니면 {"error": ...}."""
    try:
        result = subprocess.run(
            ["tools/run", name, *args],
            cwd=auto_cs_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"{name}: {timeout}초 안에 끝나지 않았다"}
    try:
        parsed: dict[str, Any] = json.loads(result.stdout)
    except ValueError:
        tail = (result.stderr or result.stdout).strip()[-500:]
        return {"error": f"{name}: JSON이 아니다. {tail}"}
    return parsed
