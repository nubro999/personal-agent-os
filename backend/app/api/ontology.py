"""온톨로지 그래프 API. 개념층이 기본이고 사례는 눌렀을 때 펼친다."""

import json
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.ontology.concepts import Concept, Verb, load_concepts, load_verbs

router = APIRouter(prefix="/api/ontology")

SettingsDep = Annotated[Settings, Depends(get_settings)]
DEFAULT_LIMIT = 120
MAX_LIMIT = 500


class ConceptOut(BaseModel):
    id: str
    name: str
    group: str
    tier: int
    key: str
    owner: str
    count: int


class ConceptDetail(ConceptOut):
    body: str


class RelationOut(BaseModel):
    source: str
    target: str
    name: str
    confidence: str
    basis: str


class Overview(BaseModel):
    concepts: list[ConceptOut]
    relations: list[RelationOut]
    verbs: dict[str, str]
    generated_at: str | None


class InstanceNode(BaseModel):
    id: str
    label: str
    concept: str
    detail: str
    source: str
    degree: int
    real_key: str = ""


class InstanceLink(BaseModel):
    source: str
    target: str
    name: str
    confidence: str


class Expansion(BaseModel):
    nodes: list[InstanceNode]
    links: list[InstanceLink]
    total: int


@dataclass
class Instances:
    generated_at: str | None = None
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_concept: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    adjacent: dict[str, list[dict[str, str]]] = field(default_factory=lambda: defaultdict(list))


@lru_cache(maxsize=1)
def _load_instances(path: Path, _mtime: float) -> Instances:
    data = json.loads(path.read_text(encoding="utf-8"))
    loaded = Instances(generated_at=data.get("generated_at"))
    for node in data["nodes"]:
        loaded.nodes[node["id"]] = node
        loaded.by_concept[node["concept"]].append(node["id"])
    for link in data["links"]:
        loaded.adjacent[link["source"]].append(link)
        loaded.adjacent[link["target"]].append(link)
    for ids in loaded.by_concept.values():
        ids.sort(key=lambda node_id: -len(loaded.adjacent[node_id]))
    return loaded


def _instances(settings: Settings) -> Instances:
    path = settings.ontology_dir / "build" / "instances.json"
    if not path.is_file():
        return Instances()
    return _load_instances(path, path.stat().st_mtime)


def _verbs(settings: Settings) -> dict[str, Verb]:
    path = settings.ontology_dir / "verbs.yaml"
    if not path.is_file():
        raise HTTPException(503, "동사 사전이 없습니다")
    return load_verbs(path)


def _concepts(settings: Settings) -> list[Concept]:
    directory = settings.ontology_dir / "concepts"
    if not directory.is_dir():
        raise HTTPException(503, "온톨로지 폴더가 없습니다")
    return load_concepts(directory, _verbs(settings))


def _out(concept: Concept, instances: Instances) -> ConceptOut:
    return ConceptOut(
        id=concept.id,
        name=concept.name,
        group=concept.group,
        tier=concept.tier,
        key=concept.key,
        owner=concept.owner,
        count=len(instances.by_concept.get(concept.id, [])),
    )


def _node(loaded: Instances, node_id: str) -> InstanceNode:
    node = loaded.nodes[node_id]
    return InstanceNode(
        id=node["id"],
        label=node["label"],
        concept=node["concept"],
        detail=node.get("detail") or "",
        source=node.get("source") or "",
        degree=len(loaded.adjacent.get(node_id, [])),
        real_key=node.get("real_key") or "",
    )


@router.get("")
async def overview(settings: SettingsDep) -> Overview:
    loaded = _instances(settings)
    concepts = _concepts(settings)
    relations = [
        RelationOut(
            source=r.source, target=r.target, name=r.name, confidence=r.confidence, basis=r.basis
        )
        for concept in concepts
        for r in concept.relations
    ]
    return Overview(
        concepts=[_out(c, loaded) for c in concepts],
        relations=relations,
        verbs={name: verb.definition for name, verb in _verbs(settings).items()},
        generated_at=loaded.generated_at,
    )


@router.get("/gaps", response_class=PlainTextResponse)
async def gaps(settings: SettingsDep) -> str:
    path = settings.ontology_dir / "빈_부분.md"
    if not path.is_file():
        raise HTTPException(404, "아직 만들지 않았습니다")
    return path.read_text(encoding="utf-8")


@router.get("/concepts/{concept_id}")
async def concept_detail(concept_id: str, settings: SettingsDep) -> ConceptDetail:
    loaded = _instances(settings)
    for concept in _concepts(settings):
        if concept.id == concept_id:
            return ConceptDetail(**_out(concept, loaded).model_dump(), body=concept.body)
    raise HTTPException(404, "개념이 없습니다")


@router.get("/instances")
async def instances_of(
    concept: str,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> Expansion:
    """연결이 많은 사례부터 돌려준다."""
    loaded = _instances(settings)
    ids = loaded.by_concept.get(concept, [])
    chosen = set(ids[:limit])
    links = {
        (link["source"], link["target"], link["name"]): link
        for node_id in chosen
        for link in loaded.adjacent[node_id]
        if link["source"] in chosen and link["target"] in chosen
    }
    return Expansion(
        nodes=[_node(loaded, node_id) for node_id in ids[:limit]],
        links=[InstanceLink(**link) for link in links.values()],
        total=len(ids),
    )


@router.get("/neighbors")
async def neighbors(
    id: str,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> Expansion:
    loaded = _instances(settings)
    if id not in loaded.nodes:
        raise HTTPException(404, "사례가 없습니다")
    links = loaded.adjacent[id]
    return Expansion(
        nodes=[
            _node(loaded, node_id)
            for node_id in dict.fromkeys(
                link["target"] if link["source"] == id else link["source"] for link in links[:limit]
            )
        ],
        links=[InstanceLink(**link) for link in links[:limit]],
        total=len(links),
    )
