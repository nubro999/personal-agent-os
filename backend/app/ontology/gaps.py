"""온톨로지 빈 곳을 모은다. 구축 에이전트가 이것을 하나씩 받아 후보를 낸다.

출처 넷:
- 시험 에이전트의 missing: 검증 결과의 "온톨로지에 없던 것". 건마다 가장 최근 회차만 쓴다.
- 빈_부분.md: 빌드가 만드는 사례 없는 개념, 확정 못 한 관계, 데이터로 입증 못 한 관계, 임시 키.
- 업무.md: 업무 10개를 덮으려고 적어 둔 새 개념 후보.
- 내가 직접 준 주제.

id는 출처와 글로 만든 짧은 해시라 다시 모아도 같다.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

MISSING_HEADER = "**온톨로지에 없던 것**"
CASE_FILE = re.compile(r"(\d{4}-\d{2}-\d{2})_(CS-\d+)\.md$")
# 빈_부분.md에서 읽는 절. 표 첫 칸이 빈 곳의 이름이다.
EMPTY_SECTIONS = ("사례가 없는 개념", "확정 못 한 관계", "임시 키")
UNPROVEN_SECTION = "사례로 확인되지 않은 관계"
WORK_SECTION = "새 개념 후보"
# 출처 순서가 곧 우선순위다. 시험에서 실제로 막힌 곳이 먼저다.
PRIORITY = {"topic": 0, "missing": 1, "empty": 2, "work": 3}


@dataclass(frozen=True)
class Gap:
    id: str
    source: str  # topic, missing, empty, work
    text: str
    where: str  # 어디서 나왔나 (파일, 절, 건)


def gap_id(source: str, text: str) -> str:
    digest = hashlib.sha1(f"{source}\n{text}".encode()).hexdigest()[:6]
    return f"{source[0]}{digest}"


def _gap(source: str, text: str, where: str) -> Gap:
    return Gap(gap_id(source, text), source, text, where)


def _sections(markdown: str) -> dict[str, list[str]]:
    """## 제목 -> 그 절의 줄."""
    found: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in markdown.splitlines():
        if line.startswith("## "):
            current = found.setdefault(line[3:].strip(), [])
        elif current is not None:
            current.append(line)
    return found


def _table_rows(lines: list[str]) -> list[list[str]]:
    """마크다운 표의 본문 행. 머리행과 구분선은 뺀다."""
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in lines
        if line.strip().startswith("|")
    ]
    return [r for r in rows[1:] if not all(set(c) <= set("-: ") for c in r)]


def missing_items(markdown: str) -> list[str]:
    """시험 결과 한 건의 "온톨로지에 없던 것" 항목."""
    lines = markdown.splitlines()
    try:
        start = lines.index(MISSING_HEADER)
    except ValueError:
        return []
    items: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("- "):
            items.append(line[2:].strip())
        elif line.strip() and items:
            break
    return items


def latest_trials(trial_dir: Path) -> dict[str, Path]:
    """건 -> 가장 최근 시험 결과 파일. 같은 날이면 회차 폴더 이름이 큰 쪽이다."""
    latest: dict[str, tuple[tuple[str, str], Path]] = {}
    for path in trial_dir.rglob("*.md"):
        found = CASE_FILE.search(path.name)
        if not found:
            continue
        day, case = found.groups()
        rank = (day, path.parent.name if path.parent != trial_dir else "~")
        if case not in latest or rank > latest[case][0]:
            latest[case] = (rank, path)
    return {case: path for case, (_, path) in sorted(latest.items())}


def from_trials(trial_dir: Path) -> list[Gap]:
    gaps: list[Gap] = []
    for case, path in latest_trials(trial_dir).items():
        where = f"{case} ({path.parent.name}/{path.name})"
        gaps += [_gap("missing", item, where) for item in missing_items(path.read_text())]
    return gaps


def from_empty(markdown: str) -> list[Gap]:
    sections = _sections(markdown)
    gaps: list[Gap] = []
    for title in EMPTY_SECTIONS:
        for row in _table_rows(sections.get(title, [])):
            text = " / ".join(cell for cell in row if cell)
            gaps.append(_gap("empty", f"{title}: {text}", f"빈_부분.md {title}"))
    for line in sections.get(UNPROVEN_SECTION, []):
        if line.startswith("- "):
            text = f"{UNPROVEN_SECTION}: {line[2:].strip()}"
            gaps.append(_gap("empty", text, f"빈_부분.md {UNPROVEN_SECTION}"))
    return gaps


def from_work(markdown: str) -> list[Gap]:
    rows = _table_rows(_sections(markdown).get(WORK_SECTION, []))
    return [
        _gap("work", " / ".join(cell for cell in row if cell), f"업무.md {WORK_SECTION}")
        for row in rows
    ]


def collect(root: Path) -> list[Gap]:
    """_ontology 폴더에서 빈 곳을 모은다. 같은 글은 한 번만."""
    gaps = from_trials(root / "검증")
    for name, reader in (("빈_부분.md", from_empty), ("업무.md", from_work)):
        path = root / name
        if path.exists():
            gaps += reader(path.read_text())
    unique = {g.id: g for g in gaps}
    return sorted(unique.values(), key=lambda g: PRIORITY[g.source])


def topic(text: str) -> Gap:
    return _gap("topic", text.strip(), "직접 준 주제")
