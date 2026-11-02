"""온톨로지 검증: 에이전트가 온톨로지 조회 도구만으로 끝난 건을 처리할 수 있는지 본다.

그 건의 실행, 진단, 수정안, 변경 항목, 판정, 반영, 수정 기록은 숨긴다. 다른 건의 기록은
과거 지식이라 보여 준다. 에이전트가 고르는 수정 대상을 실제로 바꾼 대상과 비교한다.

config_tree는 지금 운영 설정이라 그 건을 고친 뒤의 내용이다. 고친 대상은 2325 변경 항목의
고치기 전 본문(before_body)으로 바꿔 보여 주고, 고치며 새로 만든 정보는 숨긴다.

실행: uv run python -m app.ontology.trial case:Xcs_... [case:Xcs_...]
결과: <PAO_ONTOLOGY_DIR>/검증/<날짜>_<건>.md
"""

import asyncio
import json
import logging
import sys
from collections import defaultdict
from datetime import date
from typing import Any

import pymysql
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
from app.ontology.concepts import load_concepts, load_verbs
from app.ontology.live import read_conversation, read_turn
from app.ontology.prompts import TRIAL_SYSTEM
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
# 숨길 정답: 건을 처리한 기록
_WORK = {"run", "diagnosis", "proposal", "test_round", "knowledge_note", "change_item", "review"}


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="고칠 사례의 id (category:..., info:..., step:...)")
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


def _tools(view: OntologyView) -> list[Any]:
    def text(value: Any) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}

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
        "턴 하나의 원문을 채팅 DB에서 읽는다. 고객 질문, 봇 답변, 함수 호출 인자와 반환값",
        {"id": str},
    )
    async def read_turn_tool(args: dict[str, Any]) -> dict[str, Any]:
        return text(await _live(read_turn, args["id"]))

    @tool(
        "read_conversation",
        "대화 하나의 턴들을 순서대로 읽는다. 고객 질문과 봇 답변",
        {"id": str},
    )
    async def read_conversation_tool(args: dict[str, Any]) -> dict[str, Any]:
        return text(await _live(read_conversation, args["id"]))

    return [
        get_node,
        list_neighbors,
        find_nodes,
        describe_concept,
        read_turn_tool,
        read_conversation_tool,
    ]


async def _live(reader: Any, node_id: str) -> dict[str, Any]:
    try:
        result: dict[str, Any] = await asyncio.to_thread(reader, node_id)
    except (ValueError, pymysql.err.Error) as exc:
        return {"error": str(exc)}
    return result


async def run_trial(view: OntologyView, case_id: str) -> tuple[TrialResult | None, list[str], str]:
    server = create_sdk_mcp_server("onto", tools=_tools(view))
    names = [
        "get_node",
        "list_neighbors",
        "find_nodes",
        "describe_concept",
        "read_turn",
        "read_conversation",
    ]
    options = ClaudeAgentOptions(
        model=MODEL,
        system_prompt=TRIAL_SYSTEM,
        tools=[],
        setting_sources=[],
        mcp_servers={"onto": server},
        allowed_tools=[f"mcp__onto__{name}" for name in names],
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


def report(
    case: dict[str, Any],
    data_nodes: dict[str, dict[str, Any]],
    hidden: set[str],
    changed: set[str],
    result: TrialResult | None,
    calls: list[str],
    status: str,
) -> tuple[str, dict[str, Any]]:
    proposed = {t.id for t in result.targets} if result else set()
    hit = proposed & changed
    truth_text = []
    for i in sorted(hidden):
        node = data_nodes[i]
        if node["concept"] in ("diagnosis", "fix_record"):
            detail = (node.get("detail") or "")[:200]
            truth_text.append(f"- {node['concept']} `{i}`: {node['label']} — {detail}")
    score = {
        "proposed": len(proposed),
        "changed": len(changed),
        "hit": len(hit),
        "recall": round(len(hit) / len(changed), 2) if changed else None,
        "precision": round(len(hit) / len(proposed), 2) if proposed else None,
    }
    lines = [
        f"# 온톨로지 검증 {case['label']}",
        "",
        f"- 건: `{case['id']}`",
        f"- 요청: {case.get('detail', '')}",
        f"- 실행: {status}, 도구 호출 {len(calls)}번, 모델 {MODEL}",
        f"- 숨긴 정답 노드 {len(hidden)}개",
        f"- 수정 대상 일치: 실제 {score['changed']}개 중 {score['hit']}개, "
        f"제안 {score['proposed']}개",
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
                f"- `{t.id}` {'(일치)' if t.id in changed else ''}: {t.change}"
                for t in result.targets
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
        *(f"- `{i}` {'(맞힘)' if i in hit else ''}" for i in sorted(changed)),
        "",
        *truth_text,
        "",
        "## 도구 호출",
        "",
        *(f"{i}. {c}" for i, c in enumerate(calls, 1)),
    ]
    return "\n".join(lines) + "\n", score


async def main_async(case_ids: list[str]) -> None:
    settings = get_settings()
    root = settings.ontology_dir
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
    data_nodes = {n["id"]: n for n in data["nodes"]}
    out_dir = root / "검증"
    out_dir.mkdir(exist_ok=True)
    for case_id in case_ids:
        hidden, changed = hidden_for(data, case_id)
        targets = change_targets(data, hidden)
        before, created = before_bodies(sorted(targets), targets)
        hidden |= created
        view = OntologyView(data, concepts, definitions, hidden, before)
        log.info("trial %s: before %d, created %d", case_id, len(before), len(created))
        log.info("trial %s: hidden %d, changed %d", case_id, len(hidden), len(changed))
        result, calls, status = await run_trial(view, case_id)
        text, score = report(
            data_nodes[case_id], data_nodes, hidden, changed, result, calls, status
        )
        name = data_nodes[case_id]["label"].split()[0]
        path = out_dir / f"{date.today().isoformat()}_{name}.md"
        path.write_text(text, encoding="utf-8")
        log.info("trial %s: %s calls=%d score=%s -> %s", case_id, status, len(calls), score, path)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main_async(sys.argv[1:]))


if __name__ == "__main__":
    main()
