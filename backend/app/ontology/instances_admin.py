"""ai-admin config_tree를 사례로 바꾼다. 카테고리, step, 정보, 멘트, 함수의 본문과 진짜 키.

step, 정보, 멘트는 config_tree의 uuid를 real_key로 붙인다. step 사이 이동(goto)과 참조는
uuid로 이어져 있어 이름이 바뀌어도 끊기지 않는다.
"""

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from app.ontology.instances import Graph, Node

CONTENT_LIMIT = 1200
SOURCE = "ai-admin config_tree"
# 기본 매뉴얼은 카테고리 테이블이 아니라 회사 프롬프트에 있다. 고칠 곳도 회사 프롬프트다.
FALLBACK = "그 외 문의 응대 매뉴얼"
FALLBACK_SOURCE = "ai-admin company_prompts.main_prompt (회사 프롬프트 기본 매뉴얼, 카테고리 아님)"
STEP_HEAD = re.compile(r"^step(\d+)\s*$", re.MULTILINE)
GOTO = re.compile(r"goto\s+step(\d+)")


def _text(value: Any, limit: int = CONTENT_LIMIT) -> str:
    if value is None or value == "None":
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return re.sub(r"[ \t]+", " ", text).strip()[:limit]


def children(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    kids = node.get("children") or {}
    groups = kids.values() if isinstance(kids, dict) else [kids]
    for group in groups:
        for child in group if isinstance(group, list) else [group]:
            if isinstance(child, dict):
                yield child


def descendants(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for child in children(node):
        yield child
        yield from descendants(child)


def _goto(data: dict[str, Any]) -> str | None:
    return ((data.get("$link") or {}).get("goto") or {}).get("to")


def describe_step(step: dict[str, Any]) -> str:
    """step 아래 목표, 행동, 갈래, 예외를 한 덩어리 글로 편다."""
    parts: list[str] = []
    for node in descendants(step):
        kind, data = node.get("type", ""), node.get("data") or {}
        if kind.startswith(("goal.", "action.")):
            fields = ("pattern", "structure", "subject", "behavior", "constraint", "description")
            words = [_text(data.get(f), 300) for f in fields if data.get(f)]
            parts.append(f"{kind}: " + " / ".join(words))
        elif kind in ("branch_case", "action_case", "exception", "on_goal_achieved"):
            target = f" → step{data['goto']}" if data.get("goto") is not None else ""
            parts.append(f"{kind}: {_text(data.get('condition'), 300)}{target}")
        elif kind == "item" and data.get("value"):
            parts.append(f"항목: {_text(data.get('value'), 100)}")
    return _text("\n".join(parts))


def add_admin(graph: Graph, admin_dir: Path) -> None:
    files = sorted(admin_dir.glob("*_prod.json"))
    read = 0
    for path in files:
        tree = json.loads(path.read_text(encoding="utf-8"))
        bot = graph.company_bot.get(tree.get("companyId"))
        config = graph.bot_config.get(bot or "")
        if bot is None or config is None:
            continue
        read += 1
        _add_company(graph, tree["root"], bot, config)
    graph.measure("봇 구성 → config_tree", read, len(graph.bot_config))
    fallback = admin_dir / "fallback_manual.json"
    if fallback.is_file():
        add_fallback_manual(graph, json.loads(fallback.read_text(encoding="utf-8")))


def fallback_steps(section: str) -> dict[int, str]:
    """기본 매뉴얼 절의 Response Manual을 step 번호별 본문으로 나눈다."""
    manual = section.split("### Response Template", 1)[0]
    heads = list(STEP_HEAD.finditer(manual))
    return {
        int(head.group(1)): manual[head.end() : nxt.start() if nxt else None].strip()
        for head, nxt in zip(heads, [*heads[1:], None], strict=True)
    }


def fallback_category(bot: str) -> str:
    return f"category:{bot}:{FALLBACK}"


def add_fallback_manual(graph: Graph, sections: dict[str, str]) -> None:
    """봇 구성마다 기본 매뉴얼을 카테고리 사례 하나와 그 step으로 넣는다."""
    added = 0
    for company_id, section in sections.items():
        bot = graph.company_bot.get(int(company_id))
        config = graph.bot_config.get(bot or "")
        if bot is None or config is None:
            continue
        node = graph.add(Node(fallback_category(bot), FALLBACK, "category"))
        graph.nodes[node].detail = _text(section)
        graph.nodes[node].source = FALLBACK_SOURCE
        graph.nodes[node].real_key = f"{company_id}:prod:{FALLBACK}"
        graph.link(node, config, "belongs_to")
        steps = fallback_steps(section)
        for number, body in steps.items():
            step = graph.add(Node(f"step:{bot}:{FALLBACK}:{number}", f"step {number}", "step"))
            graph.nodes[step].detail = _text(body)
            graph.nodes[step].source = FALLBACK_SOURCE
            graph.link(step, node, "belongs_to")
        for number, body in steps.items():
            for target in GOTO.findall(body):
                if int(target) in steps:
                    graph.link(
                        f"step:{bot}:{FALLBACK}:{number}",
                        f"step:{bot}:{FALLBACK}:{target}",
                        "next",
                    )
        added += 1
    graph.measure("봇 구성 → 기본 매뉴얼", added, len(graph.bot_config))


def _add_company(graph: Graph, root: dict[str, Any], bot: str, config: str) -> None:
    by_uuid: dict[str, str] = {}
    for node in descendants(root):
        kind, data = node.get("type"), node.get("data") or {}
        if kind == "info" and data.get("tag"):
            found = graph.add(
                Node(f"info:{bot}:{data['tag'].strip()}", data["tag"].strip(), "info")
            )
            _fill(graph, found, _text(data.get("content")), node["uuid"])
            graph.link(found, config, "belongs_to")
            by_uuid[node["uuid"]] = found
        elif kind == "ment" and data.get("tag"):
            found = graph.add(
                Node(f"ment:{bot}:{data['tag'].strip()}", data["tag"].strip(), "ment")
            )
            _fill(graph, found, _text(data.get("content")), node["uuid"])
            graph.link(found, config, "belongs_to")
            for alias in data.get("$aliases") or []:
                by_uuid[alias] = found
            by_uuid[node["uuid"]] = found
        elif kind == "tool" and data.get("functionName"):
            name = data["functionName"]
            found = graph.add(Node(f"function:{bot}:{name}", name, "function"))
            _fill(graph, found, _text(data.get("description")), node["uuid"])
            by_uuid[node["uuid"]] = found
    for big in (n for n in descendants(root) if n.get("type") == "big_category"):
        for category in (c for c in children(big) if c.get("type") == "category"):
            _add_category(graph, big, category, bot, config, by_uuid)


def _fill(graph: Graph, node_id: str, detail: str, key: str) -> None:
    """운영 설정의 본문과 uuid가 먼저다. 다른 출처가 먼저 만든 노드에도 채운다."""
    node = graph.nodes[node_id]
    if detail:
        node.detail = detail
    node.source = SOURCE
    if key and not node.real_key:
        node.real_key = key


def _add_category(
    graph: Graph,
    big: dict[str, Any],
    category: dict[str, Any],
    bot: str,
    config: str,
    by_uuid: dict[str, str],
) -> None:
    name = f"{big['data']['name']} - {category['data']['category']}"
    node = graph.add(Node(f"category:{bot}:{name}", name, "category"))
    graph.nodes[node].detail = _text(category["data"].get("manualDescription"))
    graph.nodes[node].source = SOURCE
    graph.link(node, config, "belongs_to")
    manuals = [c for c in children(category) if c.get("type") == "channel_manual"]
    for manual in manuals:
        # 한 카테고리에 채널 매뉴얼이 여럿이면 step 번호가 겹쳐서 채널을 키에 넣는다.
        scope = name if len(manuals) == 1 else f"{name}@{manual['data'].get('channelName')}"
        steps = [s for s in children(manual) if str(s.get("type", "")).startswith("step.")]
        ids: dict[str, str] = {}
        for step in steps:
            number = step["data"]["step"]
            step_id = graph.add(Node(f"step:{bot}:{scope}:{number}", f"step {number}", "step"))
            _fill(graph, step_id, describe_step(step), step["data"].get("uid") or step["uuid"])
            graph.link(step_id, node, "belongs_to")
            ids[step["uuid"]] = step_id
        for step in steps:
            step_id = ids[step["uuid"]]
            for part in descendants(step):
                kind, data = part.get("type", ""), part.get("data") or {}
                target = _goto(data)
                if target in ids:
                    graph.link(step_id, ids[target], "next")
                if kind.startswith("ref."):
                    ref = by_uuid.get(str(data.get("ref_id")))
                    if ref:
                        graph.link(step_id, ref, "uses")
                elif kind == "function_ref" and data.get("value"):
                    function = graph.add(
                        Node(f"function:{bot}:{data['value']}", data["value"], "function")
                    )
                    graph.link(step_id, function, "invokes")
