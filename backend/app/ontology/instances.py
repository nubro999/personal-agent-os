"""출처에서 사례를 뽑아 개념에 매단다. 원본은 옮기지 않고 위치와 짧은 설명만 남긴다.

- index/brand-map.json (S3): 고객사, 브랜드
- brands/context: 봇 구성, 정책, 매뉴얼, 결정 지점, 정책 적용 위치
- context/ (S3): step, 분기, step이 쓰는 정보
"""

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DETAIL_LIMIT = 240
_BOARD_SUFFIX = "_board"


@dataclass
class Node:
    id: str
    label: str
    concept: str
    detail: str = ""
    source: str = ""
    real_key: str = ""


@dataclass
class Link:
    source: str
    target: str
    name: str
    confidence: str


@dataclass
class Graph:
    nodes: dict[str, Node] = field(default_factory=dict)
    links: dict[tuple[str, str, str], Link] = field(default_factory=dict)
    unmatched_brands: list[str] = field(default_factory=list)
    unresolved_policies: list[str] = field(default_factory=list)
    info_without_doc: int = 0
    db_brand: dict[str, str] = field(default_factory=dict)
    db_bots: dict[str, list[str]] = field(default_factory=dict)
    company_bot: dict[int, str] = field(default_factory=dict)
    bot_config: dict[str, str] = field(default_factory=dict)
    unread_chat_dbs: list[str] = field(default_factory=list)
    # 우리 쪽 사람은 2325 사용자 이름을 알아야 이을 수 있어서 나중에 처리한다.
    pending_serves: list[tuple[str, str]] = field(default_factory=list)
    measured: dict[str, tuple[int, int]] = field(default_factory=dict)

    def measure(self, name: str, hit: int, total: int) -> None:
        self.measured[name] = (hit, total)

    def add(self, node: Node) -> str:
        existing = self.nodes.get(node.id)
        if existing is None:
            self.nodes[node.id] = node
        else:
            # 뒤에 읽은 출처가 설명을 더 갖고 있으면 채운다.
            existing.detail = existing.detail or node.detail
            existing.source = existing.source or node.source
            if existing.label.startswith("step ") and len(node.label) > len(existing.label):
                existing.label = node.label
        return node.id

    def link(self, source: str, target: str, name: str, confidence: str = "확정") -> None:
        self.links.setdefault((source, target, name), Link(source, target, name, confidence))

    def as_dict(self) -> dict[str, Any]:
        return {
            "nodes": [asdict(n) for n in self.nodes.values()],
            "links": [asdict(link) for link in self.links.values()],
        }


def _short(value: Any) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return re.sub(r"\s+", " ", text).strip()[:DETAIL_LIMIT]


def _read(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", text.lower())


def _brand_aliases(name: str) -> list[str]:
    """'오아 게시판 · 오아주식회사(...)' 같은 글에서 부르는 이름 후보를 뽑는다."""
    head = re.split(r"[·—(]", name)[0].strip()
    words = head.split()
    return [head, *([words[0]] if words else [])]


class BrandIndex:
    def __init__(self) -> None:
        self.by_alias: dict[str, str] = {}

    def add(self, node_id: str, *aliases: str) -> None:
        for alias in aliases:
            if alias:
                self.by_alias.setdefault(_norm(alias), node_id)

    def find(self, *aliases: str) -> str | None:
        for alias in aliases:
            hit = self.by_alias.get(_norm(alias))
            if hit:
                return hit
        return None


def add_brand_map(graph: Graph, path: Path) -> tuple[BrandIndex, BrandIndex]:
    data = _read(path)
    brands, customers = BrandIndex(), BrandIndex()
    entries = data["brands"]
    for entry in entries:
        label = entry.get("korean") or (entry.get("keys") or [entry["id"]])[0]
        aliases = [entry["id"], entry["id"].removeprefix("b_"), label, *(entry.get("keys") or [])]
        if entry.get("kind") == "고객사":
            node = graph.add(Node(f"customer:{entry['id']}", label, "customer", source="brand-map"))
            customers.add(node, *aliases)
        elif entry.get("kind") == "브랜드":
            node = graph.add(Node(f"brand:{entry['id']}", label, "brand", source="brand-map"))
            brands.add(node, *aliases)
    for entry in entries:
        parent = f"customer:{entry.get('parent_id')}"
        child = f"brand:{entry['id']}"
        if child in graph.nodes and parent in graph.nodes:
            graph.link(child, parent, "belongs_to")
    return brands, customers


def customer_of(graph: Graph, brand: str) -> str | None:
    for source, target, name in graph.links:
        if source == brand and name == "belongs_to" and target.startswith("customer:"):
            return target
    return None


def add_people(graph: Graph, people: Any, customer: str | None, source: str) -> None:
    """communication.people를 담당자로 올린다. client는 고객사 담당자, ours는 우리 쪽 사람이다."""
    if not isinstance(people, dict) or customer is None:
        return
    company = graph.nodes[customer].label
    for person in people.get("client") or []:
        name = (person.get("name") or "").strip() if isinstance(person, dict) else ""
        if not name:
            continue
        node = graph.add(
            Node(f"contact:{company}:{name}", name, "contact", _short(person.get("role")), source)
        )
        graph.link(node, customer, "works_for")
    for person in people.get("ours") or []:
        name = (person.get("name") or "").strip() if isinstance(person, dict) else ""
        if name:
            graph.pending_serves.append((name, customer))


def _step(graph: Graph, bot: str, category: str, number: Any, label: str = "") -> str:
    return graph.add(
        Node(
            f"step:{bot}:{category}:{number}",
            f"step {number} {label}".strip(),
            "step",
            source="brands/context",
        )
    )


def _info(graph: Graph, config: str, bot: str, tag: str, source: str, detail: str = "") -> str:
    name = tag.strip()
    node = graph.add(Node(f"info:{bot}:{name}", name, "info", detail, source))
    graph.link(node, config, "belongs_to")
    return node


def _add_manual(graph: Graph, config: str, bot: str, path: Path, policies: dict[str, str]) -> None:
    data = _read(path)
    category = data.get("category") or path.stem
    source = f"brands/context/{path.parent.parent.name}/manual/{path.name}"
    node = graph.add(
        Node(
            f"category:{bot}:{category}", category, "category", _short(data.get("purpose")), source
        )
    )
    graph.link(node, config, "belongs_to")
    for tool in data.get("tools") or []:
        name = tool.get("name") if isinstance(tool, dict) else tool
        if name:
            function = graph.add(Node(f"function:{bot}:{name}", name, "function", source=source))
            graph.link(node, function, "uses")
    for i, point in enumerate(data.get("decision_points") or []):
        number = point.get("step")
        step = _step(graph, bot, category, number)
        graph.link(step, node, "belongs_to")
        decision = graph.add(
            Node(
                f"decision_point:{bot}:{category}:{i}",
                _short(point.get("purpose") or point.get("anchor") or f"step {number}")[:60],
                "decision_point",
                _short(point.get("anchor")),
                source,
            )
        )
        graph.link(decision, node, "belongs_to")
        graph.link(decision, step, "located_at", "추정")
        for policy_id in point.get("policy_ids") or []:
            target = policies.get(policy_id)
            if target:
                graph.link(decision, target, "justified_by")
            else:
                graph.unresolved_policies.append(f"{bot} {category} {policy_id}")
    for ref in data.get("info_refs") or []:
        tag = ref.get("tag") if isinstance(ref, dict) else None
        if not tag:
            continue
        info = _info(graph, config, bot, tag, source)
        for number in ref.get("steps") or []:
            step = _step(graph, bot, category, number)
            graph.link(step, node, "belongs_to")
            graph.link(step, info, "uses")
    for application in data.get("policy_application") or []:
        target = policies.get(application.get("policy_id"))
        for number in application.get("where") or []:
            if target:
                step = _step(graph, bot, category, number)
                graph.link(step, node, "belongs_to")
                graph.link(step, target, "implements")


def add_brand_context(
    graph: Graph, root: Path, brands: BrandIndex, customers: BrandIndex
) -> dict[str, str]:
    """봇 키(폴더 이름)별 봇 구성 id를 돌려준다."""
    configs: dict[str, str] = {}
    for customer_dir in sorted(p for p in root.glob("_*") if p.is_dir()):
        customer_name = customer_dir.name.removeprefix("_")
        common_policies: dict[str, str] = {}
        common = customer_dir / "공통.json"
        customer_node = customers.find(customer_name)
        if common.is_file():
            common_data = _read(common)
            customer_node = customer_node or customers.find(common_data.get("ax_id") or "")
            if customer_node is None:
                customer_node = graph.add(
                    Node(f"customer:{customer_name}", customer_name, "customer", source="공통.json")
                )
            for policy in common_data.get("policies") or []:
                node = graph.add(
                    Node(
                        f"policy:{customer_name}:{policy['id']}",
                        f"{policy['id']} {policy.get('name', '')}".strip(),
                        "policy",
                        _short(policy.get("rule")),
                        f"brands/context/{customer_dir.name}/공통.json",
                    )
                )
                graph.link(node, customer_node, "applies_to")
                common_policies[policy["id"]] = node
            add_people(
                graph,
                (common_data.get("communication") or {}).get("people"),
                customer_node,
                f"brands/context/{customer_dir.name}/공통.json",
            )
        for policy_file in sorted(customer_dir.glob("*/정책.json")):
            bot = policy_file.parent.name
            data = _read(policy_file)
            db_name = data.get("db_name") or bot
            brand_text = data.get("brand") or db_name
            base = db_name.removesuffix("_v2")
            brand = brands.find(db_name, base, f"b_{base}kr", *_brand_aliases(brand_text))
            if brand is None:
                graph.unmatched_brands.append(f"{customer_name}/{bot}")
                brand = graph.add(
                    Node(
                        f"brand:{db_name}",
                        _brand_aliases(brand_text)[0],
                        "brand",
                        source="정책.json",
                    )
                )
                if customer_node:
                    graph.link(brand, customer_node, "belongs_to")
            source = f"brands/context/{customer_dir.name}/{bot}"
            company = data.get("company_id")
            config = graph.add(
                Node(
                    f"bot_config:{company}",
                    f"{_brand_aliases(brand_text)[0]} ({data.get('channel')})",
                    "bot_config",
                    f"company_id {company}, db_name {db_name}",
                    source,
                )
            )
            graph.link(config, brand, "configures")
            configs[bot] = config
            add_people(
                graph,
                (data.get("communication") or {}).get("people"),
                customer_node or customer_of(graph, brand),
                f"{source}/정책.json",
            )
            graph.db_brand[db_name] = brand
            graph.db_bots.setdefault(db_name, []).append(bot)
            graph.bot_config[bot] = config
            if isinstance(company, int):
                graph.company_bot[company] = bot
            policies = dict(common_policies)
            for policy in data.get("policies") or []:
                node = graph.add(
                    Node(
                        f"policy:{bot}:{policy['id']}",
                        f"{policy['id']} {policy.get('name', '')}".strip(),
                        "policy",
                        _short(policy.get("rule")),
                        f"{source}/정책.json",
                    )
                )
                graph.link(node, brand, "applies_to")
                policies[policy["id"]] = node
            for manual in sorted((policy_file.parent / "manual").glob("*.json")):
                _add_manual(graph, config, bot, manual, policies)
    return configs


def latest_editions(context_root: Path) -> list[Path]:
    """브랜드, 채널마다 가장 최근 기준일 폴더."""
    latest: dict[tuple[str, str], Path] = {}
    for edition in context_root.glob("*/*/*"):
        key = (edition.parent.parent.name, edition.parent.name)
        if edition.is_dir() and (key not in latest or edition.name > latest[key].name):
            latest[key] = edition
    return [latest[key] for key in sorted(latest)]


def _bot_key(graph: Graph, brand_id: str, channel: str, configs: dict[str, str]) -> str | None:
    """이미 브랜드에 이어진 봇 구성 중 채널이 맞는 것을 찾는다."""
    for bot, config in configs.items():
        linked = (config, f"brand:{brand_id}", "configures") in graph.links
        if linked and (channel == "board") == bot.endswith(_BOARD_SUFFIX):
            return bot
    return None


def _goto_number(goto: Any) -> str | None:
    found = re.fullmatch(r"step\s*(\d+)", str(goto).strip())
    return found.group(1) if found else None


def add_s3_context(graph: Graph, context_root: Path, configs: dict[str, str]) -> None:
    for edition in latest_editions(context_root):
        brand_id, channel = edition.parent.parent.name, edition.parent.name
        bot = _bot_key(graph, brand_id, channel, configs) or f"{brand_id}_{channel}"
        config = configs.get(bot)
        if config is None:
            config = graph.add(
                Node(f"bot_config:{bot}", bot, "bot_config", "S3 판. company_id 모름", "S3 context")
            )
            if f"brand:{brand_id}" in graph.nodes:
                graph.link(config, f"brand:{brand_id}", "configures", "추정")
            configs[bot] = config
        source = f"S3 context/{brand_id}/{channel}/{edition.name}"
        documented: set[str] = set()
        bodies = []
        for path in sorted(edition.iterdir()):
            payload = _read(path).get("payload") or {}
            bodies.append((payload.get("doc_type"), payload.get("body") or {}))
        for doc_type, body in bodies:
            if doc_type == "brand":
                customer = customer_of(graph, f"brand:{brand_id}")
                add_people(graph, body.get("people"), customer, source)
            if doc_type == "info":
                name = (body.get("name") or "").strip()
                if name:
                    _info(graph, config, bot, name, source, _short(body.get("summary")))
                    documented.add(name)
        for doc_type, body in bodies:
            if doc_type != "category":
                continue
            category = body.get("category") or ""
            node = graph.add(
                Node(
                    f"category:{bot}:{category}",
                    category,
                    "category",
                    _short(body.get("summary")),
                    source,
                )
            )
            graph.link(node, config, "belongs_to")
            for tool in (body.get("manual_meta") or {}).get("tools_used") or []:
                name = tool.get("name") if isinstance(tool, dict) else tool
                if name:
                    function = graph.add(Node(f"function:{bot}:{name}", str(name), "function"))
                    graph.link(node, function, "uses")
            steps = body.get("steps") or {}
            if not isinstance(steps, dict):
                continue
            for number, step in steps.items():
                role = step.get("role") or step.get("type") or ""
                step_id = _step(graph, bot, category, number, _short(role)[:40])
                graph.nodes[step_id].detail = graph.nodes[step_id].detail or _short(
                    f"{step.get('type', '')}: {role}"
                )
                graph.nodes[step_id].source = source
                graph.link(step_id, node, "belongs_to")
                for case in step.get("cases") or []:
                    target = _goto_number(case.get("goto"))
                    if target:
                        target_id = _step(graph, bot, category, target)
                        graph.link(target_id, node, "belongs_to")
                        graph.link(step_id, target_id, "next")
                for used in step.get("information_used") or []:
                    item = (used.get("item") or "").strip() if isinstance(used, dict) else ""
                    if item:
                        if item not in documented:
                            graph.info_without_doc += 1
                        info = _info(graph, config, bot, item, source)
                        graph.link(step_id, info, "uses")
