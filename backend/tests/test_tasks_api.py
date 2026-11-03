import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.config import get_settings
from app.domain.tasks.service import Evidence, NewTask, create_task, suggest_completion
from app.persistence.db import get_sessionmaker
from app.persistence.models import ReplyDraft, Task, TaskEvent, TaskFieldEvidence, TaskSuggestion


@pytest.fixture
def owner(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    owner_id = f"test-{uuid.uuid4()}"
    monkeypatch.setattr(get_settings(), "owner_id", owner_id)
    yield owner_id


@pytest.fixture(autouse=True)
def no_slack_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """초안 목록을 읽으면 Slack을 보러 간다. 시험에서는 부르지 않는다."""
    from app.api import tasks as tasks_api

    async def nothing() -> int:
        return 0

    monkeypatch.setattr(tasks_api, "sweep_sent_drafts", nothing)


@pytest.fixture(autouse=True)
async def cleanup(owner: str) -> Any:
    yield
    async with get_sessionmaker().begin() as session:
        await session.execute(delete(ReplyDraft).where(ReplyDraft.owner_id == owner))
        ids = select(Task.id).where(Task.owner_id == owner).scalar_subquery()
        for model in (TaskSuggestion, TaskFieldEvidence, TaskEvent):
            await session.execute(delete(model).where(model.task_id.in_(ids)))
        await session.execute(delete(Task).where(Task.owner_id == owner))


async def _seed(owner: str) -> uuid.UUID:
    async with get_sessionmaker().begin() as session:
        result = await create_task(
            session,
            owner_id=owner,
            request_id="seed",
            actor="t",
            new=NewTask(title=Evidence("보고서", "explicit")),
        )
        await suggest_completion(
            session,
            owner_id=owner,
            task_id=result.task.id,
            source="slack",
            source_reference="r",
            explanation="보고함",
        )
    return result.task.id


async def test_lists_open_tasks_with_suggestion(client: AsyncClient, owner: str) -> None:
    task_id = await _seed(owner)

    response = await client.get("/api/tasks")

    assert response.status_code == 200
    [task] = response.json()
    assert (task["id"], task["title"], task["suggestion"]) == (str(task_id), "보고서", "보고함")


async def test_complete_then_retry_and_stale_version(client: AsyncClient, owner: str) -> None:
    task_id = await _seed(owner)
    url = f"/api/tasks/{task_id}/status"
    body = {"status": "done", "version": 1, "request_id": "click-1"}

    first = await client.post(url, json=body)
    retry = await client.post(url, json=body)
    stale = await client.post(url, json=body | {"request_id": "click-2", "status": "todo"})

    assert (first.status_code, first.json()["status"]) == (200, "done")
    assert (retry.status_code, retry.json()["version"]) == (200, 2)
    assert stale.status_code == 409
    assert (await client.get("/api/tasks")).json() == []


async def test_unknown_task_is_404(client: AsyncClient, owner: str) -> None:
    body = {"status": "done", "version": 1, "request_id": "x"}

    response = await client.post(f"/api/tasks/{uuid.uuid4()}/status", json=body)

    assert response.status_code == 404


async def test_dismiss_suggestion_keeps_task(client: AsyncClient, owner: str) -> None:
    task_id = await _seed(owner)

    response = await client.post(f"/api/tasks/{task_id}/suggestion/dismiss")

    assert response.json() == {"dismissed": 1}
    [task] = (await client.get("/api/tasks")).json()
    assert (task["status"], task["suggestion"]) == ("todo", None)


async def test_spa_serves_index_for_app_routes_but_not_unknown_api(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "index.html").write_text("<title>Personal OS</title>")
    (tmp_path / "icon.svg").write_text("<svg/>")
    monkeypatch.setattr(get_settings(), "web_dist_dir", tmp_path)

    assert "Personal OS" in (await client.get("/")).text
    assert "Personal OS" in (await client.get("/graph")).text
    assert (await client.get("/icon.svg")).text == "<svg/>"
    assert (await client.get("/api/nope")).status_code == 404
    assert "Personal OS" in (await client.get("/..%2f..%2fetc%2fpasswd")).text


async def test_lists_and_dismisses_drafts(client: AsyncClient, owner: str) -> None:
    task_id = await _seed(owner)
    async with get_sessionmaker().begin() as session:
        session.add(
            ReplyDraft(
                owner_id=owner,
                reference="slack:C1:1.0",
                channel="C1",
                thread_ts="1.0",
                task_id=task_id,
                marker="2.0",
                draft="넵 확인하겠습니다",
            )
        )

    [draft] = (await client.get("/api/drafts")).json()

    assert (draft["task_title"], draft["draft"]) == ("보고서", "넵 확인하겠습니다")
    assert draft["slack_url"].endswith("/C1/thread/C1-1.0")
    assert (await client.post(f"/api/drafts/{draft['id']}/dismiss")).status_code == 200
    assert (await client.get("/api/drafts")).json() == []
    assert (await client.post(f"/api/drafts/{uuid.uuid4()}/dismiss")).status_code == 404


async def test_advice_queues_a_rewrite(
    client: AsyncClient, owner: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import tasks as tasks_api

    queued: list[uuid.UUID] = []

    async def fake_rewrite(draft_id: uuid.UUID) -> None:
        queued.append(draft_id)

    monkeypatch.setattr(tasks_api, "rewrite_draft", fake_rewrite)
    async with get_sessionmaker().begin() as session:
        session.add(
            ReplyDraft(
                owner_id=owner,
                reference="slack:C1:1.0",
                channel="C1",
                thread_ts="1.0",
                marker="2.0",
                draft="넵 확인하겠습니다",
            )
        )
    [draft] = (await client.get("/api/drafts")).json()

    url = f"/api/drafts/{draft['id']}/advice"
    assert (await client.post(url, json={"text": " "})).status_code == 422
    assert (await client.post(url, json={"text": "사유를 적어"})).status_code == 200
    assert (await client.post(url, json={"text": "짧게"})).status_code == 200

    [after] = (await client.get("/api/drafts")).json()
    assert after["advice"] == ["사유를 적어", "짧게"]
    assert (after["rewriting"], after["notes"]) == (True, [])
    assert [str(i) for i in queued] == [draft["id"], draft["id"]]


async def test_send_reports_why_it_did_not_send(
    client: AsyncClient, owner: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import tasks as tasks_api
    from app.trackers.slack import SendError

    async def refuse(draft_id: uuid.UUID, text: str) -> str:
        raise SendError("이미 답한 스레드라 보내지 않았다")

    async def accept(draft_id: uuid.UUID, text: str) -> str:
        return f"<@U2> {text}"

    url = f"/api/drafts/{uuid.uuid4()}/send"
    monkeypatch.setattr(tasks_api, "send_draft", refuse)
    response = await client.post(url, json={"text": "처리했습니다"})
    assert (response.status_code, response.json()["detail"]) == (
        409,
        "이미 답한 스레드라 보내지 않았다",
    )

    monkeypatch.setattr(tasks_api, "send_draft", accept)
    assert (await client.post(url, json={"text": "처리했습니다"})).json() == {
        "sent": "<@U2> 처리했습니다"
    }
