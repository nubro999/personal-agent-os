"""함수 응답 칸을 사례로 바꾼다. 뜻은 genlab-tools 스펙과 코드(api.fields), 값별 뜻과 처리는
브랜드 정책 상태 사전(glossary.states)에서 온다.
"""

import json
from pathlib import Path
from typing import Any

from app.ontology.instances import Graph, Node

DETAIL_LIMIT = 1500
SOURCE = "genlab-tools api.fields"


def _field_text(field: dict[str, Any]) -> list[str]:
    lines = [f"타입: {field.get('type') or '-'}"]
    if field.get("doc"):
        lines.append(f"설명: {field['doc']}")
    for spec in (field.get("spec") or [])[:2]:
        lines.append(f"스펙: {spec.get('text', '')}")
    for code in (field.get("code") or [])[:2]:
        lines.append(f"코드: {code.get('text', '')}")
    allowed = [
        str(v.get("value") or v.get("name"))
        for group in field.get("allowed") or []
        for v in group.get("values") or []
    ]
    if allowed:
        lines.append("가능한 값: " + ", ".join(allowed[:20]))
    return lines


def add_fields(graph: Graph, fields_dir: Path, brand_context: Path) -> None:
    made: dict[tuple[str, str], list[str]] = {}
    for path in sorted(fields_dir.glob("*.json")):
        db_name = path.stem
        functions: dict[str, list[dict[str, Any]]] = json.loads(path.read_text(encoding="utf-8"))
        for bot in graph.db_bots.get(db_name) or [db_name]:
            for function_name, fields in functions.items():
                function = graph.add(
                    Node(f"function:{bot}:{function_name}", function_name, "function")
                )
                for field in fields:
                    name = field["field"]
                    node = graph.add(
                        Node(
                            f"response_field:{bot}:{function_name}:{name}",
                            name,
                            "response_field",
                            "\n".join(_field_text(field))[:DETAIL_LIMIT],
                            SOURCE,
                            f"{function_name}:{name}",
                        )
                    )
                    graph.link(node, function, "belongs_to")
                    made.setdefault((bot, name), []).append(node)
    graph.measure("봇 구성 → 응답 칸 있음", len({bot for bot, _ in made}), len(graph.bot_config))
    _add_glossary(graph, brand_context, made)


def _add_glossary(
    graph: Graph, brand_context: Path, made: dict[tuple[str, str], list[str]]
) -> None:
    """상태 사전의 값별 뜻을 같은 이름의 응답 칸에 붙인다. 함수가 적혀 있으면 그 함수만."""
    hit = total = 0
    for policy in sorted(brand_context.glob("_*/*/정책.json")):
        bot = policy.parent.name
        states = (json.loads(policy.read_text(encoding="utf-8")).get("glossary") or {}).get(
            "states"
        ) or []
        for state in states:
            total += 1
            functions = {f.strip() for f in str(state.get("function") or "").split("·") if f}
            targets = [
                node
                for node in made.get((bot, str(state.get("field") or "")), [])
                if not functions or node.split(":")[2] in functions
            ]
            line = f"값 {state.get('value')}: {state.get('meaning') or ''}"
            if state.get("handling"):
                line += f" / 처리: {state['handling']}"
            for node in targets:
                detail = graph.nodes[node].detail
                if len(detail) < DETAIL_LIMIT * 2:
                    graph.nodes[node].detail = f"{detail}\n{line}"
            hit += bool(targets)
    graph.measure("상태 사전 값 → 응답 칸", hit, total)
