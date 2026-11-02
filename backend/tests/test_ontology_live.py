from typing import Any

import pytest

from app.ontology import live


def test_parse_rejects_wrong_kind() -> None:
    assert live._parse("turn:oa_v2:11", "turn") == ("oa_v2", 11)
    with pytest.raises(ValueError):
        live._parse("conversation:oa_v2:5", "turn")


def test_clip_marks_what_was_cut() -> None:
    assert live._clip("가나다", 10) == "가나다"
    assert live._clip("가" * 12, 10).endswith("…(이하 2자 생략)")
    assert live._clip(None, 10) == ""


def test_read_turn_returns_text_and_tool_results(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, tuple[Any, ...]]] = []

    def query(db: str, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        seen.append((db, params))
        return [
            {
                "id": 11,
                "chat_room_id": 5,
                "status": "ai_answered",
                "time": "2026-10-27 10:00:00",
                "question": "반품 언제 돼요",
                "ai_answer": "10/24 입고됐습니다",
                "tool_call_requests": [{"name": "get_return"}],
                "tool_call_results": {"반송수령일자": "2026-10-24", "반품접수정보": None},
            }
        ]

    monkeypatch.setattr(live, "_query", query)

    turn = live.read_turn("turn:mommycare:11")

    assert seen == [("mommycare", (11,))]
    assert turn["conversation"] == "conversation:mommycare:5"
    assert '"반송수령일자": "2026-10-24"' in turn["tool_results"]
    assert turn["answer"] == "10/24 입고됐습니다"
