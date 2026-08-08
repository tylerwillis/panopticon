"""RFC 2119 coverage for guarded repository deletion."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from panopticon.core.models import Repo
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    store = SqlAlchemyStore()
    service = TaskService(
        store,
        {"spike": Spike()},
        FilesystemArtifactStore(tmp_path),
    )
    asyncio.run(service.init())

    async def foreign_keys_enabled() -> int:
        async with store._engine.connect() as connection:
            return int((await connection.execute(text("PRAGMA foreign_keys"))).scalar_one())

    assert asyncio.run(foreign_keys_enabled()) == 0
    asyncio.run(service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1")))
    asyncio.run(service.create_repo(Repo(id="r2", name="acme/other", git_url="https://x/r2")))
    with TestClient(create_app(service)) as test_client:
        yield test_client


def _new_task(client: TestClient) -> str:
    return _new_task_for_repo(client, "r1")


def _new_task_for_repo(client: TestClient, repo_id: str) -> str:
    response = client.post("/tasks", json={"repo_id": repo_id, "workflow": "spike"})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


# 2119: repo-deletion.1.1
# 2119: repo-deletion.1.3
def test_delete_unreferenced_repo_leaves_no_orphaned_tasks(client: TestClient) -> None:
    task_id = _new_task(client)
    created = client.post(
        "/repos",
        json={"id": "throwaway", "name": "acme/throwaway", "git_url": "https://x/tmp"},
    )
    assert created.status_code == 201, created.text

    deleted = client.delete("/repos/throwaway")

    assert deleted.status_code == 204, deleted.text
    assert client.get("/repos/throwaway").status_code == 404
    tasks = client.get("/tasks").json()
    assert [task["id"] for task in tasks] == [task_id]
    assert all(task["repo_id"] != "throwaway" for task in tasks)


# 2119: repo-deletion.1.1
# 2119: repo-deletion.1.2
# 2119: repo-deletion.1.3
@pytest.mark.parametrize("reference_count", [1, 2, 3])
def test_delete_repo_refuses_real_referencing_task_and_preserves_history(
    client: TestClient, reference_count: int
) -> None:
    task_ids = [_new_task(client) for _ in range(reference_count)]
    completed = client.put(f"/tasks/{task_ids[0]}/state", json={"state": "COMPLETE"})
    assert completed.status_code == 200, completed.text
    assert completed.json()["terminal"] is True
    unrelated_task = _new_task_for_repo(client, "r2")
    before_repos = client.get("/repos").json()
    before_tasks = client.get("/tasks").json()
    all_task_ids = [*task_ids, unrelated_task]
    before_task_details = {
        task_id: client.get(f"/tasks/{task_id}").json() for task_id in all_task_ids
    }

    deleted = client.delete("/repos/r1")

    assert deleted.status_code == 409, deleted.text
    noun = "task" if reference_count == 1 else "tasks"
    assert deleted.json()["detail"] == f"repo 'r1' is referenced by {reference_count} {noun}"
    assert client.get("/repos").json() == before_repos
    assert client.get("/tasks").json() == before_tasks
    assert {
        task_id: client.get(f"/tasks/{task_id}").json() for task_id in all_task_ids
    } == before_task_details
    assert client.get(f"/tasks/{unrelated_task}").json()["repo_id"] == "r2"
    assert all(client.get(f"/tasks/{task_id}").json()["repo_id"] == "r1" for task_id in task_ids)


# 2119: repo-deletion.1.4
def test_delete_unknown_repo_is_404_and_changes_nothing(client: TestClient) -> None:
    task_id = _new_task(client)
    before_repos = client.get("/repos").json()
    before_tasks = client.get("/tasks").json()
    before_task = client.get(f"/tasks/{task_id}").json()  # includes the full history

    deleted = client.delete("/repos/missing")

    assert deleted.status_code == 404, deleted.text
    assert client.get("/repos").json() == before_repos
    assert client.get("/tasks").json() == before_tasks
    assert client.get(f"/tasks/{task_id}").json() == before_task
