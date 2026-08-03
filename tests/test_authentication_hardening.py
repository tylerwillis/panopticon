"""Production-boundary checks that span task service, runner, and integrated startup."""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from panopticon.client import TaskServiceClient
from panopticon.core.models import LifecyclePhase
from panopticon.sessionservice.local_runner import LocalRunner
from panopticon.sessionservice.shell_runner import _TASK_LIB, ShellRunner
from panopticon.sessionservice.spawner import Spawner
from panopticon.taskservice import __main__ as taskservice_main
from panopticon.taskservice.__main__ import build_app
from panopticon.taskservice.api import _redact_stream_chunk
from panopticon.taskservice.auth import load_client_token, load_tokens
from panopticon.terminal import __main__ as terminal_cli


class _Completed:
    returncode = 1


def test_mcp_redaction_masks_tokens_across_every_chunk_boundary() -> None:
    tokens = (b"prefix-secret-token-next", b"prefix-secret-token")
    plaintext = b"before:prefix-secret-token-next:prefix-secret-token:after"
    expected = b"before:" + b"*" * 24 + b":" + b"*" * 19 + b":after"
    for split in range(len(plaintext) + 1):
        first, pending = _redact_stream_chunk(plaintext[:split], configured=tokens, more_body=True)
        second, pending = _redact_stream_chunk(
            plaintext[split:], configured=tokens, pending=pending, more_body=False
        )
        assert first + second == expected
        assert pending == b""


def test_shell_library_hides_token_from_xtrace(tmp_path: Path) -> None:
    # 2119: REQ-035.38.1
    credential = tmp_path / "auth.json"
    credential.write_text(json.dumps({"read": ["private-reader"], "write": ["trace-secret"]}))
    credential.chmod(0o600)
    argv = tmp_path / "argv"
    shell = f"""curl() {{ cat >/dev/null; printf '%s\\n' "$@" > {argv}; }}
{_TASK_LIB}
set -x
panopticon_advance
"""
    completed = subprocess.run(
        ["sh", "-c", shell],
        env={
            "PATH": "/usr/bin:/bin",
            "PANOPTICON_PYTHON": sys.executable,
            "PANOPTICON_SERVICE_AUTH_FILE": str(credential),
            "PANOPTICON_SERVICE_URL": "http://service",
            "PANOPTICON_TASK_ID": "task",
        },
        text=True,
        capture_output=True,
        check=True,
    )
    assert "trace-secret" not in completed.stderr
    assert "trace-secret" not in argv.read_text()
    assert argv.read_text().splitlines()[:4] == ["--disable", "--noproxy", "*", "--config"]


def test_host_validates_auth_before_docker_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-035.28.1
    from panopticon.sessionservice import host

    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))
    monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_FILE", "missing.json")
    preflight_called = False

    def preflight(_surface: str) -> str | None:
        nonlocal preflight_called
        preflight_called = True
        return None

    monkeypatch.setattr(host.docker_daemon, "preflight_message", preflight)
    with pytest.raises(ValueError, match="authentication credential"):
        host.main([])
    assert not preflight_called


@pytest.mark.skipif(not shutil.which("curl"), reason="needs curl")
def test_shell_library_ignores_hostile_curlrc(tmp_path: Path) -> None:
    credential = tmp_path / "auth.json"
    credential.write_text(json.dumps({"read": ["private-reader"], "write": ["curlrc-secret"]}))
    credential.chmod(0o600)
    curl_home = tmp_path / "curl-home"
    curl_home.mkdir()
    (curl_home / ".curlrc").write_text("verbose\n")
    completed = subprocess.run(
        [
            "sh",
            "-c",
            _TASK_LIB + "\n_panopticon_curl --connect-timeout 0.1 http://127.0.0.1:9 >/dev/null\n",
        ],
        env={
            "PATH": "/usr/bin:/bin",
            "CURL_HOME": str(curl_home),
            "PANOPTICON_PYTHON": sys.executable,
            "PANOPTICON_SERVICE_AUTH_FILE": str(credential),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert "curlrc-secret" not in completed.stderr


def test_runtime_snapshot_contains_only_active_write_token(tmp_path: Path) -> None:
    # 2119: REQ-035.39.1
    from panopticon.taskservice.auth import snapshot_tokens

    credential = tmp_path / "auth.json"
    credential.write_text(
        json.dumps(
            {
                "read": ["reader-old-token", "reader-next-token"],
                "write": ["writer-old-token", "writer-next-token"],
            }
        )
    )
    credential.chmod(0o600)
    snapshot = snapshot_tokens(credential.name, secrets_dir=tmp_path, directory=tmp_path)
    try:
        assert json.loads(snapshot.read_text()) == {
            "read": [],
            "write": ["writer-next-token"],
        }
    finally:
        snapshot.unlink()


def test_mcp_redaction_does_not_delay_a_complete_nonsecret_sse_event() -> None:
    event = b"event: ping\ndata: safe\n\n"
    output, pending = _redact_stream_chunk(
        event, configured=(b"longer-secret-token", b"secret-token"), more_body=True
    )
    assert output == event
    assert pending == b""


@pytest.mark.parametrize("mode", [0o640, 0o620, 0o610, 0o604, 0o602, 0o601])
def test_credential_loader_rejects_group_or_other_permissions(tmp_path: Path, mode: int) -> None:
    # 2119: REQ-035.34.1
    credential = tmp_path / "auth.json"
    credential.write_text(
        json.dumps({"read": ["private-reader-token"], "write": ["private-writer-token"]})
    )
    credential.chmod(mode)
    with pytest.raises(ValueError, match="authentication credential"):
        load_tokens(credential.name, secrets_dir=tmp_path)
    with pytest.raises(ValueError, match="authentication credential"):
        build_app(
            db="sqlite+aiosqlite:///:memory:",
            artifacts_root=str(tmp_path / "artifacts"),
            layers_root=str(tmp_path / "layers"),
            auth_file=credential.name,
            auth_mode="enforced",
            secrets_dir=tmp_path,
            _home_workflows=tmp_path / "workflows",
        )


@pytest.mark.parametrize("runner_type", [LocalRunner, ShellRunner])
@pytest.mark.parametrize("mode", [0o640, 0o620, 0o610, 0o604, 0o602, 0o601])
def test_runner_preflight_rejects_insecure_credential_permissions(
    tmp_path: Path, runner_type: type[LocalRunner] | type[ShellRunner], mode: int
) -> None:
    # 2119: REQ-035.34.1
    credential = tmp_path / "auth.json"
    credential.write_text(
        json.dumps({"read": ["private-reader-token"], "write": ["private-writer-token"]})
    )
    credential.chmod(mode)
    runner = runner_type(
        "http://service", auth_file=credential.name, secrets_dir=tmp_path, run=lambda *_a, **_k: ""
    )
    with pytest.raises(ValueError, match="authentication credential"):
        runner.validate_configuration()


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_credential_loader_accepts_private_owner_permissions(tmp_path: Path, mode: int) -> None:
    # 2119: REQ-035.34.1
    credential = tmp_path / "auth.json"
    credential.write_text(
        json.dumps({"read": ["private-reader-token"], "write": ["private-writer-token"]})
    )
    credential.chmod(mode)
    assert load_tokens(credential.name, secrets_dir=tmp_path).write == ("private-writer-token",)


def test_credential_loader_rejects_a_foreign_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-035.34.1
    import panopticon.taskservice.auth as auth_module

    credential = tmp_path / "auth.json"
    credential.write_text(
        json.dumps({"read": ["private-reader-token"], "write": ["private-writer-token"]})
    )
    credential.chmod(0o600)
    original_fstat = auth_module.os.fstat

    def foreign_owner(fd: int) -> os.stat_result:
        values = list(original_fstat(fd))
        values[4] += 1
        return os.stat_result(values)

    monkeypatch.setattr(auth_module.os, "fstat", foreign_owner)
    with pytest.raises(ValueError, match="authentication credential"):
        load_tokens(credential.name, secrets_dir=tmp_path)
    with pytest.raises(ValueError, match="authentication credential"):
        build_app(
            db="sqlite+aiosqlite:///:memory:",
            artifacts_root=str(tmp_path / "artifacts"),
            layers_root=str(tmp_path / "layers"),
            auth_file=credential.name,
            auth_mode="enforced",
            secrets_dir=tmp_path,
            _home_workflows=tmp_path / "workflows",
        )


@pytest.mark.parametrize("runner_type", [LocalRunner, ShellRunner])
def test_runner_preflight_rejects_a_foreign_credential_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner_type: type[LocalRunner] | type[ShellRunner],
) -> None:
    # 2119: REQ-035.34.1
    import panopticon.taskservice.auth as auth_module

    credential = tmp_path / "auth.json"
    credential.write_text(
        json.dumps({"read": ["private-reader-token"], "write": ["private-writer-token"]})
    )
    credential.chmod(0o600)
    original_fstat = auth_module.os.fstat

    def foreign_owner(fd: int) -> os.stat_result:
        values = list(original_fstat(fd))
        values[4] += 1
        return os.stat_result(values)

    monkeypatch.setattr(auth_module.os, "fstat", foreign_owner)
    runner = runner_type(
        "http://service", auth_file=credential.name, secrets_dir=tmp_path, run=lambda *_a, **_k: ""
    )
    with pytest.raises(ValueError, match="authentication credential"):
        runner.validate_configuration()


def test_non_ascii_operator_token_header_is_rejected_without_server_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PANOPTICON_OPERATOR_TOKEN", "operator-secret")
    app = build_app(
        db="sqlite://",
        artifacts_root=str(tmp_path / "artifacts"),
        layers_root=str(tmp_path / "layers"),
        _home_workflows=tmp_path / "workflows",
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.put(
            "/tasks/missing/migration",
            headers={"X-Panopticon-Operator-Token": b"caf\xe9"},  # type: ignore[dict-item]
            json={
                "source_runner": "source",
                "destination_runner": "destination",
                "workspace_disposition": "preserved",
                "session_history_disposition": "preserved",
                "discarded_changes": False,
                "workspace_method": "shared",
            },
        )

    assert response.status_code in {403, 422}
    assert response.status_code < 500


@pytest.mark.parametrize(
    "invalid_kind", ["missing", "directory", "fifo", "socket", "symlink", "malformed"]
)
def test_shell_runner_rejects_an_invalid_service_credential_before_tmux(
    tmp_path: Path, invalid_kind: str
) -> None:
    # 2119: REQ-035.28.1
    calls: list[list[str]] = []
    invalid = tmp_path / "invalid.json"
    original_stat = None
    if invalid_kind == "directory":
        invalid.mkdir()
        (invalid / "sentinel").write_text("unchanged")
        original_stat = invalid.stat()
    elif invalid_kind == "fifo":
        os.mkfifo(invalid)
        original_stat = invalid.stat()
    elif invalid_kind == "socket":
        bound_socket = socket.socket(socket.AF_UNIX)
        bound_socket.bind(str(invalid))
        original_stat = invalid.stat()
    elif invalid_kind == "symlink":
        target = tmp_path / "target.json"
        target.write_text("unchanged")
        invalid.symlink_to(target)
    elif invalid_kind == "malformed":
        invalid.write_text("not-json")
        invalid.chmod(0o600)

    def record(args: list[str], **_kwargs: object) -> str:
        calls.append(args)
        return ""

    with pytest.raises(
        ValueError, match="authentication credential file is invalid or unavailable"
    ):
        ShellRunner(
            "http://svc:8000",
            secrets_dir=tmp_path,
            auth_file=invalid.name,
            run=record,
        ).spawn("t1", script="echo hi")

    assert calls == []
    assert os.path.lexists(invalid) is (invalid_kind != "missing")
    if invalid_kind == "directory":
        assert invalid.stat() == original_stat
        assert {path.name: path.read_text() for path in invalid.iterdir()} == {
            "sentinel": "unchanged"
        }
    elif invalid_kind == "fifo":
        assert invalid.stat() == original_stat
        assert stat.S_ISFIFO(invalid.stat().st_mode)
    elif invalid_kind == "socket":
        assert invalid.stat() == original_stat
        assert stat.S_ISSOCK(invalid.stat().st_mode)
        bound_socket.close()
    elif invalid_kind == "symlink":
        assert invalid.is_symlink()
        assert invalid.readlink() == target


def test_shell_runner_does_not_snapshot_before_rejecting_repo_secret(
    tmp_path: Path,
) -> None:
    credential = tmp_path / "auth.json"
    credential.write_text(
        json.dumps({"read": ["private-reader-token"], "write": ["private-writer-token"]})
    )
    credential.chmod(0o600)
    runner = ShellRunner(
        "http://svc:8000",
        secrets_dir=tmp_path,
        auth_file=credential.name,
        script_dir=tmp_path,
        run=lambda *_args, **_kwargs: "",
    )

    with pytest.raises(ValueError, match="escapes the secrets dir"):
        runner.spawn("t1", env_file="../escape.env", script="true")

    assert list(tmp_path.glob("panopticon-service-auth-t1-*.json")) == []


def test_shell_runner_cleans_snapshot_when_spilled_script_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = tmp_path / "auth.json"
    credential.write_text(
        json.dumps({"read": ["private-reader-token"], "write": ["private-writer-token"]})
    )
    credential.chmod(0o600)
    original_write_text = Path.write_text

    def fail_script_write(path: Path, data: str, *args: object, **kwargs: object) -> int:
        if path.name == "panopticon-shell-t1.sh":
            raise OSError("disk full")
        return original_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_script_write)
    runner = ShellRunner(
        "http://svc:8000",
        secrets_dir=tmp_path,
        auth_file=credential.name,
        script_dir=tmp_path,
        run=lambda *_args, **_kwargs: "",
    )

    with pytest.raises(OSError, match="disk full"):
        runner.spawn("t1", script="x=: #" + "x" * 12_000)

    assert list(tmp_path.glob("panopticon-service-auth-t1-*.json")) == []


def test_shell_runner_cleans_snapshot_for_command_encoding_failure(tmp_path: Path) -> None:
    credential = tmp_path / "auth.json"
    credential.write_text(
        json.dumps({"read": ["private-reader-token"], "write": ["private-writer-token"]})
    )
    credential.chmod(0o600)
    runner = ShellRunner(
        "http://svc:8000",
        secrets_dir=tmp_path,
        auth_file=credential.name,
        script_dir=tmp_path,
        run=lambda *_args, **_kwargs: "",
    )

    with pytest.raises(UnicodeEncodeError):
        runner.spawn("t1", script="printf '\ud800'")

    assert list(tmp_path.glob("panopticon-service-auth-t1-*.json")) == []


def test_docker_runner_mounts_a_stable_snapshot_if_source_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import panopticon.sessionservice.local_runner as runner_module
    from panopticon.taskservice.auth import scoped_task_token

    source = tmp_path / "auth.json"
    source.write_text(
        json.dumps({"read": ["stable-reader-token"], "write": ["stable-writer-token"]})
    )
    source.chmod(0o600)
    original_snapshot = runner_module.snapshot_service_tokens
    snapshots: list[Path] = []

    def snapshot_then_replace(*args: object, **kwargs: object) -> Path:
        snapshot = original_snapshot(*args, **kwargs)  # type: ignore[arg-type]
        snapshots.append(snapshot)
        source.unlink()
        os.mkfifo(source)
        return snapshot

    docker_observations: list[tuple[bool, str]] = []

    def record(args: list[str], **_kwargs: object) -> str:
        if args[:2] == ["docker", "run"]:
            mount = next(
                argument for argument in args if "/run/secrets/panopticon-service-auth" in argument
            )
            mounted = Path(mount.split(":", 1)[0])
            docker_observations.append((mounted.is_file(), mounted.read_text()))
        return ""

    monkeypatch.setattr(runner_module, "snapshot_service_tokens", snapshot_then_replace)
    runner = runner_module.LocalRunner(
        "http://service", auth_file=source.name, secrets_dir=tmp_path, run=record
    )
    runner.spawn("task")

    assert stat.S_ISFIFO(source.stat().st_mode)
    expected = json.dumps({"read": [], "write": [scoped_task_token("stable-writer-token", "task")]})
    assert docker_observations == [(True, expected)]
    assert snapshots and not snapshots[0].exists()
    runner.stop("panopticon-task")
    assert not snapshots[0].exists()


def test_docker_runner_removes_snapshot_and_container_when_tmux_setup_fails(
    tmp_path: Path,
) -> None:
    credential = tmp_path / "auth.json"
    credential.write_text(
        json.dumps({"read": ["private-reader-token"], "write": ["private-writer-token"]})
    )
    credential.chmod(0o600)
    calls: list[list[str]] = []

    def fail_new_session(args: list[str], **_kwargs: object) -> str:
        calls.append(args)
        if "new-session" in args:
            raise RuntimeError("tmux failed")
        return ""

    runner = LocalRunner(
        "http://service", auth_file=credential.name, secrets_dir=tmp_path, run=fail_new_session
    )
    runner._snapshot_dir = tmp_path

    with pytest.raises(RuntimeError, match="tmux failed"):
        runner.spawn("task")

    assert list(tmp_path.glob("panopticon-service-auth-task-*.json")) == []
    assert ["docker", "rm", "--force", "panopticon-task"] in calls


def test_docker_runner_removes_snapshot_when_awaiting_report_fails(tmp_path: Path) -> None:
    credential = tmp_path / "auth.json"
    credential.write_text(
        json.dumps({"read": ["private-reader-token"], "write": ["private-writer-token"]})
    )
    credential.chmod(0o600)
    runner = LocalRunner(
        "http://service", auth_file=credential.name, secrets_dir=tmp_path, run=lambda *_a, **_k: ""
    )
    runner._snapshot_dir = tmp_path

    def fail_awaiting(phase: object) -> None:
        if phase == LifecyclePhase.AWAITING:
            raise RuntimeError("awaiting report failed")

    with pytest.raises(RuntimeError, match="awaiting report failed"):
        runner.spawn("task", progress=fail_awaiting)  # type: ignore[arg-type]

    assert list(tmp_path.glob("panopticon-service-auth-task-*.json")) == []


def test_docker_stop_removes_snapshot_even_when_cleanup_command_fails(tmp_path: Path) -> None:
    snapshot = tmp_path / "panopticon-service-auth-task-stranded.json"
    snapshot.write_text("secret")

    def fail_tmux(args: list[str], **_kwargs: object) -> str:
        if args[0] == "tmux":
            raise RuntimeError("tmux cleanup failed")
        return ""

    runner = LocalRunner("http://service", run=fail_tmux)
    runner._snapshot_dir = tmp_path

    with pytest.raises(RuntimeError, match="tmux cleanup failed"):
        runner.stop("panopticon-task")

    assert not snapshot.exists()


def test_terminal_cleanup_stops_backend_to_remove_runtime_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped: list[str] = []

    class _Runner:
        def is_running(self, _task_id: str) -> bool:
            return False

        def stop(self, container_id: str) -> None:
            stopped.append(container_id)

    class _Client:
        def release(self, _task_id: str) -> None:
            raise AssertionError("unclaimed task must not be released")

    spawner = object.__new__(Spawner)
    spawner._client = _Client()  # type: ignore[assignment]
    spawner._runner_id = "runner"
    spawner._tasks_root = str(tmp_path)
    spawner._exists = lambda _path: False
    spawner._rmtree = lambda _path: None
    spawner._docker_cleanup = None
    runner = _Runner()
    monkeypatch.setattr(Spawner, "_runner_for", lambda _self, _task: runner)

    spawner.cleanup({"id": "task", "state": "COMPLETE", "claimed_by": None})

    assert stopped == ["panopticon-task"]


def test_shell_runner_uses_a_stable_snapshot_if_source_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import panopticon.sessionservice.shell_runner as runner_module
    from panopticon.taskservice.auth import scoped_task_token

    source = tmp_path / "auth.json"
    source.write_text(
        json.dumps({"read": ["stable-reader-token"], "write": ["stable-writer-token"]})
    )
    source.chmod(0o600)
    original_snapshot = runner_module.snapshot_tokens
    snapshots: list[Path] = []

    def snapshot_then_replace(*args: object, **kwargs: object) -> Path:
        snapshot = original_snapshot(*args, **kwargs)  # type: ignore[arg-type]
        snapshots.append(snapshot)
        source.unlink()
        os.mkfifo(source)
        return snapshot

    tmux_observations: list[tuple[bool, str]] = []

    def record(args: list[str], **_kwargs: object) -> str:
        if "new-session" in args:
            snapshot = snapshots[0]
            tmux_observations.append((snapshot.is_file(), snapshot.read_text()))
        return ""

    monkeypatch.setattr(runner_module, "snapshot_tokens", snapshot_then_replace)
    runner_module.ShellRunner(
        "http://service",
        auth_file=source.name,
        secrets_dir=tmp_path,
        script_dir=tmp_path,
        run=record,
    ).spawn("task", script="true")

    assert stat.S_ISFIFO(source.stat().st_mode)
    expected = json.dumps({"read": [], "write": [scoped_task_token("stable-writer-token", "task")]})
    assert tmux_observations == [(True, expected)]
    snapshots[0].unlink()


@pytest.mark.skipif(not shutil.which("tmux"), reason="needs tmux")
def test_killing_real_shell_session_removes_credential_snapshot(tmp_path: Path) -> None:
    credential = tmp_path / "auth.json"
    credential.write_text(
        json.dumps({"read": ["private-reader-token"], "write": ["private-writer-token"]})
    )
    credential.chmod(0o600)
    socket_name = f"pan-auth-cleanup-{os.getpid()}"
    runner = ShellRunner(
        "http://127.0.0.1:9",
        auth_file=credential.name,
        secrets_dir=tmp_path,
        script_dir=tmp_path,
        tmux_socket=socket_name,
    )
    session = runner.spawn("cleanup", script="sleep 30")
    snapshots = list(tmp_path.glob("panopticon-service-auth-cleanup-*.json"))
    assert len(snapshots) == 1
    try:
        subprocess.run(["tmux", "-L", socket_name, "kill-session", "-t", session], check=True)
        deadline = time.monotonic() + 5
        while snapshots[0].exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not snapshots[0].exists()
    finally:
        subprocess.run(["tmux", "-L", socket_name, "kill-server"], check=False)


def test_integrated_stack_explicitly_exposes_service_to_linux_containers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 2119: REQ-035.29.1
    monkeypatch.delenv("PANOPTICON_HOST", raising=False)
    monkeypatch.setattr("sys.platform", "linux")
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
    # 2119: REQ-035.31.1
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
        command_argv = shlex.split(command)
        assert command_argv[:7] == [
            "env",
            "-u",
            "PANOPTICON_SERVICE_AUTH_FILE",
            "-u",
            "PANOPTICON_SERVICE_AUTH_MODE",
            "-u",
            "PANOPTICON_CONFIG",
        ]
        assert command_argv[7:10] == [
            "PANOPTICON_SERVICE_AUTH_FILE=current-auth.json",
            "PANOPTICON_SERVICE_AUTH_MODE=enforced",
            "PANOPTICON_CONFIG=/current/config",
        ]

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
        command_argv = shlex.split(command)
        assert command_argv[:7] == [
            "env",
            "-u",
            "PANOPTICON_SERVICE_AUTH_FILE",
            "-u",
            "PANOPTICON_SERVICE_AUTH_MODE",
            "-u",
            "PANOPTICON_CONFIG",
        ]
        assert not any(argument.startswith("PANOPTICON_") for argument in command_argv[7:])

    monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_FILE", "only-current-auth.json")
    calls.clear()
    terminal_cli._start_sessions(run=record)
    mixed_commands = [call[-1] for call in calls if "new-session" in call]
    for command in mixed_commands:
        command_argv = shlex.split(command)
        assert "PANOPTICON_SERVICE_AUTH_FILE=only-current-auth.json" in command_argv
        assert not any(item.startswith("PANOPTICON_SERVICE_AUTH_MODE=") for item in command_argv)
        assert not any(item.startswith("PANOPTICON_CONFIG=") for item in command_argv)


@pytest.mark.skipif(not shutil.which("tmux"), reason="needs tmux")
def test_session_command_replaces_stale_real_tmux_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-035.31.1
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
    # 2119: REQ-035.32.1
    credential = tmp_path / "auth.json"
    credential.write_text(
        json.dumps(
            {
                "read": ["oldest-read-token", "old-read-token"],
                "write": ["oldest-write-token", "old-write-token"],
            }
        )
    )
    credential.chmod(0o600)
    assert (
        load_client_token(credential.name, privilege="write", secrets_dir=tmp_path)
        == "old-write-token"
    )
    credential.write_text(
        json.dumps(
            {
                "read": ["oldest-read-token", "old-read-token", "new-read-token"],
                "write": ["oldest-write-token", "old-write-token", "new-write-token"],
            }
        )
    )
    restarted_token = load_client_token(credential.name, privilege="write", secrets_dir=tmp_path)
    assert restarted_token == "new-write-token"
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
        assert restarted_python._http.headers["authorization"] == "Bearer new-write-token"
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
    assert recorded.read_text() == 'header = "Authorization: Bearer new-write-token"\n'
    shell_token = recorded.read_text().split("Bearer ", 1)[1].split('"', 1)[0]

    credential.write_text(json.dumps({"read": ["new-read-token"], "write": ["new-write-token"]}))
    converged_app = build_app(
        db="sqlite://",
        artifacts_root=str(tmp_path / "converged-artifacts"),
        auth_file=credential.name,
        auth_mode="enforced",
        secrets_dir=tmp_path,
        _home_workflows=tmp_path / "workflows",
    )
    with TestClient(converged_app) as http:
        converged_python = TaskServiceClient(http)
        assert converged_python._http.headers["authorization"] == "Bearer new-write-token"
        assert converged_python.list_tasks() == []
        assert TaskServiceClient(http, token=restarted_token).list_tasks() == []
        assert TaskServiceClient(http, token=shell_token).list_tasks() == []


def test_standalone_service_retains_loopback_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-035.29.1
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
    # 2119: REQ-035.30.1
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "auth.json").write_text(
        json.dumps({"read": ["read-token-long"], "write": ["write-token-long"]})
    )
    (secrets / "auth.json").chmod(0o600)
    app = build_app(
        db="sqlite://",
        artifacts_root=str(tmp_path / "artifacts"),
        auth_file="auth.json",
        auth_mode="enforced",
        secrets_dir=secrets,
        _home_workflows=tmp_path / "workflows",
    )
    with TestClient(app, root_path="/proxy") as client:
        # 2119: REQ-035.10.1
        assert client.get("/proxy/healthz").status_code == 200
        for method, path in [
            ("GET", "/proxy/tasks/missing/live"),
            ("GET", "/proxy/runners/missing/live"),
            ("GET", "/proxy/mcp"),
            ("POST", "/proxy/mcp"),
            ("DELETE", "/proxy/mcp"),
        ]:
            reader = client.request(
                method, path, headers={"Authorization": "Bearer read-token-long"}
            )
            assert reader.status_code == 401
            if "/runners/" in path:
                continue
            writer = client.request(
                method, path, headers={"Authorization": "Bearer write-token-long"}
            )
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
        for method, path in [
            ("GET", "/tasks/missing/live"),
            ("GET", "/runners/missing/live"),
            ("GET", "/mcp"),
            ("POST", "/mcp"),
            ("DELETE", "/mcp"),
        ]:
            assert (
                client.request(
                    method, path, headers={"Authorization": "Bearer read-token-long"}
                ).status_code
                == 401
            )
            if "/runners/" in path:
                continue
            assert client.request(
                method, path, headers={"Authorization": "Bearer write-token-long"}
            ).status_code not in {401, 403}
    for root_path, route in [
        ("/task", "/tasks/missing/live"),
        ("/runner", "/runners/missing/live"),
        ("/m", "/mcp"),
    ]:
        collision_app = build_app(
            db="sqlite://",
            artifacts_root=str(tmp_path / f"collision-{root_path[1:]}"),
            auth_file="auth.json",
            auth_mode="enforced",
            secrets_dir=secrets,
            _home_workflows=tmp_path / "workflows",
        )
        with TestClient(collision_app, root_path=root_path) as client:
            reader = client.get(route, headers={"Authorization": "Bearer read-token-long"})
            assert reader.status_code == 401
            if "/runners/" in route:
                continue
            writer = client.get(route, headers={"Authorization": "Bearer write-token-long"})
            assert writer.status_code not in {401, 403}


def test_production_process_reports_enforced_mode(tmp_path: Path) -> None:
    # 2119: REQ-035.27.1
    config = tmp_path / "config"
    secrets = config / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "auth.json").write_text(
        json.dumps({"read": ["read-token-long"], "write": ["write-token-long"]})
    )
    (secrets / "auth.json").chmod(0o600)
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
    # 2119: REQ-035.27.1
    caplog.set_level(logging.DEBUG)
    build_app(
        db="sqlite://",
        artifacts_root=str(tmp_path / "disabled-artifacts"),
        _home_workflows=tmp_path / "workflows",
    )
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "auth.json").write_text(
        json.dumps({"read": ["read-token-long"], "write": ["write-token-long"]})
    )
    (secrets / "auth.json").chmod(0o600)
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
