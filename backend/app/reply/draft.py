"""에이전트가 처리한 건으로 고객사 회신 초안을 쓴다. 슬랙에 보내지 않는다.

재료는 sources.py가 모은다. 에이전트에게는 온톨로지 조회와 응대 당시 기록 도구(시험 에이전트와
같은 것)를 주고, 에이전트 결과만으로 쓸 수 없을 때만 쓰게 한다.

시험(--eval): 내가 이미 회신한 건으로 돌린다. 내 첫 회신 전의 재료만 쓰고(스레드, 실행, 판정, 반영),
같은 스레드의 내 글은 말투 예시에서 빼고, 온톨로지는 그 건을 처리하기 전으로 되돌린다.
초안과 실제 회신을 나란히 적는다.

실행: uv run python -m app.reply.draft [--eval] CS-0004456 [CS-...]
결과: <PAO_ONTOLOGY_DIR>/회신/<날짜>_<건>.md
"""

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
)
from pydantic import BaseModel, ConfigDict, Field

from app.config import get_settings
from app.ontology.trial import (
    OntologyView,
    hide_case_work,
    load_ontology,
    ontology_tools,
    turn_cutoff,
)
from app.reply import sources
from app.reply.prompts import REPLY_SYSTEM

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
MAX_TURNS = 25
MY_NAME = "신형섭"


class ReplyDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft: str = Field(description="슬랙에 남길 회신. 첫머리에 멘션 <@U...> 하나")
    answers: list[str] = Field(description="요청 원문의 질문마다 회신이 어떻게 답했는지 한 줄씩")
    check: list[str] = Field(description="보내기 전에 형섭이 확인할 것. 없으면 빈 배열")


@dataclass
class Inputs:
    case: sources.Case
    thread: sources.Thread | None
    examples: list[str]
    mention: str
    actual: sources.Message | None  # 시험일 때 내가 실제로 남긴 회신


def gather(cs: str, evaluate: bool, replies: list[sources.MyReply]) -> Inputs:
    thread = sources.load_thread(cs)
    actual = sources.first_reply(thread, MY_NAME) if thread and evaluate else None
    if evaluate and actual is None:
        raise ValueError(f"{cs}: 시험할 내 회신이 스레드에 없다")
    until = datetime.fromtimestamp(float(actual.ts), sources.KST) if actual else None
    if until:
        thread = sources.load_thread(cs, before=until)
    case = sources.load_case(cs, until)
    skip = thread.messages[0].ts if thread and thread.messages else ""
    return Inputs(
        case=case,
        thread=thread,
        examples=sources.examples(replies, skip_thread=skip),
        # 카드는 봇이 세운다. 회신은 내가 늘 부르던 고객사 소통 담당자에게 한다.
        mention=sources.usual_mention(replies) or "",
        actual=actual,
    )


def prompt_for(inputs: Inputs) -> str:
    case = inputs.case
    lines = [
        f"건 {case.cs} (온톨로지 id case:{case.item_key}), 브랜드 {case.brand}, "
        f"고객사 {case.company}, 접수 {case.received_at}",
        "",
        "## 요청 원문",
        case.feedback,
        "",
        "## 에이전트 결과",
    ]
    result = case.result
    if result:
        lines += [
            f"실행 {result.run_id} ({result.agent}, {result.ran_at})",
            f"판정: {', '.join(result.reviews) or '판정 없음'}",
            f"반영 상태: {result.applied}",
            f"같은 건의 다른 실행 {case.other_runs}개",
            "",
            "### 진단 요약",
            result.diagnosis,
            "",
            "### 수정 방향",
            result.plan,
        ]
    else:
        lines.append("없음. 에이전트가 진단한 실행이 없다.")
    lines += ["", "## 카드 스레드"]
    if inputs.thread:
        lines.append(inputs.thread.link)
        for m in inputs.thread.messages:
            files = f" (첨부: {', '.join(m.files)})" if m.files else ""
            lines.append(f"- {m.when} {m.user}: {m.text}{files}")
    else:
        lines.append("찾지 못했다.")
    lines += [
        "",
        f"## 멘션할 사람: <@{inputs.mention}>",
        "",
        "## 형섭의 지난 회신 (최근 것부터)",
        *(f"---\n{example}" for example in inputs.examples),
        "",
        "회신 초안을 형식에 맞춰 내.",
    ]
    return "\n".join(lines)


async def write_draft(
    inputs: Inputs, view: OntologyView, cutoff: dict[str, int]
) -> tuple[ReplyDraft | None, list[str], str]:
    tools = ontology_tools(view, cutoff)
    options = ClaudeAgentOptions(
        model=MODEL,
        system_prompt=REPLY_SYSTEM,
        tools=[],
        setting_sources=[],
        mcp_servers={"onto": create_sdk_mcp_server("onto", tools=tools)},
        allowed_tools=[f"mcp__onto__{t.name}" for t in tools],
        max_turns=MAX_TURNS,
        output_format={"type": "json_schema", "schema": ReplyDraft.model_json_schema()},
        extra_args={"no-session-persistence": None},
    )
    calls: list[str] = []
    result: ResultMessage | None = None
    async for message in query(prompt=prompt_for(inputs), options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    name = block.name.removeprefix("mcp__onto__")
                    calls.append(f"{name} {json.dumps(block.input, ensure_ascii=False)}")
        elif isinstance(message, ResultMessage):
            result = message
    parsed = (
        ReplyDraft.model_validate(result.structured_output)
        if result and result.structured_output
        else None
    )
    status = (
        f"{result.subtype if result else 'no result'}, turns {result.num_turns if result else '-'}"
    )
    return parsed, calls, status


def report(inputs: Inputs, draft: ReplyDraft | None, calls: list[str], status: str) -> str:
    case = inputs.case
    result = case.result
    lines = [
        f"# 회신 초안 {case.cs} {case.brand}",
        "",
        f"- 스레드: {inputs.thread.link if inputs.thread else '없음'}",
        f"- 에이전트 결과: {result.run_id if result else '없음'}"
        + (f", 판정 {', '.join(result.reviews) or '없음'}, {result.applied}" if result else ""),
        f"- 실행: {status}, 도구 호출 {len(calls)}번, 모델 {MODEL}",
        "",
        "## 초안",
        "",
    ]
    if draft:
        lines += [
            "```",
            draft.draft,
            "```",
            "",
            "**질문별 답**",
            "",
            *(f"- {a}" for a in draft.answers),
            "",
            "**보내기 전에 확인할 것**",
            "",
            *(f"- {c}" for c in draft.check or ["없음"]),
        ]
    else:
        lines.append("초안을 받지 못했다.")
    if inputs.actual:
        lines += [
            "",
            f"## 실제 회신 ({inputs.actual.when})",
            "",
            "```",
            inputs.actual.text,
            "```",
        ]
    lines += ["", "## 요청 원문", "", "```", case.feedback, "```"]
    if calls:
        lines += ["", "## 도구 호출", "", *(f"{i}. {c}" for i, c in enumerate(calls, 1))]
    return "\n".join(lines) + "\n"


async def main_async(cases: list[str], evaluate: bool) -> None:
    out_dir = get_settings().ontology_dir / "회신"
    out_dir.mkdir(exist_ok=True)
    data, concepts, definitions = load_ontology()
    replies = await asyncio.to_thread(sources.load_my_replies)
    for cs in cases:
        try:
            inputs = await asyncio.to_thread(gather, cs, evaluate, replies)
        except ValueError as exc:
            log.warning("reply %s: %s", cs, exc)
            continue
        case_id = f"case:{inputs.case.item_key}"
        hidden, before = (
            await asyncio.to_thread(hide_case_work, data, case_id) if evaluate else (set(), {})
        )
        view = OntologyView(data, concepts, definitions, hidden, before)
        cutoff = turn_cutoff(data, case_id) if evaluate else {}
        draft, calls, status = await write_draft(inputs, view, cutoff)
        path = out_dir / f"{date.today().isoformat()}_{cs}.md"
        path.write_text(report(inputs, draft, calls, status), encoding="utf-8")
        log.info("reply %s: %s calls=%d -> %s", cs, status, len(calls), path)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="고객사 회신 초안")
    parser.add_argument("cases", nargs="+", help="CS 번호")
    parser.add_argument("--eval", action="store_true", help="이미 회신한 건으로 시험")
    args = parser.parse_args()
    asyncio.run(main_async(args.cases, args.eval))


if __name__ == "__main__":
    main()
