"""2325 행을 사례로 바꾼다. 건, 실행, 진단, 수정안, 변경 항목, 판정, 반영, 수정 기록 등."""

from collections.abc import Iterable
from typing import Any

from app.ontology.instances import BrandIndex, Graph, Node, _short

FEEDBACK_LIMIT = 4000


def _full(value: Any) -> str:
    return str(value or "").strip()[:FEEDBACK_LIMIT]


Rows = dict[str, list[dict[str, Any]]]
SOURCE = "2325"
_TARGET_CONCEPT = {"카테고리": "category", "정보": "info"}
# 담당자 칸에 들어가지만 팀이 아닌 이름
NOT_TEAMS = frozenset({"기타"})


def _case_label(row: dict[str, Any]) -> str:
    return " ".join(x for x in (row.get("human_id"), row.get("company")) if x) or row["item_key"]


def _add_users(graph: Graph, rows: Rows) -> dict[str, str]:
    by_name: dict[str, str] = {}
    for row in rows["users"]:
        by_name[row["name"]] = graph.add(
            Node(f"user:{row['id']}", row["name"], "user", source="2325 user")
        )
    served = 0
    for name, customer in graph.pending_serves:
        user = by_name.get(name)
        if user:
            graph.link(user, customer, "serves")
            served += 1
    graph.measure("우리 쪽 담당 → 사용자", served, len(graph.pending_serves))
    return by_name


def _brand_of(graph: Graph, brands: BrandIndex, *keys: str | None) -> str | None:
    for key in keys:
        if not key:
            continue
        hit = graph.db_brand.get(key) or brands.find(
            key, key.removesuffix("_v2"), f"b_{key.removesuffix('_v2')}kr"
        )
        if hit:
            return hit
    return None


def _case_target(
    graph: Graph, brands: BrandIndex, customers: BrandIndex, row: dict[str, Any]
) -> str | None:
    """db_name이 먼저다. 없으면 ax id가 브랜드인지 고객사인지 정확히 본다.

    브랜드로 못 좁히는 건은 고객사 전체 수정 건이다(scope client). 별칭은 대소문자를 뭉개서
    b_OA(고객사)와 b_oa(브랜드 별칭)가 겹치므로 고객사를 별칭보다 먼저 확인한다.
    """
    brand = _brand_of(graph, brands, row.get("db_name"))
    if brand:
        return brand
    ax_id = row.get("brand_id") or ""
    for node in (f"brand:{ax_id}", f"customer:{ax_id}"):
        if node in graph.nodes:
            return node
    return _brand_of(graph, brands, ax_id) or customers.find(ax_id)


def _add_cases(
    graph: Graph,
    rows: Rows,
    brands: BrandIndex,
    customers: BrandIndex,
    users: dict[str, str],
) -> None:
    linked = 0
    for row in rows["cases"]:
        case = graph.add(
            Node(
                f"case:{row['item_key']}",
                _case_label(row),
                "case",
                _full(row.get("feedback")),
                "2325 wi_ax_item",
            )
        )
        target = _case_target(graph, brands, customers, row)
        if target:
            graph.link(case, target, "targets")
            linked += 1
    graph.measure("건 → 브랜드, 고객사", linked, len(rows["cases"]))
    for row in rows["adhoc"]:
        case = graph.add(
            Node(
                f"case:{row['item_key']}",
                f"수동 요청 {row.get('company') or row['item_key']}",
                "case",
                _full(row.get("issue")),
                "2325 rnd_adhoc_request",
            )
        )
        brand = _brand_of(graph, brands, row.get("brand"))
        if brand:
            graph.link(case, brand, "targets")
    hit = 0
    for row in rows["assignments"]:
        name, case = row["name"], f"case:{row['item_key']}"
        if case not in graph.nodes:
            continue
        user = users.get(name)
        if user is None and name not in NOT_TEAMS:
            # 사람이 아닌 담당자 이름은 팀이다(플랫폼, 툴콜, 모듈 등).
            user = graph.add(Node(f"team:{name}", name, "team", source="2325 assignee"))
        if user:
            graph.link(case, user, "assigned_to")
            hit += 1
    graph.measure("담당 → 사용자, 팀", hit, len(rows["assignments"]))


def _add_runs(graph: Graph, rows: Rows) -> None:
    linked = 0
    for row in rows["runs"]:
        run_id = row["run_id"]
        day = str(row.get("ran_at") or "")[:10]
        run = graph.add(
            Node(
                f"run:{run_id}",
                " ".join(x for x in (row.get("kind"), row.get("brand"), day) if x) or run_id,
                "run",
                source="2325 rnd_run",
            )
        )
        agent_name = row.get("agent")
        if agent_name:
            agent = graph.add(Node(f"agent:{agent_name}", agent_name, "agent", source="rnd_run"))
            graph.link(agent, run, "executes")
        case = f"case:{row.get('item_key')}"
        if case in graph.nodes:
            graph.link(run, case, "processes")
            linked += 1
    graph.measure("실행 → 건", linked, len(rows["runs"]))


def _run_of(graph: Graph, run_id: str | None) -> str | None:
    node = f"run:{run_id}"
    return node if node in graph.nodes else None


def _add_outputs(graph: Graph, rows: Rows) -> None:
    for row in rows["diagnoses"]:
        run = _run_of(graph, row["run_id"])
        if run:
            first = (row.get("body") or "").split("\n", 1)[0]
            node = graph.add(
                Node(
                    f"diagnosis:{row['run_id']}",
                    _short(first)[:60] or "진단",
                    "diagnosis",
                    _short(row.get("body")),
                    "2325 rnd_output",
                )
            )
            graph.link(node, run, "produced_by")
    for row in rows["plans"]:
        run = _run_of(graph, row["run_id"])
        if run:
            node = graph.add(
                Node(
                    f"proposal:{row['run_id']}",
                    f"수정안 {graph.nodes[run].label}",
                    "proposal",
                    source="2325 rnd_output",
                )
            )
            graph.link(node, run, "produced_by")
            diagnosis = f"diagnosis:{row['run_id']}"
            if diagnosis in graph.nodes:
                graph.link(node, diagnosis, "based_on", "추정")
    for row in rows["rounds"]:
        proposal = f"proposal:{row['run_id']}"
        if proposal not in graph.nodes:
            continue
        for item in row.get("parsed") or []:
            node = graph.add(
                Node(
                    f"test_round:{row['run_id']}:{item.get('round')}",
                    f"{item.get('round')}회차 {item.get('verdict') or ''}".strip(),
                    "test_round",
                    _short(item.get("한줄")),
                    "2325 rnd_output rounds",
                )
            )
            graph.link(node, proposal, "tests")
    for row in rows["notes"]:
        run = _run_of(graph, row["run_id"])
        if not run:
            continue
        for item in row.get("parsed") or []:
            node = graph.add(
                Node(
                    f"knowledge_note:{row['run_id']}:{item.get('id')}",
                    f"{item.get('kind')} {item.get('about')}".strip(),
                    "knowledge_note",
                    _short(item.get("text")),
                    "2325 rnd_output brand_updates",
                )
            )
            graph.link(node, run, "produced_by")


def _set_real_category_keys(graph: Graph, rows: Rows) -> None:
    """수정안에 적힌 카테고리 id로 임시 키를 보강한다."""
    found = 0
    for row in rows["changes"]:
        parsed = row.get("parsed") or {}
        bot = graph.company_bot.get(parsed.get("company_id") or 0)
        if not bot:
            continue
        for category in parsed.get("categories") or []:
            node = graph.nodes.get(f"category:{bot}:{category.get('name')}")
            if node and category.get("id") and not node.real_key:
                node.real_key = str(category["id"])
                found += 1
    total = sum(1 for n in graph.nodes.values() if n.concept == "category")
    graph.measure("카테고리 진짜 키", found, total)


def _bots_for(graph: Graph, db_name: str | None, channel: str | None) -> list[str]:
    bots = graph.db_bots.get(db_name or "", [])
    preferred = [b for b in bots if b.endswith("_board") == (channel == "board")]
    return preferred + [b for b in bots if b not in preferred]


def _targets(graph: Graph, concept: str, bots: Iterable[str], ref: str) -> str | None:
    for bot in bots:
        node = f"{concept}:{bot}:{ref.strip()}"
        if node in graph.nodes:
            return node
    return None


def _add_from_change(graph: Graph, concept: str, bot: str, ref: str) -> str | None:
    config = graph.bot_config.get(bot)
    if config is None:
        return None
    found = graph.add(Node(f"{concept}:{bot}:{ref}", ref, concept, source="2325 rnd_snapshot"))
    graph.link(found, config, "belongs_to", "추정")
    return found


def _add_changes(graph: Graph, rows: Rows) -> None:
    run_brand = {r["run_id"]: r.get("brand") for r in rows["runs"]}
    hit = total = 0
    for row in rows["snapshots"]:
        proposal = f"proposal:{row['run_id']}"
        if proposal not in graph.nodes:
            continue
        target, ref = row.get("target") or "", row.get("target_ref") or ""
        node = graph.add(
            Node(
                f"change_item:{row['id']}",
                f"{target} {ref}".strip()[:60],
                "change_item",
                f"채널 {row.get('channel') or '-'}",
                "2325 rnd_snapshot",
            )
        )
        graph.link(node, proposal, "belongs_to")
        concept = _TARGET_CONCEPT.get(target)
        if concept:
            total += 1
            bots = _bots_for(graph, run_brand.get(row["run_id"]), row.get("channel"))
            found = _targets(graph, concept, bots, ref)
            if not found and bots and ref.strip():
                # 매뉴얼 맥락은 함수를 부르는 매뉴얼만 담는다. 바꾼 기록이 있으니 사례로 올린다.
                found = _add_from_change(graph, concept, bots[0], ref.strip())
            if found:
                graph.link(node, found, "modifies", "추정")
                hit += 1
    graph.measure("변경 항목 → 카테고리, 정보", hit, total)


def _add_reviews(graph: Graph, rows: Rows, users: dict[str, str]) -> None:
    hit = 0
    for row in rows["reviews"]:
        run = _run_of(graph, row["run_id"])
        if not run:
            continue
        node = graph.add(
            Node(
                f"review:{row['run_id']}:{row['axis']}",
                f"{row['axis']} {row.get('verdict') or ''}".strip(),
                "review",
                f"평가자 {row.get('reviewer') or '-'}",
                "2325 rnd_review",
            )
        )
        graph.link(node, run, "evaluates")
        user = users.get(row.get("reviewer") or "")
        if user:
            graph.link(node, user, "authored_by")
            hit += 1
    graph.measure("판정 → 사용자", hit, len(rows["reviews"]))


def _add_promotions(graph: Graph, rows: Rows) -> None:
    for row in rows["promotions"]:
        node = graph.add(
            Node(
                f"promotion:{row['id']}",
                f"{row.get('action')} {row.get('status')}",
                "promotion",
                source="2325 rnd_promotion",
            )
        )
        proposal = f"proposal:{row.get('run_id')}"
        if proposal in graph.nodes:
            graph.link(node, proposal, "deploys")
        case = f"case:{row.get('item_key')}"
        if case in graph.nodes:
            graph.link(node, case, "targets")


def _add_fixes(graph: Graph, rows: Rows, users: dict[str, str]) -> None:
    for row in rows["fixes"]:
        label = " ".join(str(x) for x in (row.get("date"), row.get("company")) if x)
        node = graph.add(
            Node(
                f"fix_record:{row['id']}",
                label[:50] or f"수정 기록 {row['id']}",
                "fix_record",
                _short(row.get("cause")),
                "2325 maintenance_context",
            )
        )
        case = f"case:{row.get('item_key')}"
        if case in graph.nodes:
            graph.link(node, case, "records")
        run = _run_of(graph, row.get("source_run_id"))
        if run:
            graph.link(node, run, "produced_by")
        author = users.get(row.get("author") or "")
        if author:
            graph.link(node, author, "authored_by", "추정")


def _add_events(graph: Graph, rows: Rows, brands: BrandIndex) -> None:
    for row in rows["events"]:
        graph.add(
            Node(
                f"period_event:{row['label']}",
                row.get("title") or row["label"],
                "period_event",
                f"{row.get('kind')} / {row.get('state')}",
                "2325 period_event",
            )
        )
    for row in rows["event_brands"]:
        event = f"period_event:{row['label']}"
        if event not in graph.nodes:
            continue
        brand = _brand_of(graph, brands, row.get("brand"))
        if brand:
            graph.link(event, brand, "applies_to")
        config = f"bot_config:{row.get('company_id')}"
        if config in graph.nodes:
            graph.link(event, config, "modifies")


def add_2325(graph: Graph, rows: Rows, brands: BrandIndex, customers: BrandIndex) -> None:
    users = _add_users(graph, rows)
    _add_cases(graph, rows, brands, customers, users)
    _add_runs(graph, rows)
    _add_outputs(graph, rows)
    _add_changes(graph, rows)
    _set_real_category_keys(graph, rows)
    _add_reviews(graph, rows, users)
    _add_promotions(graph, rows)
    _add_fixes(graph, rows, users)
    _add_events(graph, rows, brands)
