from typing import Any

from app.ontology.trial import (
    STEP_HIDDEN,
    OntologyView,
    Target,
    change_targets,
    hidden_for,
    score_targets,
    settle_hidden,
    similarity,
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
