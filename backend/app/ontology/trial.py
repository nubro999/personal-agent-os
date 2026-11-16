"""온톨로지 검증: 에이전트가 온톨로지 조회 도구만으로 끝난 건을 처리할 수 있는지 본다.

그 건의 실행, 진단, 수정안, 변경 항목, 판정, 반영, 수정 기록은 숨긴다. 다른 건의 기록은
과거 지식이라 보여 준다. 에이전트가 낸 고칠 대상(바꾸기, 만들기, 지우기)을 실제로 고친 대상과
비교한다. 있던 것은 id와 카테고리 단위(step은 카테고리로 묶음)로, 새로 만든 것은 이름으로 맞춘다.

config_tree는 지금 운영 설정이라 그 건을 고친 뒤의 내용이다. 고친 대상은 2325 변경 항목의
고치기 전 본문(before_body)으로 바꿔 보여 주고, 고치며 새로 만든 정보는 숨긴다.

턴 원문, 서빙 프롬프트, 함수 로그는 회사 도구로 그때 읽는다. 문제 턴보다 뒤의 턴은 고친 뒤
매뉴얼로 답했을 수 있어 읽지 못하게 한다.

진단은 채점 모델이 숨긴 실제 진단(2325 rnd_output 진단 본문)과 대조해 맞음, 대체로 맞음, 일부,
틀림으로 매긴다.

실행: uv run python -m app.ontology.trial case:Xcs_... [case:Xcs_...]
      uv run python -m app.ontology.trial --set 시험건.yaml --round 9차_시험건 [--parallel 3]
      uv run python -m app.ontology.trial --judge 9차_시험건 [--set 시험건.yaml]
결과: <PAO_ONTOLOGY_DIR>/검증/[<회차>/]<날짜>_<건>.md, 회차를 주면 <회차>/점수.md
--judge는 에이전트를 다시 돌리지 않고 지난 결과 파일만 다시 판정한다.
"""

import argparse
import asyncio
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import pymysql
import yaml
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field

from app.config import get_settings
from app.ontology import live
from app.ontology.concepts import load_concepts, load_verbs
from app.ontology.prompts import JUDGE_SYSTEM, TRIAL_SYSTEM
from app.trackers.maintenance import connect

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
BEFORE_LIMIT = 20000
MAX_TURNS = 60
NEIGHBOR_PAGE = 25
FIND_LIMIT = 20
BEFORE = "2325 변경 항목의 고치기 전 본문"
STEP_HIDDEN = "(이 카테고리는 고쳐졌다. step 본문은 카테고리의 고치기 전 본문에서 본다)"
NOT_EXISTED = "없다."
# 새로 만든 것은 id가 없어 이름으로 맞춘다. 글자 두 개씩 겹치는 비율(Dice)이 이 이상이면 같은 것
SAME_NAME = 0.4
# 채점 모델에 넘기는 실제 진단 본문의 길이. 앞쪽 요약과 문제 턴이 핵심이다.
JUDGE_LIMIT = 8000
JUDGE_TURNS = 3
REQUEST_LIMIT = 4000
VERDICTS = ("맞음", "대체로 맞음", "일부", "틀림")
VERDICT_HEAD = "## 진단 판정"
# 에이전트 진단을 그대로 옮긴 수정 기록의 머리. 사람이 적은 기록이 아니다.
AGENT_FIX = "[에이전트"
AGENT_HEAD = "## 에이전트 결과"
TRUTH_HEAD = "## 실제로 한 일"
CASE_LINE = re.compile(r"^- 건: `(case:[^`]+)`", re.MULTILINE)
# 숨길 정답: 건을 처리한 기록
_WORK = {"run", "diagnosis", "proposal", "test_round", "knowledge_note", "change_item", "review"}


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["바꾸기", "만들기", "지우기"]
    id: str = Field(
        description="바꾸기, 지우기면 그 사례 id. 만들기면 새 것을 쓸 곳(step이나 카테고리 id)"
    )
    concept: str = Field(
        description="만들기면 새 것의 개념(info, ment, step, category). 아니면 빈 문자열"
    )
    name: str = Field(description="만들기면 새 이름. 정보는 [태그]로. 아니면 빈 문자열")
    change: str = Field(description="무엇을 어떻게 바꿀지 한두 문장")


class TrialResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    problem_turn: str | None = Field(description="문제 턴 id")
    what_bot_did: str = Field(description="그 턴에서 봇이 고른 매뉴얼, 정보, step, 함수")
    diagnosis: str = Field(description="왜 잘못 답했는지")
    targets: list[Target] = Field(description="고칠 대상. 온톨로지에 있는 id만")
    evidence_path: list[str] = Field(description="판단에 쓴 사례 id를 순서대로")
    missing: list[str] = Field(description="처리하는 데 필요했지만 온톨로지에 없던 정보")
    confidence: str = Field(description="높음, 중간, 낮음 중 하나와 이유")


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["맞음", "대체로 맞음", "일부", "틀림"]
    reason: str = Field(description="판정 이유 한두 문장. 무엇이 같고 무엇이 다른지")
    missed: str = Field(description="실제 진단에 있는데 에이전트가 놓친 것. 없으면 빈 문자열")


class OntologyView:
    """에이전트가 보는 온톨로지. 숨긴 노드는 없는 것처럼 다룬다."""

    def __init__(
        self,
        data: dict[str, Any],
        concepts: dict[str, dict[str, Any]],
        verbs: dict[str, str],
        hidden: set[str],
        before: dict[str, str] | None = None,
    ) -> None:
        before = before or {}
        self.nodes = {n["id"]: n for n in data["nodes"] if n["id"] not in hidden}
        for node_id, text in before.items():
            if node_id in self.nodes:
                self.nodes[node_id] = self.nodes[node_id] | {"detail": text, "source": BEFORE}
        self.concepts = concepts
        self.verbs = verbs
        self.out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.inn: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for link in data["links"]:
            if link["source"] in self.nodes and link["target"] in self.nodes:
                self.out[link["source"]].append(link)
                self.inn[link["target"]].append(link)
        # 고친 카테고리의 step 본문은 고친 뒤 것이다. 카테고리의 고치기 전 본문만 보게 한다.
        for node_id in before:
            if self.nodes.get(node_id, {}).get("concept") != "category":
                continue
            for link in self.inn[node_id]:
                step = self.nodes.get(link["source"])
                if link["name"] == "belongs_to" and step and step["concept"] == "step":
                    self.nodes[link["source"]] = step | {"detail": STEP_HIDDEN}

    def _brief(self, node_id: str) -> dict[str, str]:
        node = self.nodes[node_id]
        return {"id": node_id, "label": node["label"], "concept": node["concept"]}

    def node(self, node_id: str) -> dict[str, Any]:
        if node_id not in self.nodes:
            return {"error": f"없는 사례: {node_id}"}
        node = self.nodes[node_id]
        groups: dict[tuple[str, str], list[str]] = defaultdict(list)
        for link in self.out[node_id]:
            groups[("out", link["name"])].append(link["target"])
        for link in self.inn[node_id]:
            groups[("in", link["name"])].append(link["source"])
        return {
            "id": node_id,
            "label": node["label"],
            "concept": node["concept"],
            "detail": node.get("detail") or "",
            "source": node.get("source") or "",
            "real_key": node.get("real_key") or "",
            "edges": [
                {
                    "direction": direction,
                    "verb": verb,
                    "count": len(ids),
                    "first": [self._brief(i) for i in ids[:NEIGHBOR_PAGE]],
                }
                for (direction, verb), ids in sorted(groups.items())
            ],
        }

    def neighbors(self, node_id: str, verb: str, direction: str, offset: int) -> dict[str, Any]:
        links = self.out[node_id] if direction == "out" else self.inn[node_id]
        key = "target" if direction == "out" else "source"
        ids = [link[key] for link in links if link["name"] == verb]
        page = ids[offset : offset + NEIGHBOR_PAGE]
        return {"total": len(ids), "offset": offset, "items": [self._brief(i) for i in page]}

    def find(self, text: str, concept: str) -> list[dict[str, str]]:
        needle = text.strip().lower()
        hits = [
            n["id"]
            for n in self.nodes.values()
            if (not concept or n["concept"] == concept) and needle in n["label"].lower()
        ]
        return [self._brief(i) for i in hits[:FIND_LIMIT]]

    def concept(self, concept_id: str) -> dict[str, Any]:
        found = self.concepts.get(concept_id)
        if found is None:
            return {"error": f"없는 개념: {concept_id}"}
        return found | {
            "verbs": {r["name"]: self.verbs.get(r["name"], "") for r in found["relations"]}
        }


def change_targets(data: dict[str, Any], hidden: set[str]) -> dict[int, str]:
    """숨긴 변경 항목의 2325 id -> 그 항목이 바꾼 대상."""
    return {
        int(link["source"].split(":", 1)[1]): link["target"]
        for link in data["links"]
        if link["name"] == "modifies" and link["source"] in hidden
    }


def hidden_for(data: dict[str, Any], case_id: str) -> tuple[set[str], set[str]]:
    """(숨길 노드, 실제로 바꾼 대상). 그 건을 처리한 기록과 그 산출물을 숨긴다."""
    concept = {n["id"]: n["concept"] for n in data["nodes"]}
    inn: dict[str, list[dict[str, Any]]] = defaultdict(list)
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for link in data["links"]:
        inn[link["target"]].append(link)
        out[link["source"]].append(link)
    hidden = {
        link["source"]
        for link in inn[case_id]
        if concept.get(link["source"]) in ("run", "fix_record", "promotion")
    }
    frontier = list(hidden)
    while frontier:
        node = frontier.pop()
        for link in inn[node]:
            source = link["source"]
            if concept.get(source) in _WORK | {"promotion", "fix_record"} and source not in hidden:
                hidden.add(source)
                frontier.append(source)
    changed = {
        link["target"]
        for node in hidden
        if concept.get(node) == "change_item"
        for link in out[node]
        if link["name"] == "modifies"
    }
    # 이 건을 고치며 새로 만든 정보나 카테고리는 숨긴 기록에만 근거가 있다. 고치기 전에는
    # 없던 것이라 보여 주면 정답이 샌다. 소속(belongs_to) 말고 다른 근거가 없으면 숨긴다.
    for target in changed:
        evidence = [
            link
            for link in inn[target] + out[target]
            if link["name"] != "belongs_to"
            and link["source"] not in hidden
            and link["target"] not in hidden
        ]
        if not evidence:
            hidden.add(target)
    return hidden, changed


def settle_hidden(hidden: set[str], before: dict[str, str], created: set[str]) -> set[str]:
    """2325 고치기 전 본문이 근거 규칙보다 앞선다.

    고치기 전 본문이 있으면 고치기 전에도 있던 것이라 보여 준다. 고친 뒤 매뉴얼이 그 정보를
    더는 쓰지 않으면 근거 규칙만으로는 새로 만든 것처럼 보여 숨겨진다. "없다"였으면 숨긴다.
    """
    return (hidden - before.keys()) | created


def turn_cutoff(data: dict[str, Any], case_id: str) -> dict[str, int]:
    """브랜드 db -> 그 건이 가리킨 가장 늦은 턴 번호. 이보다 뒤의 턴은 읽지 못하게 한다."""
    cutoff: dict[str, int] = {}
    for link in data["links"]:
        if link["source"] == case_id and link["target"].startswith("turn:"):
            _, db, number = link["target"].split(":", 2)
            cutoff[db] = max(cutoff.get(db, 0), int(number))
    return cutoff


def after_cutoff(node_id: str, cutoff: dict[str, int]) -> bool:
    kind, db, number = node_id.split(":", 2)
    return kind == "turn" and db in cutoff and number.isdigit() and int(number) > cutoff[db]


def before_bodies(
    snapshot_ids: list[int], changed: dict[int, str]
) -> tuple[dict[str, str], set[str]]:
    """변경 항목 id -> 고치기 전 본문. 같은 대상은 가장 먼저 기록된 것을 쓴다.

    고치기 전이 "없다"인 대상은 그 건을 고치며 새로 만든 것이라 숨길 목록으로 돌려준다.
    """
    if not snapshot_ids:
        return {}, set()
    settings = get_settings()
    conn = connect(dotenv_values(settings.maint_env_file), settings.maint_db_port)
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            marks = ",".join(["%s"] * len(snapshot_ids))
            cur.execute(
                f"SELECT id, before_body FROM rnd_snapshot WHERE id IN ({marks}) ORDER BY id",
                snapshot_ids,
            )
            rows = list(cur.fetchall())
    finally:
        conn.close()
    before: dict[str, str] = {}
    created: set[str] = set()
    for row in rows:
        target = changed.get(row["id"])
        if target is None or target in before or target in created:
            continue
        body = (row.get("before_body") or "").strip()
        if not body or body == NOT_EXISTED:
            created.add(target)
        else:
            before[target] = body[:BEFORE_LIMIT]
    return before, created


def ontology_tools(view: OntologyView, cutoff: dict[str, int]) -> list[Any]:
    """온톨로지 조회 네 개와 그때 읽는 기록 여섯 개. cutoff 뒤의 턴은 읽지 못한다."""

    def text(value: Any) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}

    def conversation_before(node_id: str) -> dict[str, Any]:
        """대화 목록에도 문제 턴 뒤의 턴(사람 상담원의 답, 고친 뒤의 응대)이 있다. 잘라 낸다."""
        result = live.read_conversation(node_id)
        turns = result.get("turns")
        if isinstance(turns, list):
            kept = [t for t in turns if not after_cutoff(str(t.get("turn") or ""), cutoff)]
            result = result | {"turns": kept, "cut_after_problem_turn": len(turns) - len(kept)}
        return result

    async def read(reader: Any, node_id: str, *args: Any) -> dict[str, Any]:
        if after_cutoff(node_id, cutoff):
            return text({"error": "문제 턴보다 뒤의 턴은 이 시험에서 읽을 수 없다"})
        try:
            result = await asyncio.to_thread(reader, node_id, *args)
        except (ValueError, pymysql.err.Error) as exc:
            result = {"error": str(exc)}
        return text(result)

    @tool("get_node", "사례 하나의 내용과 동사별 이웃 요약을 본다", {"id": str})
    async def get_node(args: dict[str, Any]) -> dict[str, Any]:
        return text(view.node(args["id"]))

    @tool(
        "list_neighbors",
        "사례의 이웃을 동사와 방향(out: 이 사례가 주어, in: 이 사례가 대상)으로 더 본다",
        {"id": str, "verb": str, "direction": str, "offset": int},
    )
    async def list_neighbors(args: dict[str, Any]) -> dict[str, Any]:
        return text(
            view.neighbors(args["id"], args["verb"], args["direction"], int(args.get("offset", 0)))
        )

    @tool(
        "find_nodes",
        "이름으로 사례를 찾는다. concept를 주면 그 개념만",
        {"text": str, "concept": str},
    )
    async def find_nodes(args: dict[str, Any]) -> dict[str, Any]:
        return text(view.find(args["text"], args.get("concept", "")))

    @tool("describe_concept", "개념의 정의, 키, 정본, 관계와 동사 뜻", {"concept_id": str})
    async def describe_concept(args: dict[str, Any]) -> dict[str, Any]:
        return text(view.concept(args["concept_id"]))

    @tool(
        "read_turn",
        "턴 하나를 깊게(chat.turn). 질문, 답변, 생각 과정(고른 매뉴얼, step 진행), 실린 정보, "
        "함수 호출 인자와 반환값",
        {"id": str},
    )
    async def read_turn(args: dict[str, Any]) -> dict[str, Any]:
        return await read(live.read_turn, args["id"])

    @tool(
        "read_conversation",
        "대화 한 방의 턴을 순서대로(chat.conversation). 질문, 답변, 고른 매뉴얼 이름, "
        "부른 함수 이름",
        {"id": str},
    )
    async def read_conversation(args: dict[str, Any]) -> dict[str, Any]:
        return await read(conversation_before, args["id"])

    @tool(
        "read_prompt",
        "그 턴에 봇이 실제로 받은 서빙 프롬프트(chat.prompt). section 0이면 목차와 실린 정보 태그, "
        "n이면 n번 섹션 본문(그때의 매뉴얼, Information 등). 길면 offset으로 이어 읽는다",
        {"id": str, "section": int, "offset": int},
    )
    async def read_prompt(args: dict[str, Any]) -> dict[str, Any]:
        section, offset = int(args.get("section", 0)), int(args.get("offset", 0))
        return await read(live.read_prompt, args["id"], section, offset)

    @tool(
        "read_tool_logs",
        "그 턴의 함수가 서버에서 무엇을 조회했나(logs.tool_calls, CloudWatch). 외부 API 조회 범위, "
        "건수, 중간 분기",
        {"id": str},
    )
    async def read_tool_logs(args: dict[str, Any]) -> dict[str, Any]:
        return await read(live.read_tool_logs, args["id"])

    @tool(
        "read_api_spec",
        "함수가 낼 수 있는 응답 코드와 메시지(api.spec, 배포된 openapi). id는 function 사례",
        {"id": str},
    )
    async def read_api_spec(args: dict[str, Any]) -> dict[str, Any]:
        return await read(live.read_api_spec, args["id"])

    @tool(
        "read_function_spec",
        "함수 스펙 문서(genlab-tools). section, pattern이 비면 파일별 절 목록. "
        "section이면 그 절 본문, pattern이면 그 말이 든 줄. file은 spec이나 external-apis. "
        "id는 function 사례",
        {"id": str, "section": str, "pattern": str, "file": str},
    )
    async def read_function_spec(args: dict[str, Any]) -> dict[str, Any]:
        return await read(
            live.read_function_spec,
            args["id"],
            args.get("section", ""),
            args.get("pattern", ""),
            args.get("file", ""),
        )

    return [
        get_node,
        list_neighbors,
        find_nodes,
        describe_concept,
        read_turn,
        read_conversation,
        read_prompt,
        read_tool_logs,
        read_api_spec,
        read_function_spec,
    ]


async def run_trial(
    view: OntologyView, case_id: str, cutoff: dict[str, int]
) -> tuple[TrialResult | None, list[str], str]:
    tools = ontology_tools(view, cutoff)
    server = create_sdk_mcp_server("onto", tools=tools)
    options = ClaudeAgentOptions(
        model=MODEL,
        system_prompt=TRIAL_SYSTEM,
        tools=[],
        setting_sources=[],
        mcp_servers={"onto": server},
        allowed_tools=[f"mcp__onto__{t.name}" for t in tools],
        max_turns=MAX_TURNS,
        output_format={"type": "json_schema", "schema": TrialResult.model_json_schema()},
        extra_args={"no-session-persistence": None},
    )
    calls: list[str] = []
    result: ResultMessage | None = None
    prompt = f"건 {case_id}를 처리해 줘. 도구로 직접 찾아보고 결과를 형식에 맞춰 내."
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    name = block.name.removeprefix("mcp__onto__")
                    calls.append(f"{name} {json.dumps(block.input, ensure_ascii=False)}")
        elif isinstance(message, ResultMessage):
            result = message
    parsed = (
        TrialResult.model_validate(result.structured_output)
        if result and result.structured_output
        else None
    )
    status = (
        f"{result.subtype if result else 'no result'}, turns {result.num_turns if result else '-'}"
    )
    return parsed, calls, status


def step_categories(data: dict[str, Any]) -> dict[str, str]:
    """step id -> 그 step이 속한 카테고리 id."""
    return {
        link["source"]: link["target"]
        for link in data["links"]
        if link["name"] == "belongs_to" and link["source"].startswith("step:")
    }


def unit_of(node_id: str, categories: dict[str, str]) -> str:
    """채점 단위. step은 카테고리로 묶고, 나머지는 그 사례 그대로다."""
    return categories.get(node_id, node_id)


def similarity(a: str, b: str) -> float:
    def pairs(text: str) -> set[str]:
        text = "".join(ch for ch in text if ch.isalnum())
        return {text[i : i + 2] for i in range(len(text) - 1)}

    left, right = pairs(a), pairs(b)
    return 2 * len(left & right) / (len(left) + len(right)) if left and right else 0.0


def name_groups(created: dict[str, str]) -> list[set[str]]:
    """새로 만든 것 중 같은 개념에 이름이 비슷한 것은 하나로 본다.

    에이전트 여럿이 같은 정보를 각자 만들어 정답에 비슷한 이름이 여럿 남는다(CS-0004242).
    하나씩 세면 만들기 하나로 여러 개를 맞힌 것처럼 점수가 부푼다.
    """
    groups: list[set[str]] = []
    for node_id, label in sorted(created.items()):
        concept = node_id.split(":", 1)[0]
        for group in groups:
            if any(
                other.split(":", 1)[0] == concept and similarity(label, created[other]) >= SAME_NAME
                for other in group
            ):
                group.add(node_id)
                break
        else:
            groups.append({node_id})
    return groups


def score_targets(
    targets: list[Target],
    existing: set[str],
    created: dict[str, str],
    categories: dict[str, str],
) -> tuple[dict[str, Any], list[str]]:
    """(점수, 제안마다 붙일 표시).

    existing: 실제로 바꾼, 고치기 전에도 있던 대상. id와 카테고리 단위로 맞춘다.
    created: 실제로 새로 만든 대상 id -> 이름. 같은 개념의 만들기 제안과 이름으로 맞춘다.
    비슷한 이름끼리는 한 묶음으로 세고, 묶음 안 하나라도 맞히면 그 묶음을 맞힌 것이다.
    """
    by_id = {t.id for t in targets if t.action != "만들기"}
    # 만들기도 그것을 쓸 곳은 고치는 것이라 단위에 넣는다
    units = {unit_of(t.id, categories) for t in targets}
    made: dict[str, str] = {}
    marks: list[str] = []
    for t in targets:
        if t.action == "만들기":
            same = [
                truth
                for truth, label in created.items()
                if truth.split(":", 1)[0] == t.concept and similarity(t.name, label) >= SAME_NAME
            ]
            made.update(dict.fromkeys(same, t.name))
            marks.append(f"새로 만든 것과 같음 {len(same)}개" if same else "")
        elif t.id in existing:
            marks.append("일치")
        elif unit_of(t.id, categories) in {unit_of(e, categories) for e in existing}:
            marks.append("카테고리 일치")
        else:
            marks.append("")
    id_hit = existing & by_id
    unit_hit = {e for e in existing if unit_of(e, categories) in units}
    groups = name_groups(created)
    group_hit = [g for g in groups if g & made.keys()]
    total = len(existing) + len(groups)
    score = {
        "existing": len(existing),
        "created": len(groups),
        "proposed": len(targets),
        "proposed_create": sum(t.action == "만들기" for t in targets),
        "id_hit": len(id_hit),
        "unit_hit": len(unit_hit),
        "create_hit": len(group_hit),
        "recall_id": round((len(id_hit) + len(group_hit)) / total, 2) if total else None,
        "recall_unit": round((len(unit_hit) + len(group_hit)) / total, 2) if total else None,
    }
    return score, marks


def report(
    case: dict[str, Any],
    data_nodes: dict[str, dict[str, Any]],
    hidden: set[str],
    changed: set[str],
    categories: dict[str, str],
    result: TrialResult | None,
    calls: list[str],
    status: str,
) -> tuple[str, dict[str, Any]]:
    existing = changed - hidden
    created = {i: data_nodes[i]["label"] for i in changed & hidden}
    targets = result.targets if result else []
    score, marks = score_targets(targets, existing, created, categories)
    proposed_units = {unit_of(t.id, categories) for t in targets}
    truth_text = []
    for i in sorted(hidden):
        node = data_nodes[i]
        if node["concept"] in ("diagnosis", "fix_record"):
            detail = (node.get("detail") or "")[:200]
            truth_text.append(f"- {node['concept']} `{i}`: {node['label']} — {detail}")
    total = score["existing"] + score["created"]
    lines = [
        f"# 온톨로지 검증 {case['label']}",
        "",
        f"- 건: `{case['id']}`",
        f"- 요청: {case.get('detail', '')}",
        f"- 실행: {status}, 도구 호출 {len(calls)}번, 모델 {MODEL}",
        f"- 숨긴 정답 노드 {len(hidden)}개",
        f"- 실제로 고친 것 {total}개(있던 것 {score['existing']}, 새로 만든 것 "
        f"{score['created']}), 제안 {score['proposed']}개(만들기 {score['proposed_create']})",
        f"- id 기준 {score['id_hit'] + score['create_hit']} / {total}, "
        f"카테고리 기준 {score['unit_hit'] + score['create_hit']} / {total} "
        f"(새로 만든 것 {score['create_hit']} / {score['created']})",
        "",
        "## 에이전트 결과",
        "",
    ]
    if result:
        lines += [
            f"**문제 턴:** {result.problem_turn}",
            "",
            f"**봇이 한 일:** {result.what_bot_did}",
            "",
            f"**진단:** {result.diagnosis}",
            "",
            "**고칠 대상**",
            "",
            *(
                f"- {t.action} `{t.id}`"
                + (f" 새 {t.concept} {t.name}" if t.action == "만들기" else "")
                + (f" ({mark})" if mark else "")
                + ("" if t.id in data_nodes else " (없는 id)")
                + f": {t.change}"
                for t, mark in zip(targets, marks, strict=True)
            ),
            "",
            f"**확신:** {result.confidence}",
            "",
            "**온톨로지에 없던 것**",
            "",
            *(f"- {m}" for m in result.missing),
            "",
            "**근거 경로:** " + " → ".join(f"`{i}`" for i in result.evidence_path),
        ]
    else:
        lines.append("결과를 받지 못했다.")
    lines += [
        "",
        "## 실제로 한 일 (숨겼던 정답)",
        "",
        "**바꾼 대상**",
        "",
        *(
            f"- `{i}`"
            + (" (맞힘)" if i in {t.id for t in targets} else "")
            + (
                " (카테고리로 맞힘)"
                if i not in {t.id for t in targets} and unit_of(i, categories) in proposed_units
                else ""
            )
            for i in sorted(existing)
        ),
        "",
        "**새로 만든 것**",
        "",
        *(f"- `{i}` {label}" for i, label in sorted(created.items())),
        *([] if created else ["- 없음"]),
        "",
        *truth_text,
        "",
        "## 도구 호출",
        "",
        *(f"{i}. {c}" for i, c in enumerate(calls, 1)),
    ]
    return "\n".join(lines) + "\n", score


def real_runs(data: dict[str, Any], hidden: set[str]) -> dict[str, str]:
    """숨긴 진단의 실행 id -> 사람 판정(diagnosis O 같은 것, 없으면 빈 문자열)."""
    labels = {n["id"]: n["label"] for n in data["nodes"]}
    return {
        node_id.split(":", 1)[1]: labels.get(f"review:{node_id.split(':', 1)[1]}:diagnosis", "")
        for node_id in hidden
        if labels.get(node_id) is not None and node_id.startswith("diagnosis:")
    }


def hidden_truths(reviews: dict[str, str], fix_ids: list[int]) -> list[tuple[str, str, str]]:
    """(기준 id, 제목, 본문). 숨긴 진단 본문(실행 시각 순)과 사람이 적은 수정 기록.

    빌드에는 글이 240자만 있어 2325에서 다시 읽는다. 에이전트 진단을 옮겨 적은 수정 기록은
    진단과 같으므로 뺀다. 진단이 X인 건은 사람 수정 기록이 정답이다.
    """
    if not reviews and not fix_ids:
        return []
    settings = get_settings()
    conn = connect(dotenv_values(settings.maint_env_file), settings.maint_db_port)
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            runs: list[dict[str, Any]] = []
            if reviews:
                marks = ",".join(["%s"] * len(reviews))
                cur.execute(
                    f"""SELECT o.run_id, LEFT(o.body, {JUDGE_LIMIT}) AS body
                        FROM rnd_output o JOIN rnd_run r ON r.run_id = o.run_id
                        WHERE o.part = 'diagnosis' AND o.run_id IN ({marks})
                        ORDER BY r.ran_at, o.run_id""",
                    sorted(reviews),
                )
                runs = list(cur.fetchall())
            fixes: list[dict[str, Any]] = []
            if fix_ids:
                marks = ",".join(["%s"] * len(fix_ids))
                cur.execute(
                    f"""SELECT id, author, date, LEFT(misresponse_cause, {JUDGE_LIMIT}) AS cause,
                        LEFT(revision_process, {JUDGE_LIMIT}) AS process
                        FROM maintenance_context WHERE id IN ({marks}) ORDER BY date, id""",
                    fix_ids,
                )
                fixes = list(cur.fetchall())
    finally:
        conn.close()
    truths = [
        (
            row["run_id"],
            f"실제 진단 {row['run_id']} (판정: {reviews.get(row['run_id']) or '없음'})",
            row["body"] or "",
        )
        for row in runs
    ]
    for row in fixes:
        cause, process = (row["cause"] or "").strip(), (row["process"] or "").strip()
        if cause.startswith(AGENT_FIX) or not (cause or process):
            continue
        truths.append(
            (
                f"fix_record:{row['id']}",
                f"사람 수정 기록 fix_record:{row['id']} ({row['author'] or '-'}, {row['date']})",
                f"오응대 원인: {cause or '(비어 있음)'}\n수정 과정: {process or '(비어 있음)'}",
            )
        )
    return truths


def agent_section(report_text: str) -> str:
    """결과 파일에서 에이전트 결과 절만. 채점 모델이 숨긴 정답 절을 보지 않게 한다."""
    start = report_text.find(AGENT_HEAD)
    end = report_text.find(TRUTH_HEAD)
    if start < 0:
        return ""
    return report_text[start + len(AGENT_HEAD) : end if end > start else None].strip()


def judge_prompt(request: str, truths: list[tuple[str, str, str]], agent_text: str) -> str:
    """truths: (기준 id, 제목, 본문)."""
    parts = ["## 건 원문", "", request[:REQUEST_LIMIT], ""]
    for _, title, body in truths:
        parts += [f"## {title}", "", body, ""]
    parts += ["## 시험 에이전트 결과", "", agent_text, ""]
    parts.append("시험 에이전트의 진단을 실제 진단과 대조해 판정을 형식에 맞춰 내.")
    return "\n".join(parts)


async def judge(
    request: str, truths: list[tuple[str, str, str]], agent_text: str
) -> Verdict | None:
    if not truths or not agent_text:
        return None
    options = ClaudeAgentOptions(
        model=MODEL,
        system_prompt=JUDGE_SYSTEM,
        tools=[],
        setting_sources=[],
        max_turns=JUDGE_TURNS,
        output_format={"type": "json_schema", "schema": Verdict.model_json_schema()},
        extra_args={"no-session-persistence": None},
    )
    result: ResultMessage | None = None
    async for message in query(prompt=judge_prompt(request, truths, agent_text), options=options):
        if isinstance(message, ResultMessage):
            result = message
    if not result or not result.structured_output:
        return None
    return Verdict.model_validate(result.structured_output)


async def judge_case(
    data: dict[str, Any], case_id: str, report_text: str
) -> tuple[Verdict | None, list[str]]:
    """(판정, 기준으로 쓴 진단과 수정 기록 id). 숨긴 진단도 수정 기록도 없으면 판정하지 않는다."""
    hidden, _ = hidden_for(data, case_id)
    reviews = real_runs(data, hidden)
    fix_ids = sorted(int(n.split(":", 1)[1]) for n in hidden if n.startswith("fix_record:"))
    truths = await asyncio.to_thread(hidden_truths, reviews, fix_ids)
    case = next(n for n in data["nodes"] if n["id"] == case_id)
    verdict = await judge(case.get("detail") or "", truths, agent_section(report_text))
    return verdict, [key for key, _, _ in truths]


def verdict_block(verdict: Verdict | None, runs: list[str]) -> str:
    if verdict is None:
        return f"{VERDICT_HEAD}\n\n판정하지 못했다(숨긴 진단이나 에이전트 결과가 없다).\n\n"
    lines = [
        VERDICT_HEAD,
        "",
        f"**{verdict.verdict}** ({MODEL}, 기준 " + ", ".join(f"`{r}`" for r in runs) + ")",
        "",
        verdict.reason,
    ]
    if verdict.missed:
        lines += ["", f"놓친 것: {verdict.missed}"]
    return "\n".join(lines) + "\n\n"


def with_verdict(report_text: str, block: str) -> str:
    """에이전트 결과 절 앞에 판정 절을 넣는다. 이미 있으면 바꾼다."""
    start = report_text.find(VERDICT_HEAD)
    if start >= 0:
        end = report_text.find("\n## ", start + len(VERDICT_HEAD))
        report_text = report_text[:start] + report_text[end + 1 :]
    at = report_text.find(AGENT_HEAD)
    return report_text + "\n" + block if at < 0 else report_text[:at] + block + report_text[at:]


def load_ontology() -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, str]]:
    """(사례 그래프, 개념, 동사 뜻). 에이전트에게 보여 줄 온톨로지 재료."""
    root = get_settings().ontology_dir
    data = json.loads((root / "build" / "instances.json").read_text(encoding="utf-8"))
    verbs = load_verbs(root / "verbs.yaml")
    concepts = {
        c.id: {
            "id": c.id,
            "name": c.name,
            "group": c.group,
            "key": c.key,
            "owner": c.owner,
            "definition": c.body,
            "relations": [{"to": r.target, "name": r.name} for r in c.relations],
        }
        for c in load_concepts(root / "concepts", verbs)
    }
    definitions = {name: verb.definition for name, verb in verbs.items()}
    return data, concepts, definitions


def hide_case_work(data: dict[str, Any], case_id: str) -> tuple[set[str], dict[str, str]]:
    """(숨길 노드, 고치기 전 본문). 그 건을 처리하기 전의 온톨로지로 되돌린다."""
    hidden, _ = hidden_for(data, case_id)
    targets = change_targets(data, hidden)
    before, created = before_bodies(sorted(targets), targets)
    return settle_hidden(hidden, before, created), before


@dataclass(frozen=True)
class TrialCase:
    case: str  # case:Xcs_...
    kind: str = ""  # 업무 성격: 분류 오류, 내용 오류, 함수 해석 오류, step 흐름 오류, 멘트 오류
    note: str = ""


@dataclass
class Outcome:
    trial: TrialCase
    name: str
    score: dict[str, Any]
    calls: int
    status: str
    verdict: str = ""


def load_set(path: Path) -> list[TrialCase]:
    """시험 건 목록. 항목마다 case, kind, note."""
    rows = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [TrialCase(r["case"], r.get("kind", ""), r.get("note", "")) for r in rows]


def _ratio(hit: int, total: int) -> str:
    return f"{hit} / {total}" if total else "-"


def verdict_counts(verdicts: list[str]) -> str:
    """맞음 2, 일부 1 처럼. 판정 못 한 것은 판정 없음으로 센다."""
    counts = {v: verdicts.count(v) for v in (*VERDICTS, "") if verdicts.count(v)}
    return ", ".join(f"{v or '판정 없음'} {n}" for v, n in counts.items()) or "-"


def summary(round_name: str, outcomes: list[Outcome]) -> str:
    """회차 점수표. 건마다 한 줄, 업무 성격마다 합계."""
    lines = [
        f"# {round_name} 점수",
        "",
        "진단은 채점 모델이 숨긴 실제 진단과 대조해 매긴다. 이유는 결과 파일의 진단 판정 절에 있다.",  # noqa: E501
        "id 기준은 바꾼 대상의 id와 새로 만든 묶음, 카테고리 기준은 step을 카테고리로 묶어 센다.",
        "",
        "| 건 | 성격 | 진단 | id 기준 | 카테고리 기준 | 새로 만든 것 | 제안 | 도구 호출 | 실행 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    verdicts: dict[str, list[str]] = defaultdict(list)
    for o in outcomes:
        s = o.score
        total = s["existing"] + s["created"]
        id_hit = s["id_hit"] + s["create_hit"]
        unit_hit = s["unit_hit"] + s["create_hit"]
        lines.append(
            f"| {o.name} | {o.trial.kind} | {o.verdict or '-'} | {_ratio(id_hit, total)} "
            f"| {_ratio(unit_hit, total)} "
            f"| {_ratio(s['create_hit'], s['created'])} | {s['proposed']} | {o.calls} "
            f"| {o.status} |"
        )
        for key in (o.trial.kind or "-", "전체"):
            totals[key][0] += id_hit
            totals[key][1] += unit_hit
            totals[key][2] += total
            verdicts[key].append(o.verdict)
    lines += ["", "| 성격 | 진단 | id 기준 | 카테고리 기준 |", "|---|---|---|---|"]
    # 성격별을 먼저, 전체를 맨 끝에 둔다
    order = sorted(totals, key=lambda k: k == "전체")
    for key in order:
        id_hit, unit_hit, total = totals[key]
        lines.append(
            f"| {key} | {verdict_counts(verdicts[key])} | {_ratio(id_hit, total)} "
            f"| {_ratio(unit_hit, total)} |"
        )
    return "\n".join(lines) + "\n"


async def run_case(
    trial: TrialCase,
    data: dict[str, Any],
    concepts: dict[str, dict[str, Any]],
    definitions: dict[str, str],
    categories: dict[str, str],
    out_dir: Path,
) -> Outcome:
    data_nodes = {n["id"]: n for n in data["nodes"]}
    case_id = trial.case
    _, changed = hidden_for(data, case_id)
    hidden, before = await asyncio.to_thread(hide_case_work, data, case_id)
    view = OntologyView(data, concepts, definitions, hidden, before)
    log.info(
        "trial %s: hidden %d, before %d, changed %d",
        case_id,
        len(hidden),
        len(before),
        len(changed),
    )
    result, calls, status = await run_trial(view, case_id, turn_cutoff(data, case_id))
    text, score = report(
        data_nodes[case_id], data_nodes, hidden, changed, categories, result, calls, status
    )
    verdict, runs = await judge_case(data, case_id, text)
    text = with_verdict(text, verdict_block(verdict, runs))
    name = data_nodes[case_id]["label"].split()[0]
    path = out_dir / f"{date.today().isoformat()}_{name}.md"
    path.write_text(text, encoding="utf-8")
    log.info("trial %s: %s calls=%d score=%s -> %s", case_id, status, len(calls), score, path)
    return Outcome(trial, name, score, len(calls), status, verdict.verdict if verdict else "")


async def main_async(trials: list[TrialCase], round_name: str = "", parallel: int = 1) -> None:
    root = get_settings().ontology_dir
    data, concepts, definitions = load_ontology()
    categories = step_categories(data)
    out_dir = root / "검증" / round_name if round_name else root / "검증"
    out_dir.mkdir(parents=True, exist_ok=True)
    gate = asyncio.Semaphore(parallel)

    async def one(trial: TrialCase) -> Outcome | None:
        async with gate:
            try:
                return await run_case(trial, data, concepts, definitions, categories, out_dir)
            except Exception as exc:  # 한 건이 실패해도 나머지 건은 돈다
                log.warning("trial %s failed: %s", trial.case, exc)
                return None

    outcomes = [o for o in await asyncio.gather(*map(one, trials)) if o]
    if round_name:
        (out_dir / "점수.md").write_text(summary(round_name, outcomes), encoding="utf-8")


async def judge_round(round_name: str, kinds: dict[str, str], parallel: int = 3) -> None:
    """지난 회차의 결과 파일을 다시 판정한다. 에이전트는 다시 돌리지 않는다.

    판정 절을 결과 파일에 넣고, 점수.md의 진단 자동 판정 절을 바꾼다.
    """
    out_dir = get_settings().ontology_dir / "검증" / round_name
    data, _, _ = load_ontology()
    gate = asyncio.Semaphore(parallel)

    async def one(name: str, text: str) -> tuple[str, str, str, Verdict | None] | None:
        found = CASE_LINE.search(text)
        if not found:
            return None
        async with gate:
            try:
                verdict, runs = await judge_case(data, found.group(1), text)
            except Exception as exc:  # 한 건이 실패해도 나머지 건은 돈다
                log.warning("judge %s failed: %s", name, exc)
                return None
        log.info("judge %s: %s", name, verdict.verdict if verdict else "-")
        block = verdict_block(verdict, runs)
        return name, kinds.get(found.group(1), ""), with_verdict(text, block), verdict

    paths = sorted(p for p in out_dir.glob("*.md") if p.name != "점수.md")
    texts = {p: p.read_text(encoding="utf-8") for p in paths}
    judged = await asyncio.gather(*(one(p.stem.split("_", 1)[-1], texts[p]) for p in paths))
    rows = []
    for path, row in zip(paths, judged, strict=True):
        if row:
            path.write_text(row[2], encoding="utf-8")
            rows.append((row[0], row[1], row[3]))
    lines = [
        "## 진단 자동 판정",
        "",
        f"채점 모델({MODEL})이 숨긴 실제 진단과 대조했다. {date.today().isoformat()}",
        "",
        "| 건 | 성격 | 진단 | 이유 |",
        "|---|---|---|---|",
        *(
            f"| {name} | {kind} | {v.verdict if v else '-'} | "
            + (v.reason.replace("|", "/").replace("\n", " ") if v else "")
            + " |"
            for name, kind, v in rows
        ),
        "",
        verdict_counts([v.verdict if v else "" for _, _, v in rows]) + ".",
    ]
    score_path = out_dir / "점수.md"
    old = score_path.read_text(encoding="utf-8") if score_path.exists() else ""
    cut = old.find("\n## 진단 자동 판정")
    kept = old if cut < 0 else old[:cut]
    score_path.write_text(kept.rstrip("\n") + "\n\n" + "\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="온톨로지 검증")
    parser.add_argument("cases", nargs="*", help="case:Xcs_...")
    parser.add_argument("--set", type=Path, help="시험 건 목록(yaml). 상대 경로는 검증/ 기준")
    parser.add_argument("--round", default="", help="회차 폴더 이름. 주면 점수표도 쓴다")
    parser.add_argument("--parallel", type=int, default=1, help="동시에 돌릴 건 수")
    parser.add_argument("--judge", default="", help="이 회차의 결과 파일만 다시 판정한다")
    args = parser.parse_args()
    trials = [TrialCase(c) for c in args.cases]
    if args.set:
        path = (
            args.set if args.set.is_absolute() else get_settings().ontology_dir / "검증" / args.set
        )
        trials += load_set(path)
    if args.judge:
        kinds = {t.case: t.kind for t in trials}
        asyncio.run(judge_round(args.judge, kinds, max(args.parallel, 3)))
        return
    if not trials:
        parser.error("건이나 --set이 있어야 한다")
    asyncio.run(main_async(trials, args.round, args.parallel))


if __name__ == "__main__":
    main()
