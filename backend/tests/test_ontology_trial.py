from pathlib import Path
from typing import Any

from app.ontology.trial import (
    STEP_HIDDEN,
    OntologyView,
    Outcome,
    Target,
    TrialCase,
    Verdict,
    agent_section,
    change_targets,
    hidden_for,
    judge_prompt,
    load_set,
    name_groups,
    real_runs,
    score_targets,
    settle_hidden,
    similarity,
    summary,
    verdict_block,
    verdict_counts,
    with_verdict,
)


def graph() -> dict[str, Any]:
    nodes = [
        ("case:A", "case"),
        ("case:B", "case"),
        ("run:a", "run"),
        ("diagnosis:a", "diagnosis"),
        ("proposal:a", "proposal"),
        ("change_item:1", "change_item"),
        ("fix_record:1", "fix_record"),
        ("fix_record:2", "fix_record"),
        ("category:x", "category"),
        ("info:new", "info"),
        ("turn:1", "turn"),
    ]
    links = [
        ("run:a", "processes", "case:A"),
        ("diagnosis:a", "produced_by", "run:a"),
        ("proposal:a", "produced_by", "run:a"),
        ("change_item:1", "belongs_to", "proposal:a"),
        ("change_item:1", "modifies", "category:x"),
        ("change_item:1", "modifies", "info:new"),
        ("fix_record:1", "records", "case:A"),
        ("fix_record:2", "records", "case:B"),
        ("case:A", "refers_to", "turn:1"),
        ("turn:1", "selected", "category:x"),
    ]
    return {
        "nodes": [{"id": i, "label": i, "concept": c} for i, c in nodes],
        "links": [{"source": s, "name": n, "target": t, "confidence": "확정"} for s, n, t in links],
    }


def test_hides_only_the_trial_cases_own_work() -> None:
    hidden, changed = hidden_for(graph(), "case:A")

    # info:new는 이 건을 고치며 생겨서 다른 근거가 없다. 보이면 정답이 샌다.
    assert hidden == {
        "run:a",
        "diagnosis:a",
        "proposal:a",
        "change_item:1",
        "fix_record:1",
        "info:new",
    }
    assert changed == {"category:x", "info:new"}


def test_view_acts_as_if_hidden_nodes_do_not_exist() -> None:
    hidden, _ = hidden_for(graph(), "case:A")
    view = OntologyView(graph(), {}, {}, hidden)

    case = view.node("case:A")
    verbs = {(e["direction"], e["verb"]) for e in case["edges"]}
    assert verbs == {("out", "refers_to")}
    assert view.node("run:a") == {"error": "없는 사례: run:a"}
    category = view.node("category:x")
    assert [e["verb"] for e in category["edges"]] == ["selected"]
    # 다른 건의 수정 기록은 과거 지식이라 보인다.
    assert view.find("fix_record", "") == [
        {"id": "fix_record:2", "label": "fix_record:2", "concept": "fix_record"}
    ]
    assert view.neighbors("turn:1", "selected", "out", 0)["total"] == 1


def test_fixed_targets_show_pre_fix_body_and_their_steps_hide_post_fix_text() -> None:
    data = graph()
    data["nodes"].append(
        {"id": "step:x:1", "label": "step 1", "concept": "step", "detail": "고친 뒤"}
    )
    data["links"].append(
        {"source": "step:x:1", "name": "belongs_to", "target": "category:x", "confidence": "확정"}
    )
    hidden, _ = hidden_for(data, "case:A")

    assert change_targets(data, hidden) == {1: "info:new"}
    view = OntologyView(data, {}, {}, hidden, {"category:x": "고치기 전 매뉴얼"})

    assert view.node("category:x")["detail"] == "고치기 전 매뉴얼"
    assert view.node("step:x:1")["detail"] == STEP_HIDDEN


def test_pre_fix_body_wins_over_the_evidence_rule() -> None:
    hidden, _ = hidden_for(graph(), "case:A")
    assert "info:new" in hidden

    # 2325에 고치기 전 본문이 있으면 고치기 전에도 있던 정보다. 보여 준다.
    shown = settle_hidden(hidden, {"info:new": "고치기 전 본문"}, set())
    assert "info:new" not in shown
    assert "change_item:1" in shown

    # 고치기 전이 "없다"였으면 새로 만든 것이라 숨긴다.
    assert "category:x" in settle_hidden(hidden, {}, {"category:x"})


def target(action: str, node_id: str, concept: str = "", name: str = "") -> Target:
    return Target.model_validate(
        {"action": action, "id": node_id, "concept": concept, "name": name, "change": ""}
    )


def test_steps_score_by_category_and_new_items_by_name() -> None:
    categories = {"step:b:반품:5": "category:b:반품", "step:b:교환:5": "category:b:교환"}
    existing = {"category:b:반품", "category:b:교환", "info:b:[날짜 해석]"}
    created = {"info:b:[라이브 시청 방법·혜택]": "[라이브 시청 방법·혜택]"}
    targets = [
        target("바꾸기", "step:b:반품:5"),
        target("바꾸기", "info:b:[날짜 해석]"),
        target("만들기", "category:b:할인", "info", "[라이브 시청 방법과 혜택 안내]"),
        target("만들기", "category:b:할인", "ment", "[라이브 시청 방법·혜택]"),
    ]

    score, marks = score_targets(targets, existing, created, categories)

    assert marks == ["카테고리 일치", "일치", "새로 만든 것과 같음 1개", ""]
    assert (score["id_hit"], score["unit_hit"], score["create_hit"]) == (1, 2, 1)
    assert (score["recall_id"], score["recall_unit"]) == (0.5, 0.75)


def test_similarity_ignores_brackets_and_punctuation() -> None:
    assert similarity("[반송수령일자 해석]", "반송수령일자해석") == 1.0
    assert similarity("[반송수령일자 해석]", "[카카오 라이브]") == 0.0


def test_similar_new_names_count_as_one() -> None:
    created = {
        "info:m:[라이브 시청 방법·구매 혜택]": "[라이브 시청 방법·구매 혜택]",
        "info:m:[라이브 시청 방법·혜택 안내]": "[라이브 시청 방법·혜택 안내]",
        "info:m:[반송수령일자 해석]": "[반송수령일자 해석]",
    }
    assert sorted(len(g) for g in name_groups(created)) == [1, 2]

    score, _ = score_targets(
        [target("만들기", "category:m:할인", "info", "[라이브 시청 방법과 혜택]")],
        set(),
        created,
        {},
    )
    assert (score["created"], score["create_hit"], score["recall_id"]) == (2, 1, 0.5)


def test_set_file_and_summary(tmp_path: Path) -> None:
    path = tmp_path / "set.yaml"
    path.write_text("- case: case:Xcs_1\n  kind: 분류 오류\n- case: case:Xcs_2\n")
    trials = load_set(path)
    assert trials == [TrialCase("case:Xcs_1", "분류 오류"), TrialCase("case:Xcs_2")]

    score = {
        "existing": 2,
        "created": 1,
        "proposed": 3,
        "id_hit": 1,
        "unit_hit": 2,
        "create_hit": 1,
    }
    outcomes = [
        Outcome(trials[0], "CS-1", score, 20, "success", "맞음"),
        Outcome(trials[1], "CS-2", score, 30, "success"),
    ]
    text = summary("9차", outcomes)
    assert "| CS-1 | 분류 오류 | 맞음 | 2 / 3 | 3 / 3 | 1 / 1 | 3 | 20 | success |" in text
    assert "| CS-2 |  | - | 2 / 3 |" in text
    assert "| 분류 오류 | 맞음 1 | 2 / 3 | 3 / 3 |" in text
    assert text.rstrip().endswith("| 전체 | 맞음 1, 판정 없음 1 | 4 / 6 | 6 / 6 |")


def test_verdict_counts_keep_fixed_order() -> None:
    assert verdict_counts(["일부", "맞음", "맞음", ""]) == "맞음 2, 일부 1, 판정 없음 1"
    assert verdict_counts([]) == "-"


def test_judge_sees_the_hidden_diagnosis_and_agent_result_but_not_the_truth_section() -> None:
    data = graph()
    data["nodes"].append({"id": "review:a:diagnosis", "label": "diagnosis O", "concept": "review"})
    hidden, _ = hidden_for(data, "case:A")
    assert real_runs(data, hidden) == {"a": "diagnosis O"}

    report_text = (
        "# 온톨로지 검증 CS-1\n\n- 건: `case:A`\n\n"
        "## 에이전트 결과\n\n**진단:** 설명이 가로챔\n\n"
        "## 실제로 한 일 (숨겼던 정답)\n\n- `category:x` (맞힘)\n"
    )
    agent = agent_section(report_text)
    assert agent == "**진단:** 설명이 가로챔"
    truths = [("a", "실제 진단 a (판정: diagnosis O)", "결론: 설명이 가로챘다")]
    prompt = judge_prompt("요청 원문", truths, agent)
    assert "## 실제 진단 a (판정: diagnosis O)" in prompt
    assert "설명이 가로챔" in prompt and "맞힘" not in prompt


def test_verdict_goes_before_agent_result_and_replaces_old_one() -> None:
    report_text = "# 검증\n\n- 건: `case:A`\n\n## 에이전트 결과\n\n진단\n"
    first = with_verdict(
        report_text, verdict_block(Verdict(verdict="일부", reason="추정", missed=""), ["a"])
    )
    assert first.index("## 진단 판정") < first.index("## 에이전트 결과")
    assert "**일부**" in first and "놓친 것" not in first

    second = with_verdict(
        first, verdict_block(Verdict(verdict="맞음", reason="같다", missed="턴"), ["a"])
    )
    assert second.count("## 진단 판정") == 1
    assert "**맞음**" in second and "**일부**" not in second and "놓친 것: 턴" in second
    assert second.endswith("## 에이전트 결과\n\n진단\n")
