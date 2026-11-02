"""채팅 DB 구조를 사례로 바꾼다. 대화, 턴, 함수 호출과 턴이 실제로 한 일."""

from typing import Any

from app.ontology.instances import BrandIndex, Graph, Node
from app.ontology.instances_2325 import _brand_of

# 인용이나 이관 사유로 찾은 문제 턴은 근거가 있고, 나머지는 짐작이 섞인다.
_FIRM_METHODS = {"인용 일치", "이관 사유"}


def _bot(graph: Graph, db_name: str) -> str:
    bots = graph.db_bots.get(db_name) or []
    chat = [b for b in bots if not b.endswith("_board")]
    return (chat or bots or [db_name])[0]


def _category(graph: Graph, bot: str, item: dict[str, Any], ids: dict[tuple[str, str], str]) -> str:
    key = str(item.get("id") or "")
    name = str(item["category"])
    found = ids.get((bot, key)) if key else None
    if found is None:
        found = graph.add(Node(f"category:{bot}:{name}", name, "category", source="채팅 DB"))
        config = graph.bot_config.get(bot)
        if config:
            graph.link(found, config, "belongs_to", "추정")
    node = graph.nodes[found]
    if key and not node.real_key:
        node.real_key = key
        ids[(bot, key)] = found
    return found


def _info(graph: Graph, bot: str, tag: str) -> str:
    found = f"info:{bot}:{tag}"
    if found not in graph.nodes:
        graph.add(Node(found, tag, "info", source="채팅 DB"))
        config = graph.bot_config.get(bot)
        if config:
            graph.link(found, config, "belongs_to", "추정")
    return found


def _function(graph: Graph, bot: str, name: str) -> str:
    return graph.add(Node(f"function:{bot}:{name}", name, "function", source="채팅 DB"))


def add_chat(graph: Graph, chat: dict[str, Any], brands: BrandIndex) -> None:
    ids = {
        (node.id.split(":", 2)[1], node.real_key): node.id
        for node in graph.nodes.values()
        if node.concept == "category" and node.real_key
    }
    linked_brand = 0
    for row in chat["conversations"]:
        node = graph.add(
            Node(
                f"conversation:{row['db']}:{row['room']}",
                f"대화 {row['room']}",
                "conversation",
                f"{row.get('platform') or '-'} / 이관 {row.get('handoff') or '없음'}",
                f"채팅 DB {row['db']}",
            )
        )
        brand = _brand_of(graph, brands, row["db"])
        if brand:
            graph.link(node, brand, "belongs_to")
            linked_brand += 1
    graph.measure("대화 → 브랜드", linked_brand, len(chat["conversations"]))

    judged = selected = visited = visited_total = 0
    for row in chat["turns"]:
        conversation = f"conversation:{row['db']}:{row['room']}"
        turn = graph.add(
            Node(
                f"turn:{row['db']}:{row['id']}",
                f"턴 {row['id']} {row.get('status') or ''}".strip(),
                "turn",
                f"{row.get('time') or '-'} / {row.get('agent') or '-'}",
                f"채팅 DB {row['db']}",
            )
        )
        graph.link(turn, conversation, "belongs_to")
        bot = _bot(graph, row["db"])
        manuals = row.get("selected") or []
        judged += bool(manuals)
        for item in manuals:
            graph.link(turn, _category(graph, bot, item, ids), "selected")
            selected += 1
        for tag in row.get("tags") or []:
            graph.link(turn, _info(graph, bot, tag), "loaded", "추정")
        for index, name in enumerate(row.get("functions") or []):
            call = graph.add(
                Node(
                    f"tool_call:{row['db']}:{row['id']}:{index}",
                    name,
                    "tool_call",
                    source="채팅 DB",
                )
            )
            graph.link(call, turn, "belongs_to")
            graph.link(call, _function(graph, bot, name), "invokes")
        # 매뉴얼이 하나일 때만 CoT의 step 번호가 어느 매뉴얼 것인지 알 수 있다.
        if len(manuals) == 1:
            category = manuals[0]["category"]
            for number in row.get("steps") or []:
                visited_total += 1
                step = f"step:{bot}:{category}:{number}"
                if step in graph.nodes:
                    graph.link(turn, step, "visited", "추정")
                    visited += 1
    graph.measure("턴 → 고른 매뉴얼 판정 있음", judged, len(chat["turns"]))
    graph.measure("턴 step 번호 → step 사례", visited, visited_total)

    firm = 0
    for row in chat["problems"]:
        case = f"case:{row['item_key']}"
        conversation = f"conversation:{row['db']}:{row['room']}"
        if case not in graph.nodes:
            continue
        graph.link(case, conversation, "refers_to")
        turn = f"turn:{row['db']}:{row.get('turn')}"
        if turn in graph.nodes:
            sure = row.get("method") in _FIRM_METHODS
            graph.link(case, turn, "refers_to", "확정" if sure else "추정")
            firm += sure
    graph.measure("건 → 문제 턴 인용 근거", firm, len(chat["problems"]))
    for db_name, reason in chat.get("skipped", {}).items():
        graph.unread_chat_dbs.append(f"{db_name} ({reason})")
