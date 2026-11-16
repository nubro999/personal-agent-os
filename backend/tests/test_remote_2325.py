import json
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.reply import sources
from app.trackers import case_replies, maintenance
from app.trackers.remote_2325 import Remote, RemoteError


def remote_with(tmp_path: Path, routes: dict[str, Any]) -> Remote:
    """경로 -> 응답(JSON 이나 상태 코드). 쿼리가 다른 같은 경로는 함수로 가른다."""
    session = tmp_path / "session.json"
    session.write_text(json.dumps({"url": "http://mac:2325", "cookie": "c"}))
    remote = Remote(session)

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["cookie"] == "session=c"
        assert request.method == "GET"
        answer = routes.get(request.url.path, 404)
        if callable(answer):
            answer = answer(request)
        if isinstance(answer, int):
            return httpx.Response(answer)
        return httpx.Response(200, json=answer)

    remote._client = httpx.Client(
        base_url=remote.url, headers={"Cookie": "session=c"}, transport=httpx.MockTransport(handle)
    )
    return remote


def test_missing_session_says_how_to_log_in(tmp_path: Path) -> None:
    with pytest.raises(RemoteError, match="r2325.py login"):
        Remote(tmp_path / "none.json")


def test_expired_session_says_how_to_log_in(tmp_path: Path) -> None:
    remote = remote_with(tmp_path, {"/api/statuses": 401})
    with pytest.raises(RemoteError, match="r2325.py login"):
        remote.stages()


def test_items_reads_every_page_of_dated_undated_and_trash(tmp_path: Path) -> None:
    def items(request: httpx.Request) -> dict[str, Any]:
        params = request.url.params
        offset = int(params["offset"])
        if params["lane"] == "trash":
            return {"items": [{"id": "trash"}], "total": 1}
        if "date_from" in params:
            return {"items": [{"id": f"k{offset}"}], "total": 1200}
        # 기본 조건은 바닥 이후 건과 날짜 없는 건을 준다
        return {"items": [{"id": "k500"}, {"id": "undated"}], "total": 2}

    remote = remote_with(tmp_path, {"/api/items": items})
    assert [r["id"] for r in remote.items()] == ["k0", "k500", "k1000", "undated", "trash"]


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        (None, "반영 전"),
        ({"action": "approve", "status": "done"}, "반영됨"),
        ({"action": "approve", "status": "failed"}, "반영 실패"),
        ({"action": "rollback", "status": "done"}, "되돌림"),
        # 되돌리기가 실패했으면 승인은 끝나 있다
        ({"action": "rollback", "status": "failed"}, "반영됨"),
    ],
)
def test_promotions_rebuild_history_from_the_last_job(
    tmp_path: Path, job: dict[str, str] | None, expected: str
) -> None:
    body = {"job": job | {"requested_at": "2026-11-13T02:42:10"} if job else None}
    remote = remote_with(tmp_path, {"/api/rnd/runs/r1/promotion": body})
    assert sources._applied(remote.promotions("r1"), "r1") == expected


class FakeRemote:
    def __init__(self) -> None:
        self.detail_calls: list[str] = []
        self.items_rows = [
            {"id": "Xa", "display_id": "CS-1", "company": "가", "status_group": "할 것",
             "detail_preview": "앞부분"},
            {"id": "Xb", "display_id": "CS-2", "company": "나", "status_group": "끝남",
             "detail_preview": "앞부분"},
            {"id": "Xother", "display_id": "CS-3", "company": "다", "status_group": "할 것",
             "detail_preview": ""},
        ]  # fmt: skip
        self.runs_by_key: dict[str, list[dict[str, Any]]] = {
            "Xa": [
                {"run_id": "old", "agent": "agent_1", "ran_at": datetime(2026, 11, 1)},
                {"run_id": "new", "agent": "agent_4", "ran_at": datetime(2026, 11, 2)},
                {"run_id": "nodiag", "agent": "agent_4", "ran_at": datetime(2026, 11, 3)},
            ]
        }
        self.run_details: dict[str, dict[str, Any]] = {
            "old": {"outputs": {"diagnosis": "옛 진단", "plan": "옛 계획"}, "reviews": {}},
            "new": {
                "outputs": {"diagnosis": "진단 본문", "plan": "수정 방향\n바꾼다\n고칠 것\nx"},
                "reviews": {
                    "diagnosis": {"axis": "diagnosis", "verdict": "O", "comment": "",
                                  "reviewed_at": "2026-11-02T03:00:00"},
                    "plan": None,
                },
            },
            "nodiag": {"outputs": {"plan": "계획만"}, "reviews": {}},
        }  # fmt: skip

    def my_item_keys(self, assignee: str) -> set[str]:
        assert assignee == "신형섭"
        return {"Xa", "Xb"}

    def stages(self) -> dict[str, str]:
        return {"Xb": "deployed"}

    def items(self) -> list[dict[str, Any]]:
        return self.items_rows

    def item(self, key: str) -> dict[str, Any] | None:
        self.detail_calls.append(key)
        return {
            "item_key": key,
            "human_id": "CS-1" if key == "Xa" else "CS-2",
            "company": "가",
            "db_name": "hdex",
            "feedback": "전체 피드백",
            "received_at": datetime(2026, 11, 1, 9, 0),
        }

    def item_by_cs(self, cs: str) -> dict[str, Any] | None:
        return self.item("Xa") if cs == "CS-1" else None

    def runs(self, key: str) -> list[dict[str, Any]]:
        return self.runs_by_key.get(key, [])

    def run(self, run_id: str) -> dict[str, Any] | None:
        return self.run_details.get(run_id)

    def promotions(self, run_id: str) -> list[dict[str, Any]]:
        if run_id != "new":
            return []
        return [{"run_id": "new", "action": "approve", "status": "done",
                 "requested_at": datetime(2026, 11, 2, 4)}]  # fmt: skip


def test_remote_items_reads_detail_only_where_needed() -> None:
    remote = FakeRemote()
    items = maintenance.remote_items(
        remote,  # type: ignore[arg-type]
        "신형섭",
        lambda item: item.item_key == "Xa",
    )
    assert [i.item_key for i in items] == ["Xa", "Xb"]
    assert remote.detail_calls == ["Xa"]
    a, b = items
    assert (a.feedback, a.received_at) == ("전체 피드백", datetime(2026, 11, 1, 9, 0))
    assert (b.feedback, b.received_at, b.stage, b.status) == ("앞부분", None, "deployed", "done")


def test_remote_latest_runs_takes_the_latest_run_with_a_diagnosis() -> None:
    found = case_replies.remote_latest_runs(FakeRemote(), ["Xa", "Xb"])  # type: ignore[arg-type]
    assert list(found) == ["Xa"]
    run = found["Xa"]
    assert (run.run_id, run.cs, run.brand, run.ran_at) == (
        "new",
        "CS-1",
        "hdex",
        datetime(2026, 11, 2),
    )


def test_remote_case_rows_build_the_same_case() -> None:
    rows = sources.remote_case_rows(FakeRemote(), "CS-1")  # type: ignore[arg-type]
    case = sources.build_case("CS-1", rows)
    assert (case.item_key, case.brand, case.feedback, case.other_runs) == (
        "Xa",
        "hdex",
        "전체 피드백",
        1,
    )
    assert case.result is not None
    assert case.result.run_id == "new"  # 반영되고 O 받은 실행
    assert case.result.reviews == ["diagnosis O"]
    assert case.result.applied == "반영됨"
    assert case.result.diagnosis == "진단 본문"
    assert case.result.plan == "수정 방향\n바꾼다"


def test_remote_case_rows_respects_until() -> None:
    rows = sources.remote_case_rows(FakeRemote(), "CS-1")  # type: ignore[arg-type]
    case = sources.build_case("CS-1", rows, until=datetime(2026, 11, 1, 12))
    assert case.result is not None
    assert (case.result.run_id, case.result.applied, case.result.reviews) == ("old", "반영 전", [])


def test_unknown_case_is_an_error() -> None:
    with pytest.raises(ValueError, match="없는 건"):
        sources.remote_case_rows(FakeRemote(), "CS-9")  # type: ignore[arg-type]
