"""건 번호로 그 건의 대화를 찾는다. 온톨로지 빌드 뒤에 들어온 건도 읽을 수 있다."""

import asyncio
import json
import re
from typing import Any

from claude_agent_sdk import tool

from app.config import get_settings
from app.ontology import auto_cs

TEXT_LIMIT = 1500
TURN_LIMIT = 60
# 제로톡 대화 링크: talk.zeroworks.ai/w/<워크스페이스>/conversations/<user_chat_id>
ZEROTALK = re.compile(r"talk\.zeroworks\.ai/w/[0-9a-f-]+/conversations/([0-9a-f-]{36})")


def chat_id_of(case: dict[str, Any]) -> str | None:
    """2325 건의 대화 id. 비어 있으면 요청 원문의 제로톡 링크에서 꺼낸다."""
    if case.get("chat_id"):
        return str(case["chat_id"])
    found = ZEROTALK.search(case.get("issue") or "")
    return found.group(1) if found else None


def read_case_chat(cs: str) -> dict[str, Any]:
    """maint.case로 브랜드와 대화 id를 받고 chat.conversation으로 턴을 읽는다."""
    auto_cs_dir = get_settings().auto_cs_dir
    case = auto_cs.run(auto_cs_dir, "maint.case", cs)
    if "error" in case:
        return case
    data = case.get("data") or {}
    brand, chat_id = data.get("brand"), chat_id_of(data)
    if not brand or not chat_id:
        return {"error": f"{cs}: 브랜드나 대화 id가 없다", "issue": data.get("issue")}
    conversation = auto_cs.run(auto_cs_dir, "chat.conversation", brand, chat_id)
    if "error" in conversation:
        return conversation
    turns = (conversation.get("data") or {}).get("turns") or []
    return {
        "cs": cs,
        "brand": brand,
        "chat_id": chat_id,
        "turns": [
            t
            | {
                "turn": f"turn:{brand}:{t.get('id')}",
                "question": str(t.get("question") or "")[:TEXT_LIMIT],
                "answer": str(t.get("answer") or "")[:TEXT_LIMIT],
            }
            for t in turns[-TURN_LIMIT:]
        ],
    }


@tool(
    "read_case_chat",
    "건 번호(CS-...)로 그 건의 대화 턴 목록을 읽는다. 질문, 답변, 고른 매뉴얼, 부른 함수가 있다. "
    "턴 id(turn)를 read_turn에 넘기면 그 턴을 깊게 본다.",
    {"cs": str},
)
async def case_chat_tool(args: dict[str, Any]) -> dict[str, Any]:
    result = await asyncio.to_thread(read_case_chat, args["cs"])
    text = json.dumps(result, ensure_ascii=False, default=str)
    return {"content": [{"type": "text", "text": text}]}
