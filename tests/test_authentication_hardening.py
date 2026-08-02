"""Production-boundary checks that span task service, runner, and integrated startup."""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from panopticon.client import TaskServiceClient
from panopticon.taskservice import __main__ as taskservice_main
from panopticon.taskservice.__main__ import build_app
from panopticon.taskservice.auth import load_client_token
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
    service_argv = shlex.split(command.split(" 2>&1", 1)[0])
    assert service_argv.count("--host") == 1
    assert service_argv[service_argv.index("--host") + 1] == "0.0.0.0"


def test_integrated_sessions_pin_current_auth_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 2119: REQ-034.31.1
    monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_FILE", "current-auth.json")
    monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_MODE", "enforced")
    monkeypatch.setenv("PANOPTICON_CONFIG", "/current/config")
    calls: list[list[str]] = []

    def record(args: list[str], **_kwargs: object) -> _Completed:
        calls.append(args)
        return _Completed()

    terminal_cli._start_sessions(run=record)
    commands = {call[call.index("-s") + 1]: call[-1] for call in calls if "new-session" in call}
    assert set(commands) == {"service", "runner"}
    for command in commands.values():
        assert "-u PANOPTICON_SERVICE_AUTH_FILE" in command
        assert "-u PANOPTICON_SERVICE_AUTH_MODE" in command
        assert "-u PANOPTICON_CONFIG" in command
        assert "PANOPTICON_SERVICE_AUTH_FILE=current-auth.json" in command
        assert "PANOPTICON_SERVICE_AUTH_MODE=enforced" in command
        assert "PANOPTICON_CONFIG=/current/config" in command

    for name in [
        "PANOPTICON_SERVICE_AUTH_FILE",
        "PANOPTICON_SERVICE_AUTH_MODE",
        "PANOPTICON_CONFIG",
    ]:
        monkeypatch.delenv(name)
    calls.clear()
    terminal_cli._start_sessions(run=record)
    cleared_commands = {
        call[call.index("-s") + 1]: call[-1] for call in calls if "new-session" in call
    }
    assert set(cleared_commands) == {"service", "runner"}
    for command in cleared_commands.values():
        assert "-u PANOPTICON_SERVICE_AUTH_FILE" in command
        assert "-u PANOPTICON_SERVICE_AUTH_MODE" in command
        assert "-u PANOPTICON_CONFIG" in command
        assert "PANOPTICON_SERVICE_AUTH_FILE=" not in command
        assert "PANOPTICON_SERVICE_AUTH_MODE=" not in command
        assert "PANOPTICON_CONFIG=" not in command


@pytest.mark.skipif(not shutil.which("tmux"), reason="needs tmux")
def test_session_command_replaces_stale_real_tmux_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-034.31.1
    socket_name = f"pan-auth-{os.getpid()}"
    output = tmp_path / "environment.json"
    subprocess.run(
        ["tmux", "-L", socket_name, "new-session", "-d", "-s", "keeper", "sleep 30"],
        check=True,
    )
    try:
        names = [
            "PANOPTICON_SERVICE_AUTH_FILE",
            "PANOPTICON_SERVICE_AUTH_MODE",
            "PANOPTICON_CONFIG",
        ]
        for name in names:
            subprocess.run(
                ["tmux", "-L", socket_name, "set-environment", "-g", name, f"stale-{name}"],
                check=True,
            )
        monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_FILE", "fresh-auth.json")
        monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_MODE", "enforced")
        monkeypatch.delenv("PANOPTICON_CONFIG", raising=False)
        script = (
            "import json, os; "
            f"open({str(output)!r}, 'w').write(json.dumps({{name: os.environ.get(name) "
            f"for name in {names!r}}}))"
        )
        command = terminal_cli._session_command(
            f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
        )
        subprocess.run(
            ["tmux", "-L", socket_name, "new-window", "-t", "keeper", command],
            check=True,
        )
        deadline = time.monotonic() + 5
        while not output.exists():
            if time.monotonic() >= deadline:
                raise AssertionError("tmux child did not record its environment")
            time.sleep(0.05)
        assert json.loads(output.read_text()) == {
            "PANOPTICON_SERVICE_AUTH_FILE": "fresh-auth.json",
            "PANOPTICON_SERVICE_AUTH_MODE": "enforced",
            "PANOPTICON_CONFIG": None,
        }
        cleared_output = tmp_path / "cleared-environment.json"
        monkeypatch.delenv("PANOPTICON_SERVICE_AUTH_FILE")
        monkeypatch.delenv("PANOPTICON_SERVICE_AUTH_MODE")
        cleared_script = (
            "import json, os; "
            f"open({str(cleared_output)!r}, 'w').write(json.dumps({{name: os.environ.get(name) "
            f"for name in {names!r}}}))"
        )
        cleared_command = terminal_cli._session_command(
            f"{shlex.quote(sys.executable)} -c {shlex.quote(cleared_script)}"
        )
        subprocess.run(
            ["tmux", "-L", socket_name, "new-window", "-t", "keeper", cleared_command],
            check=True,
        )
        deadline = time.monotonic() + 5
        while not cleared_output.exists():
            if time.monotonic() >= deadline:
                raise AssertionError("tmux child did not record its cleared environment")
            time.sleep(0.05)
        assert json.loads(cleared_output.read_text()) == dict.fromkeys(names)
    finally:
        subprocess.run(["tmux", "-L", socket_name, "kill-server"], check=False)


def test_overlap_clients_select_the_appended_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-034.32.1
    credential = tmp_path / "auth.json"
    credential.write_text(
        json.dumps({"read": ["oldest-read", "old-read"], "write": ["oldest", "old"]})
    )
    assert load_client_token(credential.name, privilege="write", secrets_dir=tmp_path) == "old"
    credential.write_text(
        json.dumps(
            {
                "read": ["oldest-read", "old-read", "new-read"],
                "write": ["oldest", "old", "new"],
            }
        )
    )
    restarted_token = load_client_token(credential.name, privilege="write", secrets_dir=tmp_path)
    assert restarted_token == "new"
    monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_FILE", str(credential))
    overlap_app = build_app(
        db="sqlite://",
        artifacts_root=str(tmp_path / "overlap-artifacts"),
        auth_file=credential.name,
        auth_mode="enforced",
        secrets_dir=tmp_path,
        _home_workflows=tmp_path / "workflows",
    )
    with TestClient(overlap_app) as http:
        restarted_python = TaskServiceClient(http)
        assert restarted_python.list_tasks() == []

    recorded = tmp_path / "curl-config"
    task_lib = Path("src/panopticon/sessionservice/task_lib.sh").read_text()
    shell = f"""curl() {{ cat > {shlex.quote(str(recorded))}; }}
{task_lib}
_panopticon_curl --silent http://service
"""
    subprocess.run(
        ["sh", "-c", shell],
        env={
            "PATH": "/usr/bin:/bin",
            "PANOPTICON_PYTHON": sys.executable,
            "PANOPTICON_SERVICE_AUTH_FILE": str(credential),
        },
        check=True,
    )
    assert recorded.read_text() == 'header = "Authorization: Bearer new"\n'
    shell_token = recorded.read_text().split("Bearer ", 1)[1].split('"', 1)[0]

    credential.write_text(json.dumps({"read": ["new-read"], "write": ["new"]}))
    converged_app = build_app(
        db="sqlite://",
        artifacts_root=str(tmp_path / "converged-artifacts"),
        auth_file=credential.name,
        auth_mode="enforced",
        secrets_dir=tmp_path,
        _home_workflows=tmp_path / "workflows",
    )
    with TestClient(converged_app) as http:
        assert TaskServiceClient(http, token=restarted_token).list_tasks() == []
        assert TaskServiceClient(http, token=shell_token).list_tasks() == []


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
        for method, path in [("GET", "/proxy/tasks/missing/live"), ("GET", "/proxy/mcp")]:
            reader = client.request(method, path, headers={"Authorization": "Bearer read-token"})
            writer = client.request(method, path, headers={"Authorization": "Bearer write-token"})
            assert reader.status_code == 401
            assert writer.status_code not in {401, 403}
    root_app = build_app(
        db="sqlite://",
        artifacts_root=str(tmp_path / "root-artifacts"),
        auth_file="auth.json",
        auth_mode="enforced",
        secrets_dir=secrets,
        _home_workflows=tmp_path / "workflows",
    )
    with TestClient(root_app, root_path="/") as client:
        for path in ["/tasks/missing/live", "/mcp"]:
            assert (
                client.get(path, headers={"Authorization": "Bearer read-token"}).status_code == 401
            )


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
