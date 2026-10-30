from datetime import date

from app.agents.task.prompts import build_prompt


def test_prompt_lists_this_and_next_week_calendar() -> None:
    prompt = build_prompt(date(2026, 10, 30), [("사용자", "금요일까지 정리해야 함")])

    assert "오늘: 2026-10-30 (금요일)" in prompt
    assert "이번 주: 월 2026-10-26, 화 2026-10-27" in prompt
    assert "금 2026-10-30" in prompt and "일 2026-11-01" in prompt
    assert "다음 주: 월 2026-11-02" in prompt and "금 2026-11-06" in prompt
    assert prompt.endswith("사용자: 금요일까지 정리해야 함")
