"""REST integration for deterministic briefing and durable stage-entry wake delivery facts."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from panopticon.client import TaskServiceClient
from panopticon.core.models import Repo, Responsibility, Skill, Status
from panopticon.core.state import Complete, InitialState, State
from panopticon.core.workflow import Workflow
from panopticon.sessionservice.stage_entry_wake import StageEntryWaker
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore


class _Workflow(Workflow):
    name = "stage-entry-wake-test"

    class Waiting(InitialState):
        label = "WAITING"
        transitions = ("WORKING",)

    class Working(State):
        label = "WORKING"
        description = "Implement the approved change."
        responsibilities = (
            Responsibility(key="implementation", description="The implementation is complete."),
        )
        transitions = (Complete,)

    initial = Waiting

    def skills(self) -> Sequence[Skill]:
        return (Skill("do-work", "Implement this phase.", "Implement it."),)


class _Runner:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def submit_prompt(self, task_id: str, prompt: str) -> bool:
        self.prompts.append(prompt)
        return True


def _service(tmp_path: Path, store: SqlAlchemyStore) -> TaskService:
    service = TaskService(
        store,
        {_Workflow.name: _Workflow()},
        FilesystemArtifactStore(tmp_path),
    )
    asyncio.run(service.init())
    asyncio.run(service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git")))
    return service


def test_stage_entry_briefing_contains_recorded_phase_context_and_is_deterministic(
    tmp_path: Path,
) -> None:
    # 2119: REQ-027.2.1
    # 2119: REQ-027.2.2
    store = SqlAlchemyStore()
    service = _service(tmp_path, store)
    with TestClient(create_app(service)) as http:
        client = TaskServiceClient(http)
        task_id = client.create_task("r1", _Workflow.name)["id"]
        client.apply_operation(str(task_id), "advance")
        client.resolve_responsibility(str(task_id), "implementation", Status.MET)

        first = client.get_stage_entry_briefing(str(task_id), 1)
        second = client.get_stage_entry_briefing(str(task_id), 1)

    assert first == second
    assert first.splitlines()[0] == "You have entered WORKING."
    assert "Implement the approved change." in first
    assert "[met] implementation: The implementation is complete." in first
    assert "/do-work" in first


@pytest.mark.parametrize("entry_path", ["advance", "free-move"])
def test_live_agent_state_entry_is_submitted_on_the_next_runner_observation(
    tmp_path: Path, entry_path: str
) -> None:
    # 2119: REQ-027.1.1
    service = _service(tmp_path, SqlAlchemyStore())
    with TestClient(create_app(service)) as http:
        client = TaskServiceClient(http)
        task_id = str(client.create_task("r1", _Workflow.name)["id"])
        client.claim(task_id, "host-1")
        asyncio.run(service.register(task_id, "panopticon-t1", runner_id="host-1"))
        if entry_path == "advance":
            client.apply_operation(task_id, "advance")
        else:
            client.set_state(task_id, "WORKING")

        runner = _Runner()
        StageEntryWaker(client, runner, runner_id="host-1").wake(client.get_task(task_id))

    assert len(runner.prompts) == 1
    assert runner.prompts[0].startswith("You have entered WORKING.\n")


def test_rest_advance_to_live_agent_state_is_runner_delivered_and_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-027.5.1
    # 2119: REQ-027.5.2
    database_url = f"sqlite:///{tmp_path / 'stage-entry-wake.db'}"
    store = SqlAlchemyStore(database_url)
    service = _service(tmp_path, store)

    def forbid_control_plane_processes(
        *_args: object, **_kwargs: object
    ) -> subprocess.CompletedProcess:
        raise AssertionError("the task service must not invoke tmux or any external process")

    monkeypatch.setattr(subprocess, "run", forbid_control_plane_processes)

    with TestClient(create_app(service)) as http:
        client = TaskServiceClient(http)
        task_id = str(client.create_task("r1", _Workflow.name)["id"])
        client.claim(task_id, "host-1")
        asyncio.run(service.register(task_id, "panopticon-t1", runner_id="host-1"))

        advanced = client.apply_operation(task_id, "advance")
        assert advanced["history"][0]["wake_status"] == "skipped"
        assert advanced["history"][-1]["wake_status"] == "pending"  # transition only records intent

        runner = _Runner()
        assert runner.prompts == []  # no control-plane injection
        StageEntryWaker(client, runner).wake(client.get_task(task_id))

        delivered = client.get_task(task_id)
        assert runner.prompts[0].startswith("You have entered WORKING.\n")
        assert delivered["history"][0]["wake_status"] == "skipped"
        assert delivered["history"][-1]["wake_status"] == "delivered"

    # The delivered fact comes back through a new adapter and database connection.
    asyncio.run(store.close())
    reloaded_store = SqlAlchemyStore(database_url)
    asyncio.run(reloaded_store.init())
    reloaded = asyncio.run(reloaded_store.get_task(task_id))
    assert reloaded is not None
    assert reloaded.history[0].wake_status.value == "skipped"
    assert reloaded.history[-1].wake_status.value == "delivered"
    asyncio.run(reloaded_store.close())
