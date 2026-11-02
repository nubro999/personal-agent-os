from datetime import datetime

from app.reply import sources
from app.reply.draft import Inputs, prompt_for


def test_section_cuts_between_markers() -> None:
    body = "테스트\n끝\n\nhdex 수정 방향\n\n요약\n수정: step10\n\n고칠 것\n1. 자세히"
    assert sources.section(body, "수정 방향", "\n고칠 것", 100) == "수정 방향\n\n요약\n수정: step10"
    assert sources.section("진단 요약\n결론\n\n요청과 판정\n상세", "", "\n요청과 판정", 100) == (
        "진단 요약\n결론"
    )
    assert sources.section("가" * 20, "", "", 10) == "가" * 10 + "…"


def run(run_id: str, hour: int) -> dict[str, object]:
    return {
        "run_id": run_id,
        "agent": "agent_1",
        "ran_at": datetime(2026, 11, 2, hour),
        "has_diagnosis": 1,
    }


def test_pick_run_prefers_applied_then_good_then_latest() -> None:
    runs = [run("a", 10), run("b", 11), run("c", 12)]
    good = [{"run_id": "a", "axis": "diagnosis", "verdict": "O"}]
    applied = [{"run_id": "b", "action": "approve", "status": "done"}]

    assert sources.pick_run(runs, [], [])["run_id"] == "c"  # type: ignore[index]
    assert sources.pick_run(runs, good, [])["run_id"] == "a"  # type: ignore[index]
    assert sources.pick_run(runs, good, applied)["run_id"] == "b"  # type: ignore[index]
    assert sources.pick_run([{**run("d", 9), "has_diagnosis": 0}], [], []) is None


def test_applied_reads_promotions_of_that_run() -> None:
    rows = [
        {"run_id": "a", "action": "approve", "status": "done"},
        {"run_id": "b", "action": "approve", "status": "failed"},
        {"run_id": "c", "action": "approve", "status": "done"},
        {"run_id": "c", "action": "rollback", "status": "done"},
    ]
    assert sources._applied(rows, "a") == "반영됨"
    assert sources._applied(rows, "b") == "반영 실패"
    assert sources._applied(rows, "c") == "되돌림"
    assert sources._applied(rows, "z") == "반영 전"


def test_first_reply_skips_short_acks_and_others() -> None:
    thread = sources.Thread(
        link="",
        messages=[
            sources.Message("1", "", "zerotalk-feedback", "Hdex (CS-0004456)"),
            sources.Message("2", "", "신형섭", "넵 전달해주세요!"),
            sources.Message("3", "", "신형섭", "<@U1> " + "원인 설명 " * 20),
        ],
    )
    found = sources.first_reply(thread, "신형섭")
    assert found is not None and found.ts == "3"


def test_examples_skip_the_case_thread_and_mention_is_most_common() -> None:
    replies = [
        sources.MyReply("t1", "1", "<@UA> 이 건의 실제 회신"),
        sources.MyReply("t2", "2", "<@UA> 다른 건"),
        sources.MyReply("t3", "3", "<@UB> 또 다른 건"),
    ]
    assert sources.examples(replies, skip_thread="t1") == ["<@UA> 다른 건", "<@UB> 또 다른 건"]
    assert sources.usual_mention(replies) == "UA"


def test_prompt_never_contains_the_actual_reply() -> None:
    actual = sources.Message("3", "2026-11-02 16:23", "신형섭", "<@UA> 정답 회신 문장")
    case = sources.Case(
        cs="CS-0004456",
        item_key="Xcs_1",
        brand="hdex",
        company="DF",
        received_at="2026-11-02 13:36",
        feedback="반품 안내가 다시 나간 사유 확인 부탁드립니다.",
        result=sources.AgentResult(
            run_id="r1",
            agent="agent_4",
            ran_at="2026-11-02 13:38",
            diagnosis="결론: step11이 멘트를 붙였다",
            plan="수정: step10을 둘로 가른다",
            reviews=["diagnosis O"],
            applied="반영됨",
        ),
        other_runs=1,
    )
    inputs = Inputs(case=case, thread=None, examples=["<@UA> 다른 건"], mention="UA", actual=actual)

    prompt = prompt_for(inputs)

    assert "정답 회신 문장" not in prompt
    assert "반영 상태: 반영됨" in prompt
    assert "<@UA>" in prompt
    assert "case:Xcs_1" in prompt
