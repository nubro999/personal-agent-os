"""개념 파일과 동사 사전을 읽는다.

개념 하나당 마크다운 하나이고 관계는 앞머리(frontmatter)에 적는다. 관계 이름은 verbs.yaml에
정의된 동사만 쓸 수 있다.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConceptError(ValueError):
    pass


@dataclass(frozen=True)
class Relation:
    source: str
    target: str
    name: str
    confidence: str
    basis: str


@dataclass(frozen=True)
class Concept:
    id: str
    name: str
    group: str
    tier: int
    key: str
    owner: str
    body: str
    relations: tuple[Relation, ...]


@dataclass(frozen=True)
class Verb:
    name: str
    definition: str
    pairs: frozenset[tuple[str, str]]


def load_verbs(path: Path) -> dict[str, Verb]:
    try:
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        return {
            name: Verb(
                name,
                item["definition"],
                frozenset((a, b) for a, b in item.get("pairs") or []),
            )
            for name, item in data.items()
        }
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
        raise ConceptError(f"{path.name}: {exc}") from exc


def _parse(path: Path) -> Concept:
    text = path.read_text(encoding="utf-8")
    try:
        _, front, body = text.split("---\n", 2)
        meta: dict[str, Any] = yaml.safe_load(front)
        relations = tuple(
            Relation(meta["id"], r["to"], r["name"], r["confidence"], r.get("basis", ""))
            for r in meta.get("relations") or []
        )
        return Concept(
            id=meta["id"],
            name=meta["name"],
            group=meta["group"],
            tier=meta["tier"],
            key=meta["key"],
            owner=meta["owner"],
            body=body.strip(),
            relations=relations,
        )
    except (ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
        raise ConceptError(f"{path.name}: {exc}") from exc


def load_concepts(directory: Path, verbs: dict[str, Verb]) -> list[Concept]:
    concepts = [_parse(path) for path in sorted(directory.glob("*.md"))]
    ids = {concept.id for concept in concepts}
    for concept in concepts:
        for relation in concept.relations:
            if relation.target not in ids:
                raise ConceptError(f"{concept.id}: 없는 개념을 가리킴: {relation.target}")
            verb = verbs.get(relation.name)
            if verb is None:
                raise ConceptError(f"{concept.id}: 사전에 없는 동사: {relation.name}")
            if (relation.source, relation.target) not in verb.pairs:
                pair = f"{relation.source} {relation.name} {relation.target}"
                raise ConceptError(f"{concept.id}: 사전에 없는 짝: {pair}")
    return concepts
