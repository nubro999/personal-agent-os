"""온톨로지 구축 에이전트: 빈 곳 하나를 받아 출처를 읽고 후보 하나를 낸다. 아무것도 바꾸지 않는다.

빈 곳은 gaps.py가 모은다(시험의 missing, 빈_부분.md, 업무.md, 직접 준 주제). 에이전트는
온톨로지 조회, 응대 기록, 회사 함수(읽기만), 2325와 채팅 DB(SELECT만), 개념 파일과 빌드 코드를
읽고 개념, 관계, 키, 정본, 실측을 적은 후보를 낸다. 형섭이 후보를 읽고 concepts/에 넣을지 정한다.

실행:
  uv run python -m app.ontology.builder list
  uv run python -m app.ontology.builder propose <빈 곳 id> [<id> ...]
  uv run python -m app.ontology.builder propose --next 3
  uv run python -m app.ontology.builder propose --topic "공통 fallback 매뉴얼을 카테고리로"
결과: <PAO_ONTOLOGY_DIR>/후보/<날짜>_<빈 곳 id>.md
"""

import argparse
import asyncio
import json
import logging
import re
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
from app.ontology import auto_cs, gaps
from app.ontology.builder_prompts import BUILDER_SYSTEM
from app.ontology.concepts import Verb, load_verbs
from app.ontology.live import _aliases
from app.ontology.trial import OntologyView, load_ontology, ontology_tools
from app.trackers.maintenance import connect

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
MAX_TURNS = 80
ROW_LIMIT = 50
CELL_LIMIT = 300
RESULT_LIMIT = 20000
OUT_DIR = "후보"
READ_ONLY_SQL = re.compile(r"^\s*(select|show|describe|desc|explain)\b", re.IGNORECASE)
# 읽기만 하는 회사 함수. 결과 폴더나 2325에 쓰는 함수(brand.note, diff.*, test.*, logs.stage,
# admin.bundle, slack.attachments)는 넣지 않는다.
AUTO_CS_READ = frozenset(
    {
        "admin.changed",
        "admin.info",
        "admin.manual",
        "admin.ment",
        "api.fields",
        "api.spec",
        "brand.context",
        "brand.policy",
        "chat.answers",
        "chat.conversation",
        "chat.passed",
        "chat.platform",
        "chat.target",
        "chat.turn",
        "maint.case",
        "sheet.find",
        "sheet.read",
        "sheet.row",
    }
)
Kind = Literal["새 개념", "새 관계", "사례 채우기", "개념 고치기", "이미 있음", "온톨로지 밖"]


class RelationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(description="주어 개념 id")
    verb: str = Field(description="verbs.yaml의 동사. 새 동사면 그 이름")
    target: str = Field(description="목적어 개념 id")
    confidence: Literal["확정", "추정", "유사", "모름"]
    basis: str = Field(description="어느 칸, 어느 파일로 잇는지")


class Measurement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    what: str = Field(description="무엇을 셌나")
    value: str = Field(description="센 값. 예: 2,549 / 4,044 (63%)")
    how: str = Field(description="쿼리나 파일 경로, 함수 호출 그대로")


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    restated: str = Field(description="무엇이 없어서 막혔는지 한 문장")
    kind: Kind
    summary: str = Field(description="후보를 한두 문장으로")
    concept_file: str = Field(
        description="새 개념이나 개념 고치기면 개념 파일 전문 초안(---로 감싼 앞머리 포함). "
        "아니면 빈 문자열"
    )
    relations: list[RelationDraft] = Field(description="더하거나 바꿀 관계. 없으면 빈 배열")
    new_verbs: list[str] = Field(description="새 동사가 필요하면 '이름: 정의'. 없으면 빈 배열")
    sources: list[str] = Field(description="정본과 칸. '출처: 위치, 칸' 꼴")
    measurements: list[Measurement] = Field(
        description="실제로 센 것. 세지 않은 수치는 넣지 않는다"
    )
    build_change: str = Field(
        description="빌드 코드(instances*.py, source*.py)에 더할 것. 없으면 빈 문자열"
    )
    covers: str = Field(description="이 후보로 빈 곳이 얼마나 채워지나. 남는 것")
    work_items: list[int] = Field(description="쓰이는 업무 번호(1~10)")
    risks: list[str]
    open_questions: list[str] = Field(description="형섭이 정할 것, 못 찾은 것")
    confidence: str = Field(description="높음, 중간, 낮음 중 하나와 이유")


def run_sql(db: str, sql: str) -> dict[str, Any]:
    """2325(db="2325") 또는 채팅 DB에서 읽기 쿼리 하나. 행은 ROW_LIMIT개, 칸은 CELL_LIMIT자까지."""
    if not READ_ONLY_SQL.match(sql) or ";" in sql.strip().rstrip(";"):
        return {"error": "SELECT, SHOW, DESCRIBE, EXPLAIN 한 문장만 된다"}
    settings = get_settings()
    env = dotenv_values(settings.maint_env_file)
    database = None if db == "2325" else _aliases().get(db, db)
    try:
        conn = connect(env, settings.maint_db_port, database)
    except pymysql.err.Error as exc:
        return {"error": f"접속 실패: {exc}"}
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.execute(sql.strip().rstrip(";"))
            rows = list(cur.fetchmany(ROW_LIMIT + 1))
    except pymysql.err.Error as exc:
        return {"error": str(exc)}
    finally:
        conn.close()
    clipped = [{key: _cell(value) for key, value in row.items()} for row in rows[:ROW_LIMIT]]
    return {"rows": clipped, "more": len(rows) > ROW_LIMIT}


def _cell(value: Any) -> Any:
    if isinstance(value, int | float) or value is None:
        return value
    text = str(value)
    return text if len(text) <= CELL_LIMIT else text[:CELL_LIMIT] + "…"


def run_auto_cs(name: str, args: list[str]) -> dict[str, Any]:
    if name not in AUTO_CS_READ:
        return {"error": f"쓸 수 없는 함수: {name}. 읽기 함수만 된다: {sorted(AUTO_CS_READ)}"}
    result = auto_cs.run(get_settings().auto_cs_dir, name, *args)
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > RESULT_LIMIT:
        return {"clipped": text[:RESULT_LIMIT], "note": f"{len(text)}자 중 앞부분. 인자로 좁혀라"}
    return result


def source_tools() -> list[Any]:
    def text(value: Any) -> dict[str, Any]:
        body = json.dumps(value, ensure_ascii=False, default=str)
        return {"content": [{"type": "text", "text": body}]}

    @tool(
        "sql",
        "읽기 쿼리 하나. db는 '2325'(유지보수 DB) 또는 브랜드 채팅 DB 이름(db_name). "
        "SELECT, SHOW, DESCRIBE만. 행은 50개까지 돌려준다. 셀 때는 COUNT를 써라.",
        {"db": str, "query": str},
    )
    async def sql(args: dict[str, Any]) -> dict[str, Any]:
        return text(await asyncio.to_thread(run_sql, args["db"], args["query"]))

    @tool(
        "auto_cs",
        "회사 함수(tools/run) 하나를 부른다. 읽기 함수만 된다. args는 인자 목록. "
        "쓰임은 args에 ['--help']를 넣어 본다.",
        {"name": str, "args": list},
    )
    async def auto_cs_tool(args: dict[str, Any]) -> dict[str, Any]:
        call_args = [str(a) for a in args.get("args") or []]
        return text(await asyncio.to_thread(run_auto_cs, args["name"], call_args))

    return [sql, auto_cs_tool]


def concept_id_of(draft: str) -> str | None:
    """개념 파일 초안의 id. 앞머리가 없거나 깨졌으면 None."""
    try:
        _, front, _ = draft.split("---\n", 2)
        meta = yaml.safe_load(front)
    except (ValueError, yaml.YAMLError):
        return None
    return str(meta["id"]) if isinstance(meta, dict) and meta.get("id") else None


def check_proposal(proposal: Proposal, concept_ids: set[str], verbs: dict[str, Verb]) -> list[str]:
    """concepts/와 verbs.yaml에 넣기 전에 걸릴 것. 후보 문서 머리에 적는다."""
    problems: list[str] = []
    new_id = concept_id_of(proposal.concept_file) if proposal.concept_file else None
    if proposal.kind in ("새 개념", "개념 고치기") and new_id is None:
        problems.append("개념 파일 초안의 앞머리를 읽지 못했다")
    known = concept_ids | ({new_id} if new_id else set())
    new_verbs = {v.split(":", 1)[0].strip() for v in proposal.new_verbs}
    for r in proposal.relations:
        pair = f"{r.source} {r.verb} {r.target}"
        for end in (r.source, r.target):
            if end not in known:
                problems.append(f"{pair}: 없는 개념 {end}")
        verb = verbs.get(r.verb)
        if verb is None and r.verb not in new_verbs:
            problems.append(f"{pair}: 사전에 없는 동사인데 new_verbs에 정의가 없다")
        elif verb is not None and (r.source, r.target) not in verb.pairs:
            problems.append(f"{pair}: verbs.yaml에 짝을 더해야 한다")
    return problems


def report(
    gap: gaps.Gap, proposal: Proposal | None, problems: list[str], calls: list[str], status: str
) -> str:
    lines = [
        f"# 온톨로지 후보 {gap.id}",
        "",
        "- 상태: 제안 (형섭 확인 전)",
        f"- 빈 곳 ({gap.where}): {gap.text}",
        f"- 실행: {status}, 도구 호출 {len(calls)}번, 모델 {MODEL}",
        "",
    ]
    if proposal is None:
        return "\n".join([*lines, "후보를 받지 못했다.", ""])
    lines += [
        f"## {proposal.kind}: {proposal.summary}",
        "",
        f"**막힌 것:** {proposal.restated}",
        "",
        f"**채우는 정도:** {proposal.covers}",
        "",
        f"**확신:** {proposal.confidence}",
        "",
        f"**업무:** {', '.join(str(n) for n in proposal.work_items) or '없음'}",
        "",
    ]
    if problems:
        lines += ["**넣기 전에 걸리는 것**", "", *(f"- {p}" for p in problems), ""]
    if proposal.concept_file:
        lines += ["## 개념 파일 초안", "", "```markdown", proposal.concept_file.strip(), "```", ""]
    if proposal.relations:
        lines += [
            "## 관계",
            "",
            "| 주어 | 동사 | 목적어 | 신뢰도 | 근거 |",
            "|---|---|---|---|---|",
            *(
                f"| {r.source} | {r.verb} | {r.target} | {r.confidence} | {r.basis} |"
                for r in proposal.relations
            ),
            "",
        ]
    if proposal.new_verbs:
        lines += ["## 새 동사", "", *(f"- {v}" for v in proposal.new_verbs), ""]
    lines += ["## 정본과 칸", "", *(f"- {s}" for s in proposal.sources or ["없음"]), ""]
    if proposal.measurements:
        lines += [
            "## 실측",
            "",
            *(f"- {m.what}: {m.value}\n  - `{m.how}`" for m in proposal.measurements),
            "",
        ]
    if proposal.build_change:
        lines += ["## 빌드에 더할 것", "", proposal.build_change.strip(), ""]
    lines += ["## 위험", "", *(f"- {r}" for r in proposal.risks or ["없음"]), ""]
    lines += ["## 정할 것", "", *(f"- {q}" for q in proposal.open_questions or ["없음"]), ""]
    lines += ["## 도구 호출", "", *(f"{i}. {c}" for i, c in enumerate(calls, 1)), ""]
    return "\n".join(lines)


def proposed_ids(out_dir: Path) -> set[str]:
    """이미 후보를 낸 빈 곳."""
    found = (re.match(r"\d{4}-\d{2}-\d{2}_(\w+)\.md$", p.name) for p in out_dir.glob("*.md"))
    return {m.group(1) for m in found if m}


async def propose(gap: gaps.Gap, view: OntologyView) -> tuple[Proposal | None, list[str], str]:
    settings = get_settings()
    root = settings.ontology_dir
    tools = [*ontology_tools(view, {}), *source_tools()]
    options = ClaudeAgentOptions(
        model=MODEL,
        system_prompt=BUILDER_SYSTEM,
        tools=["Read", "Glob", "Grep"],
        setting_sources=[],
        cwd=str(root),
        add_dirs=[
            str(Path(__file__).parent),
            str(settings.auto_cs_dir / "brands" / "context"),
            str(root.parent / "_review"),
        ],
        mcp_servers={"onto": create_sdk_mcp_server("onto", tools=tools)},
        allowed_tools=["Read", "Glob", "Grep", *(f"mcp__onto__{t.name}" for t in tools)],
        max_turns=MAX_TURNS,
        output_format={"type": "json_schema", "schema": Proposal.model_json_schema()},
        extra_args={"no-session-persistence": None},
    )
    prompt = "\n".join(
        [
            f"빈 곳 ({gap.where}):",
            gap.text,
            "",
            f"_ontology 폴더: {root}",
            f"빌드 코드: {Path(__file__).parent}",
            f"객체 정의서: {root.parent / '_review' / '온톨로지_객체_정의.md'}",
            "",
            "출처를 직접 읽고 세어 본 뒤 후보를 형식에 맞춰 내.",
        ]
    )
    calls: list[str] = []
    result: ResultMessage | None = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    name = block.name.removeprefix("mcp__onto__")
                    calls.append(f"{name} {json.dumps(block.input, ensure_ascii=False)[:300]}")
        elif isinstance(message, ResultMessage):
            result = message
    parsed = (
        Proposal.model_validate(result.structured_output)
        if result and result.structured_output
        else None
    )
    status = (
        f"{result.subtype if result else 'no result'}, turns {result.num_turns if result else '-'}"
    )
    return parsed, calls, status


async def main_async(chosen: list[gaps.Gap]) -> None:
    root = get_settings().ontology_dir
    out_dir = root / OUT_DIR
    out_dir.mkdir(exist_ok=True)
    data, concepts, definitions = load_ontology()
    verbs = load_verbs(root / "verbs.yaml")
    view = OntologyView(data, concepts, definitions, set(), {})
    for gap in chosen:
        proposal, calls, status = await propose(gap, view)
        problems = check_proposal(proposal, set(concepts), verbs) if proposal else []
        path = out_dir / f"{date.today().isoformat()}_{gap.id}.md"
        path.write_text(report(gap, proposal, problems, calls, status), encoding="utf-8")
        kind = proposal.kind if proposal else "-"
        log.info("builder %s: %s %s calls=%d -> %s", gap.id, status, kind, len(calls), path)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="온톨로지 구축 에이전트")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="빈 곳 목록")
    run = sub.add_parser("propose", help="빈 곳마다 후보 하나")
    run.add_argument("ids", nargs="*", help="빈 곳 id")
    run.add_argument("--next", type=int, default=0, help="후보가 없는 빈 곳을 우선순위대로 n개")
    run.add_argument("--topic", default="", help="직접 준 주제")
    args = parser.parse_args()

    root = get_settings().ontology_dir
    found = gaps.collect(root)
    done = proposed_ids(root / OUT_DIR)
    if args.command == "list":
        for gap in found:
            mark = "후보 있음" if gap.id in done else ""
            print(f"{gap.id}\t{gap.source}\t{mark}\t{gap.where}\t{gap.text[:100]}")
        return
    by_id = {g.id: g for g in found}
    unknown = [i for i in args.ids if i not in by_id]
    if unknown:
        parser.error(f"없는 빈 곳 id: {', '.join(unknown)}. list로 본다")
    chosen = [by_id[i] for i in args.ids]
    if args.topic:
        chosen.append(gaps.topic(args.topic))
    if args.next:
        chosen += [g for g in found if g.id not in done and g not in chosen][: args.next]
    if not chosen:
        parser.error("id, --next, --topic 중 하나는 있어야 한다")
    asyncio.run(main_async(chosen))


if __name__ == "__main__":
    main()
