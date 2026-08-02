"""Production-boundary checks that span task service, runner, and integrated startup."""

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

from panopticon.taskservice import __main__ as taskservice_main
from panopticon.taskservice.__main__ import build_app
from panopticon.terminal import __main__ as terminal_cli


class _Completed:
    returncode = 1


def test_integrated_stack_explicitly_exposes_service_to_linux_containers() -> None:
    # 2119: REQ-034.29.1
    calls: list[list[str]] = []

    def record(args: list[str], **_kwargs: object) -> _Completed:
        calls.append(args)
        return _Completed()

    terminal_cli._start_sessions(run=record)
    service = next(call for call in calls if "new-session" in call and "service" in call)
    command = service[-1]
    assert " -m panopticon.taskservice --host 0.0.0.0 " in command


def test_standalone_service_retains_loopback_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-034.29.1
    calls: list[dict[str, object]] = []
    monkeypatch.delenv("PANOPTICON_HOST", raising=False)
    monkeypatch.setattr(taskservice_main, "user_data_dir", lambda: tmp_path)
    monkeypatch.setattr(taskservice_main, "_migrate_legacy_to_home", lambda *_args: None)
    monkeypatch.setattr(taskservice_main, "build_app", lambda **_kwargs: object())
    monkeypatch.setattr(
        taskservice_main.uvicorn,
        "run",
        lambda _app, **kwargs: calls.append(kwargs),
    )

    taskservice_main.main(["--db", "sqlite://"])
    assert calls[0]["host"] == "127.0.0.1"


def test_root_path_does_not_downgrade_write_only_routes(tmp_path: Path) -> None:
    # 2119: REQ-034.30.1
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "auth.json").write_text(
        json.dumps({"read": ["read-token"], "write": ["write-token"]})
    )
    app = build_app(
        db="sqlite://",
        artifacts_root=str(tmp_path / "artifacts"),
        auth_file="auth.json",
        auth_mode="enforced",
        secrets_dir=secrets,
        _home_workflows=tmp_path / "workflows",
    )
    with TestClient(app, root_path="/proxy") as client:
        # 2119: REQ-034.10.1
        assert client.get("/proxy/healthz").status_code == 200
        for method, path in [("GET", "/proxy/tasks/missing/live"), ("POST", "/proxy/mcp")]:
            reader = client.request(method, path, headers={"Authorization": "Bearer read-token"})
            writer = client.request(method, path, headers={"Authorization": "Bearer write-token"})
            assert reader.status_code == 401
            assert writer.status_code not in {401, 403}


def test_production_process_reports_enforced_mode(tmp_path: Path) -> None:
    # 2119: REQ-034.27.1
    config = tmp_path / "config"
    secrets = config / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "auth.json").write_text(
        json.dumps({"read": ["read-token"], "write": ["write-token"]})
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "panopticon.taskservice",
            "--port",
            str(port),
            "--db",
            f"sqlite:///{tmp_path / 'task.db'}",
        ],
        env={
            **os.environ,
            "PANOPTICON_CONFIG": str(config),
            "PANOPTICON_DATA": str(tmp_path / "data"),
            "PANOPTICON_SERVICE_AUTH_FILE": "auth.json",
            "PANOPTICON_SERVICE_AUTH_MODE": "enforced",
        },
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
    finally:
        process.terminate()
        output, _ = process.communicate(timeout=10)
    assert "task-service authentication mode: enforced" in output


def test_non_enforced_modes_log_warning_level(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # 2119: REQ-034.27.1
    caplog.set_level(logging.DEBUG)
    build_app(
        db="sqlite://",
        artifacts_root=str(tmp_path / "disabled-artifacts"),
        _home_workflows=tmp_path / "workflows",
    )
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "auth.json").write_text(
        json.dumps({"read": ["read-token"], "write": ["write-token"]})
    )
    build_app(
        db="sqlite://",
        artifacts_root=str(tmp_path / "permissive-artifacts"),
        auth_file="auth.json",
        auth_mode="permissive",
        secrets_dir=secrets,
        _home_workflows=tmp_path / "workflows",
    )
    mode_records = [
        record
        for record in caplog.records
        if "task-service authentication mode:" in record.getMessage()
    ]
    assert [
        (record.levelno, record.getMessage().rsplit(" ", 1)[-1]) for record in mode_records
    ] == [
        (logging.WARNING, "disabled"),
        (logging.WARNING, "permissive"),
    ]
