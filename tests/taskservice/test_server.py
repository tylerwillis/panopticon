"""The runnable task-service server (`python -m panopticon.taskservice`).

Exercises the default control-plane wiring via :func:`build_app` over an in-process
``TestClient`` — no socket bound, no uvicorn, no LLM. Proves the process entry point produces
a working app backed by the built-in workflows.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from panopticon.taskservice.__main__ import build_app


def test_build_app_honors_production_auth_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # 2119: REQ-035.26.1
    # 2119: REQ-035.27.1
    config = tmp_path / "config"
    secrets = config / "secrets"
    secrets.mkdir(parents=True)
    token = "production-write-token-long"
    (secrets / "task-service-auth.json").write_text(
        json.dumps({"read": ["production-read-token-long"], "write": [token]})
    )
    (secrets / "task-service-auth.json").chmod(0o600)
    monkeypatch.setenv("PANOPTICON_CONFIG", str(config))
    monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_FILE", "task-service-auth.json")
    monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_MODE", "enforced")
    caplog.set_level(logging.INFO)

    app = build_app(
        db="sqlite://",
        artifacts_root=str(tmp_path / "artifacts"),
        _home_workflows=tmp_path / "empty-home-workflows",
    )
    with TestClient(app) as client:
        assert client.get("/tasks").status_code == 401
        assert client.get("/tasks", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert "task-service authentication mode: enforced" in caplog.text


def test_main_defaults_to_loopback_and_disables_raw_access_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-035.25.1
    import panopticon.taskservice.__main__ as server

    calls: list[dict[str, object]] = []
    monkeypatch.delenv("PANOPTICON_HOST", raising=False)
    monkeypatch.setattr(server, "user_data_dir", lambda: tmp_path)
    monkeypatch.setattr(server, "_migrate_legacy_to_home", lambda *_args: None)
    monkeypatch.setattr(server, "build_app", lambda **_kwargs: object())
    monkeypatch.setattr(
        server.uvicorn,
        "run",
        lambda _app, **kwargs: calls.append(kwargs),
    )

    server.main(["--db", "sqlite://"])
    assert calls == [{"host": "127.0.0.1", "port": 8000, "access_log": False}]
    monkeypatch.setenv("PANOPTICON_HOST", "100.64.0.10")
    server.main(["--db", "sqlite://"])
    assert calls[-1] == {"host": "100.64.0.10", "port": 8000, "access_log": False}


def test_production_composition_treats_empty_auth_reference_as_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-035.12.1
    monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_FILE", "")
    monkeypatch.delenv("PANOPTICON_SERVICE_AUTH_MODE", raising=False)

    app = build_app(
        db="sqlite://",
        artifacts_root=str(tmp_path / "artifacts"),
        _home_workflows=tmp_path / "empty-home-workflows",
    )

    with TestClient(app) as client:
        assert client.get("/tasks").status_code == 200


def test_production_launcher_does_not_persist_rejected_query_credentials(tmp_path: Path) -> None:
    # 2119: REQ-035.18.1
    # 2119: REQ-035.22.1
    config = tmp_path / "config"
    secrets = config / "secrets"
    secrets.mkdir(parents=True)
    query_token = "write-token-long"
    (secrets / "auth.json").write_text(
        json.dumps({"read": ["read-token-long"], "write": ["write-token-long"]})
    )
    (secrets / "auth.json").chmod(0o600)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {
        **os.environ,
        "PANOPTICON_CONFIG": str(config),
        "PANOPTICON_DATA": str(tmp_path / "data"),
        "PANOPTICON_SERVICE_AUTH_FILE": "auth.json",
        "PANOPTICON_SERVICE_AUTH_MODE": "enforced",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "panopticon.taskservice",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--db",
            f"sqlite:///{tmp_path / 'task.db'}",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/healthz").status_code == 200:
                    break
            except httpx.TransportError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        response = httpx.get(f"http://127.0.0.1:{port}/tasks", params={"token": query_token})
        assert response.status_code == 401
    finally:
        process.terminate()
        output, _ = process.communicate(timeout=10)
    assert query_token not in output


def test_build_app_warns_for_non_enforced_modes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # 2119: REQ-035.27.1
    caplog.set_level(logging.WARNING)
    build_app(
        db="sqlite://",
        artifacts_root=str(tmp_path / "artifacts"),
        _home_workflows=tmp_path / "empty-home-workflows",
    )
    assert "task-service authentication mode: disabled" in caplog.text
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "auth.json").write_text(
        json.dumps({"read": ["read-token-long"], "write": ["write-token-long"]})
    )
    (secrets / "auth.json").chmod(0o600)
    build_app(
        db="sqlite://",
        artifacts_root=str(tmp_path / "permissive-artifacts"),
        _home_workflows=tmp_path / "empty-home-workflows",
        auth_file="auth.json",
        auth_mode="permissive",
        secrets_dir=secrets,
    )
    assert "task-service authentication mode: permissive" in caplog.text


def test_auth_documentation_presents_enforced_as_steady_state() -> None:
    # 2119: REQ-035.27.1
    documentation = Path("docs/auth.md").read_text()
    steady_state, migration = documentation.split(
        "Roll a live fleet out without killing existing containers:", 1
    )
    assert "The required steady-state configuration is enforced mode:" in steady_state
    assert "PANOPTICON_SERVICE_AUTH_MODE=enforced" in steady_state
    assert "PANOPTICON_SERVICE_AUTH_MODE=permissive" not in steady_state
    assert "`permissive` mode" in migration


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
        "2119-auto-sol",
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
