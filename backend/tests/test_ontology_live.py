from pathlib import Path
from typing import Any

import pytest

from app.ontology import live
from app.ontology.trial import after_cutoff, turn_cutoff


def test_parse_rejects_wrong_kind() -> None:
    assert live._parse("turn:oa_v2:11", "turn") == ("oa_v2", "11")
    assert live._parse("function:oa_v2:get_order", "function") == ("oa_v2", "get_order")
    with pytest.raises(ValueError):
        live._parse("conversation:oa_v2:5", "turn")
    with pytest.raises(ValueError):
        live._parse("turn:oa_v2:abc", "turn")


def test_clip_marks_what_was_cut() -> None:
    assert live._clip("가나다", 10) == "가나다"
    assert live._clip("가" * 12, 10).endswith("…(이하 2자 생략)")
    assert live._clip(None, 10) == ""


def test_read_turn_uses_chat_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, ...]] = []

    def run(name: str, *args: str) -> dict[str, Any]:
        seen.append((name, *args))
        return {
            "id": 11,
            "room_id": 5,
            "answer": "10/24 입고됐습니다",
            "cot": [{"step 진행 현황": "step5 → step6"}],
            "tools": [{"name": "get_return", "result": "가" * 9000}],
            "tokens": {"x": 1},
        }

    monkeypatch.setattr(live, "_run", run)

    turn = live.read_turn("turn:mommycare:11")

    assert seen == [("chat.turn", "mommycare", "11")]
    assert turn["conversation"] == "conversation:mommycare:5"
    assert turn["cot"][0]["step 진행 현황"] == "step5 → step6"
    assert turn["tools"][0]["result"].endswith("생략)")
    assert "tokens" not in turn


def test_read_turn_passes_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live, "_run", lambda *a: {"error": "없는 턴"})
    assert live.read_turn("turn:mommycare:11") == {"error": "없는 턴"}


def test_read_conversation_keeps_only_that_room(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live, "_chat_id", lambda db, room: "chat-1")
    monkeypatch.setattr(
        live,
        "_run",
        lambda *a: {
            "rooms": [{"id": 5}, {"id": 6}],
            "turns": [
                {"id": 1, "room_id": 5, "question": "q", "answer": "a"},
                {"id": 2, "room_id": 6, "question": "q", "answer": "a"},
            ],
        },
    )

    conv = live.read_conversation("conversation:mommycare:5")

    assert [t["turn"] for t in conv["turns"]] == ["turn:mommycare:1"]
    assert conv["room"] == {"id": 5}


def test_read_prompt_toc_then_section(monkeypatch: pytest.MonkeyPatch) -> None:
    def prompt(db: str, turn: str, out: Path) -> dict[str, Any]:
        out.mkdir(parents=True, exist_ok=True)
        (out / "s9.txt").write_text("step5 본문" * 5000, encoding="utf-8")
        return {
            "chars": 100,
            "toc": [{"n": 9, "title": "1번째 응대 매뉴얼", "chars": 40000, "file": "s9.txt"}],
            "info_tags": ["반품 환불 일정"],
            "tools": {"layers": [{"function": "get_return", "called": True}]},
        }

    monkeypatch.setattr(live, "_prompt", prompt)

    toc = live.read_prompt("turn:mommycare:11")
    assert toc["toc"] == [{"n": 9, "title": "1번째 응대 매뉴얼", "chars": 40000}]
    assert toc["info_tags"] == ["반품 환불 일정"]

    first = live.read_prompt("turn:mommycare:11", 9)
    assert len(first["text"]) == live.SECTION_LIMIT and first["more"] is True
    rest = live.read_prompt("turn:mommycare:11", 9, live.SECTION_LIMIT)
    assert rest["more"] is False
    assert live.read_prompt("turn:mommycare:11", 3) == {"error": "없는 섹션: 3"}


def test_trial_cannot_read_turns_after_the_problem_turn() -> None:
    data = {
        "links": [
            {"source": "case:A", "target": "turn:mommycare:100", "name": "refers_to"},
            {"source": "case:A", "target": "conversation:mommycare:5", "name": "refers_to"},
            {"source": "case:B", "target": "turn:mommycare:900", "name": "refers_to"},
        ]
    }
    cutoff = turn_cutoff(data, "case:A")

    assert cutoff == {"mommycare": 100}
    assert not after_cutoff("turn:mommycare:100", cutoff)
    assert after_cutoff("turn:mommycare:101", cutoff)
    assert not after_cutoff("turn:oa_v2:999", cutoff)
    assert not after_cutoff("conversation:mommycare:5", cutoff)
