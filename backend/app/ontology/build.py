"""사례를 뽑아 build/instances.json과 빈_부분.md를 만든다.

실행: uv run python -m app.ontology.build [--cached]
--cached는 2325를 다시 읽지 않고 지난번에 받은 행을 쓴다. 터널로 받는 데 몇 분 걸린다.
"""

import json
import logging
import sys
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pymysql

from app.config import get_settings
from app.ontology.concepts import Concept, load_concepts, load_verbs
from app.ontology.instances import Graph, add_brand_context, add_brand_map, add_s3_context
from app.ontology.instances_2325 import add_2325
from app.ontology.instances_chat import add_chat
from app.ontology.source_2325 import fetch_rows
from app.ontology.source_chat import fetch_chat

log = logging.getLogger(__name__)


def check_links(concepts: list[Concept], graph: Graph) -> None:
    """사례 링크는 개념 파일에 적힌 관계여야 한다. 지도와 데이터가 어긋나면 멈춘다."""
    allowed = {(r.source, r.name, r.target) for c in concepts for r in c.relations}
    seen = {
        (graph.nodes[link.source].concept, link.name, graph.nodes[link.target].concept)
        for link in graph.links.values()
    }
    unknown = sorted(seen - allowed)
    if unknown:
        raise ValueError(f"개념 파일에 없는 관계: {unknown}")


def build(
    ontology_dir: Path,
    brand_context: Path,
    rows_2325: dict[str, Any] | None = None,
    chat: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    verbs = load_verbs(ontology_dir / "verbs.yaml")
    concepts = load_concepts(ontology_dir / "concepts", verbs)
    known = {concept.id for concept in concepts}
    graph = Graph()
    overmind = ontology_dir / "source" / "overmind"
    brands, customers = add_brand_map(graph, overmind / "index" / "brand-map.json")
    configs = add_brand_context(graph, brand_context, brands, customers)
    add_s3_context(graph, overmind / "context", configs)
    if rows_2325:
        add_2325(graph, rows_2325, brands, customers)
    if chat:
        add_chat(graph, chat, brands)

    unknown = {node.concept for node in graph.nodes.values()} - known
    if unknown:
        raise ValueError(f"개념 파일에 없는 사례 종류: {sorted(unknown)}")
    dangling = [
        link
        for link in graph.links.values()
        if link.source not in graph.nodes or link.target not in graph.nodes
    ]
    if dangling:
        raise ValueError(f"끊어진 링크 {len(dangling)}개: {dangling[:3]}")

    check_links(concepts, graph)

    counts = Counter(node.concept for node in graph.nodes.values())
    data = graph.as_dict() | {
        "generated_at": datetime.now(UTC).isoformat(),
        "counts": dict(counts),
    }
    return data, render_gaps(concepts, counts, graph)


def render_gaps(concepts: list[Concept], counts: Counter[str], graph: Graph) -> str:
    empty = [c for c in concepts if not counts[c.id]]
    weak = [r for c in concepts for r in c.relations if r.confidence != "확정"]
    names = {c.id: c.name for c in concepts}
    seen = {
        (graph.nodes[link.source].concept, link.name, graph.nodes[link.target].concept)
        for link in graph.links.values()
    }
    relations = [r for c in concepts for r in c.relations]
    unproven = [r for r in relations if (r.source, r.name, r.target) not in seen]
    lines = [
        "# 온톨로지 빈 부분",
        "",
        "`python -m app.ontology.build`가 만드는 파일이다. 손으로 고치지 않는다.",
        "",
        f"개념 {len(concepts)}개 중 사례가 있는 것 {len(concepts) - len(empty)}개, "
        f"사례 {sum(counts.values())}개, 사례 링크 {len(graph.links)}개.",
        "",
        "## 사례가 없는 개념",
        "",
        "| 개념 | 묶음 | 차수 | 정본 |",
        "|---|---|---|---|",
        *(f"| {c.name} | {c.group} | {c.tier} | {c.owner} |" for c in empty),
        "",
        "## 확정 못 한 관계",
        "",
        "| 관계 | 신뢰도 | 이유 |",
        "|---|---|---|",
        *(
            f"| {names[r.source]} {r.name} {names[r.target]} | {r.confidence} | {r.basis} |"
            for r in weak
        ),
        "",
        "## 사례로 확인되지 않은 관계",
        "",
        f"관계 {len(relations)}개 중 {len(relations) - len(unproven)}개는 실제 연결이 있다. "
        "나머지는 정의만 있고 데이터로 입증되지 않았다.",
        "",
        *(f"- {names[r.source]} `{r.name}` {names[r.target]}" for r in unproven),
        "",
        "## 잇지 못한 사례",
        "",
        f"- brand-map에서 못 찾은 브랜드 폴더 {len(graph.unmatched_brands)}개: "
        + (", ".join(graph.unmatched_brands) or "없음"),
        f"- 매뉴얼의 정책 id가 정책 문서에 없는 곳 {len(graph.unresolved_policies)}개",
        f"- 정보 문서 없이 step에서만 참조된 정보 참조 {graph.info_without_doc}개",
        f"- 읽지 못한 채팅 DB {len(graph.unread_chat_dbs)}개: "
        + (", ".join(graph.unread_chat_dbs) or "없음"),
        "",
        "## 연결 실측",
        "",
        "| 연결 | 이어진 수 | 전체 | 비율 |",
        "|---|---|---|---|",
        *(
            f"| {name} | {hit} | {total} | {hit / total:.0%} |"
            for name, (hit, total) in graph.measured.items()
            if total
        ),
        "",
        "## 임시 키",
        "",
        "출처에 정의서의 키가 없어서 임시 키를 썼다. 정본에 붙일 때 바꿔야 한다.",
        "",
        "| 개념 | 정의서의 키 | 지금 쓴 키 |",
        "|---|---|---|",
        "| 브랜드 | db_name | ax id (brand-map) |",
        "| 고객사 | ax id | 같음 |",
        "| 카테고리 | category_id | 봇 폴더 + 카테고리 이름 |",
        "| step | uid | 봇 폴더 + 카테고리 이름 + step 번호 |",
        "| 정보 | uid | 봇 폴더 + 태그 |",
        "| 조회 함수 | db_name + 함수 이름 | 봇 폴더 + 함수 이름 |",
        "| 결정 지점 | 카테고리 + anchor | 봇 폴더 + 카테고리 이름 + 순번 |",
    ]
    return "\n".join(lines) + "\n"


def _load(cache: Path, fetch: Callable[[], dict[str, Any]]) -> dict[str, Any] | None:
    """터널이 끊겨 있으면 지난번에 받은 행으로 만든다."""
    try:
        rows = fetch()
    except (pymysql.err.OperationalError, KeyError, FileNotFoundError) as exc:
        log.warning("db not reachable, using cache %s: %s", cache.name, exc)
        return _cached(cache)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return rows


def _cached(cache: Path) -> dict[str, Any] | None:
    if not cache.is_file():
        return None
    data: dict[str, Any] = json.loads(cache.read_text(encoding="utf-8"))
    return data


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    cache_dir = settings.ontology_dir / "source" / "2325"
    rows_cache, chat_cache = cache_dir / "rows.json", cache_dir / "chat.json"
    if "--cached" in sys.argv:
        rows, chat = _cached(rows_cache), _cached(chat_cache)
    else:
        env_file, port = settings.maint_env_file, settings.maint_db_port
        rows = _load(rows_cache, lambda: fetch_rows(env_file, port))
        chat = _load(chat_cache, lambda: fetch_chat(env_file, port, settings.auto_cs_dir))
    data, gaps = build(settings.ontology_dir, settings.brand_context_dir, rows, chat)
    out = settings.ontology_dir / "build"
    out.mkdir(parents=True, exist_ok=True)
    (out / "instances.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (settings.ontology_dir / "빈_부분.md").write_text(gaps, encoding="utf-8")
    log.info("ontology: %s", data["counts"])


if __name__ == "__main__":
    main()
