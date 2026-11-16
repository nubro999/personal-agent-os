"""서버 맥 2325 웹 API. 이 PC 의 2325 DB 터널이 닫혀 있을 때만 쓴다.

서버 맥은 DB 터널을 쥐고 2325 를 사내망에 열어 둔다. 이 PC 의 AWS 키가 죽어 터널이 안 떠도
2325 화면이 보는 값을 그대로 읽을 수 있다. GET 만 보낸다.

세션은 glab_mt/.claude/skills/2325-remote/r2325.py login 이 남긴 쿠키를 쓴다. 401 이 나면
그 명령으로 다시 로그인해야 한다. 비밀번호는 여기서 다루지 않는다.

값은 DB 에서 읽을 때와 같은 모양(행 dict, 시각은 naive datetime)으로 돌려준다.
"""

import json
import socket
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

DEFAULT_URL = "http://192.168.0.20:2325"
PAGE = 500
EPOCH = "2000-01-01"
LOGIN_HINT = "/usr/bin/python3 -I ~/glab/glab_mt/.claude/skills/2325-remote/r2325.py login"


class RemoteError(RuntimeError):
    """서버 맥 2325 에도 닿지 못했다."""


def tunnel_up(port: int) -> bool:
    """이 PC 의 2325 DB 터널 포트가 열려 있나.

    닫혀 있으면 재시도를 기다리지 않고 서버 맥으로 간다.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def when(value: Any) -> datetime | None:
    """API 의 ISO 시각을 DB 와 같은 naive datetime 으로."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value)).replace(tzinfo=None)


class Remote:
    def __init__(self, session_file: Path, url: str | None = None, timeout: float = 30) -> None:
        try:
            session = json.loads(session_file.read_text())
        except (OSError, ValueError) as exc:
            raise RemoteError(f"서버 맥 2325 세션이 없다. 로그인: {LOGIN_HINT}") from exc
        cookie = session.get("cookie")
        if not cookie:
            raise RemoteError(f"서버 맥 2325 세션이 없다. 로그인: {LOGIN_HINT}")
        self.url = (url or session.get("url") or DEFAULT_URL).rstrip("/")
        self._client = httpx.Client(
            base_url=self.url, headers={"Cookie": f"session={cookie}"}, timeout=timeout
        )

    def __enter__(self) -> "Remote":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get(self, path: str, **params: Any) -> Any:
        """GET 하나. 404 는 None 이다."""
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise RemoteError(
                f"서버 맥 2325({self.url})에 닿지 않음: {type(exc).__name__}"
            ) from exc
        if response.status_code == 401:
            raise RemoteError(f"서버 맥 2325 세션 만료. 다시 로그인: {LOGIN_HINT}")
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise RemoteError(f"서버 맥 2325 {path}: HTTP {response.status_code}")
        return response.json()

    # ── 건 ──

    def my_item_keys(self, assignee: str) -> set[str]:
        people = self.get("/api/assignees")["assignees"]
        ids = {p["id"] for p in people if p["name"] == assignee}
        if not ids:
            raise RemoteError(f"서버 맥 2325에 담당자가 없다: {assignee}")
        mapping: dict[str, list[int]] = self.get("/api/assignments")["assignments"]
        return {key for key, owners in mapping.items() if ids & set(owners)}

    def stages(self) -> dict[str, str]:
        result: dict[str, str] = self.get("/api/statuses")["statuses"]
        return result

    def items(self) -> list[dict[str, Any]]:
        """wi_ax_item 전체. 목록이라 피드백은 앞부분만 있다.

        목록 API 는 기본으로 큐 바닥 이전 건과 휴지통을 가린다. DB 조회와 같게 하려고
        날짜가 있는 건 전부(먼 시작일), 날짜 없는 건(기본 조건이 남긴다), 휴지통을 합친다.
        """
        rows: dict[str, dict[str, Any]] = {}
        for params in (
            {"lane": "all", "date_from": EPOCH},
            {"lane": "all"},
            {"lane": "trash"},
        ):
            for row in self._pages(params):
                rows.setdefault(row["id"], row)
        return list(rows.values())

    def _pages(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.get("/api/items", **params, limit=PAGE, offset=offset)
            rows += page["items"]
            offset += PAGE
            if offset >= page["total"] or not page["items"]:
                return rows

    def item(self, item_key: str) -> dict[str, Any] | None:
        """wi_ax_item 한 줄(전체 피드백, db_name, received_at)."""
        found = self.get(f"/api/items/{item_key}")
        if not found:
            return None
        row: dict[str, Any] = found["item"]
        row["received_at"] = when(row.get("received_at"))
        return row

    def item_by_cs(self, cs: str) -> dict[str, Any] | None:
        """접수번호로 찾는다. 같은 번호가 여럿이면 가장 늦게 접수된 건."""
        page = self.get("/api/items", q=cs, lane="all", limit=50)
        keys = [i["id"] for i in page["items"] if i.get("display_id") == cs]
        rows = [row for key in keys if (row := self.item(key))]
        if not rows:
            return None
        return max(rows, key=lambda r: r["received_at"] or datetime.min)

    # ── 에이전트 실행 ──

    def runs(self, item_key: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = self.get("/api/rnd/runs", item_key=item_key, limit=500)["runs"]
        for row in rows:
            row["ran_at"] = when(row.get("ran_at"))
        return rows

    def run(self, run_id: str) -> dict[str, Any] | None:
        """실행 하나: run, outputs{part: body}, reviews{axis: {...}}."""
        found: dict[str, Any] | None = self.get(f"/api/rnd/runs/{run_id}")
        return found

    def promotions(self, run_id: str) -> list[dict[str, Any]]:
        """rnd_promotion 줄. API 는 마지막 요청만 주므로 거기서 앞 이력을 되짚는다.

        되돌리기는 승인이 끝난 뒤에만 누를 수 있다.
        마지막이 끝나지 않은 되돌리기면 승인은 끝나 있다.
        """
        found = self.get(f"/api/rnd/runs/{run_id}/promotion")
        job = (found or {}).get("job")
        if not job:
            return []
        last = {
            "run_id": run_id,
            "action": job.get("action"),
            "status": job.get("status"),
            "requested_at": when(job.get("requested_at")),
        }
        if last["action"] == "rollback" and last["status"] != "done":
            return [last | {"action": "approve", "status": "done"}, last]
        return [last]
