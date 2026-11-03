from pathlib import Path

from app.ontology import gaps
from app.ontology.builder import (
    Proposal,
    RelationDraft,
    check_proposal,
    concept_id_of,
    proposed_ids,
    run_sql,
)
from app.ontology.concepts import Verb

TRIAL = """# 온톨로지 검증 CS-0004252

**온톨로지에 없던 것**

- 물류센터 입고일 칸
- 반송 송장 매칭 기준

**근거 경로:** `case:Xcs_1`
"""

EMPTY = """# 온톨로지 빈 부분

## 사례가 없는 개념

| 개념 | 묶음 | 차수 | 정본 |
|---|---|---|---|
| 모듈 | 봇 설정 | 2 | ai-admin |

## 사례로 확인되지 않은 관계

- 턴 `sent` 멘트

## 임시 키

| 개념 | 정의서의 키 | 지금 쓴 키 |
|---|---|---|
| 카테고리 | category_id | 봇 폴더 + 카테고리 이름 |
"""

WORK = """# 업무

## 새 개념 후보

| 후보 | 업무 | 정의 |
|---|---|---|
| 약속한 일 | 5 | 고객사에 하겠다고 한 일 |
"""


def test_collect_reads_four_kinds_and_keeps_the_latest_trial(tmp_path: Path) -> None:
    old = tmp_path / "검증" / "6차"
    new = tmp_path / "검증" / "7차"
    old.mkdir(parents=True)
    new.mkdir()
    (old / "2026-11-02_CS-0004252.md").write_text(TRIAL.replace("입고일", "옛날"))
    (new / "2026-11-03_CS-0004252.md").write_text(TRIAL)
    (tmp_path / "빈_부분.md").write_text(EMPTY)
    (tmp_path / "업무.md").write_text(WORK)

    found = gaps.collect(tmp_path)

    assert [(g.source, g.text) for g in found] == [
        ("missing", "물류센터 입고일 칸"),
        ("missing", "반송 송장 매칭 기준"),
        ("empty", "사례가 없는 개념: 모듈 / 봇 설정 / 2 / ai-admin"),
        ("empty", "임시 키: 카테고리 / category_id / 봇 폴더 + 카테고리 이름"),
        ("empty", "사례로 확인되지 않은 관계: 턴 `sent` 멘트"),
        ("work", "약속한 일 / 5 / 고객사에 하겠다고 한 일"),
    ]
    assert found[0].where == "CS-0004252 (7차/2026-11-03_CS-0004252.md)"
    assert gaps.collect(tmp_path)[0].id == found[0].id
    assert gaps.topic("공통 fallback").id.startswith("t")


def proposal(**values: object) -> Proposal:
    base: dict[str, object] = {
        "restated": "",
        "kind": "새 개념",
        "summary": "",
        "concept_file": "---\nid: promise\nname: 약속한 일\n---\n\n# 약속한 일\n",
        "relations": [],
        "new_verbs": [],
        "sources": [],
        "measurements": [],
        "build_change": "",
        "covers": "",
        "work_items": [5],
        "risks": [],
        "open_questions": [],
        "confidence": "중간",
    }
    return Proposal.model_validate(base | values)


def relation(source: str, verb: str, target: str) -> RelationDraft:
    return RelationDraft(source=source, verb=verb, target=target, confidence="추정", basis="")


def test_check_flags_unknown_concepts_verbs_and_pairs() -> None:
    verbs = {"belongs_to": Verb("belongs_to", "", frozenset({("brand", "customer")}))}
    ok = proposal(
        relations=[relation("promise", "made_to", "customer")], new_verbs=["made_to: 약속"]
    )
    assert check_proposal(ok, {"customer"}, verbs) == []

    bad = proposal(
        concept_file="앞머리 없음",
        relations=[relation("brand", "belongs_to", "team"), relation("brand", "owes", "customer")],
    )
    assert check_proposal(bad, {"brand", "customer"}, verbs) == [
        "개념 파일 초안의 앞머리를 읽지 못했다",
        "brand belongs_to team: 없는 개념 team",
        "brand belongs_to team: verbs.yaml에 짝을 더해야 한다",
        "brand owes customer: 사전에 없는 동사인데 new_verbs에 정의가 없다",
    ]
    assert concept_id_of("---\nid: x\n---\n") == "x"


def test_sql_refuses_anything_but_one_read_query() -> None:
    assert "error" in run_sql("2325", "UPDATE wi_ax_item SET feedback = ''")
    assert "error" in run_sql("2325", "SELECT 1; DROP TABLE x")


def test_proposed_ids_reads_file_names(tmp_path: Path) -> None:
    (tmp_path / "2026-11-03_m1a2b3c.md").write_text("")
    (tmp_path / "메모.md").write_text("")
    assert proposed_ids(tmp_path) == {"m1a2b3c"}
