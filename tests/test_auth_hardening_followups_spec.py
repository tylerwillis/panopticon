"""Executable contract for REQ-047 authentication hardening follow-ups."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import selectors
import shlex
import shutil
import stat
import subprocess
import sys
import time
from io import BytesIO, StringIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from panopticon.core.models import Repo
from panopticon.sessionservice.local_runner import LocalRunner
from panopticon.sessionservice.spawner import Spawner
from panopticon.taskservice.api import _redact_stream_chunk, create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.terminal import __main__ as terminal_cli
from panopticon.terminal import log_tee
from panopticon.workflows import Spike

READ_TOKEN = "followup-reader-token"
WRITE_TOKEN = "followup-writer-token"


class _Completed:
    returncode = 1


def _service(root: Path) -> TaskService:
    root.mkdir(parents=True, exist_ok=True)
    service = TaskService(
        SqlAlchemyStore(f"sqlite:///{root / 'task.db'}"),
        {"spike": Spike()},
        FilesystemArtifactStore(root / "artifacts"),
    )
    asyncio.run(service.init())
    asyncio.run(
        service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://example/r1"))
    )
    return service


def _credential(root: Path, *, read: str = READ_TOKEN, write: str = WRITE_TOKEN) -> str:
    secrets = root / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    path = secrets / "auth.json"
    path.write_text(json.dumps({"read": [read], "write": [write]}))
    path.chmod(0o600)
    return path.name


def _authenticated_app(root: Path, *, read: str = READ_TOKEN, write: str = WRITE_TOKEN):
    service = _service(root)
    reference = _credential(root, read=read, write=write)
    return create_app(
        service,
        auth_file=reference,
        auth_mode="enforced",
        secrets_dir=root / "secrets",
    )


def test_mcp_redaction_is_constant_width_across_every_chunk_boundary() -> None:
    # 2119: REQ-047.1.1
    tokens = (b"a-much-longer-secret-token", b"short-secret")
    plaintext = (
        b"before:a-much-longer-secret-token:short-secret:"
        b"a-much-longer-secret-token:short-secret:after"
    )
    expected = b"before:[redacted]:[redacted]:[redacted]:[redacted]:after"

    for split in range(len(plaintext) + 1):
        first, pending = _redact_stream_chunk(plaintext[:split], configured=tokens, more_body=True)
        second, pending = _redact_stream_chunk(
            plaintext[split:], configured=tokens, pending=pending, more_body=False
        )
        assert first + second == expected
        assert pending == b""

    pending = b""
    output_parts: list[bytes] = []
    for byte in plaintext:
        output, pending = _redact_stream_chunk(
            bytes([byte]), configured=tokens, pending=pending, more_body=True
        )
        output_parts.append(output)
    output, pending = _redact_stream_chunk(b"", configured=tokens, pending=pending, more_body=False)
    output_parts.append(output)
    assert b"".join(output_parts) == expected
    assert pending == b""


def test_mcp_transport_applies_constant_redaction_to_streamed_responses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-047.1.1
    from panopticon.taskservice import mcp as mcp_module

    class _SessionManager:
        @contextlib.asynccontextmanager
        async def run(self):
            yield

    class _Settings:
        streamable_http_path = "/"

    class _FakeMcp:
        settings = _Settings()
        session_manager = _SessionManager()

        def streamable_http_app(self):
            async def app(_scope, _receive, send):
                plaintext = (
                    f"before:{WRITE_TOKEN}:{WRITE_TOKEN}:{READ_TOKEN}:{READ_TOKEN}:after"
                ).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [(b"content-length", str(len(plaintext)).encode())],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": f"before:{WRITE_TOKEN[:8]}".encode(),
                        "more_body": True,
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": (
                            f"{WRITE_TOKEN[8:]}:{WRITE_TOKEN}:{READ_TOKEN}:{READ_TOKEN}:after"
                        ).encode(),
                        "more_body": False,
                    }
                )

            return app

    monkeypatch.setattr(mcp_module, "build_mcp_server", lambda _service: _FakeMcp())
    with TestClient(_authenticated_app(tmp_path)) as client:
        response = client.post("/mcp/", headers={"Authorization": f"Bearer {WRITE_TOKEN}"})

    assert response.status_code == 200
    assert "content-length" not in response.headers
    assert response.content == b"before:[redacted]:[redacted]:[redacted]:[redacted]:after"


def test_mcp_redaction_does_not_hold_a_complete_nonsecret_sse_event() -> None:
    # 2119: REQ-047.1.2
    payload_suffix = b"safe"
    event = b"event: ping\ndata: " + payload_suffix + b"\n\n"
    configured = (payload_suffix + b"-prefix-extended-secret", b"other-secret")

    output, pending = _redact_stream_chunk(
        event,
        configured=configured,
        more_body=True,
    )

    assert output == event
    assert pending == b""


def test_mcp_transport_emits_a_complete_nonsecret_sse_event_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-047.1.2
    from panopticon.taskservice import mcp as mcp_module

    event = b"event: ping\ndata: safe\n\n"

    class _SessionManager:
        @contextlib.asynccontextmanager
        async def run(self):
            yield

    class _Settings:
        streamable_http_path = "/"

    class _FakeMcp:
        settings = _Settings()
        session_manager = _SessionManager()

        def streamable_http_app(self):
            async def app(_scope, _receive, send):
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send(
                    {
                        "type": "http.response.body",
                        "body": event,
                        "more_body": True,
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b"later",
                        "more_body": False,
                    }
                )

            return app

    async def exchange(app) -> list[dict[str, object]]:
        messages: list[dict[str, object]] = []
        received = False

        async def receive() -> dict[str, object]:
            nonlocal received
            if received:
                return {"type": "http.disconnect"}
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            messages.append(message)

        async with app.router.lifespan_context(app):
            await app(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "POST",
                    "scheme": "http",
                    "path": "/mcp/",
                    "raw_path": b"/mcp/",
                    "query_string": b"",
                    "headers": [(b"authorization", f"Bearer {WRITE_TOKEN}".encode())],
                    "client": ("127.0.0.1", 1234),
                    "server": ("testserver", 80),
                    "root_path": "",
                },
                receive,
                send,
            )
        return messages

    monkeypatch.setattr(mcp_module, "build_mcp_server", lambda _service: _FakeMcp())
    messages = asyncio.run(
        exchange(_authenticated_app(tmp_path, read="safe-prefix-extended-secret"))
    )
    bodies = [message for message in messages if message["type"] == "http.response.body"]
    assert bodies[0]["body"] == event
    assert bodies[0]["more_body"] is True
    assert bodies[1]["body"] == b"later"


class _CleanupRunner:
    def __init__(self, *, running: bool, events: list[tuple[str, str]]) -> None:
        self._running = running
        self._events = events

    def is_running(self, _task_id: str) -> bool:
        return self._running

    def stop(self, container_id: str) -> None:
        self._events.append(("stop", container_id))
        self._events.append(("credentials", container_id.removeprefix("panopticon-")))

    def cleanup_runtime_credentials(self, task_id: str) -> None:
        self._events.append(("credentials", task_id))


def _cleanup_spawner(runner: _CleanupRunner, events: list[tuple[str, str]]) -> Spawner:
    spawner = object.__new__(Spawner)
    spawner._runner = runner  # type: ignore[assignment]
    spawner._shell_runner = None
    spawner._runner_id = "runner"
    spawner._tasks_root = "/tasks"
    spawner._exists = lambda _path: True
    spawner._rmtree = lambda path: events.append(("workspace", path))
    spawner._docker_cleanup = None
    spawner._client = object()  # type: ignore[assignment]
    return spawner


@pytest.mark.parametrize("terminal_state", ["COMPLETE", "DROPPED"])
def test_exited_terminal_cleanup_removes_only_runtime_credentials(terminal_state: str) -> None:
    # 2119: REQ-047.2.1
    events: list[tuple[str, str]] = []
    runner = _CleanupRunner(running=False, events=events)
    spawner = _cleanup_spawner(runner, events)

    spawner.cleanup({"id": "task", "state": terminal_state, "claimed_by": None})

    assert events == [("credentials", "task"), ("workspace", "/tasks/task")]


def test_snapshot_only_cleanup_unlinks_credentials_without_runner_commands(tmp_path: Path) -> None:
    # 2119: REQ-047.2.1
    snapshots = [
        tmp_path / "panopticon-service-auth-task-first.json",
        tmp_path / "panopticon-service-auth-task-second.json",
    ]
    for snapshot in snapshots:
        snapshot.write_text("secret")
    unrelated = tmp_path / "panopticon-service-auth-other-stranded.json"
    unrelated.write_text("other secret")
    commands: list[list[str]] = []
    runner = LocalRunner("http://service", run=lambda args, **_kwargs: commands.append(args))
    runner._snapshot_dir = tmp_path

    runner.cleanup_runtime_credentials("task")

    assert all(not snapshot.exists() for snapshot in snapshots)
    assert unrelated.read_text() == "other secret"
    assert commands == []


@pytest.mark.parametrize("terminal_state", ["COMPLETE", "DROPPED"])
def test_running_terminal_cleanup_removes_all_snapshots_before_workspace_deletion(
    tmp_path: Path, terminal_state: str
) -> None:
    # 2119: REQ-047.2.2
    events: list[tuple[str, str]] = []
    snapshots = [
        tmp_path / "panopticon-service-auth-task-first.json",
        tmp_path / "panopticon-service-auth-task-second.json",
    ]
    for snapshot in snapshots:
        snapshot.write_text("secret")

    def record(args: list[str], **_kwargs: object) -> str:
        events.append(("command", " ".join(args)))
        return ""

    runner = LocalRunner("http://service", run=record)
    runner._snapshot_dir = tmp_path
    runner.is_running = lambda _task_id: True  # type: ignore[method-assign]
    spawner = object.__new__(Spawner)
    spawner._runner = runner
    spawner._shell_runner = None
    spawner._runner_id = "runner"
    spawner._tasks_root = "/tasks"
    spawner._exists = lambda _path: True

    def remove_workspace(path: str) -> None:
        assert all(not snapshot.exists() for snapshot in snapshots)
        events.append(("workspace", path))

    spawner._rmtree = remove_workspace
    spawner._docker_cleanup = None
    spawner._client = object()  # type: ignore[assignment]

    spawner.cleanup({"id": "task", "state": terminal_state, "claimed_by": None})

    assert all(not snapshot.exists() for snapshot in snapshots)
    assert events[-1] == ("workspace", "/tasks/task")
    assert "tmux -L panopticon kill-session -t panopticon-task" in events[0][1]
    assert events[1] == ("command", "docker rm --force panopticon-task")


@pytest.mark.parametrize("failed_command", ["tmux", "docker"])
def test_running_terminal_stop_failure_cleans_snapshots_but_preserves_workspace(
    tmp_path: Path, failed_command: str
) -> None:
    # 2119: REQ-047.2.2
    snapshot = tmp_path / "panopticon-service-auth-task-secret.json"
    snapshot.write_text("secret")

    def fail_stop(args: list[str], **_kwargs: object) -> str:
        if (failed_command == "tmux" and "kill-session" in args) or (
            failed_command == "docker" and args[:3] == ["docker", "rm", "--force"]
        ):
            raise RuntimeError("stop failed")
        return ""

    runner = LocalRunner("http://service", run=fail_stop)
    runner._snapshot_dir = tmp_path
    runner.is_running = lambda _task_id: True  # type: ignore[method-assign]
    events: list[tuple[str, str]] = []
    spawner = object.__new__(Spawner)
    spawner._runner = runner
    spawner._shell_runner = None
    spawner._runner_id = "runner"
    spawner._tasks_root = "/tasks"
    spawner._exists = lambda _path: True
    spawner._rmtree = lambda path: events.append(("workspace", path))
    spawner._docker_cleanup = None
    spawner._client = object()  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="stop failed"):
        spawner.cleanup({"id": "task", "state": "COMPLETE", "claimed_by": None})

    assert not snapshot.exists()
    assert events == []


def test_terminal_cleanup_probe_failure_cleans_snapshots_but_preserves_workspace(
    tmp_path: Path,
) -> None:
    # 2119: REQ-047.2.2
    snapshot = tmp_path / "panopticon-service-auth-task-secret.json"
    snapshot.write_text("secret")

    def fail_probe(args: list[str], **_kwargs: object) -> str:
        if args[:2] == ["docker", "ps"] and "--all" not in args:
            raise subprocess.CalledProcessError(1, args, stderr="daemon unavailable")
        return ""

    runner = LocalRunner("http://service", run=fail_probe)
    runner._snapshot_dir = tmp_path
    events: list[tuple[str, str]] = []
    spawner = object.__new__(Spawner)
    spawner._runner = runner
    spawner._shell_runner = None
    spawner._runner_id = "runner"
    spawner._tasks_root = "/tasks"
    spawner._exists = lambda _path: True
    spawner._rmtree = lambda path: events.append(("workspace", path))
    spawner._docker_cleanup = None
    spawner._client = object()  # type: ignore[assignment]

    with pytest.raises(subprocess.CalledProcessError, match=r"docker.*ps"):
        spawner.cleanup({"id": "task", "state": "COMPLETE", "claimed_by": None})

    assert not snapshot.exists()
    assert events == []


def test_replacement_spawn_removes_preserved_resources_before_docker_run() -> None:
    # 2119: REQ-047.2.3
    calls: list[tuple[list[str], bool]] = []
    resources = {"tmux": True, "container": True}

    def record(args: list[str], *, check: bool = True, **_kwargs: object) -> str:
        calls.append((list(args), check))
        if "kill-session" in args:
            resources["tmux"] = False
        elif args[:3] == ["docker", "rm", "--force"]:
            resources["container"] = False
        elif "list-sessions" in args:
            return "panopticon-task\n" if resources["tmux"] else ""
        elif args[:3] == ["docker", "ps", "--all"]:
            return "panopticon-task\n" if resources["container"] else ""
        elif args[:3] == ["docker", "run", "--detach"]:
            assert resources == {"tmux": False, "container": False}
        return "%1\n" if "display-message" in args else ""

    LocalRunner("http://service", run=record).spawn("task")

    kill_session, remove_container = (call[0] for call in calls[:2])
    docker_run = next(call[0] for call in calls if call[0][:3] == ["docker", "run", "--detach"])
    assert kill_session[-3:] == ["kill-session", "-t", "panopticon-task"]
    assert remove_container == ["docker", "rm", "--force", "panopticon-task"]
    assert docker_run[:3] == ["docker", "run", "--detach"]


@pytest.mark.parametrize("failed_cleanup", ["tmux", "container"])
def test_replacement_does_not_start_when_preserved_resource_cleanup_fails(
    failed_cleanup: str,
) -> None:
    # 2119: REQ-047.2.3
    docker_started = False

    def record(args: list[str], **_kwargs: object) -> str:
        nonlocal docker_started
        if failed_cleanup == "tmux" and "kill-session" in args:
            raise RuntimeError("tmux cleanup failed")
        if failed_cleanup == "container" and args[:3] == ["docker", "rm", "--force"]:
            raise RuntimeError("container cleanup failed")
        if args[:3] == ["docker", "run", "--detach"]:
            docker_started = True
        return ""

    with pytest.raises(RuntimeError, match="cleanup failed"):
        LocalRunner("http://service", run=record).spawn("task")
    assert not docker_started


@pytest.mark.parametrize("preserved_resource", ["tmux", "container"])
def test_replacement_does_not_start_when_cleanup_leaves_a_resource(
    preserved_resource: str,
) -> None:
    # 2119: REQ-047.2.3
    docker_started = False

    def record(args: list[str], **_kwargs: object) -> str:
        nonlocal docker_started
        if "list-sessions" in args:
            return "panopticon-task\n" if preserved_resource == "tmux" else ""
        if args[:3] == ["docker", "ps", "--all"]:
            return "panopticon-task\n" if preserved_resource == "container" else ""
        if args[:3] == ["docker", "run", "--detach"]:
            docker_started = True
        return ""

    with pytest.raises(subprocess.CalledProcessError) as raised:
        LocalRunner("http://service", run=record).spawn("task")
    assert "failed to remove stale runtime resources" in str(raised.value.output)
    assert "tmux output" in str(raised.value.output)
    assert "docker output" in str(raised.value.output)
    assert not docker_started


def test_log_redaction_does_not_monkeypatch_record_construction(tmp_path: Path) -> None:
    # 2119: REQ-047.3.1
    script = f"""
import json
import logging
from pathlib import Path
factory = logging.getLogRecordFactory()
make_record = logging.Logger.makeRecord
original_handler_handle = logging.Handler.handle
def custom_handler_handle(self, record):
    return original_handler_handle(self, record)
logging.Handler.handle = custom_handler_handle
handler_handle = logging.Handler.handle
logger_handle = logging.Logger.handle
from fastapi.testclient import TestClient
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike
root = Path({str(tmp_path)!r})
credential = root / "auth.json"
credential.write_text(json.dumps({{"read": ["{READ_TOKEN}"], "write": ["{WRITE_TOKEN}"]}}))
credential.chmod(0o600)
assert logging.getLogRecordFactory() is factory
assert logging.Logger.makeRecord is make_record
assert logging.Handler.handle is handler_handle
assert logging.Logger.handle is logger_handle
service = TaskService(SqlAlchemyStore(), {{"spike": Spike()}}, FilesystemArtifactStore(root / "artifacts"))
app = create_app(service, auth_file=credential.name, auth_mode="enforced", secrets_dir=root)
assert logging.getLogRecordFactory() is factory
assert logging.Logger.makeRecord is make_record
assert logging.Handler.handle is handler_handle
assert logging.Logger.handle is logger_handle
def custom_logger_handle(self, record):
    return logger_handle(self, record)
logging.Logger.handle = custom_logger_handle
pre_lifespan_logger_handle = logging.Logger.handle
with TestClient(app):
    assert logging.getLogRecordFactory() is factory
    assert logging.Logger.makeRecord is make_record
    assert logging.Handler.handle is handler_handle
    assert logging.Logger.handle is not pre_lifespan_logger_handle
    stale_logger_handle = logging.Logger.handle
assert logging.getLogRecordFactory() is factory
assert logging.Logger.makeRecord is make_record
assert logging.Handler.handle is handler_handle
assert logging.Logger.handle is pre_lifespan_logger_handle
stream = __import__("io").StringIO()
handler = logging.StreamHandler(stream)
race_logger = logging.getLogger("mcp.race")
race_logger.addHandler(handler)
race_logger.propagate = False
stale_logger_handle(race_logger, logging.LogRecord("mcp.race", logging.INFO, "<race>", 0, "after", (), None))
assert stream.getvalue() == "after\\n"
race_logger.removeHandler(handler)
second_app = create_app(service, auth_file=credential.name, auth_mode="enforced", secrets_dir=root)
assert logging.getLogRecordFactory() is factory
assert logging.Logger.makeRecord is make_record
assert logging.Handler.handle is handler_handle
assert logging.Logger.handle is pre_lifespan_logger_handle
with TestClient(second_app):
    assert logging.getLogRecordFactory() is factory
    assert logging.Logger.makeRecord is make_record
    assert logging.Handler.handle is handler_handle
    assert logging.Logger.handle is not pre_lifespan_logger_handle
assert logging.getLogRecordFactory() is factory
assert logging.Logger.makeRecord is make_record
assert logging.Handler.handle is handler_handle
assert logging.Logger.handle is pre_lifespan_logger_handle
logging.Logger.handle = logger_handle
logging.Handler.handle = original_handler_handle
"""
    completed = subprocess.run(
        [sys.executable, "-c", script], text=True, capture_output=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_active_app_redacts_every_supported_log_record_field(tmp_path: Path) -> None:
    # 2119: REQ-047.3.2
    class _CapturingHandler(logging.Handler):
        def __init__(self, records: list[dict[str, object]]) -> None:
            super().__init__()
            self._records = records

        def handle(self, record: logging.LogRecord) -> bool:
            self._records.append(dict(record.__dict__))
            return True

    names = [
        "panopticon.taskservice",
        "panopticon.taskservice.api",
        "panopticon.taskservice.api.child",
        "panopticon.taskservice.service",
        "panopticon.taskservice.service.child",
        "fastapi",
        "fastapi.child",
        "fastapi.routing",
        "fastapi.routing.child",
        "uvicorn",
        "uvicorn.error",
        "uvicorn.error.child",
        "uvicorn.access",
        "uvicorn.protocols.http",
        "mcp",
        "mcp.client",
        "mcp.client.child",
        "mcp.server.fastmcp.server",
        "mcp.server.fastmcp.server.child",
    ]
    streams = {name: StringIO() for name in names}
    handlers = {name: logging.StreamHandler(streams[name]) for name in names}
    captured: dict[str, list[dict[str, object]]] = {name: [] for name in names}
    capturing_handlers = {name: _CapturingHandler(captured[name]) for name in names}
    loggers = {name: logging.getLogger(name) for name in names}
    previous_states = {name: (loggers[name].disabled, loggers[name].level) for name in names}
    formatter = logging.Formatter(
        "%(message)s %(credential)s %(other_credential)s %(arbitrary_key)s "
        "%(exc_text)s %(stack_info)s"
    )
    for name in names:
        handlers[name].setFormatter(formatter)
        loggers[name].disabled = False
        loggers[name].setLevel(logging.INFO)
    try:
        with TestClient(_authenticated_app(tmp_path)):
            # Attach handlers after lifespan entry so dynamically configured logging is covered.
            for name in names:
                loggers[name].addHandler(handlers[name])
                loggers[name].addHandler(capturing_handlers[name])
            for name in names:
                for token in (READ_TOKEN, WRITE_TOKEN):
                    payload = f"prefix-{token}-{token}-suffix"
                    safe_extra = {
                        "credential": "safe",
                        "other_credential": "also-safe",
                        "arbitrary_key": "arbitrary-safe",
                    }
                    loggers[name].info(f"template {payload}", extra=safe_extra)
                    loggers[name].info("message %s %s", payload, payload, extra=safe_extra)
                    loggers[name].info("mapping %(value)s", {"value": payload}, extra=safe_extra)
                    split = len(token) // 2
                    loggers[name].info("split %s%s", token[:split], token[split:], extra=safe_extra)
                    loggers[name].info(
                        "extra",
                        extra={
                            "credential": payload,
                            "other_credential": payload,
                            "arbitrary_key": {f"nested-{token}": [payload]},
                            f"field-{token}": payload,
                        },
                    )
                    try:
                        raise RuntimeError(f"exception {payload}")
                    except RuntimeError:
                        loggers[name].exception("failure", extra=safe_extra)
                    stack_record = logging.LogRecord(
                        name, logging.ERROR, __file__, 0, "stack record", (), None
                    )
                    stack_record.credential = "safe"
                    stack_record.other_credential = "also-safe"
                    stack_record.arbitrary_key = "arbitrary-safe"
                    stack_record.stack_info = f"stack {payload}"
                    loggers[name].handle(stack_record)
    finally:
        for name in names:
            loggers[name].removeHandler(handlers[name])
            loggers[name].removeHandler(capturing_handlers[name])
            loggers[name].disabled, loggers[name].level = previous_states[name]

    for name in names:
        observed = streams[name].getvalue()
        redacted_payload = "prefix-[redacted]-[redacted]-suffix"
        assert f"template {redacted_payload} safe also-safe arbitrary-safe" in observed
        assert (
            f"message {redacted_payload} {redacted_payload} safe also-safe arbitrary-safe"
            in observed
        )
        assert f"mapping {redacted_payload} safe also-safe arbitrary-safe" in observed
        assert "split [redacted] safe also-safe arbitrary-safe" in observed
        assert f"extra {redacted_payload} {redacted_payload}" in observed
        assert f"'nested-[redacted]': ['{redacted_payload}']" in observed
        assert f"exception {redacted_payload}" in observed
        assert f"stack {redacted_payload}" in observed
        assert READ_TOKEN not in observed
        assert WRITE_TOKEN not in observed
        records = captured[name]
        assert READ_TOKEN not in repr(records)
        assert WRITE_TOKEN not in repr(records)
        assert any(record.get("msg") == f"template {redacted_payload}" for record in records)
        assert any(
            record.get("msg") == "message %s %s"
            and record.get("args") == (redacted_payload, redacted_payload)
            for record in records
        )
        assert any(
            record.get("msg") == "mapping %(value)s"
            and record.get("args") == {"value": redacted_payload}
            for record in records
        )
        assert any(
            record.get("msg") == "split [redacted]" and record.get("args") == ()
            for record in records
        )
        assert any(
            record.get("credential") == redacted_payload
            and record.get("other_credential") == redacted_payload
            and record.get("arbitrary_key") == {"nested-[redacted]": [redacted_payload]}
            and record.get("field-[redacted]") == redacted_payload
            for record in records
        )
        assert any(
            f"exception {redacted_payload}" in str(record.get("exc_text")) for record in records
        )
        assert any(record.get("stack_info") == f"stack {redacted_payload}" for record in records)


def test_log_redaction_preserves_standard_field_names_that_equal_tokens(tmp_path: Path) -> None:
    # 2119: REQ-047.3.2
    token = "relativeCreated"
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(relativeCreated)d %(message)s"))
    logger = logging.getLogger("uvicorn.error")
    previous_state = (logger.disabled, logger.level, logger.propagate)
    logger.disabled = False
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        with TestClient(_authenticated_app(tmp_path, read=token)):
            logger.info("credential %s", token)
    finally:
        logger.removeHandler(handler)
        logger.disabled, logger.level, logger.propagate = previous_state

    milliseconds, message = stream.getvalue().split(" ", 1)
    assert milliseconds.isdigit()
    assert message == "credential [redacted]\n"


@pytest.mark.parametrize("reverse_entry", [False, True])
@pytest.mark.parametrize("ended_first", [False, True])
def test_overlapping_app_lifespans_retain_only_active_tokens(
    tmp_path: Path, ended_first: bool, reverse_entry: bool
) -> None:
    # 2119: REQ-047.3.1
    # 2119: REQ-047.3.2
    # 2119: REQ-047.3.3
    first_read = READ_TOKEN
    second_token = f"{WRITE_TOKEN}-extended"
    second_read = "second-reader-token"
    first_app = _authenticated_app(tmp_path / "first")
    second_app = _authenticated_app(tmp_path / "second", read=second_read, write=second_token)
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("uvicorn.error")
    previous_disabled = logger.disabled
    logger.disabled = False
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    first_client = TestClient(first_app)
    second_client = TestClient(second_app)
    handler_handle = logging.Handler.handle
    logger_handle = logging.Logger.handle
    factory = logging.getLogRecordFactory()
    make_record = logging.Logger.makeRecord
    first_active = False
    second_active = False
    try:
        if reverse_entry:
            second_client.__enter__()
            second_active = True
            first_client.__enter__()
            first_active = True
        else:
            first_client.__enter__()
            first_active = True
            second_client.__enter__()
            second_active = True
        assert logging.Handler.handle is handler_handle
        assert logging.Logger.handle is not logger_handle
        assert logging.getLogRecordFactory() is factory
        assert logging.Logger.makeRecord is make_record
        logger.info("both %s %s %s %s", first_read, WRITE_TOKEN, second_read, second_token)
        if ended_first:
            first_client.__exit__(None, None, None)
            first_active = False
        else:
            second_client.__exit__(None, None, None)
            second_active = False
        assert logging.Handler.handle is handler_handle
        assert logging.Logger.handle is not logger_handle
        assert logging.getLogRecordFactory() is factory
        assert logging.Logger.makeRecord is make_record
        logger.info("remaining %s %s %s %s", first_read, WRITE_TOKEN, second_read, second_token)
        if first_active:
            first_client.__exit__(None, None, None)
            first_active = False
        if second_active:
            second_client.__exit__(None, None, None)
            second_active = False
        assert logging.Handler.handle is handler_handle
        assert logging.Logger.handle is logger_handle
        assert logging.getLogRecordFactory() is factory
        assert logging.Logger.makeRecord is make_record
        logger.info("neither %s %s %s %s", first_read, WRITE_TOKEN, second_read, second_token)
    finally:
        if second_active:
            second_client.__exit__(None, None, None)
        if first_active:
            first_client.__exit__(None, None, None)
        logger.removeHandler(handler)
        logger.disabled = previous_disabled

    assert logging.Handler.handle is handler_handle
    assert logging.Logger.handle is logger_handle
    assert logging.getLogRecordFactory() is factory
    assert logging.Logger.makeRecord is make_record

    both, remaining, neither = stream.getvalue().splitlines()
    assert all(token not in both for token in (first_read, WRITE_TOKEN, second_read, second_token))
    if ended_first:
        assert first_read in remaining
        assert WRITE_TOKEN in remaining
        assert second_read not in remaining
        assert second_token not in remaining
    else:
        assert second_read in remaining
        assert "[redacted]-extended" in remaining
        assert first_read not in remaining
        assert WRITE_TOKEN not in remaining
    assert all(token in neither for token in (first_read, WRITE_TOKEN, second_read, second_token))


@pytest.mark.parametrize("source", ["panopticon", "xdg", "default"])
def test_integrated_stack_uses_private_per_user_state_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    # 2119: REQ-047.4.1
    # 2119: REQ-047.4.2
    # 2119: REQ-047.4.3
    monkeypatch.delenv("PANOPTICON_STATE", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    if source == "panopticon":
        expected_root = tmp_path / "explicit-state"
        monkeypatch.setenv("PANOPTICON_STATE", str(expected_root))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "must-not-win"))
    elif source == "xdg":
        expected_root = tmp_path / "xdg-state" / "panopticon"
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    else:
        expected_root = tmp_path / "home" / ".local" / "state" / "panopticon"
    calls: list[list[str]] = []

    def record(args: list[str], **_kwargs: object) -> _Completed:
        calls.append(args)
        return _Completed()

    terminal_cli._start_sessions(run=record)

    session_commands = {
        call[call.index("-s") + 1]: call[-1] for call in calls if "new-session" in call
    }
    assert set(session_commands) == {"service", "runner"}
    assert "/tmp/panopticon-" not in "\n".join(session_commands.values())
    paths: list[Path] = []
    for session in ("service", "runner"):
        command = session_commands[session]
        argv = shlex.split(command)
        assert argv[-3:-1] == ["-m", "panopticon.terminal.log_tee"]
        log_path = Path(argv[-1])
        log_path.relative_to(expected_root)
        assert log_path.parent == expected_root
        paths.append(log_path)
        assert "2>&1" in command and "|" in argv
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(expected_root.stat().st_mode) == 0o700
    assert len(set(paths)) == 2


@pytest.mark.parametrize("target", ["directory", "service", "runner"])
def test_integrated_stack_refuses_symlinked_log_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    # 2119: REQ-047.4.2
    state_root = tmp_path / f"state-{target}"
    monkeypatch.setenv("PANOPTICON_STATE", str(state_root))
    initial_calls: list[list[str]] = []

    def initial_record(args: list[str], **_kwargs: object) -> _Completed:
        initial_calls.append(args)
        return _Completed()

    terminal_cli._start_sessions(run=initial_record)
    commands = [call[-1] for call in initial_calls if "new-session" in call]
    log_paths = [Path(shlex.split(command)[-1]) for command in commands]
    outside = tmp_path / f"outside-{target}"
    outside.mkdir()
    if target == "directory":
        for path in log_paths:
            path.unlink()
        state_root.rmdir()
        state_root.symlink_to(outside, target_is_directory=True)
    else:
        selected = log_paths[0 if target == "service" else 1]
        selected.unlink()
        captured = outside / "captured.log"
        captured.write_text("sentinel")
        selected.symlink_to(captured)
    calls: list[list[str]] = []

    def record(args: list[str], **_kwargs: object) -> _Completed:
        calls.append(args)
        return _Completed()

    with pytest.raises((OSError, ValueError)):
        terminal_cli._start_sessions(run=record)

    if target == "directory":
        assert list(outside.iterdir()) == []
    else:
        assert {path.name: path.read_text() for path in outside.iterdir()} == {
            "captured.log": "sentinel"
        }
    assert not any("new-session" in call for call in calls)


def test_integrated_stack_refuses_log_symlink_swapped_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-047.4.2
    state_root = tmp_path / "state-race"
    monkeypatch.setenv("PANOPTICON_STATE", str(state_root))
    log_path = terminal_cli._private_log_paths()["service"]
    log_path.unlink()
    captured = tmp_path / "captured.log"
    captured.write_text("sentinel")
    log_path.symlink_to(captured)

    with pytest.raises(OSError):
        log_tee.open_private_log(log_path)

    assert captured.read_text() == "sentinel"


def test_private_logs_refuse_symlinked_intermediate_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-047.4.2
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)
    state_root = linked_parent / "state"
    monkeypatch.setenv("PANOPTICON_STATE", str(state_root))

    with pytest.raises(OSError):
        terminal_cli._start_sessions(run=lambda *_args, **_kwargs: _Completed())
    assert list(outside.iterdir()) == []

    escaped_state = outside / "state"
    escaped_state.mkdir()
    with pytest.raises(OSError):
        log_tee.open_private_log(state_root / "service.log")
    assert list(escaped_state.iterdir()) == []


def test_private_log_tee_forwards_available_output_without_waiting_for_eof(
    tmp_path: Path,
) -> None:
    # 2119: REQ-047.4.1
    # 2119: REQ-047.4.3
    state_root = tmp_path / "state-live"
    state_root.mkdir(mode=0o700)
    log_path = state_root / "service.log"
    process = subprocess.Popen(
        [sys.executable, "-m", "panopticon.terminal.log_tee", str(log_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    try:
        process.stdin.write(b"live-line\n")
        process.stdin.flush()
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            assert ready.select(timeout=2), "log sink withheld available pane output"
        assert process.stdout.readline() == b"live-line\n"
        deadline = time.monotonic() + 2
        while not log_path.exists() or log_path.read_bytes() != b"live-line\n":
            if time.monotonic() >= deadline:
                raise AssertionError("log sink withheld available persisted output")
            time.sleep(0.01)
    finally:
        process.stdin.close()
        process.wait(timeout=2)

    assert process.returncode == 0
    assert log_path.read_bytes() == b"live-line\n"


def test_private_log_tee_keeps_forwarding_after_persistence_fails() -> None:
    # 2119: REQ-047.4.3
    class _FailedLog(BytesIO):
        def write(self, data: bytes) -> int:
            raise OSError("disk full")

    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"still-visible\n")
    finally:
        os.close(write_fd)
    output = BytesIO()
    try:
        log_tee.copy_available(read_fd, output, _FailedLog())
    finally:
        os.close(read_fd)

    assert output.getvalue() == b"still-visible\n"


@pytest.mark.skipif(not shutil.which("tmux"), reason="needs tmux")
def test_integrated_stack_tees_identical_output_to_tmux_pane_and_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-047.4.1
    # 2119: REQ-047.4.3
    state_root = tmp_path / "state"
    producer = tmp_path / "producer"
    real_python = sys.executable
    producer.write_text(
        "#!/bin/sh\n"
        'if [ "$2" = panopticon.terminal.log_tee ]; then\n'
        f'  exec {shlex.quote(real_python)} "$@"\n'
        "fi\n"
        "printf 'stdout-line\\n'\n"
        "printf 'stderr-line\\n' >&2\n"
        "sleep 5\n"
    )
    producer.chmod(0o700)
    monkeypatch.setenv("PANOPTICON_STATE", str(state_root))
    monkeypatch.setattr(sys, "executable", str(producer))
    calls: list[list[str]] = []

    def record(args: list[str], **_kwargs: object) -> _Completed:
        calls.append(args)
        return _Completed()

    terminal_cli._start_sessions(run=record)

    for index, command in enumerate(call[-1] for call in calls if "new-session" in call):
        socket_name = f"panopticon-log-{index}-{id(tmp_path)}"
        session = f"log-{index}"
        try:
            subprocess.run(
                ["tmux", "-L", socket_name, "new-session", "-d", "-s", session, command],
                check=True,
            )
            log_path = Path(shlex.split(command)[-1])
            deadline = time.monotonic() + 3
            while not log_path.exists() or "stderr-line" not in log_path.read_text():
                if time.monotonic() >= deadline:
                    raise AssertionError("generated command did not persist producer output")
                time.sleep(0.05)
            pane = subprocess.run(
                ["tmux", "-L", socket_name, "capture-pane", "-p", "-S", "-", "-t", session],
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            pane_lines = [line.rstrip() for line in pane.splitlines()]
            while pane_lines and not pane_lines[-1]:
                pane_lines.pop()
            log_lines = log_path.read_text().splitlines()
            assert pane_lines == log_lines == ["stdout-line", "stderr-line"]
        finally:
            subprocess.run(["tmux", "-L", socket_name, "kill-server"], check=False)


@pytest.mark.parametrize("mode", ["disabled", "permissive", "enforced"])
def test_head_health_is_public_and_matches_get_without_a_body(tmp_path: Path, mode: str) -> None:
    # 2119: REQ-047.5.1
    root = tmp_path / mode
    kwargs: dict[str, object] = {"auth_mode": mode}
    service = _service(root)
    if mode != "disabled":
        kwargs.update(auth_file=_credential(root), secrets_dir=root / "secrets")
    app = create_app(service, **kwargs)  # type: ignore[arg-type]

    async def compare() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        async def exchange(method: str) -> list[dict[str, object]]:
            messages: list[dict[str, object]] = []
            received = False

            async def receive() -> dict[str, object]:
                nonlocal received
                if received:
                    return {"type": "http.disconnect"}
                received = True
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message: dict[str, object]) -> None:
                messages.append(message)

            await app(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": method,
                    "scheme": "http",
                    "path": "/healthz",
                    "raw_path": b"/healthz",
                    "query_string": b"",
                    "headers": [],
                    "client": ("127.0.0.1", 1234),
                    "server": ("testserver", 80),
                    "root_path": "",
                },
                receive,
                send,
            )
            return messages

        async with app.router.lifespan_context(app):
            return await exchange("GET"), await exchange("HEAD")

    get_messages, head_messages = asyncio.run(compare())
    get_start = next(
        message for message in get_messages if message["type"] == "http.response.start"
    )
    head_start = next(
        message for message in head_messages if message["type"] == "http.response.start"
    )
    assert get_start["status"] == 200
    assert head_start == get_start
    assert (
        b"".join(
            message.get("body", b"")  # type: ignore[arg-type]
            for message in head_messages
            if message["type"] == "http.response.body"
        )
        == b""
    )
