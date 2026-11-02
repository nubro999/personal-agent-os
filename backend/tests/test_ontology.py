import json
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.config import get_settings
from app.ontology.build import build
from app.ontology.concepts import ConceptError, load_concepts, load_verbs

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
    "info": [("bot_config", "belongs_to", "확정", "표")],
    "policy": [("brand", "applies_to", "확정", "표"), ("customer", "applies_to", "확정", "표")],
    "category": [("bot_config", "belongs_to", "확정", "표"), ("function", "uses", "확정", "표")],
    "step": [
        ("category", "belongs_to", "확정", "표"),
        ("info", "uses", "추정", "약함"),
        ("step", "next", "확정", "표"),
        ("policy", "implements", "확정", "표"),
    ],
    "decision_point": [
        ("category", "belongs_to", "확정", "표"),
        ("step", "located_at", "추정", "표"),
        ("policy", "justified_by", "확정", "표"),
    ],
    "case": [
        ("brand", "targets", "확정", "표"),
        ("customer", "targets", "확정", "표"),
        ("user", "assigned_to", "확정", "표"),
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
    assert "| 담당 → 사용자 | 1 | 2 | 50% |" in gaps
    assert "| 변경 항목 → 카테고리, 정보 | 2 | 2 | 100% |" in gaps
