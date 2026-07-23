"""The runnable task-service server (`python -m panopticon.taskservice`).

Exercises the default control-plane wiring via :func:`build_app` over an in-process
``TestClient`` — no socket bound, no uvicorn, no LLM. Proves the process entry point produces
a working app backed by the built-in workflows.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from panopticon.taskservice.__main__ import build_app


def _workflow_source(
    *, name: str = "custom", class_name: str = "Custom", label: str = "ONLY", when: str = "first"
) -> str:
    return f'''\
from typing import ClassVar

from panopticon.core.state import Complete, InitialState
from panopticon.core.workflow import Workflow


class {class_name}(Workflow):
    name: ClassVar[str] = "{name}"
    when_to_use: ClassVar[str] = "{when}"

    class Only(InitialState):
        label = "{label}"
        transitions = (Complete,)

    initial = Only
'''


def test_build_app_serves_default_wiring(tmp_path: Path) -> None:
    app = build_app(
        db="sqlite://",
        artifacts_root=str(tmp_path),
        _home_workflows=tmp_path / "empty-home-workflows",
    )  # in-memory DB; tmp artifacts
    client = TestClient(app)

    assert client.get("/healthz").json() == {"status": "ok"}
    # setup-repo is hidden → absent from /workflows (the menu source); the rest are shown.
    assert {w["name"] for w in client.get("/workflows").json()} == {
        "spike",
        "2119-auto-spec",
        "2119-human-spec",
        "github-peer-reviewed",
        "github-self-reviewed",
        "local-git-self-reviewed",
        "orchestrator",
    }


def test_build_app_includes_workflow_from_configured_home(tmp_path: Path) -> None:
    home_workflows = tmp_path / "config" / "workflows"
    home_workflows.mkdir(parents=True)
    (home_workflows / "custom.py").write_text(_workflow_source())
    app = build_app(
        db="sqlite://",
        artifacts_root=str(tmp_path / "artifacts"),
        _home_workflows=home_workflows,
    )

    with TestClient(app) as client:
        assert "custom" in {item["name"] for item in client.get("/workflows").json()}


def test_runtime_workflow_is_listed_by_both_endpoints_and_creatable(tmp_path: Path) -> None:
    workflows = tmp_path / "workflows"
    app = build_app(
        db="sqlite://",
        artifacts_root=str(tmp_path / "artifacts"),
        workflows_path=str(workflows),
        _home_workflows=tmp_path / "empty-home-workflows",
    )

    with TestClient(app) as client:
        response = client.post(
            "/repos", json={"id": "r1", "name": "widgets", "git_url": "https://x/r1.git"}
        )
        assert response.status_code == 201
        workflows.mkdir()
        (workflows / "custom.py").write_text(_workflow_source())

        assert "custom" in {item["name"] for item in client.get("/workflows").json()}
        assert "custom" in {item["name"] for item in client.get("/workflow-files").json()}
        response = client.post("/tasks", json={"repo_id": "r1", "workflow": "custom"})
        assert response.status_code == 201
        assert response.json()["state"] == "ONLY"


def test_runtime_workflow_is_creatable_without_a_prior_list(tmp_path: Path) -> None:
    workflows = tmp_path / "workflows"
    app = build_app(
        db="sqlite://",
        artifacts_root=str(tmp_path / "artifacts"),
        workflows_path=str(workflows),
        _home_workflows=tmp_path / "empty-home-workflows",
    )

    with TestClient(app) as client:
        response = client.post(
            "/repos", json={"id": "r1", "name": "widgets", "git_url": "https://x/r1.git"}
        )
        assert response.status_code == 201
        workflows.mkdir()
        (workflows / "custom.py").write_text(_workflow_source())

        response = client.post("/tasks", json={"repo_id": "r1", "workflow": "custom"})
        assert response.status_code == 201
        assert response.json()["state"] == "ONLY"


def test_runtime_rescan_does_not_replace_a_registered_workflow(tmp_path: Path) -> None:
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    module = workflows / "custom.py"
    module.write_text(_workflow_source())
    app = build_app(
        db="sqlite://",
        artifacts_root=str(tmp_path / "artifacts"),
        workflows_path=str(workflows),
        _home_workflows=tmp_path / "empty-home-workflows",
    )

    with TestClient(app) as client:
        module.write_text(_workflow_source(label="CHANGED", when="second"))
        custom = next(item for item in client.get("/workflows").json() if item["name"] == "custom")
        assert custom["when_to_use"] == "first"


def test_runtime_duplicate_name_does_not_crash_workflow_lists(tmp_path: Path) -> None:
    workflows = tmp_path / "workflows"
    app = build_app(
        db="sqlite://",
        artifacts_root=str(tmp_path / "artifacts"),
        workflows_path=str(workflows),
        _home_workflows=tmp_path / "empty-home-workflows",
    )

    with TestClient(app) as client:
        workflows.mkdir()
        (workflows / "duplicate.py").write_text(
            _workflow_source(name="spike", class_name="DuplicateSpike")
        )
        assert client.get("/workflows").status_code == 200
        assert client.get("/workflow-files").status_code == 200
