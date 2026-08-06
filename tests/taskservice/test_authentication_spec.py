"""Executable contract for REQ-035 task-service authentication.

These tests intentionally describe the public seam before its implementation. Authentication is
configured with the same host-local filename reference operators use at runtime; tests never rely
on a database-backed secret.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from io import StringIO
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from panopticon.client import TaskServiceClient
from panopticon.core.models import Repo
from panopticon.core.workflow import ResponsibilitiesNotMet
from panopticon.taskservice.api import MAX_AUTH_INSPECTION_BODY_BYTES, create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.auth import derive_task_capability
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike

READ_TOKEN = "phone-reader-token"
WRITE_TOKEN = "fleet-writer-token"
NEXT_WRITE_TOKEN = "fleet-writer-token-next"
NEXT_READ_TOKEN = "phone-reader-token-next"
OPAQUE_READ_TOKEN = "opaque._~+-/=="
OPAQUE_WRITE_TOKEN = "write._~+-/=="
GENERIC_FAILURE = {"detail": "authentication required"}
TOKEN_GRAMMAR = re.compile(r"[A-Za-z0-9._~+/-]+=*\Z")
INVALID_TOKEN_VALUES = [
    f"validprefix{character}tail"
    for character in map(chr, range(128))
    if TOKEN_GRAMMAR.fullmatch(f"validprefix{character}tail") is None
] + ["", "=", "==", "validprefixé", "validprefixλ", "validprefix😀", "validprefixＡ"]


def _service(tmp_path: Path) -> TaskService:
    tmp_path.mkdir(parents=True, exist_ok=True)
    service = TaskService(
        SqlAlchemyStore(f"sqlite:///{tmp_path / 'task.db'}"),
        {"spike": Spike()},
        FilesystemArtifactStore(tmp_path / "artifacts"),
    )
    asyncio.run(service.init())
    asyncio.run(service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1")))
    return service


def _credential_file(tmp_path: Path, *, overlap: bool = False, opaque: bool = False) -> str:
    secrets = tmp_path / "secrets"
    secrets.mkdir(exist_ok=True)
    read = [READ_TOKEN, NEXT_READ_TOKEN] if overlap else [READ_TOKEN]
    write = [WRITE_TOKEN, NEXT_WRITE_TOKEN] if overlap else [WRITE_TOKEN]
    if opaque:
        read.append(OPAQUE_READ_TOKEN)
        write.append(OPAQUE_WRITE_TOKEN)
    credential = secrets / "task-service-auth.json"
    credential.write_text(json.dumps({"read": read, "write": write}))
    credential.chmod(0o600)
    return "task-service-auth.json"


def _client(
    tmp_path: Path,
    *,
    mode: str = "enforced",
    overlap: bool = False,
) -> TestClient:
    return TestClient(
        create_app(
            _service(tmp_path),
            auth_file=_credential_file(tmp_path, overlap=overlap),
            auth_mode=mode,
            secrets_dir=tmp_path / "secrets",
        )
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _route_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "missing", path)


def _rest_operations(client: TestClient) -> list[tuple[str, str]]:
    """Enumerate the actual registered REST routes, including schema-hidden routes."""
    return [
        (method.upper(), _route_path(route.path))
        for route in client.app.routes
        if hasattr(route, "methods")
        and hasattr(route, "path")
        and route.path != "/healthz"
        and not route.path.startswith("/mcp")
        for method in route.methods
        if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    ]


def _is_mutating(method: str, path: str) -> bool:
    return (
        method not in {"GET", "HEAD"}
        or path.endswith("/live")
        or (method == "GET" and path.endswith("/session/input"))
    )


def _asgi_status(
    app: object,
    path: str,
    *,
    method: str = "GET",
    token: str | None = None,
    client_host: str = "testclient",
    root_path: str = "",
) -> tuple[int, dict[str, str], bytes]:
    """Call a streaming route until response start, then disconnect without buffering forever."""
    sent: list[dict[str, object]] = []
    first = True

    async def receive() -> dict[str, object]:
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    headers = [] if token is None else [(b"authorization", f"Bearer {token}".encode())]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": (client_host, 12345),
        "server": ("testserver", 80),
        "root_path": root_path,
    }
    asyncio.run(app(scope, receive, send))  # type: ignore[operator]
    start = next(message for message in sent if message["type"] == "http.response.start")
    response_headers = {
        key.decode().lower(): value.decode()
        for key, value in start.get("headers", [])  # type: ignore[union-attr]
    }
    body = b"".join(
        message.get("body", b"")  # type: ignore[arg-type]
        for message in sent
        if message["type"] == "http.response.body"
    )
    return int(start["status"]), response_headers, body


def test_tokens_are_host_local_and_never_serialized(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # 2119: REQ-035.1.1
    # 2119: REQ-035.14.1
    # 2119: REQ-035.18.1
    caplog.set_level("DEBUG", logger="panopticon")
    configured = (
        READ_TOKEN,
        NEXT_READ_TOKEN,
        OPAQUE_READ_TOKEN,
        WRITE_TOKEN,
        NEXT_WRITE_TOKEN,
        OPAQUE_WRITE_TOKEN,
    )
    with TestClient(
        create_app(
            _service(tmp_path),
            auth_file=_credential_file(tmp_path, overlap=True, opaque=True),
            auth_mode="enforced",
            secrets_dir=tmp_path / "secrets",
        )
    ) as client:
        repo = client.get("/repos/r1", headers=_bearer(WRITE_TOKEN))
        assert repo.status_code == 200
        task = client.post(
            "/tasks",
            headers=_bearer(WRITE_TOKEN),
            json={"repo_id": "r1", "workflow": "spike"},
        ).json()
        client.put(
            f"/tasks/{task['id']}/artifacts/safe",
            headers=_bearer(WRITE_TOKEN),
            content=b"safe",
        )
        validations = [
            client.post("/tasks", headers=_bearer(WRITE_TOKEN), json={"repo_id": token})
            for token in configured
        ]
        domain_failure = client.post(
            "/repos",
            headers=_bearer(WRITE_TOKEN),
            json={
                "id": "r2",
                "name": "unsafe-reference",
                "git_url": "https://x/r2",
                "env_file": WRITE_TOKEN,
            },
        )
        assert domain_failure.status_code == 400
        image_layer = client.get("/workflows/spike/image-layer", headers=_bearer(WRITE_TOKEN))
        not_found = [
            client.get(f"/tasks/{token}", headers=_bearer(WRITE_TOKEN)) for token in configured
        ]
        serialized = (
            repo.text
            + client.get(f"/tasks/{task['id']}", headers=_bearer(WRITE_TOKEN)).text
            + "".join(response.text for response in validations)
            + domain_failure.text
            + image_layer.text
            + "".join(response.text for response in not_found)
        )
        assert all(token not in serialized for token in configured)
        rejected = client.post(
            "/tasks",
            headers=_bearer(READ_TOKEN),
            json={"repo_id": "r1", "workflow": "spike"},
        )
        assert rejected.json() == GENERIC_FAILURE
        assert client.put(
            "/tasks/missing/artifacts/proof", headers=_bearer(WRITE_TOKEN), content=b"safe"
        ).status_code in {204, 404}
    assert all(token not in caplog.text for token in configured)
    secrets = tmp_path / "bad-secrets"
    secrets.mkdir()
    overlap = secrets / "bad.json"
    overlap.write_text(json.dumps({"read": [WRITE_TOKEN], "write": [WRITE_TOKEN]}))
    overlap.chmod(0o600)
    with pytest.raises(ValueError) as exc:
        create_app(
            _service(tmp_path / "bad-service"),
            auth_file="bad.json",
            auth_mode="enforced",
            secrets_dir=secrets,
        )
    assert WRITE_TOKEN not in str(exc.value)
    file_only_token = f"file-only-{tmp_path.name}"
    (tmp_path / "secrets" / "task-service-auth.json").write_text(
        json.dumps({"read": [file_only_token], "write": [WRITE_TOKEN]})
    )
    file_loaded_app = create_app(
        _service(tmp_path / "file-loaded"),
        auth_file="task-service-auth.json",
        auth_mode="enforced",
        secrets_dir=tmp_path / "secrets",
    )
    # Loading is a startup boundary: later file edits do not affect this running app.
    (tmp_path / "secrets" / "task-service-auth.json").write_text(
        json.dumps({"read": ["later-token"], "write": ["later-write-token"]})
    )
    with TestClient(file_loaded_app) as file_loaded_client:
        assert file_loaded_client.get("/tasks", headers=_bearer(file_only_token)).status_code == 200
        assert file_loaded_client.get("/tasks", headers=_bearer(READ_TOKEN)).status_code == 401
    credential_paths = {
        (tmp_path / "secrets" / "task-service-auth.json").resolve(),
        (tmp_path / "bad-secrets" / "bad.json").resolve(),
    }
    # Inspect every SQLite/artifact/log file produced by the production adapters, not merely API
    # serialization, so a copied credential value cannot hide in durable service state.
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.resolve() not in credential_paths:
            contents = path.read_bytes()
            assert all(token.encode() not in contents for token in (*configured, file_only_token))
    assert all(token not in caplog.text for token in (*configured, file_only_token))
    with pytest.raises(ValueError, match="authentication credential"):
        create_app(
            _service(tmp_path / "escape"),
            auth_file="../task-service-auth.json",
            auth_mode="enforced",
            secrets_dir=tmp_path / "escape" / "secrets",
        )
    with pytest.raises(ValueError, match="authentication credential"):
        create_app(
            _service(tmp_path / "raw"),
            auth_file=WRITE_TOKEN,
            auth_mode="enforced",
            secrets_dir=tmp_path / "raw" / "secrets",
        )
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"read": [READ_TOKEN], "write": [WRITE_TOKEN]}))
    symlink_root = tmp_path / "symlink" / "secrets"
    symlink_root.mkdir(parents=True)
    (symlink_root / "linked.json").symlink_to(outside)
    for reference in [str(outside.resolve()), "linked.json"]:
        with pytest.raises(ValueError, match="authentication credential"):
            create_app(
                _service(tmp_path / f"reject-{reference == 'linked.json'}"),
                auth_file=reference,
                auth_mode="enforced",
                secrets_dir=symlink_root,
            )


def test_startup_rejects_a_token_equal_to_fixed_authentication_response_text(
    tmp_path: Path,
) -> None:
    # 2119: REQ-035.18.1
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    credential = secrets / "auth.json"
    credential.write_text(json.dumps({"read": [READ_TOKEN], "write": ["authentication"]}))
    credential.chmod(0o600)

    with pytest.raises(ValueError, match="authentication credential"):
        create_app(
            _service(tmp_path),
            auth_file=credential.name,
            auth_mode="enforced",
            secrets_dir=secrets,
        )


def test_rest_redaction_masks_longest_prefix_related_token_first(tmp_path: Path) -> None:
    # 2119: REQ-035.18.1
    shorter = "prefix-secret-token"
    longer = f"{shorter}-next"
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    credential = secrets / "auth.json"
    credential.write_text(json.dumps({"read": [shorter, longer], "write": [WRITE_TOKEN]}))
    credential.chmod(0o600)

    with TestClient(
        create_app(
            _service(tmp_path),
            auth_file=credential.name,
            auth_mode="enforced",
            secrets_dir=secrets,
        )
    ) as client:
        rejected = client.get(f"/tasks/{longer}", headers=_bearer(WRITE_TOKEN))

    assert rejected.status_code == 400
    assert rejected.json() == {"detail": "request rejected"}


def test_tokens_never_reach_any_failure_body_or_spawned_command(tmp_path: Path) -> None:
    # 2119: REQ-035.18.1
    configured = (
        READ_TOKEN,
        NEXT_READ_TOKEN,
        OPAQUE_READ_TOKEN,
        WRITE_TOKEN,
        NEXT_WRITE_TOKEN,
        OPAQUE_WRITE_TOKEN,
    )
    with TestClient(
        create_app(
            _service(tmp_path),
            auth_file=_credential_file(tmp_path, overlap=True, opaque=True),
            auth_mode="enforced",
            secrets_dir=tmp_path / "secrets",
        )
    ) as client:
        for method, path in _rest_operations(client):
            body = client.request(method, path).content
            assert all(token.encode() not in body for token in configured)

    from panopticon.sessionservice.local_runner import LocalRunner
    from panopticon.sessionservice.shell_runner import ShellRunner

    calls: list[list[str]] = []

    def record(args: object, **_kwargs: object) -> str:
        calls.append(list(args))  # type: ignore[arg-type]
        return "%1\n" if "display-message" in calls[-1] else ""

    reference = "task-service-auth.json"
    LocalRunner(
        "http://service", auth_file=reference, secrets_dir=tmp_path / "secrets", run=record
    ).spawn("docker-task")
    ShellRunner(
        "http://service", auth_file=reference, secrets_dir=tmp_path / "secrets", run=record
    ).spawn("shell-task", script="true")
    emitted = "\n".join(" ".join(call) for call in calls)
    assert all(token not in emitted for token in configured)


def test_mcp_validation_failure_redacts_a_configured_token(tmp_path: Path) -> None:
    # 2119: REQ-035.18.1
    configured = (
        READ_TOKEN,
        NEXT_READ_TOKEN,
        OPAQUE_READ_TOKEN,
        WRITE_TOKEN,
        NEXT_WRITE_TOKEN,
        OPAQUE_WRITE_TOKEN,
    )
    with TestClient(
        create_app(
            _service(tmp_path),
            auth_file=_credential_file(tmp_path, overlap=True, opaque=True),
            auth_mode="enforced",
            secrets_dir=tmp_path / "secrets",
        )
    ) as client:
        for token in configured:
            response = client.post(
                "/mcp",
                headers={
                    **_bearer(NEXT_WRITE_TOKEN),
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": token,
                },
            )

            assert response.status_code == 400
            assert all(configured_token not in response.text for configured_token in configured)
            assert response.json() == {"detail": "request rejected"}


def test_mcp_tool_arguments_never_log_or_return_configured_tokens(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # 2119: REQ-035.18.1
    # 2119: REQ-035.42.1
    configured = (
        READ_TOKEN,
        NEXT_READ_TOKEN,
        OPAQUE_READ_TOKEN,
        WRITE_TOKEN,
        NEXT_WRITE_TOKEN,
        OPAQUE_WRITE_TOKEN,
    )
    caplog.set_level("DEBUG")
    app = create_app(
        _service(tmp_path),
        auth_file=_credential_file(tmp_path, overlap=True, opaque=True),
        auth_mode="enforced",
        secrets_dir=tmp_path / "secrets",
    )
    late_stream = StringIO()
    late_handler = logging.StreamHandler(late_stream)
    late_handler.setFormatter(logging.Formatter("%(message)s %(payload)s"))
    late_logger = logging.getLogger("mcp.late.configured.handler")
    late_logger.addHandler(late_handler)
    late_logger.setLevel(logging.INFO)

    def emit_sensitive_logs() -> None:
        late_logger.info("late payload", extra={"payload": WRITE_TOKEN.encode()})
        try:
            raise RuntimeError(f"traceback carried {WRITE_TOKEN}")
        except RuntimeError:
            logging.getLogger("mcp.server.fastmcp.server").exception(
                "SDK failure for %s", READ_TOKEN, extra={"credential": WRITE_TOKEN}
            )
        stack_record = logging.LogRecord(
            "mcp.server.lowlevel.server",
            logging.ERROR,
            __file__,
            0,
            "SDK stack failure",
            (),
            None,
        )
        stack_record.stack_info = f"stack carried {WRITE_TOKEN}"
        logging.getLogger(stack_record.name).handle(stack_record)

    headers = {
        **_bearer(WRITE_TOKEN),
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(app) as client:
        emit_sensitive_logs()
        initialized = client.post(
            "/mcp/",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "redaction-test", "version": "1"},
                },
            },
        )
        session_headers = {**headers, "Mcp-Session-Id": initialized.headers["mcp-session-id"]}
        client.post(
            "/mcp/",
            headers=session_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        responses = [
            client.post(
                "/mcp/",
                headers=session_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": index + 2,
                    "method": "tools/call",
                    "params": {
                        "name": "apply_operation",
                        "arguments": {"task_id": token, "operation": "advance"},
                    },
                },
            )
            for index, token in enumerate(configured)
        ]
        responses.extend(
            client.post(
                "/mcp/",
                headers=session_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": index + 20,
                    "method": "resources/read",
                    "params": {"uri": f"panopticon://tasks/{token}/artifacts/missing"},
                },
            )
            for index, token in enumerate(configured)
        )

    assert all(response.status_code == 400 for response in responses)
    captured_records = "".join(repr(record.__dict__) for record in caplog.records)
    observed = (
        caplog.text
        + captured_records
        + late_stream.getvalue()
        + "".join(response.text for response in responses)
    )
    late_logger.removeHandler(late_handler)
    assert "[redacted]" in observed
    assert all(token not in observed for token in configured)


def test_authenticated_domain_error_redacts_configured_token(tmp_path: Path) -> None:
    # 2119: REQ-035.18.1
    app = create_app(
        _service(tmp_path),
        auth_file=_credential_file(tmp_path),
        auth_mode="enforced",
        secrets_dir=tmp_path / "secrets",
    )

    @app.get("/redaction-domain-error")
    async def domain_error() -> None:
        raise ResponsibilitiesNotMet(f"unresolved {WRITE_TOKEN}")

    with TestClient(app) as client:
        response = client.get("/redaction-domain-error", headers=_bearer(WRITE_TOKEN))
    assert response.status_code == 409
    assert response.json() == {"detail": "unresolved [redacted]"}
    assert WRITE_TOKEN not in response.text


def test_configured_tokens_are_rejected_before_persistence_or_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-035.18.1
    # 2119: REQ-035.44.1
    service_root = tmp_path / "service"
    service = _service(service_root)
    dispatched: list[str] = []
    original_create_task = service.create_task
    original_put_artifact = service.put_artifact

    async def observed_create_task(*args: object, **kwargs: object) -> object:
        dispatched.append("create_task")
        return await original_create_task(*args, **kwargs)  # type: ignore[arg-type]

    async def observed_put_artifact(*args: object, **kwargs: object) -> object:
        dispatched.append("put_artifact")
        return await original_put_artifact(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "create_task", observed_create_task)
    monkeypatch.setattr(service, "put_artifact", observed_put_artifact)
    with TestClient(
        create_app(
            service,
            auth_file=_credential_file(tmp_path),
            auth_mode="enforced",
            secrets_dir=tmp_path / "secrets",
        )
    ) as client:
        task_response = client.post(
            "/tasks",
            headers=_bearer(WRITE_TOKEN),
            json={"repo_id": "r1", "workflow": "spike", "memo": WRITE_TOKEN},
        )
        escaped_response = client.post(
            "/tasks",
            headers={**_bearer(WRITE_TOKEN), "Content-Type": "application/json"},
            content=(
                '{"repo_id":"r1","workflow":"spike","memo":"'
                + WRITE_TOKEN[:-1]
                + f'\\u{ord(WRITE_TOKEN[-1]):04x}"}}'
            ),
        )
        headerless_escaped_response = client.post(
            "/tasks",
            headers=_bearer(WRITE_TOKEN),
            content=(
                '{"repo_id":"r1","workflow":"spike","memo":"'
                + WRITE_TOKEN[:-1]
                + f'\\u{ord(WRITE_TOKEN[-1]):04x}"}}'
            ),
        )
        path_response = client.put(
            f"/tasks/missing/artifacts/{READ_TOKEN}",
            headers=_bearer(WRITE_TOKEN),
            content=b"safe",
        )
        artifact_response = client.put(
            "/tasks/missing/artifacts/proof",
            headers=_bearer(WRITE_TOKEN),
            content=f"prefix {READ_TOKEN} suffix",
        )
        encoded_query_response = client.get(
            "/runners/missing/live?host=" + WRITE_TOKEN.replace("-", "%2D"),
            headers=_bearer(WRITE_TOKEN),
        )
        encoded_path_token = "".join(f"%{ord(character):02X}" for character in READ_TOKEN)
        encoded_path_response = client.put(
            f"/tasks/missing/artifacts/{encoded_path_token}",
            headers=_bearer(WRITE_TOKEN),
            content=b"safe",
        )
        assert task_response.status_code == 400
        assert escaped_response.status_code == 400
        assert headerless_escaped_response.status_code == 400
        assert path_response.status_code == 400
        assert artifact_response.status_code == 400
        assert encoded_query_response.status_code == 400
        assert encoded_path_response.status_code == 400
        assert task_response.json() == {"detail": "request rejected"}
        assert escaped_response.json() == {"detail": "request rejected"}
        assert headerless_escaped_response.json() == {"detail": "request rejected"}
        assert path_response.json() == {"detail": "request rejected"}
        assert artifact_response.json() == {"detail": "request rejected"}
        assert encoded_query_response.json() == {"detail": "request rejected"}
        assert encoded_path_response.json() == {"detail": "request rejected"}
        assert dispatched == []

    durable = (service_root / "task.db").read_bytes()
    artifact_files = list((service_root / "artifacts").glob("**/*"))
    artifact_bytes = b"".join(path.read_bytes() for path in artifact_files if path.is_file())
    for token in (READ_TOKEN, WRITE_TOKEN):
        assert token.encode() not in durable
        assert token.encode() not in artifact_bytes


def test_authentication_inspection_rejects_oversized_body_before_dispatch(tmp_path: Path) -> None:
    # 2119: REQ-035.46.1
    with _client(tmp_path) as client:
        response = client.request(
            "GET",
            "/tasks",
            headers=_bearer(READ_TOKEN),
            content=b"x" * (MAX_AUTH_INSPECTION_BODY_BYTES + 1),
        )
    assert response.status_code == 413
    assert response.json() == {"detail": "request too large"}


def test_authentication_inspection_stops_reading_at_the_limit_and_skips_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-035.46.1
    service = _service(tmp_path / "service")
    dispatched = False

    async def observed_list_tasks(*_args: object, **_kwargs: object) -> list[object]:
        nonlocal dispatched
        dispatched = True
        return []

    monkeypatch.setattr(service, "list_tasks", observed_list_tasks)
    app = create_app(
        service,
        auth_file=_credential_file(tmp_path),
        auth_mode="enforced",
        secrets_dir=tmp_path / "secrets",
    )
    chunks_read = 0
    chunks = [b"x" * (1024 * 1024) for _ in range(32)]
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        nonlocal chunks_read
        chunk = chunks[chunks_read]
        chunks_read += 1
        return {"type": "http.request", "body": chunk, "more_body": chunks_read < len(chunks)}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/tasks",
        "raw_path": b"/tasks",
        "query_string": b"",
        "headers": [(b"authorization", f"Bearer {READ_TOKEN}".encode())],
        "client": ("testclient", 12345),
        "server": ("testserver", 80),
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))  # type: ignore[operator]
    start = next(message for message in sent if message["type"] == "http.response.start")
    assert start["status"] == 413
    assert chunks_read == MAX_AUTH_INSPECTION_BODY_BYTES // len(chunks[0]) + 1
    assert chunks_read < len(chunks)
    assert dispatched is False


def test_read_and_write_tokens_can_read_but_only_write_token_can_mutate(tmp_path: Path) -> None:
    # 2119: REQ-035.2.1
    # 2119: REQ-035.3.1
    # 2119: REQ-035.4.1
    # 2119: REQ-035.5.1
    # 2119: REQ-035.9.1
    with TestClient(
        create_app(
            _service(tmp_path),
            auth_file=_credential_file(tmp_path, overlap=True, opaque=True),
            auth_mode="enforced",
            secrets_dir=tmp_path / "secrets",
        )
    ) as client:
        created = client.post(
            "/tasks",
            headers=_bearer(WRITE_TOKEN),
            json={"repo_id": "r1", "workflow": "spike"},
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        for path in [
            f"/tasks/{task_id}",
            f"/tasks/{task_id}/skills",
            f"/tasks/{task_id}/transitions",
            f"/tasks/{task_id}/artifacts",
        ]:
            assert client.get(path, headers=_bearer(READ_TOKEN)).status_code == 200
            assert client.get(path, headers=_bearer(WRITE_TOKEN)).status_code == 200
        operations = _rest_operations(client)
        assert operations
        mutating_gets = {
            path for method, path in operations if method == "GET" and path.endswith("/live")
        }
        assert mutating_gets == {"/tasks/missing/live", "/runners/missing/live"}
        for method, path in operations:
            before = client.get("/tasks", headers=_bearer(WRITE_TOKEN)).json()
            read_responses = [
                client.request(method, path, headers=_bearer(token))
                for token in [READ_TOKEN, NEXT_READ_TOKEN, OPAQUE_READ_TOKEN]
            ]
            # TestClient buffers streaming responses to completion; successful liveness streams
            # intentionally never complete. Their rejection boundary is observable here, while
            # successful header propagation is covered by the shared-client transport test.
            if path.endswith("/live"):
                assert all(
                    response.status_code == 401
                    and response.json() == GENERIC_FAILURE
                    and response.headers["www-authenticate"] == "Bearer"
                    for response in read_responses
                )
                for write_token in [WRITE_TOKEN, NEXT_WRITE_TOKEN, OPAQUE_WRITE_TOKEN]:
                    status, _, _ = _asgi_status(client.app, path, token=write_token)
                    assert status not in {401, 403}
                continue
            for write_token in [WRITE_TOKEN, NEXT_WRITE_TOKEN, OPAQUE_WRITE_TOKEN]:
                write_response = client.request(method, path, headers=_bearer(write_token))
                assert not (
                    write_response.status_code in {401, 403}
                    and write_response.json() == GENERIC_FAILURE
                ), (method, path, write_response.text)
            if _is_mutating(method, path):
                assert all(
                    response.status_code == 401
                    and response.json() == GENERIC_FAILURE
                    and response.headers["www-authenticate"] == "Bearer"
                    for response in read_responses
                ), (method, path)
                assert client.get("/tasks", headers=_bearer(WRITE_TOKEN)).json() == before
            else:
                assert all(
                    not (response.status_code == 401 and response.json() == GENERIC_FAILURE)
                    for response in read_responses
                ), (method, path)
        assert client.get("/tasks", headers=_bearer(NEXT_READ_TOKEN)).status_code == 200
        assert client.get("/tasks", headers=_bearer(NEXT_WRITE_TOKEN)).status_code == 200
        assert client.get("/tasks", headers=_bearer(OPAQUE_READ_TOKEN)).status_code == 200
        assert client.get("/tasks", headers=_bearer(OPAQUE_READ_TOKEN[:-1])).status_code == 401
        assert (
            client.post(
                "/tasks",
                headers=_bearer(OPAQUE_WRITE_TOKEN),
                json={"repo_id": "r1", "workflow": "spike"},
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/repos",
                headers=_bearer(WRITE_TOKEN),
                json={"id": "live", "name": "live", "git_url": "https://x/live"},
            ).status_code
            == 201
        )
        assert client.get("/repos/live", headers=_bearer(READ_TOKEN)).status_code == 200
        task = client.post(
            "/tasks",
            headers=_bearer(WRITE_TOKEN),
            json={"repo_id": "r1", "workflow": "spike"},
        ).json()
        assert (
            client.put(
                f"/tasks/{task['id']}/artifacts/live",
                headers=_bearer(WRITE_TOKEN),
                content=b"proof",
            ).status_code
            == 204
        )
        assert (
            client.get(
                f"/tasks/{task['id']}/artifacts/live", headers=_bearer(READ_TOKEN)
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/tasks",
                headers=_bearer(WRITE_TOKEN),
                json={"repo_id": "missing", "workflow": "spike"},
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/tasks",
                headers=_bearer(WRITE_TOKEN),
                json={"repo_id": "r1", "workflow": "missing"},
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/tasks",
                headers=_bearer(OPAQUE_WRITE_TOKEN[:-1]),
                json={"repo_id": "r1", "workflow": "spike"},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/tasks",
                headers=_bearer(NEXT_READ_TOKEN),
                json={"repo_id": "r1", "workflow": "spike"},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/tasks",
                headers=_bearer(NEXT_WRITE_TOKEN),
                json={"repo_id": "r1", "workflow": "spike"},
            ).status_code
            == 201
        )


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer one two"},
        {"Authorization": f"bearer {WRITE_TOKEN}"},
        _bearer("unknown"),
        _bearer(WRITE_TOKEN[:-1]),
        _bearer(WRITE_TOKEN + "x"),
        _bearer(READ_TOKEN),
    ],
)
def test_all_authentication_failures_are_indistinguishable(
    tmp_path: Path, headers: dict[str, str]
) -> None:
    # 2119: REQ-035.7.1
    with _client(tmp_path) as client:
        operations = [
            *_rest_operations(client),
            ("GET", "/mcp"),
            ("POST", "/mcp"),
            ("DELETE", "/mcp"),
        ]
        for method, path in operations:
            if headers == _bearer(READ_TOKEN) and not _is_mutating(method, path) and path != "/mcp":
                continue
            response = client.request(method, path, headers=headers)
            assert response.status_code == 401, (method, path, response.text)
            assert response.headers["www-authenticate"] == "Bearer"
            assert response.json() == GENERIC_FAILURE


def test_non_ascii_bearer_token_receives_generic_failure(tmp_path: Path) -> None:
    # 2119: REQ-035.7.1
    with _client(tmp_path) as client:
        status, headers, body = _asgi_status(client.app, "/tasks", token="täken")
        assert status == 401
        assert headers["www-authenticate"] == "Bearer"
        assert json.loads(body) == GENERIC_FAILURE


def test_read_token_can_reach_safe_head_route(tmp_path: Path) -> None:
    # 2119: REQ-035.3.1
    # 2119: REQ-035.23.1
    with _client(tmp_path) as client:
        assert client.head("/openapi.json").status_code == 401
        assert client.head("/openapi.json", headers=_bearer(READ_TOKEN)).status_code == 200
        assert client.head("/openapi.json", headers=_bearer(WRITE_TOKEN)).status_code == 200


def test_read_and_write_tokens_reach_framework_documentation_reads(tmp_path: Path) -> None:
    # 2119: REQ-035.3.1
    with _client(tmp_path) as client:
        for path in ["/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"]:
            for method in ["GET", "HEAD"]:
                for token in [READ_TOKEN, WRITE_TOKEN]:
                    response = client.request(method, path, headers=_bearer(token))
                    assert not (
                        response.status_code == 401 and response.json() == GENERIC_FAILURE
                    ), (method, path, token)


def test_authentication_precedes_route_and_resource_disclosure(tmp_path: Path) -> None:
    # 2119: REQ-035.8.1
    service = _service(tmp_path)
    asyncio.run(service.register_runner("present-runner", host="runner.example"))
    app = create_app(
        service,
        auth_file=_credential_file(tmp_path),
        auth_mode="enforced",
        secrets_dir=tmp_path / "secrets",
    )
    with TestClient(app) as client:
        created = client.post(
            "/tasks",
            headers=_bearer(WRITE_TOKEN),
            json={"repo_id": "r1", "workflow": "spike"},
        ).json()
        task_id = created["id"]
        client.put(
            f"/tasks/{task_id}/artifacts/present",
            headers=_bearer(WRITE_TOKEN),
            content=b"present",
        )
        registration = client.post(
            f"/tasks/{task_id}/registrations",
            headers=_bearer(WRITE_TOKEN),
            json={"container_id": "c1"},
        ).json()["id"]
        paths = [
            "/not-a-route",
            "/tasks/not-a-task",
            f"/tasks/{task_id}",
            "/repos/not-a-repo",
            "/repos/r1",
            "/runners/not-a-runner",
            "/runners/present-runner",
            "/registrations/not-a-registration",
            f"/registrations/{registration}",
            "/tasks/not-a-task/artifacts/not-an-artifact",
            f"/tasks/{task_id}/artifacts/not-an-artifact",
            f"/tasks/{task_id}/artifacts/present",
            "/workflows/not-a-workflow/image-layer",
            "/workflows/spike/image-layer",
            "/mcp/not-a-method",
        ]
        for path in paths:
            for http_method in ["GET", "POST", "PUT", "PATCH", "DELETE"]:
                response = client.request(http_method, path)
                assert (response.status_code, response.json()) == (401, GENERIC_FAILURE)
                assert response.headers["www-authenticate"] == "Bearer"
        for method in ["tools/list", "not/an-mcp-method"]:
            response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method})
            assert (response.status_code, response.json()) == (401, GENERIC_FAILURE)
            assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    ("kwargs"),
    [
        {"params": {name: WRITE_TOKEN}}
        for name in [
            "access_token",
            "access-token",
            "token",
            "auth_token",
            "auth-token",
            "api_key",
            "api-key",
            "accessToken",
            "authToken",
            "apiKey",
            "authorization",
            "Authorization",
        ]
    ]
    + [
        {"cookies": {name: WRITE_TOKEN}}
        for name in [
            "access_token",
            "access-token",
            "token",
            "auth_token",
            "auth-token",
            "api_key",
            "api-key",
            "accessToken",
            "authToken",
            "apiKey",
            "authorization",
        ]
    ]
    + [
        {"headers": {name: WRITE_TOKEN}}
        for name in [
            "X-API-Key",
            "X-Auth-Token",
            "X-Access-Token",
            "Authentication",
            "Proxy-Authorization",
        ]
    ]
    + [
        {"headers": {"Authorization": f"Basic {WRITE_TOKEN}"}},
        {"headers": {"Authorization": WRITE_TOKEN}},
        {"headers": {"Authorization": f"bearer {WRITE_TOKEN}"}},
        {"headers": {"Authorization": "Bearer"}},
        {"headers": {"Authorization": f"Bearer {WRITE_TOKEN} extra"}},
        {"headers": {"Authorization": f" Bearer {WRITE_TOKEN}"}},
        {"headers": {"Authorization": f"Bearer  {WRITE_TOKEN}"}},
        {"headers": {"Authorization": f"Bearer {WRITE_TOKEN}\t"}},
        {"headers": {"Authorization": f"Bearer {WRITE_TOKEN} "}},
    ]
    + [
        {"json": {name: WRITE_TOKEN}}
        for name in [
            "access_token",
            "access-token",
            "token",
            "auth_token",
            "auth-token",
            "api_key",
            "api-key",
            "accessToken",
            "authToken",
            "apiKey",
            "authorization",
        ]
    ],
)
def test_only_authorization_bearer_is_accepted(tmp_path: Path, kwargs: dict[str, object]) -> None:
    # 2119: REQ-035.9.1
    with _client(tmp_path) as client:
        for method, path in [
            *_rest_operations(client),
            ("GET", "/mcp"),
            ("POST", "/mcp"),
            ("DELETE", "/mcp"),
        ]:
            assert client.request(method, path, **kwargs).status_code == 401, (method, path)


def test_health_is_the_only_open_readiness_surface(tmp_path: Path) -> None:
    # 2119: REQ-035.10.1
    with _client(tmp_path) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("mode", ["disabled", "enforced"])
def test_health_omits_retired_permissive_counter(tmp_path: Path, mode: str) -> None:
    # 2119: REQ-035.43.1
    app = (
        create_app(_service(tmp_path), auth_mode="disabled")
        if mode == "disabled"
        else _client(tmp_path).app
    )
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert "x-panopticon-permissive-unauthenticated-total" not in response.headers


def test_source_address_never_exempts_authentication(tmp_path: Path) -> None:
    # 2119: REQ-035.11.1
    outcomes: list[tuple[int, ...]] = []
    route_outcomes: list[tuple[tuple[int, int, int], ...]] = []
    mcp_method_outcomes: list[tuple[tuple[int, int, int], ...]] = []
    for index, address in enumerate(
        [
            "127.0.0.1",
            "127.0.0.2",
            "127.255.255.255",
            "::1",
            "100.64.0.0",
            "100.64.1.2",
            "100.127.255.255",
            "fd7a:115c:a1e0::2",
            "10.0.0.7",
            "172.16.0.7",
            "192.168.1.7",
            "203.0.113.9",
            "8.8.8.8",
        ]
    ):
        case_dir = tmp_path / str(index)
        case_dir.mkdir()
        app = _client(case_dir).app
        with TestClient(app, client=(address, 12345)) as client:
            outcomes.append(
                (
                    client.get("/tasks").status_code,
                    client.get("/tasks", headers=_bearer(READ_TOKEN)).status_code,
                    client.get("/tasks", headers=_bearer(WRITE_TOKEN)).status_code,
                    client.post("/tasks", json={"repo_id": "r1", "workflow": "spike"}).status_code,
                    client.post(
                        "/tasks",
                        headers=_bearer(READ_TOKEN),
                        json={"repo_id": "r1", "workflow": "spike"},
                    ).status_code,
                    client.post(
                        "/tasks",
                        headers=_bearer(WRITE_TOKEN),
                        json={"repo_id": "r1", "workflow": "spike"},
                    ).status_code,
                    client.post(
                        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
                    ).status_code,
                    client.post(
                        "/mcp",
                        headers=_bearer(READ_TOKEN),
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    ).status_code,
                    client.post(
                        "/mcp",
                        headers=_bearer(WRITE_TOKEN),
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    ).status_code,
                )
            )
            route_outcomes.append(
                tuple(
                    (
                        client.request(method, path).status_code,
                        client.request(method, path, headers=_bearer(READ_TOKEN)).status_code,
                        (
                            _asgi_status(client.app, path, token=WRITE_TOKEN, client_host=address)[
                                0
                            ]
                            if path.endswith("/live")
                            else client.request(
                                method, path, headers=_bearer(WRITE_TOKEN)
                            ).status_code
                        ),
                    )
                    for method, path in _rest_operations(client)
                )
            )
            mcp_method_outcomes.append(
                tuple(
                    (
                        client.request(method, "/mcp").status_code,
                        client.request(method, "/mcp", headers=_bearer(READ_TOKEN)).status_code,
                        client.request(method, "/mcp", headers=_bearer(WRITE_TOKEN)).status_code,
                    )
                    for method in ["GET", "POST", "DELETE"]
                )
            )
    assert len(set(outcomes)) == 1
    assert len(set(route_outcomes)) == 1
    assert len(set(mcp_method_outcomes)) == 1
    assert outcomes[0][:7] == (401, 200, 200, 401, 401, 201, 401)


def test_absent_configuration_preserves_legacy_callers(tmp_path: Path) -> None:
    # 2119: REQ-035.12.1
    with TestClient(create_app(_service(tmp_path))) as client:
        missing_route = client.get("/definitely-not-a-route")
        assert missing_route.status_code == 404
        assert missing_route.json() != GENERIC_FAILURE
        for method, path in _rest_operations(client):
            if path.endswith("/live"):
                if path.startswith("/tasks/"):
                    assert client.get(path, params={"container_id": "c"}).status_code == 404
                else:
                    status, _, _ = _asgi_status(client.app, path)
                    assert status != 401
                continue
            response = client.request(method, path)
            assert not (response.status_code == 401 and response.json() == GENERIC_FAILURE)
        for payload in [
            {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_task", "arguments": {"task_id": "missing"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": "panopticon://tasks/missing/artifacts/missing"},
            },
            {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
            {"jsonrpc": "2.0", "id": 5, "method": "resources/templates/list"},
        ]:
            response = client.post("/mcp", json=payload)
            assert not (response.status_code == 401 and response.json() == GENERIC_FAILURE)
        for body in [b"{", b"{}"]:
            response = client.post(
                "/mcp", content=body, headers={"content-type": "application/json"}
            )
            assert not (response.status_code == 401 and response.json() == GENERIC_FAILURE)
        for method in ["GET", "DELETE"]:
            response = client.request(method, "/mcp")
            assert not (response.status_code == 401 and response.json() == GENERIC_FAILURE)


def test_permissive_authentication_mode_is_rejected_before_startup(tmp_path: Path) -> None:
    # 2119: REQ-035.13.1
    with pytest.raises(ValueError, match="authentication mode must be disabled or enforced"):
        create_app(
            _service(tmp_path),
            auth_file=_credential_file(tmp_path),
            auth_mode="permissive",
            secrets_dir=tmp_path / "secrets",
        )


@pytest.mark.parametrize(
    "contents",
    [
        "not-json",
        "[]",
        json.dumps({"read": READ_TOKEN, "write": [WRITE_TOKEN]}),
        json.dumps({"read": [1], "write": [WRITE_TOKEN]}),
        json.dumps({"read": [READ_TOKEN], "write": WRITE_TOKEN}),
        json.dumps({"read": [READ_TOKEN], "write": [1]}),
        json.dumps({"read": [READ_TOKEN], "write": [WRITE_TOKEN], "extra": []}),
        json.dumps({"read": [READ_TOKEN]}),
        json.dumps({"read": [READ_TOKEN], "write": []}),
        json.dumps({"read": [READ_TOKEN], "write": [READ_TOKEN]}),
        json.dumps({"read": [READ_TOKEN], "write": ["=valid-padding-token"]}),
        *[json.dumps({"read": [READ_TOKEN], "write": [token]}) for token in INVALID_TOKEN_VALUES],
        *[json.dumps({"read": [token], "write": [WRITE_TOKEN]}) for token in INVALID_TOKEN_VALUES],
        json.dumps(
            {
                "read": [READ_TOKEN, "shared-token"],
                "write": [WRITE_TOKEN, "shared-token"],
            }
        ),
    ],
)
def test_enforced_mode_rejects_invalid_credential_files(tmp_path: Path, contents: str) -> None:
    # 2119: REQ-035.2.1
    # 2119: REQ-035.14.1
    # 2119: REQ-035.24.1
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    invalid = secrets / "bad.json"
    invalid.write_text(contents)
    invalid.chmod(0o600)
    with pytest.raises(ValueError, match="authentication credential"):
        create_app(
            _service(tmp_path),
            auth_file="bad.json",
            auth_mode="enforced",
            secrets_dir=secrets,
        )


def test_startup_rejects_tokens_colliding_with_fixed_failure_response(
    tmp_path: Path,
) -> None:
    # 2119: REQ-035.40.1
    with _client(tmp_path / "control") as client:
        rejected = client.get("/tasks")
    wire = (
        rejected.text
        + "\n"
        + "\n".join(f"{name}: {value}" for name, value in rejected.headers.items())
    )
    words = re.findall(r"[A-Za-z0-9._~+/-]{12,}", wire)
    tokens = sorted(
        {
            word[start:end]
            for word in words
            for start in range(len(word))
            for end in range(start + 12, len(word) + 1)
        }
    )
    assert "uthentication" in tokens
    assert tokens

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    credential = secrets / "auth.json"
    for privilege in ["read", "write"]:
        for index, token in enumerate(tokens):
            credentials = {"read": [READ_TOKEN], "write": [WRITE_TOKEN]}
            credentials[privilege] = [token]
            credential.write_text(json.dumps(credentials))
            credential.chmod(0o600)
            with pytest.raises(ValueError, match="authentication credential"):
                create_app(
                    _service(tmp_path / f"{privilege}-{index}"),
                    auth_file=credential.name,
                    auth_mode="enforced",
                    secrets_dir=secrets,
                )


@pytest.mark.parametrize("privilege", ["read", "write"])
@pytest.mark.parametrize("short_token", ["a", "short", "x" * 11])
def test_enforced_mode_rejects_short_tokens(
    tmp_path: Path, privilege: str, short_token: str
) -> None:
    # 2119: REQ-035.33.1
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    credentials = {"read": [READ_TOKEN], "write": [WRITE_TOKEN]}
    credentials[privilege] = [short_token]
    invalid = secrets / "bad.json"
    invalid.write_text(json.dumps(credentials))
    invalid.chmod(0o600)

    with pytest.raises(ValueError, match="authentication credential"):
        create_app(
            _service(tmp_path),
            auth_file="bad.json",
            auth_mode="enforced",
            secrets_dir=secrets,
        )


@pytest.mark.parametrize("mode", ["disabled", "enforced"])
@pytest.mark.parametrize("privilege", ["read", "write"])
@pytest.mark.parametrize("position", ["only", "first", "middle", "last"])
def test_every_overlap_generation_enforces_minimum_token_length(
    tmp_path: Path, mode: str, privilege: str, position: str
) -> None:
    # 2119: REQ-035.33.1
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    credentials = {"read": [READ_TOKEN], "write": [WRITE_TOKEN]}
    valid = credentials[privilege][0]
    credentials[privilege] = {
        "only": ["x" * 11],
        "first": ["x" * 11, valid],
        "middle": [valid, "x" * 11, valid],
        "last": [valid, "x" * 11],
    }[position]
    credential = secrets / "bad.json"
    credential.write_text(json.dumps(credentials))
    credential.chmod(0o600)
    with pytest.raises(ValueError, match="authentication credential"):
        create_app(
            _service(tmp_path),
            auth_file=credential.name,
            auth_mode=mode,
            secrets_dir=secrets,
        )


@pytest.mark.parametrize("mode", ["disabled", "enforced"])
@pytest.mark.parametrize("privilege", ["read", "write"])
def test_supported_modes_accept_twelve_character_tokens(
    tmp_path: Path, mode: str, privilege: str
) -> None:
    # 2119: REQ-035.33.1
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    credentials = {"read": [READ_TOKEN], "write": [WRITE_TOKEN]}
    credentials[privilege] = ["x" * 12]
    boundary = secrets / "boundary.json"
    boundary.write_text(json.dumps(credentials))
    boundary.chmod(0o600)

    with TestClient(
        create_app(
            _service(tmp_path),
            auth_file="boundary.json",
            auth_mode=mode,
            secrets_dir=secrets,
        )
    ):
        pass


def test_enforced_mode_rejects_escaping_reference(tmp_path: Path) -> None:
    # 2119: REQ-035.14.1
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"read": [READ_TOKEN], "write": [WRITE_TOKEN]}))
    (secrets / "escaping-link.json").symlink_to(outside)
    service = _service(tmp_path)
    for reference in ["../outside.json", str(outside.resolve()), "escaping-link.json"]:
        with pytest.raises(ValueError, match="authentication credential"):
            create_app(
                service,
                auth_file=reference,
                auth_mode="enforced",
                secrets_dir=secrets,
            )


def test_enforced_mode_rejects_absent_reference(tmp_path: Path) -> None:
    # 2119: REQ-035.14.1
    with pytest.raises(ValueError, match="authentication credential"):
        create_app(
            _service(tmp_path),
            auth_file=None,
            auth_mode="enforced",
            secrets_dir=tmp_path / "secrets",
        )


@pytest.mark.parametrize("reference", ["missing.json", "unreadable.json"])
def test_enforced_mode_rejects_missing_or_unreadable_reference(
    tmp_path: Path, reference: str
) -> None:
    # 2119: REQ-035.14.1
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    if reference == "unreadable.json":
        (secrets / reference).write_text(json.dumps({"read": [READ_TOKEN], "write": [WRITE_TOKEN]}))
        (secrets / reference).chmod(0)
    try:
        with pytest.raises(ValueError, match="authentication credential"):
            create_app(
                _service(tmp_path),
                auth_file=reference,
                auth_mode="enforced",
                secrets_dir=secrets,
            )
    finally:
        if (secrets / reference).exists():
            (secrets / reference).chmod(0o600)


def test_mcp_requires_a_write_token(tmp_path: Path) -> None:
    # 2119: REQ-035.6.1
    # 2119: REQ-035.9.1
    with _client(tmp_path) as client:
        for method in ["GET", "DELETE"]:
            assert client.request(method, "/mcp", headers=_bearer(READ_TOKEN)).status_code == 401
            assert client.request(method, "/mcp", headers=_bearer(WRITE_TOKEN)).status_code != 401
        for payload in [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_task"}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": "panopticon://tasks/missing/artifacts/missing"},
            },
        ]:
            assert client.post("/mcp", headers=_bearer(READ_TOKEN), json=payload).status_code == 401
            assert (
                client.post("/mcp", headers=_bearer(WRITE_TOKEN), json=payload).status_code != 401
            )


def test_overlapping_tokens_support_rotation(tmp_path: Path) -> None:
    # 2119: REQ-035.2.1
    # 2119: REQ-035.19.1
    with _client(tmp_path) as old_client:
        assert old_client.get("/tasks", headers=_bearer(WRITE_TOKEN)).status_code == 200
        assert old_client.get("/tasks", headers=_bearer(NEXT_WRITE_TOKEN)).status_code == 401
    _credential_file(tmp_path, overlap=True)
    overlap_app = create_app(
        _service(tmp_path / "overlap"),
        auth_file="task-service-auth.json",
        auth_mode="enforced",
        secrets_dir=tmp_path / "secrets",
    )
    with TestClient(overlap_app) as client:
        assert client.get("/tasks", headers=_bearer(WRITE_TOKEN)).status_code == 200

        assert client.get("/tasks", headers=_bearer(NEXT_WRITE_TOKEN)).status_code == 200
        assert client.get("/tasks", headers=_bearer(READ_TOKEN)).status_code == 200
        assert client.get("/tasks", headers=_bearer(NEXT_READ_TOKEN)).status_code == 200
        for token in [WRITE_TOKEN, NEXT_WRITE_TOKEN]:
            assert (
                client.post(
                    "/tasks",
                    headers=_bearer(token),
                    json={"repo_id": "r1", "workflow": "spike"},
                ).status_code
                == 201
            )
        (tmp_path / "secrets" / "task-service-auth.json").write_text(
            json.dumps({"read": [NEXT_READ_TOKEN], "write": [NEXT_WRITE_TOKEN]})
        )
        # The running service keeps its loaded overlap until the explicit reload/restart boundary.
        assert client.get("/tasks", headers=_bearer(WRITE_TOKEN)).status_code == 200
        assert client.get("/tasks", headers=_bearer(NEXT_WRITE_TOKEN)).status_code == 200
        assert client.get("/tasks", headers=_bearer(READ_TOKEN)).status_code == 200
        assert client.get("/tasks", headers=_bearer(NEXT_READ_TOKEN)).status_code == 200
    rotated_app = create_app(
        _service(tmp_path / "rotated"),
        auth_file="task-service-auth.json",
        auth_mode="enforced",
        secrets_dir=tmp_path / "secrets",
    )
    with TestClient(rotated_app) as client:
        assert client.get("/tasks", headers=_bearer(WRITE_TOKEN)).status_code == 401
        assert client.get("/tasks", headers=_bearer(NEXT_WRITE_TOKEN)).status_code == 200
        assert client.get("/tasks", headers=_bearer(READ_TOKEN)).status_code == 401
        assert client.get("/tasks", headers=_bearer(NEXT_READ_TOKEN)).status_code == 200
        assert (
            client.post(
                "/tasks",
                headers=_bearer(NEXT_WRITE_TOKEN),
                json={"repo_id": "r1", "workflow": "spike"},
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/tasks",
                headers=_bearer(WRITE_TOKEN),
                json={"repo_id": "r1", "workflow": "spike"},
            ).status_code
            == 401
        )


def test_shared_client_authenticates_requests_without_url_leakage() -> None:
    # 2119: REQ-035.15.1
    # 2119: REQ-035.17.1
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[], headers={"X-Tasks-Version": "0"})

    http = httpx.Client(base_url="http://service", transport=httpx.MockTransport(respond))
    client = TaskServiceClient(http, token=WRITE_TOKEN)
    assert client._http.headers["authorization"] == f"Bearer {WRITE_TOKEN}"
    client.list_tasks()
    client.list_workflows()
    client.get_task("task")
    client.set_turn("task", "agent")
    next(client.live("task", container_id="container"), None)
    next(client.live_runner("runner"), None)
    assert len(seen) == 6
    assert all(request.headers["authorization"] == f"Bearer {WRITE_TOKEN}" for request in seen)
    assert all(WRITE_TOKEN not in str(request.url) for request in seen)


def test_host_client_factories_resolve_and_send_the_local_write_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-035.16.1
    reference = _credential_file(tmp_path)
    unique_write_token = f"host-factory-{tmp_path.name}"
    (tmp_path / "secrets" / reference).write_text(
        json.dumps({"read": [READ_TOKEN], "write": [unique_write_token]})
    )
    monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_FILE", reference)
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))
    seen: list[httpx.Request] = []
    real_client = httpx.Client

    def fake_http_client(*args: object, **kwargs: object) -> httpx.Client:
        base_url = kwargs.get("base_url", args[0] if args else "http://service")

        def respond(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            status = (
                200
                if request.headers.get("authorization") == f"Bearer {unique_write_token}"
                else 401
            )
            return httpx.Response(status, json=[])

        return real_client(base_url=base_url, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(httpx, "Client", fake_http_client)
    from panopticon.sessionservice.host import _make_client as make_runner_client
    from panopticon.terminal.__main__ import _make_client as make_cli_client
    from panopticon.terminal.console import _make_client as make_console_dashboard_client

    clients = [
        make_runner_client("http://service"),
        make_console_dashboard_client("http://service"),
        make_cli_client("http://service"),
    ]
    assert all(isinstance(client, TaskServiceClient) for client in clients)
    assert all(
        client._http.headers["authorization"] == f"Bearer {unique_write_token}"
        for client in clients
    )
    for client in clients:
        client.list_tasks()
    assert len(seen) == 3
    assert all(
        response.status_code == 200
        for response in [client._http.get("/tasks") for client in clients]
    )
    assert all(
        request.headers["authorization"] == f"Bearer {unique_write_token}" for request in seen
    )


def test_runner_injects_write_token_into_docker_and_shell_tasks_without_command_line_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-035.17.1
    # 2119: REQ-035.18.1
    from panopticon.sessionservice.local_runner import LocalRunner
    from panopticon.sessionservice.shell_runner import ShellRunner
    from panopticon.taskservice.auth import scoped_task_token

    class Recorder:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.mounted_auth: str | None = None

        def __call__(self, args: object, **_kwargs: object) -> str:
            self.calls.append(list(args))  # type: ignore[arg-type]
            if self.calls[-1][:2] == ["docker", "run"]:
                mount = next(
                    item
                    for item in self.calls[-1]
                    if "/run/secrets/panopticon-service-auth" in item
                )
                self.mounted_auth = Path(mount.split(":", 1)[0]).read_text()
            return "%1\n" if "display-message" in self.calls[-1] else ""

    reference = _credential_file(tmp_path)
    monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_FILE", reference)
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))
    docker_recorder = Recorder()
    docker_runner = LocalRunner(
        "http://service",
        run=docker_recorder,
    )
    docker_runner.spawn("t1")
    docker_run = next(call for call in docker_recorder.calls if call[:2] == ["docker", "run"])
    assert WRITE_TOKEN not in " ".join(docker_run)
    assert READ_TOKEN not in " ".join(docker_run)
    assert "PANOPTICON_SERVICE_AUTH_FILE=/run/secrets/panopticon-service-auth" in docker_run
    assert "--env-file" not in docker_run
    auth_mount = next(
        argument for argument in docker_run if "/run/secrets/panopticon-service-auth" in argument
    )
    assert str((tmp_path / "secrets" / reference).resolve()) not in auth_mount
    mounted_snapshot = Path(auth_mount.split(":", 1)[0])
    assert json.loads(docker_recorder.mounted_auth or "") == {
        "task": derive_task_capability(WRITE_TOKEN, "t1")
    }
    assert mounted_snapshot.is_file()
    docker_runner.stop("panopticon-t1")
    assert not mounted_snapshot.exists()

    shell_recorder = Recorder()
    ShellRunner(
        "http://service",
        run=shell_recorder,
    ).spawn("t2", script="sleep 0.1; panopticon_advance", env_file=None)
    command = shell_recorder.calls[-1][-1]
    assert WRITE_TOKEN not in command
    assert READ_TOKEN not in command
    assert "PANOPTICON_SERVICE_AUTH_FILE" in command
    assert "Authorization: Bearer" in command
    assert "r1.env" not in command
    shell_snapshot_match = re.search(
        r"export PANOPTICON_SERVICE_AUTH_FILE=(?:'([^']+)'|(\S+))", command
    )
    assert shell_snapshot_match is not None
    shell_snapshot = Path(shell_snapshot_match.group(1) or shell_snapshot_match.group(2))
    assert shell_snapshot != (tmp_path / "secrets" / reference).resolve()
    assert json.loads(shell_snapshot.read_text()) == {
        "read": [],
        "write": [scoped_task_token(WRITE_TOKEN, "t2")],
    }
    recorded_live = tmp_path / "shell-live-curl-arguments"
    live_env = {
        "PATH": "/usr/bin:/bin",
        "PANOPTICON_SERVICE_URL": "http://service",
        "PANOPTICON_TASK_ID": "t2",
    }
    recorded_live_input = tmp_path / "shell-live-curl-input"
    executable_command = f"""curl() {{ printf 'CALL\\n' >> {recorded_live_input}; cat >> {recorded_live_input}; printf '\\nEND\\n' >> {recorded_live_input}; printf 'CALL\\n'; printf '%s\\n' "$@"; }} >> {recorded_live}
{command}
"""
    subprocess.run(["sh", "-c", executable_command], env=live_env, check=True)
    calls = recorded_live.read_text().split("CALL\n")[1:]
    live_call_index = next(index for index, call in enumerate(calls) if "/tasks/t2/live" in call)
    live_call = calls[live_call_index]
    curl_inputs = recorded_live_input.read_text().split("CALL\n")[1:]
    assert WRITE_TOKEN not in live_call
    assert "Authorization: Bearer" in curl_inputs[live_call_index]
    assert scoped_task_token(WRITE_TOKEN, "t2") in curl_inputs[live_call_index]


def test_container_python_callers_and_shell_library_use_injected_auth_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-035.17.1
    from panopticon.container.agent import _default_client
    from panopticon.container.entrypoint import _make_client
    from panopticon.harnesses.claude import write_mcp_config
    from panopticon.harnesses.codex import render_config
    from panopticon.harnesses.pi import TURN_EXTENSION
    from panopticon.sessionservice.shell_runner import _TASK_LIB

    reference = _credential_file(tmp_path)
    monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_FILE", reference)
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))
    for client in [_default_client("http://service"), _make_client("http://service")]:
        assert client._http.headers["authorization"] == f"Bearer {WRITE_TOKEN}"
    claude_mcp = json.loads(
        write_mcp_config(tmp_path / "claude", "http://service", authenticated=True).read_text()
    )
    assert claude_mcp["mcpServers"]["panopticon"]["headers"] == {
        "Authorization": "Bearer ${PANOPTICON_SERVICE_AUTH_TOKEN}"
    }
    assert 'bearer_token_env_var = "PANOPTICON_SERVICE_AUTH_TOKEN"' in render_config(
        "http://service", "", tmp_path, authenticated=True
    )
    assert "const token = process.env.PANOPTICON_SERVICE_AUTH_TOKEN" in TURN_EXTENSION
    assert '...(token ? { "authorization": `Bearer ${token}` } : {})' in TURN_EXTENSION
    assert "PANOPTICON_SERVICE_AUTH_FILE" in _TASK_LIB
    assert "Authorization: Bearer" in _TASK_LIB
    recorded = tmp_path / "curl-arguments"
    recorded_input = tmp_path / "curl-input"
    shell = f"""curl() {{ cat > {recorded_input}; printf '%s\\n' "$@" > {recorded}; }}
{_TASK_LIB}
panopticon_advance
"""
    env = {
        "PATH": "/usr/bin:/bin",
        "PANOPTICON_SERVICE_URL": "http://service",
        "PANOPTICON_TASK_ID": "task",
        "PANOPTICON_SERVICE_AUTH_FILE": str(tmp_path / "secrets" / reference),
        "PANOPTICON_PYTHON": sys.executable,
    }
    subprocess.run(["sh", "-c", shell], env=env, check=True)
    arguments = recorded.read_text()
    assert WRITE_TOKEN not in arguments
    assert "Authorization: Bearer" in recorded_input.read_text()
    assert WRITE_TOKEN in recorded_input.read_text()


def test_pi_operation_keeps_runtime_token_out_of_curl_argv(tmp_path: Path) -> None:
    # 2119: REQ-035.18.1
    # 2119: REQ-035.21.1
    # 2119: REQ-035.41.1
    from panopticon.harnesses.pi import operation_instructions

    instructions = operation_instructions(
        "advance", "COMPLETE", "task", "http://service", authenticated=True
    )
    command = instructions.split("needed): `", 1)[1].split("`. ", 1)[0]
    argv = tmp_path / "curl-argv"
    stdin = tmp_path / "curl-stdin"
    shell = f"""curl() {{ cat > {stdin}; printf '%s\\n' "$@" > {argv}; }}
set -x
{command}
"""
    env = {
        "PATH": "/usr/bin:/bin",
        "PANOPTICON_SERVICE_AUTH_TOKEN": WRITE_TOKEN,
    }
    completed = subprocess.run(
        ["sh", "-c", shell], env=env, check=True, text=True, capture_output=True
    )
    assert WRITE_TOKEN not in argv.read_text()
    assert WRITE_TOKEN not in completed.stderr
    assert "--config\n-\n" in argv.read_text()
    assert stdin.read_text() == f'header = "Authorization: Bearer {WRITE_TOKEN}"\n'


def test_artifact_rest_fallback_keeps_runtime_token_out_of_curl_argv(tmp_path: Path) -> None:
    # 2119: REQ-035.17.1
    # 2119: REQ-035.18.1
    # 2119: REQ-035.41.1
    from panopticon.core.artifact_skills import ARTIFACT_SKILL

    command = ARTIFACT_SKILL.instructions.split("without MCP, send the artifact bytes with `", 1)[
        1
    ].split("`", 1)[0]
    artifact = tmp_path / "report.md"
    artifact.write_text("proof")
    command = command.replace("<artifact-file>", str(artifact)).replace("<name>", "report.md")
    argv = tmp_path / "curl-argv"
    stdin = tmp_path / "curl-stdin"
    shell = f"""curl() {{ cat > {stdin}; printf '%s\\n' "$@" > {argv}; }}
set -x
{command}
"""
    completed = subprocess.run(
        ["sh", "-c", shell],
        env={
            "PATH": "/usr/bin:/bin",
            "PANOPTICON_SERVICE_AUTH_TOKEN": WRITE_TOKEN,
            "PANOPTICON_SERVICE_URL": "http://service",
            "PANOPTICON_TASK_ID": "task",
        },
        text=True,
        capture_output=True,
        check=True,
    )
    assert WRITE_TOKEN not in argv.read_text()
    assert WRITE_TOKEN not in completed.stderr
    assert "--config\n-\n" in argv.read_text()
    assert "--data-binary\n@" in argv.read_text()
    assert stdin.read_text() == f'header = "Authorization: Bearer {WRITE_TOKEN}"\n'


def test_artifact_rest_fallback_omits_authorization_in_disabled_mode(tmp_path: Path) -> None:
    # 2119: REQ-035.37.1
    from panopticon.core.artifact_skills import ARTIFACT_SKILL

    command = ARTIFACT_SKILL.instructions.split("without MCP, send the artifact bytes with `", 1)[
        1
    ].split("`", 1)[0]
    artifact = tmp_path / "report.md"
    artifact.write_text("proof")
    command = command.replace("<artifact-file>", str(artifact)).replace("<name>", "report.md")
    service = _service(tmp_path / "service")
    task = asyncio.run(service.create_task("r1", "spike"))
    app = create_app(service, auth_mode="disabled")
    received_headers: list[dict[str, str]] = []

    @app.middleware("http")
    async def record_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        received_headers.append({name.casefold(): value for name, value in request.headers.items()})
        return await call_next(request)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.01)
        assert server.started
        service_url = f"http://127.0.0.1:{port}"
        subprocess.run(
            ["sh", "-c", command],
            env={
                "HOME": str(tmp_path),
                "PATH": "/usr/bin:/bin",
                "PANOPTICON_SERVICE_URL": service_url,
                "PANOPTICON_TASK_ID": task.id,
            },
            check=True,
        )
    finally:
        server.should_exit = True
        thread.join()
        listener.close()
    assert len(received_headers) == 1
    assert "authorization" not in received_headers[0]
    assert asyncio.run(service.get_artifact(task.id, "report.md")) == b"proof"


@pytest.mark.parametrize("harness_name", ["pi", "outfitter"])
def test_artifact_fallback_survives_real_harness_rendering(
    tmp_path: Path, harness_name: str
) -> None:
    # 2119: REQ-035.41.1
    from panopticon.core.artifact_skills import ARTIFACT_SKILL
    from panopticon.harnesses import BootstrapContext
    from panopticon.harnesses.outfitter import OutfitterHarness
    from panopticon.harnesses.pi import PiHarness

    harness = PiHarness() if harness_name == "pi" else OutfitterHarness()
    harness.bootstrap(
        BootstrapContext(
            home=tmp_path,
            cwd=Path("/workspace"),
            service_url="http://service",
            task_id="task",
            skills=[ARTIFACT_SKILL],
            environ={"PANOPTICON_SERVICE_AUTH_TOKEN": WRITE_TOKEN},
        )
    )
    rendered = (tmp_path / ".agents" / "skills" / "artifacts" / "SKILL.md").read_text()
    command = rendered.split("without MCP, send the artifact bytes with `", 1)[1].split("`", 1)[0]
    artifact = tmp_path / "report.md"
    artifact.write_text("proof")
    command = command.replace("<artifact-file>", str(artifact)).replace("<name>", "report.md")
    argv = tmp_path / f"{harness_name}-argv"
    stdin = tmp_path / f"{harness_name}-stdin"
    completed = subprocess.run(
        [
            "sh",
            "-c",
            f"curl() {{ cat > {stdin}; printf '%s\\n' \"$@\" > {argv}; }}\nset -x\n{command}",
        ],
        env={
            "PATH": "/usr/bin:/bin",
            "PANOPTICON_SERVICE_AUTH_TOKEN": WRITE_TOKEN,
            "PANOPTICON_SERVICE_URL": "http://service",
            "PANOPTICON_TASK_ID": "task",
        },
        text=True,
        capture_output=True,
        check=True,
    )
    assert WRITE_TOKEN not in argv.read_text()
    assert WRITE_TOKEN not in completed.stderr
    assert stdin.read_text() == f'header = "Authorization: Bearer {WRITE_TOKEN}"\n'
