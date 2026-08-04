from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from panopticon.core.models import Repo
from panopticon.core.state import Complete, InitialState
from panopticon.core.workflow import Workflow
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.terminal.dashboard import resolve_launch_selection
from panopticon.workflows import Spike


class _TunedWorkflow(Workflow):
    name = "tuned"
    default_harness = "codex"
    default_model = "gpt-5.6-sol:high"

    class Working(InitialState):
        label = "WORKING"
        transitions = (Complete,)

    initial = Working


# 2119: REQ-018.7.1
# 2119: REQ-018.12.1
# 2119: layered-settings-hints.6.1
def test_repo_and_workflow_endpoints_preserve_launch_default_fields(tmp_path: Path) -> None:
    service = TaskService(
        SqlAlchemyStore(),
        {"spike": Spike(), "tuned": _TunedWorkflow()},
        FilesystemArtifactStore(tmp_path),
    )
    asyncio.run(service.init())
    asyncio.run(
        service.create_repo(
            Repo(
                id="r1",
                name="acme/widgets",
                git_url="https://x/r1.git",
                default_harness="claude",
                default_model="opus:low",
            )
        )
    )

    with TestClient(create_app(service)) as api_client:
        repo = api_client.get("/repos/r1").json()
        workflows = {
            workflow["name"]: workflow for workflow in api_client.get("/repos/r1/workflows").json()
        }
        changed = api_client.patch(
            "/repos/r1",
            json={"default_harness": "codex", "default_model": "gpt-5.6-sol:medium"},
        )
        assert changed.status_code == 200, changed.text
        changed_repo = api_client.get("/repos/r1").json()

    assert (repo["default_harness"], repo["default_model"]) == ("claude", "opus:low")
    assert (workflows["tuned"]["default_harness"], workflows["tuned"]["default_model"]) == (
        "codex",
        "gpt-5.6-sol:high",
    )
    assert resolve_launch_selection(repo, workflows["spike"]).summary == (
        "claude · opus:low — set by repo default"
    )
    assert resolve_launch_selection(repo, workflows["tuned"]).summary == (
        "codex · gpt-5.6-sol:high — set by workflow default"
    )
    assert resolve_launch_selection(changed_repo, workflows["spike"]).summary == (
        "codex · gpt-5.6-sol:medium — set by repo default"
    )
