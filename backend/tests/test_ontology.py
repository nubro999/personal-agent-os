import json
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.config import get_settings
from app.ontology.build import build
from app.ontology.concepts import ConceptError, load_concepts, load_verbs
from app.ontology.instances_admin import fallback_steps
from app.ontology.source_admin import fallback_section
from app.ontology.source_chat import (
    fallback_use,
    read_db,
    selected_manuals,
    slim_turn,
    visited_steps,
    with_found_chat_ids,
)

CONCEPT = """---
id: {id}
name: "{name}"
group: "주체"
tier: 1
key: "키"
owner: "정본"
relations: {relations}
---

# {name}

{name}의 정의.
"""
RELATIONS: dict[str, list[tuple[str, str, str, str]]] = {
    "brand": [("customer", "belongs_to", "확정", "맵")],
    "bot_config": [("brand", "configures", "확정", "표")],
    "conversation": [("brand", "belongs_to", "확정", "표")],
    "turn": [
        ("conversation", "belongs_to", "확정", "표"),
        ("category", "selected", "확정", "표"),
        ("info", "loaded", "추정", "표"),
        ("step", "visited", "추정", "표"),
    ],
    "tool_call": [("turn", "belongs_to", "확정", "표"), ("function", "invokes", "확정", "표")],
    "info": [("bot_config", "belongs_to", "확정", "표")],
    "policy": [("brand", "applies_to", "확정", "표"), ("customer", "applies_to", "확정", "표")],
    "category": [("bot_config", "belongs_to", "확정", "표"), ("function", "uses", "확정", "표")],
    "step": [
        ("category", "belongs_to", "확정", "표"),
        ("info", "uses", "추정", "약함"),
        ("step", "next", "확정", "표"),
        ("policy", "implements", "확정", "표"),
        ("ment", "uses", "확정", "표"),
        ("function", "invokes", "추정", "표"),
    ],
    "ment": [("bot_config", "belongs_to", "확정", "표")],
    "response_field": [("function", "belongs_to", "확정", "표")],
    "decision_point": [
        ("category", "belongs_to", "확정", "표"),
        ("step", "located_at", "추정", "표"),
        ("policy", "justified_by", "확정", "표"),
    ],
    "contact": [("customer", "works_for", "확정", "표")],
    "user": [("customer", "serves", "확정", "표")],
    "case": [
        ("brand", "targets", "확정", "표"),
        ("customer", "targets", "확정", "표"),
        ("user", "assigned_to", "확정", "표"),
        ("team", "assigned_to", "확정", "표"),
        ("conversation", "refers_to", "확정", "표"),
        ("turn", "refers_to", "확정", "표"),
    ],
    "run": [("case", "processes", "확정", "표")],
    "agent": [("run", "executes", "확정", "표")],
    "diagnosis": [("run", "produced_by", "확정", "표")],
    "proposal": [("run", "produced_by", "확정", "표"), ("diagnosis", "based_on", "추정", "표")],
    "test_round": [("proposal", "tests", "확정", "표")],
    "knowledge_note": [("run", "produced_by", "확정", "표")],
    "change_item": [
        ("proposal", "belongs_to", "확정", "표"),
        ("category", "modifies", "추정", "이름"),
        ("info", "modifies", "추정", "이름"),
    ],
    "review": [("run", "evaluates", "확정", "표"), ("user", "authored_by", "확정", "이름")],
    "promotion": [("proposal", "deploys", "확정", "표"), ("case", "targets", "확정", "표")],
    "fix_record": [
        ("case", "records", "확정", "표"),
        ("run", "produced_by", "확정", "표"),
        ("user", "authored_by", "추정", "이름"),
    ],
    "period_event": [
        ("brand", "applies_to", "확정", "표"),
        ("bot_config", "modifies", "확정", "표"),
    ],
}
NAMES = {
    "customer": "고객사",
    "brand": "브랜드",
    "bot_config": "봇 구성",
    "policy": "정책",
    "category": "카테고리",
    "function": "조회 함수",
    "step": "step",
    "decision_point": "결정 지점",
    "info": "정보",
    "case": "건",
    "user": "사용자",
    "run": "실행",
    "agent": "에이전트",
    "diagnosis": "진단",
    "proposal": "수정안",
    "test_round": "시험 회차",
    "knowledge_note": "지식 제안",
    "change_item": "변경 항목",
    "review": "판정",
    "promotion": "반영",
    "fix_record": "수정 기록",
    "period_event": "기간 이벤트",
    "contact": "고객사 담당자",
    "ment": "멘트",
    "response_field": "응답 칸",
    "team": "팀",
    "conversation": "대화",
    "turn": "턴",
    "tool_call": "함수 호출",
}


def concept_text(concept_id: str, name: str) -> str:
    lines = "".join(
        f'\n  - to: {to}\n    name: "{rel}"\n    confidence: "{conf}"\n    basis: "{basis}"'
        for to, rel, conf, basis in RELATIONS.get(concept_id, [])
    )
    return CONCEPT.format(id=concept_id, name=name, relations=lines or "[]")


def verbs_yaml() -> str:
    """fixture 관계에서 동사 사전을 만든다."""
    pairs: dict[str, list[str]] = {}
    for source, items in RELATIONS.items():
        for target, verb, _, _ in items:
            pairs.setdefault(verb, []).append(f"    - [{source}, {target}]")
    return "".join(
        f'{verb}:\n  definition: "{verb} 정의"\n  pairs:\n' + "\n".join(items) + "\n"
        for verb, items in pairs.items()
    )


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def ontology(tmp_path: Path) -> tuple[Path, Path]:
    root, context = tmp_path / "onto", tmp_path / "brands"
    (root / "concepts").mkdir(parents=True)
    (root / "verbs.yaml").write_text(verbs_yaml(), encoding="utf-8")
    for concept_id, name in NAMES.items():
        text = concept_text(concept_id, name)
        (root / "concepts" / f"{concept_id}.md").write_text(text, encoding="utf-8")
    write_json(
        root / "source/overmind/index/brand-map.json",
        {
            "brands": [
                {
                    "id": "b_OA",
                    "keys": ["오아주식회사"],
                    "korean": "오아주식회사",
                    "kind": "고객사",
                },
                {"id": "b_oakr", "keys": ["오아", "b_oa"], "kind": "브랜드", "parent_id": "b_OA"},
            ]
        },
    )
    bot = context / "_오아주식회사" / "oa_v2"
    write_json(
        bot / "정책.json",
        {
            "db_name": "oa_v2",
            "brand": "오아 · 오아주식회사",
            "company_id": 240,
            "channel": "chat",
            "policies": [{"id": "P01", "name": "자가조치 1회", "rule": "한 번만 안내"}],
            "glossary": {
                "states": [
                    {
                        "field": "반송수령일자",
                        "value": "날짜",
                        "function": "get_return",
                        "meaning": "택배 배달 완료일",
                        "handling": "입고일로 읽지 않는다",
                    },
                    {"field": "없는칸", "value": "x", "meaning": "y"},
                ]
            },
            "communication": {
                "people": {
                    "client": [{"name": "안정현", "role": "CS팀장"}],
                    "ours": [{"name": "신형섭"}, {"name": "외부인"}],
                }
            },
        },
    )
    write_json(
        bot / "manual" / "A_S문의.json",
        {
            "category": "A/S문의",
            "purpose": "AS 접수",
            "tools": [{"name": "get_order_info"}],
            "decision_points": [
                {"step": 1, "purpose": "유무상 판정", "policy_ids": ["P01", "P99"]}
            ],
            "info_refs": [{"tag": "[AS 기간]", "steps": [1]}],
            "policy_application": [{"policy_id": "P01", "where": [2]}],
        },
    )
    write_json(
        root / "source/overmind/context/b_oakr/chat/2026-10-23/a.json",
        {
            "payload": {
                "doc_type": "category",
                "body": {
                    "category": "A/S문의",
                    "summary": "요약",
                    "manual_meta": {"tools_used": []},
                    "steps": {
                        "1": {
                            "type": "조건별 이동",
                            "role": "유무상을 가른다",
                            "cases": [{"goto": "step2"}, {"goto": "이 step 안에서 안내"}],
                            "information_used": [
                                {"item": "[AS 기간]"},
                                {"item": "[문서 없는 정보]"},
                            ],
                        }
                    },
                },
            }
        },
    )
    write_json(
        root / "source/overmind/context/b_oakr/chat/2026-09-01/old.json",
        {"payload": {"doc_type": "category", "body": {"category": "옛 판"}}},
    )
    return root, context


def test_concept_file_must_point_to_known_concept(tmp_path: Path) -> None:
    (tmp_path / "brand.md").write_text(concept_text("brand", "브랜드"), encoding="utf-8")

    with pytest.raises(ConceptError, match="customer"):
        load_concepts(tmp_path, {})


def test_relation_must_use_a_verb_and_pair_from_the_dictionary(
    ontology: tuple[Path, Path],
) -> None:
    root, _ = ontology
    verbs = load_verbs(root / "verbs.yaml")
    assert load_concepts(root / "concepts", verbs)

    bad_verb = CONCEPT.format(
        id="run",
        name="실행",
        relations='\n  - to: case\n    name: "나왔다"\n    confidence: "확정"\n    basis: "x"',
    )
    (root / "concepts" / "run.md").write_text(bad_verb, encoding="utf-8")
    with pytest.raises(ConceptError, match="사전에 없는 동사"):
        load_concepts(root / "concepts", verbs)

    bad_pair = bad_verb.replace("나왔다", "uses")
    (root / "concepts" / "run.md").write_text(bad_pair, encoding="utf-8")
    with pytest.raises(ConceptError, match="사전에 없는 짝"):
        load_concepts(root / "concepts", verbs)


def test_build_links_sources_into_one_graph(ontology: tuple[Path, Path]) -> None:
    data, gaps = build(*ontology)

    nodes = {n["id"]: n for n in data["nodes"]}
    links = {(x["source"], x["name"], x["target"]) for x in data["links"]}
    assert ("brand:b_oakr", "belongs_to", "customer:b_OA") in links
    assert ("bot_config:240", "configures", "brand:b_oakr") in links
    assert ("policy:oa_v2:P01", "applies_to", "brand:b_oakr") in links
    assert ("category:oa_v2:A/S문의", "belongs_to", "bot_config:240") in links
    step = "step:oa_v2:A/S문의:1"
    assert (step, "next", "step:oa_v2:A/S문의:2") in links
    assert (step, "uses", "info:oa_v2:[AS 기간]") in links
    assert ("step:oa_v2:A/S문의:2", "implements", "policy:oa_v2:P01") in links
    assert ("decision_point:oa_v2:A/S문의:0", "justified_by", "policy:oa_v2:P01") in links
    # S3 판은 같은 봇 구성에 합쳐지고, 최근 판만 읽는다.
    assert data["counts"]["bot_config"] == 1
    assert "category:oa_v2:옛 판" not in nodes
    assert nodes[step]["label"] == "step 1 유무상을 가른다"
    assert "| 건 | 주체 | 1 | 정본 |" in gaps
    assert "정책 id가 정책 문서에 없는 곳 1개" in gaps
    assert "참조된 정보 참조 2개" in gaps
    assert "| step uses 정보 | 추정 | 약함 |" in gaps
    assert "- 건 `targets` 브랜드" in gaps


@pytest.fixture
def built(ontology: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> Path:
    root, context = ontology
    data, gaps = build(root, context)
    write_json(root / "build/instances.json", data)
    (root / "빈_부분.md").write_text(gaps, encoding="utf-8")
    monkeypatch.setattr(get_settings(), "ontology_dir", root)
    return root


async def test_overview_is_concepts_only_with_counts(client: AsyncClient, built: Path) -> None:
    body = (await client.get("/api/ontology")).json()

    counts = {c["id"]: c["count"] for c in body["concepts"]}
    assert (counts["brand"], counts["case"]) == (1, 0)
    pairs = {(r["source"], r["target"]) for r in body["relations"]}
    assert ("brand", "customer") in pairs


async def test_expand_concept_then_neighbors(client: AsyncClient, built: Path) -> None:
    detail = (await client.get("/api/ontology/concepts/brand")).json()
    assert "브랜드의 정의" in detail["body"]

    expansion = (await client.get("/api/ontology/instances", params={"concept": "step"})).json()
    assert expansion["total"] == 2 and expansion["nodes"][0]["degree"] > 0

    around = await client.get("/api/ontology/neighbors", params={"id": "brand:b_oakr"})
    assert {n["concept"] for n in around.json()["nodes"]} >= {"customer", "bot_config", "policy"}


async def test_unknown_ids_and_gaps(client: AsyncClient, built: Path) -> None:
    assert (await client.get("/api/ontology/concepts/nope")).status_code == 404
    assert (await client.get("/api/ontology/neighbors", params={"id": "x"})).status_code == 404
    assert "사례가 없는 개념" in (await client.get("/api/ontology/gaps")).text


def rows_2325() -> dict[str, list[dict[str, object]]]:
    return {
        "users": [{"id": 1, "name": "신형섭"}],
        "cases": [
            {
                "item_key": "Xcs_1",
                "human_id": "CS-0000001",
                "brand_id": "b_oakr",
                "db_name": "oa_v2",
                "company": "오아",
                "status_group": "할 것",
                "feedback": "AS 문의",
            },
            {"item_key": "Xcs_2", "human_id": "CS-0000002", "brand_id": "", "db_name": ""},
            {"item_key": "Xcs_3", "human_id": "CS-0000003", "brand_id": "b_OA", "db_name": ""},
        ],
        "adhoc": [],
        "assignments": [
            {"item_key": "Xcs_1", "name": "신형섭"},
            {"item_key": "Xcs_1", "name": "플랫폼"},
            {"item_key": "Xcs_1", "name": "기타"},
        ],
        "runs": [
            {
                "run_id": "r1",
                "item_key": "Xcs_1",
                "brand": "oa_v2",
                "kind": "이슈수정",
                "agent": "agent_1",
                "ran_at": "2026-11-02T10:00:00",
            },
            {"run_id": "r2", "item_key": "없는건", "brand": "oa_v2", "kind": "", "agent": ""},
        ],
        "diagnoses": [{"run_id": "r1", "body": "멘트 줄바꿈이 벌어졌다\n요약"}],
        "plans": [{"run_id": "r1"}],
        "rounds": [
            {"run_id": "r1", "parsed": [{"round": 1, "verdict": "FAIL", "한줄": "0/3"}]},
        ],
        "notes": [
            {
                "run_id": "r1",
                "parsed": [{"id": "1", "kind": "manual", "about": "A/S문의", "text": "새 사실"}],
            }
        ],
        "changes": [
            {
                "run_id": "r1",
                "parsed": {"company_id": 240, "categories": [{"name": "A/S문의", "id": 3630}]},
            }
        ],
        "snapshots": [
            {"id": 7, "run_id": "r1", "target": "카테고리", "target_ref": "A/S문의", "channel": ""},
            {"id": 8, "run_id": "r1", "target": "정보", "target_ref": "[새 태그]", "channel": ""},
            {"id": 10, "run_id": "r2", "target": "정보", "target_ref": "", "channel": ""},
            {"id": 9, "run_id": "r1", "target": "멘트", "target_ref": "[인사]", "channel": ""},
        ],
        "reviews": [{"run_id": "r1", "axis": "plan", "verdict": "O", "reviewer": "신형섭"}],
        "promotions": [
            {"id": 3, "run_id": "r1", "item_key": "Xcs_1", "action": "approve", "status": "done"}
        ],
        "fixes": [
            {
                "id": 5,
                "item_key": "Xcs_1",
                "date": "2026-11-02",
                "company": "오아",
                "source_run_id": "r1",
                "author": "신형섭",
                "cause": "조건 누락",
            }
        ],
        "events": [{"label": "개천절", "title": "개천절", "kind": "공휴일", "state": "active"}],
        "event_brands": [{"label": "개천절", "brand": "oa_v2", "company_id": 240}],
    }


def test_link_outside_concept_relations_stops_the_build(ontology: tuple[Path, Path]) -> None:
    root, context = ontology
    bare = CONCEPT.format(id="run", name="실행", relations="[]")
    (root / "concepts" / "run.md").write_text(bare, encoding="utf-8")

    with pytest.raises(ValueError, match="개념 파일에 없는"):
        build(root, context, rows_2325())


def test_2325_rows_become_linked_instances(ontology: tuple[Path, Path]) -> None:
    data, gaps = build(*ontology, rows_2325())

    nodes = {n["id"]: n for n in data["nodes"]}
    links = {(x["source"], x["name"], x["target"]) for x in data["links"]}
    assert ("case:Xcs_1", "targets", "brand:b_oakr") in links
    assert ("case:Xcs_1", "assigned_to", "user:1") in links
    assert ("run:r1", "processes", "case:Xcs_1") in links
    assert ("agent:agent_1", "executes", "run:r1") in links
    assert ("diagnosis:r1", "produced_by", "run:r1") in links
    assert ("proposal:r1", "based_on", "diagnosis:r1") in links
    assert ("test_round:r1:1", "tests", "proposal:r1") in links
    assert ("change_item:7", "modifies", "category:oa_v2:A/S문의") in links
    assert ("case:Xcs_3", "targets", "customer:b_OA") in links
    # 매뉴얼 맥락에 없던 정보도 바꾼 기록이 있으면 사례가 된다.
    assert ("change_item:8", "modifies", "info:oa_v2:[새 태그]") in links
    assert ("promotion:3", "deploys", "proposal:r1") in links
    assert ("fix_record:5", "produced_by", "run:r1") in links
    assert ("period_event:개천절", "modifies", "bot_config:240") in links
    # 수정안의 id로 카테고리 진짜 키를 채운다. 없는 사용자와 없는 건은 잇지 않는다.
    assert nodes["category:oa_v2:A/S문의"]["real_key"] == "3630"
    assert nodes["diagnosis:r1"]["label"] == "멘트 줄바꿈이 벌어졌다"
    assert "| 건 → 브랜드, 고객사 | 2 | 3 | 67% |" in gaps
    assert "| 실행 → 건 | 1 | 2 | 50% |" in gaps
    assert "| 담당 → 사용자, 팀 | 2 | 3 | 67% |" in gaps
    assert ("case:Xcs_1", "assigned_to", "team:플랫폼") in links
    assert "team:기타" not in nodes
    assert ("contact:오아주식회사:안정현", "works_for", "customer:b_OA") in links
    assert ("user:1", "serves", "customer:b_OA") in links
    assert "| 우리 쪽 담당 → 사용자 | 1 | 2 | 50% |" in gaps
    assert "| 변경 항목 → 카테고리, 정보 | 2 | 2 | 100% |" in gaps


RAW_TURN = {
    "id": 11,
    "chat_room_id": 5,
    "status": "ai_answered",
    "agent_name": "guide:chat",
    "time": "2026-10-21 16:07:23",
    "question": "윤창호 010 3243 3700 환불해 주세요",
    "ai_answer": "확인해 드릴게요",
    "manual_usage": json.dumps(
        {
            "manuals": [
                {"category": "배송문의", "category_id": 1, "used": False, "p": 0.03},
                {"category": "A/S문의", "category_id": 3630, "used": True, "p": 0.96},
            ]
        }
    ),
    "ai_cots": json.dumps(["윤창호 고객 step1 달성, step1 다시, step3 이동"]),
    "tags": json.dumps([None, "[AS 기간]", "[새 정보]"]),
    "functions": json.dumps(["get_order_info"]),
}


def test_slim_turn_keeps_only_structure() -> None:
    turn = slim_turn("oa_v2", RAW_TURN)

    assert turn["selected"] == [{"category": "A/S문의", "id": 3630, "p": 0.96}]
    assert turn["tags"] == ["[AS 기간]", "[새 정보]"]
    assert turn["functions"] == ["get_order_info"]
    assert turn["steps"] == [1, 3]
    saved = json.dumps(turn, ensure_ascii=False)
    assert "윤창호" not in saved and "010" not in saved and "환불" not in saved


def test_turn_without_judgement_selects_nothing() -> None:
    assert selected_manuals(None) == [] and visited_steps(None) == []


class FakeCursor:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.rows: list[dict[str, object]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        self.calls.append(sql)
        if "FROM chat_room" in sql:
            self.rows = [{"id": 5, "user_chat_id": "chat-1", "platform": "zerotalk"}]
        elif "FROM conversation" in sql:
            self.rows = [RAW_TURN]

    def fetchall(self) -> list[dict[str, object]]:
        return self.rows


class FakeConn:
    def __init__(self) -> None:
        self.cur = FakeCursor()

    def cursor(self, *_: object) -> FakeCursor:
        return self.cur


def test_read_db_finds_problem_turn_in_memory_and_stays_read_only() -> None:
    seen: list[object] = []

    def finder(feedback: str, turns: list[dict[str, object]], room: object) -> dict[str, object]:
        seen.append(turns[0]["question"])
        return {"conv_id": 11, "method": "인용 일치", "score": 1.0}

    conn = FakeConn()
    cases = [{"item_key": "Xcs_1", "chat_id": "chat-1", "feedback": "요청"}]
    out = read_db(conn, "oa_v2", cases, finder)

    assert "READ ONLY" in conn.cur.calls[0]
    assert seen == [RAW_TURN["question"]]
    assert out["problems"] == [
        {
            "item_key": "Xcs_1",
            "db": "oa_v2",
            "room": 5,
            "turn": 11,
            "method": "인용 일치",
            "score": 1.0,
        }
    ]
    assert "윤창호" not in json.dumps(out, ensure_ascii=False)


def chat_rows() -> dict[str, object]:
    return {
        "conversations": [{"db": "oa_v2", "room": 5, "platform": "zerotalk", "handoff": None}],
        "turns": [
            slim_turn("oa_v2", RAW_TURN),
            {"db": "oa_v2", "room": 5, "id": 12, "status": "x", "selected": [], "steps": [1]},
        ],
        "problems": [
            {"item_key": "Xcs_1", "db": "oa_v2", "room": 5, "turn": 11, "method": "인용 일치"},
            {"item_key": "Xcs_2", "db": "oa_v2", "room": 5, "turn": 12, "method": "추정"},
        ],
        "skipped": {"carmore_v2": "읽기 실패 1146"},
    }


def test_chat_rows_prove_what_a_turn_actually_did(ontology: tuple[Path, Path]) -> None:
    data, gaps = build(*ontology, rows_2325(), chat_rows())

    links = {(x["source"], x["name"], x["target"]): x["confidence"] for x in data["links"]}
    turn = "turn:oa_v2:11"
    assert (turn, "belongs_to", "conversation:oa_v2:5") in links
    assert ("conversation:oa_v2:5", "belongs_to", "brand:b_oakr") in links
    assert ("tool_call:oa_v2:11:0", "belongs_to", turn) in links
    assert ("tool_call:oa_v2:11:0", "invokes", "function:oa_v2:get_order_info") in links
    # 같은 카테고리 id로 찾아 이름이 달라도 한 사례로 잇는다.
    assert links[(turn, "selected", "category:oa_v2:A/S문의")] == "확정"
    assert links[(turn, "loaded", "info:oa_v2:[AS 기간]")] == "추정"
    assert links[(turn, "visited", "step:oa_v2:A/S문의:1")] == "추정"
    assert ("case:Xcs_1", "refers_to", "conversation:oa_v2:5") in links
    assert links[("case:Xcs_1", "refers_to", turn)] == "확정"
    assert links[("case:Xcs_2", "refers_to", "turn:oa_v2:12")] == "추정"
    assert "| 턴 → 고른 매뉴얼 판정 있음 | 1 | 2 | 50% |" in gaps
    assert "| 건 → 문제 턴 인용 근거 | 1 | 2 | 50% |" in gaps
    assert "carmore_v2 (읽기 실패 1146)" in gaps


def node(
    kind: str, uuid: str, data: dict[str, object], *kids: dict[str, object]
) -> dict[str, object]:
    return {"type": kind, "uuid": uuid, "data": data, "children": {"": list(kids)}}


def config_tree() -> dict[str, object]:
    step2 = node(
        "step.regular",
        "s2",
        {"uid": "s2", "step": 2},
        node("goal.정보제공", "g", {"pattern": "정보제공", "description": "AS 기간을 안내"}),
        node(
            "action.정보제공",
            "a",
            {"structure": "단순안내"},
            node("ref.info", "r1", {"ref_id": "i1", "identifier": "[AS 기간]"}),
            node("ref.ment_strong", "r2", {"ref_id": "m-alias", "identifier": "[접수 멘트]"}),
            node("function_ref", "f", {"value": "get_order_info"}),
        ),
        node("exception", "e", {"goto": 999, "condition": "고객이 다시 물음"}),
    )
    step1 = node(
        "step.branch",
        "s1",
        {"uid": "s1", "step": 1},
        node(
            "branch_case",
            "b",
            {"goto": 2, "condition": "AS 기간 문의", "$link": {"goto": {"to": "s2"}}},
        ),
    )
    manual = node("channel_manual", "cm", {"channelName": "chat"}, step1, step2)
    category = node(
        "category", "c", {"category": "A/S정책문의", "manualDescription": "AS 정책"}, manual
    )
    return {
        "schema": "config_tree",
        "companyId": 240,
        "root": node(
            "company",
            "root",
            {},
            node("big_category", "big", {"name": "A/S문의"}, category),
            node(
                "info_scope", "is", {}, node("info", "i1", {"tag": "[AS 기간]", "content": "1년"})
            ),
            node(
                "ment_scope",
                "ms",
                {},
                node(
                    "ment",
                    "m1",
                    {"tag": "[접수 멘트]", "content": "접수했어요", "$aliases": ["m1", "m-alias"]},
                ),
            ),
        ),
    }


def test_config_tree_gives_content_real_keys_and_step_links(ontology: tuple[Path, Path]) -> None:
    root, context = ontology
    write_json(root / "source/admin/240_prod.json", config_tree())

    data, gaps = build(root, context)

    nodes = {n["id"]: n for n in data["nodes"]}
    links = {(x["source"], x["name"], x["target"]) for x in data["links"]}
    step1, step2 = "step:oa_v2:A/S문의 - A/S정책문의:1", "step:oa_v2:A/S문의 - A/S정책문의:2"
    assert (step1, "next", step2) in links
    assert (step2, "uses", "info:oa_v2:[AS 기간]") in links
    # 멘트는 별칭 uuid로 참조돼도 같은 멘트로 잇는다.
    assert (step2, "uses", "ment:oa_v2:[접수 멘트]") in links
    assert (step2, "invokes", "function:oa_v2:get_order_info") in links
    assert ("ment:oa_v2:[접수 멘트]", "belongs_to", "bot_config:240") in links
    assert nodes["info:oa_v2:[AS 기간]"]["detail"] == "1년"
    assert nodes["info:oa_v2:[AS 기간]"]["real_key"] == "i1"
    assert nodes[step2]["real_key"] == "s2"
    assert "AS 기간을 안내" in nodes[step2]["detail"] and "→ step999" in nodes[step2]["detail"]
    assert "| 봇 구성 → config_tree | 1 | 1 | 100% |" in gaps


def test_response_fields_carry_code_meaning_and_glossary(ontology: tuple[Path, Path]) -> None:
    root, context = ontology
    write_json(
        root / "source/fields/oa_v2.json",
        {
            "get_return": [
                {
                    "field": "반송수령일자",
                    "type": "str | None",
                    "doc": "반송 배송 완료 시에만 존재",
                    "code": [{"text": "반송수령일자=tracking.delivered_on"}],
                }
            ]
        },
    )

    data, gaps = build(root, context)

    nodes = {n["id"]: n for n in data["nodes"]}
    field = "response_field:oa_v2:get_return:반송수령일자"
    links = {(x["source"], x["name"], x["target"]) for x in data["links"]}
    assert (field, "belongs_to", "function:oa_v2:get_return") in links
    detail = nodes[field]["detail"]
    assert "delivered_on" in detail and "반송 배송 완료 시에만" in detail
    assert "값 날짜: 택배 배달 완료일 / 처리: 입고일로 읽지 않는다" in detail
    assert "| 상태 사전 값 → 응답 칸 | 1 | 2 | 50% |" in gaps


def test_found_chat_ids_fill_only_empty_cases_and_prefer_people() -> None:
    cases = [
        {"item_key": "own", "chat_id": "c0"},
        {"item_key": "human", "chat_id": ""},
        {"item_key": "agent", "chat_id": ""},
        {"item_key": "none", "chat_id": ""},
    ]
    found = [
        {"item_key": "own", "user_chat_id": "x", "found_by": "human"},
        {"item_key": "human", "user_chat_id": "h", "found_by": "human"},
        {"item_key": "human", "user_chat_id": "a", "found_by": "agent"},
        {"item_key": "agent", "user_chat_id": "a1", "found_by": "agent"},
        {"item_key": "agent", "user_chat_id": "a2", "found_by": "agent"},
    ]

    assert with_found_chat_ids(cases, found) == [
        {"item_key": "own", "chat_id": "c0"},
        {"item_key": "human", "chat_id": "h", "found_by": "human"},
        {"item_key": "agent", "chat_id": "a2", "found_by": "agent"},
    ]


FALLBACK_SECTION = """## 그 외 문의 응대 매뉴얼

### Response Manual

step1
Goal:
    Information으로 의문을 해결
Exception:
    case 답하기 어려운 경우:
        goto step999


step999
Goal:
담당 매니저를 연결

### Response Template

[연결 멘트]"""


def cots(choice: str) -> str:
    return json.dumps([json.dumps({"매뉴얼 선택 과정": choice}, ensure_ascii=False)])


def test_fallback_use_reads_only_what_the_bot_said_it_used() -> None:
    sole = "감사 인사입니다. 간단한 마무리는 가능하므로 그 외 문의 응대 매뉴얼을 사용한다."
    both = "2번째 응대 매뉴얼을 사용한다. 답이 없는 부분은 그 외 문의 응대 매뉴얼을 함께 이용한다."
    maybe = "1번째 매뉴얼을 사용한다. 필요 시 그 외 문의 응대 매뉴얼 step999로 이어질 수 있음."
    assert fallback_use(cots(sole)) == "sole"
    assert fallback_use(cots(both)) == "with"
    assert fallback_use(cots(maybe)) == ""
    assert fallback_use(cots("그 외 문의 응대 맥락이 관련됨.")) == ""
    assert fallback_use(None) == "" and fallback_use(json.dumps(["글"])) == ""


def test_fallback_section_stops_at_the_next_header() -> None:
    prompt = f"# 지침\n\n{FALLBACK_SECTION}\n\n## Response Style\n짧게"
    assert fallback_section(prompt) == FALLBACK_SECTION
    assert fallback_section("## 다른 절") is None and fallback_section(None) is None
    steps = fallback_steps(FALLBACK_SECTION)
    assert sorted(steps) == [1, 999]
    assert "goto step999" in steps[1] and "[연결 멘트]" not in steps[999]


def test_fallback_manual_is_a_category_and_cot_links_turns_to_it(
    ontology: tuple[Path, Path],
) -> None:
    root, context = ontology
    write_json(root / "source/admin/240_prod.json", config_tree())
    write_json(root / "source/admin/fallback_manual.json", {"240": FALLBACK_SECTION})
    chat = chat_rows()
    sole = {
        **RAW_TURN,
        "id": 13,
        "ai_cots": cots("마무리이므로 그 외 문의 응대 매뉴얼을 사용한다. step1"),
    }
    both = {
        **RAW_TURN,
        "id": 14,
        "ai_cots": cots("2번째와 그 외 문의 응대 매뉴얼을 함께 이용. step1"),
    }
    chat["turns"] = [slim_turn("oa_v2", sole), slim_turn("oa_v2", both)]  # type: ignore[index]

    data, gaps = build(root, context, rows_2325(), chat)

    nodes = {n["id"]: n for n in data["nodes"]}
    links = {(x["source"], x["name"], x["target"]): x["confidence"] for x in data["links"]}
    base = "category:oa_v2:그 외 문의 응대 매뉴얼"
    step1, step999 = "step:oa_v2:그 외 문의 응대 매뉴얼:1", "step:oa_v2:그 외 문의 응대 매뉴얼:999"
    assert (
        "company_prompts" in nodes[base]["source"]
        and nodes[base]["real_key"] == "240:prod:그 외 문의 응대 매뉴얼"
    )
    assert (base, "belongs_to", "bot_config:240") in links
    assert (step1, "belongs_to", base) in links and (step1, "next", step999) in links
    # 판정기 결과는 두고, CoT가 쓴 기본 매뉴얼을 추정으로 더한다.
    for turn in ("turn:oa_v2:13", "turn:oa_v2:14"):
        assert links[(turn, "selected", "category:oa_v2:A/S문의")] == "확정"
        assert links[(turn, "selected", base)] == "추정"
    # 이것만 썼다고 하면 step 번호는 기본 매뉴얼 것이고, 함께 썼으면 어느 것인지 몰라 잇지 않는다.
    assert links[("turn:oa_v2:13", "visited", step1)] == "추정"
    assert ("turn:oa_v2:13", "visited", "step:oa_v2:A/S문의:1") not in links
    assert not any(k[0] == "turn:oa_v2:14" and k[1] == "visited" for k in links)
    assert "| 기본 매뉴얼만인데 판정기는 다른 매뉴얼 | 1 | 1 | 100% |" in gaps
