"""그래프 탭 데이터. 지금은 열린 Task를 프로젝트와 출처에 잇는다.

온톨로지가 생기면 같은 nodes, links 형식으로 내주면 화면은 그대로 쓴다.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.domain.tasks.service import list_tasks
from app.persistence.db import get_session
from app.persistence.models import Task

router = APIRouter(prefix="/api")

NodeKind = Literal["task", "project", "source"]
SOURCE_LABEL = {
    "natural_language": "직접 등록",
    "claude_code": "Claude Code",
    "slack": "Slack",
    "2325": "2325",
}


class GraphNode(BaseModel):
    id: str
    label: str
    kind: NodeKind
    status: str | None = None
    detail: str | None = None


class GraphLink(BaseModel):
    source: str
    target: str
    kind: str


class Graph(BaseModel):
    nodes: list[GraphNode]
    links: list[GraphLink]


def build_graph(tasks: list[Task]) -> Graph:
    nodes: dict[str, GraphNode] = {}
    links: list[GraphLink] = []
    for task in tasks:
        task_id = f"task:{task.id}"
        due = f"마감 {task.due_date.isoformat()}" if task.due_date else "마감 없음"
        nodes[task_id] = GraphNode(
            id=task_id, label=task.title, kind="task", status=task.status, detail=due
        )
        source_id = f"source:{task.source}"
        nodes.setdefault(
            source_id,
            GraphNode(
                id=source_id, label=SOURCE_LABEL.get(task.source, task.source), kind="source"
            ),
        )
        links.append(GraphLink(source=task_id, target=source_id, kind="from"))
        if task.project:
            project_id = f"project:{task.project}"
            nodes.setdefault(
                project_id, GraphNode(id=project_id, label=task.project, kind="project")
            )
            links.append(GraphLink(source=task_id, target=project_id, kind="in"))
    return Graph(nodes=list(nodes.values()), links=links)


@router.get("/graph")
async def graph(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Graph:
    return build_graph(await list_tasks(session, owner_id=settings.owner_id))
