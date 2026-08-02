"""Executable contract for REQ-034 task-service authentication.

These tests intentionally describe the public seam before its implementation. Authentication is
configured with the same host-local filename reference operators use at runtime; tests never rely
on a database-backed secret.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from panopticon.client import TaskServiceClient
from panopticon.core.models import Repo
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike

READ_TOKEN = "phone-reader-token"
WRITE_TOKEN = "fleet-writer-token"
NEXT_WRITE_TOKEN = "fleet-writer-token-next"
NEXT_READ_TOKEN = "phone-reader-token-next"
OPAQUE_READ_TOKEN = "opaque.!~*'()-_+:/@"
OPAQUE_WRITE_TOKEN = "write.!~*'()-_+:/@"
GENERIC_FAILURE = {"detail": "authentication required"}


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
    (secrets / "task-service-auth.json").write_text(json.dumps({"read": read, "write": write}))
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
    return [
        (method.upper(), _route_path(path))
        for path, path_item in client.app.openapi()["paths"].items()
        if path != "/healthz" and not path.startswith("/mcp")
        for method in path_item
        if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"}
    ]


def _is_mutating(method: str, path: str) -> bool:
    return method != "GET" or path.endswith("/live")


def _asgi_status(
    app: object,
    path: str,
    *,
    token: str | None = None,
    client_host: str = "testclient",
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
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": (client_host, 12345),
        "server": ("testserver", 80),
        "root_path": "",
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
    # 2119: REQ-034.1.1
    # 2119: REQ-034.14.1
    # 2119: REQ-034.18.1
    caplog.set_level("DEBUG", logger="panopticon")
    with _client(tmp_path) as client:
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
        validation = client.post(
            "/tasks", headers=_bearer(WRITE_TOKEN), json={"repo_id": WRITE_TOKEN}
        )
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
        not_found = client.get(f"/tasks/{WRITE_TOKEN}", headers=_bearer(WRITE_TOKEN))
        serialized = (
            repo.text
            + client.get(f"/tasks/{task['id']}", headers=_bearer(WRITE_TOKEN)).text
            + validation.text
            + domain_failure.text
            + image_layer.text
            + not_found.text
        )
        assert READ_TOKEN not in serialized
        assert WRITE_TOKEN not in serialized
        rejected = client.post(
            "/tasks",
            headers=_bearer(READ_TOKEN),
            json={"repo_id": "r1", "workflow": "spike"},
        )
        assert rejected.json() == GENERIC_FAILURE
        assert client.put(
            "/tasks/missing/artifacts/proof", headers=_bearer(WRITE_TOKEN), content=b"safe"
        ).status_code in {204, 404}
    assert READ_TOKEN not in caplog.text
    assert WRITE_TOKEN not in caplog.text
    secrets = tmp_path / "bad-secrets"
    secrets.mkdir()
    (secrets / "bad.json").write_text(json.dumps({"read": [WRITE_TOKEN], "write": [WRITE_TOKEN]}))
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
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.resolve() not in credential_paths:
            assert READ_TOKEN.encode() not in path.read_bytes()
            assert WRITE_TOKEN.encode() not in path.read_bytes()
            assert file_only_token.encode() not in path.read_bytes()
    assert READ_TOKEN not in caplog.text
    assert WRITE_TOKEN not in caplog.text
    assert file_only_token not in caplog.text
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


def test_tokens_never_reach_any_failure_body_or_spawned_command(tmp_path: Path) -> None:
    # 2119: REQ-034.18.1
    with _client(tmp_path) as client:
        for method, path in _rest_operations(client):
            body = client.request(method, path).content
            assert READ_TOKEN.encode() not in body
            assert WRITE_TOKEN.encode() not in body

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
    assert READ_TOKEN not in emitted
    assert WRITE_TOKEN not in emitted


def test_read_and_write_tokens_can_read_but_only_write_token_can_mutate(tmp_path: Path) -> None:
    # 2119: REQ-034.2.1
    # 2119: REQ-034.3.1
    # 2119: REQ-034.4.1
    # 2119: REQ-034.5.1
    # 2119: REQ-034.9.1
    with TestClient(
        create_app(
            _service(tmp_path),
            auth_file=_credential_file(tmp_path, overlap=True, opaque=True),
            auth_mode="enforced",
            secrets_dir=tmp_path / "secrets",
        )
    ) as client:
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
                    assert status != 401
                continue
            for write_token in [WRITE_TOKEN, NEXT_WRITE_TOKEN, OPAQUE_WRITE_TOKEN]:
                write_response = client.request(method, path, headers=_bearer(write_token))
                assert write_response.status_code != 401, (method, path, write_response.text)
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
    # 2119: REQ-034.7.1
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
    # 2119: REQ-034.7.1
    with _client(tmp_path) as client:
        status, headers, body = _asgi_status(client.app, "/tasks", token="täken")
        assert status == 401
        assert headers["www-authenticate"] == "Bearer"
        assert json.loads(body) == GENERIC_FAILURE


def test_authentication_precedes_route_and_resource_disclosure(tmp_path: Path) -> None:
    # 2119: REQ-034.8.1
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
    # 2119: REQ-034.9.1
    with _client(tmp_path) as client:
        for method, path in [
            *_rest_operations(client),
            ("GET", "/mcp"),
            ("POST", "/mcp"),
            ("DELETE", "/mcp"),
        ]:
            assert client.request(method, path, **kwargs).status_code == 401, (method, path)


def test_health_is_the_only_open_readiness_surface(tmp_path: Path) -> None:
    # 2119: REQ-034.10.1
    with _client(tmp_path) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_source_address_never_exempts_authentication(tmp_path: Path) -> None:
    # 2119: REQ-034.11.1
    outcomes: list[tuple[int, ...]] = []
    route_outcomes: list[tuple[tuple[int, int, int], ...]] = []
    for index, address in enumerate(
        ["127.0.0.1", "::1", "100.64.1.2", "fd7a:115c:a1e0::2", "203.0.113.9"]
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
    assert len(set(outcomes)) == 1
    assert len(set(route_outcomes)) == 1
    assert outcomes[0][:7] == (401, 200, 200, 401, 401, 201, 401)


def test_absent_configuration_preserves_legacy_callers(tmp_path: Path) -> None:
    # 2119: REQ-034.12.1
    with TestClient(create_app(_service(tmp_path))) as client:
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
        for method in ["GET", "DELETE"]:
            response = client.request(method, "/mcp")
            assert not (response.status_code == 401 and response.json() == GENERIC_FAILURE)


def test_permissive_mode_accepts_legacy_and_authenticated_callers(tmp_path: Path) -> None:
    # 2119: REQ-034.13.1
    with _client(tmp_path, mode="permissive") as client:
        for method, path in _rest_operations(client):
            if path.endswith("/live"):
                continue
            legacy = client.request(method, path)
            authenticated = client.request(method, path, headers=_bearer(WRITE_TOKEN))
            assert not (legacy.status_code in {401, 403} and legacy.json() == GENERIC_FAILURE)
            assert not (
                authenticated.status_code in {401, 403} and authenticated.json() == GENERIC_FAILURE
            )
            assert authenticated.status_code == legacy.status_code
            if not _is_mutating(method, path):
                reader = client.request(method, path, headers=_bearer(READ_TOKEN))
                assert not (reader.status_code in {401, 403} and reader.json() == GENERIC_FAILURE)
        for payload in [
            {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
            {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
            {"jsonrpc": "2.0", "id": 6, "method": "resources/templates/list"},
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "get_task", "arguments": {"task_id": "missing"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": "panopticon://tasks/missing/artifacts/missing"},
            },
        ]:
            for headers in ({}, _bearer(WRITE_TOKEN)):
                response = client.post("/mcp", headers=headers, json=payload)
                assert not (
                    response.status_code in {401, 403} and response.json() == GENERIC_FAILURE
                )
            assert (
                client.post("/mcp", json=payload).status_code
                == client.post("/mcp", headers=_bearer(WRITE_TOKEN), json=payload).status_code
            )
        for method in ["GET", "DELETE"]:
            for headers in ({}, _bearer(WRITE_TOKEN)):
                response = client.request(method, "/mcp", headers=headers)
                assert not (
                    response.status_code in {401, 403} and response.json() == GENERIC_FAILURE
                )
        for headers in ({}, _bearer(WRITE_TOKEN)):
            response = client.get(
                "/tasks/missing/live", params={"container_id": "c"}, headers=headers
            )
            assert response.status_code == 404
        for token in [None, WRITE_TOKEN]:
            status, _, body = _asgi_status(client.app, "/runners/missing/live", token=token)
            assert status not in {401, 403}, body


def test_permissive_mode_requires_a_credential_file() -> None:
    # 2119: REQ-034.13.1
    with pytest.raises(ValueError, match="credential file is required in permissive mode"):
        create_app(object(), auth_mode="permissive")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "contents",
    [
        "not-json",
        "[]",
        json.dumps({"read": READ_TOKEN, "write": [WRITE_TOKEN]}),
        json.dumps({"read": [1], "write": [WRITE_TOKEN]}),
        json.dumps({"read": [READ_TOKEN], "write": WRITE_TOKEN}),
        json.dumps({"read": [READ_TOKEN], "write": [1]}),
        json.dumps({"read": [], "write": [WRITE_TOKEN]}),
        json.dumps({"read": [READ_TOKEN]}),
        json.dumps({"write": [WRITE_TOKEN]}),
        json.dumps({"read": [READ_TOKEN], "write": []}),
        json.dumps({"read": [READ_TOKEN], "write": [READ_TOKEN]}),
        json.dumps(
            {
                "read": [READ_TOKEN, "shared-token"],
                "write": [WRITE_TOKEN, "shared-token"],
            }
        ),
    ],
)
def test_enforced_mode_rejects_invalid_credential_files(tmp_path: Path, contents: str) -> None:
    # 2119: REQ-034.2.1
    # 2119: REQ-034.14.1
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "bad.json").write_text(contents)
    with pytest.raises(ValueError, match="authentication credential"):
        create_app(
            _service(tmp_path),
            auth_file="bad.json",
            auth_mode="enforced",
            secrets_dir=secrets,
        )


def test_enforced_mode_rejects_escaping_reference(tmp_path: Path) -> None:
    # 2119: REQ-034.14.1
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
    # 2119: REQ-034.14.1
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
    # 2119: REQ-034.14.1
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
    # 2119: REQ-034.6.1
    # 2119: REQ-034.9.1
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
    # 2119: REQ-034.2.1
    # 2119: REQ-034.19.1
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
    # 2119: REQ-034.15.1
    # 2119: REQ-034.17.1
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
    # 2119: REQ-034.16.1
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
    tmp_path: Path,
) -> None:
    # 2119: REQ-034.17.1
    # 2119: REQ-034.18.1
    from panopticon.sessionservice.local_runner import LocalRunner
    from panopticon.sessionservice.shell_runner import ShellRunner

    class Recorder:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def __call__(self, args: object, **_kwargs: object) -> str:
            self.calls.append(list(args))  # type: ignore[arg-type]
            return "%1\n" if "display-message" in self.calls[-1] else ""

    reference = _credential_file(tmp_path)
    docker_recorder = Recorder()
    LocalRunner(
        "http://service",
        auth_file=reference,
        secrets_dir=tmp_path / "secrets",
        run=docker_recorder,
    ).spawn("t1")
    docker_run = next(call for call in docker_recorder.calls if call[:2] == ["docker", "run"])
    assert WRITE_TOKEN not in " ".join(docker_run)
    assert READ_TOKEN not in " ".join(docker_run)
    assert "PANOPTICON_SERVICE_AUTH_FILE=/run/secrets/panopticon-service-auth" in docker_run
    assert "--env-file" not in docker_run
    assert any(
        str((tmp_path / "secrets" / reference).resolve()) in argument
        and "/run/secrets/panopticon-service-auth" in argument
        for argument in docker_run
    )

    shell_recorder = Recorder()
    ShellRunner(
        "http://service",
        auth_file=reference,
        secrets_dir=tmp_path / "secrets",
        run=shell_recorder,
    ).spawn("t2", script="sleep 0.1; panopticon_advance", env_file=None)
    command = shell_recorder.calls[-1][-1]
    assert WRITE_TOKEN not in command
    assert READ_TOKEN not in command
    assert "PANOPTICON_SERVICE_AUTH_FILE" in command
    assert "Authorization: Bearer" in command
    assert "r1.env" not in command
    recorded_live = tmp_path / "shell-live-curl-arguments"
    live_env = {
        "PATH": "/usr/bin:/bin",
        "PANOPTICON_SERVICE_URL": "http://service",
        "PANOPTICON_TASK_ID": "t2",
    }
    recorded_live_input = tmp_path / "shell-live-curl-input"
    executable_command = f"""curl() {{ cat >> {recorded_live_input}; printf 'CALL\\n'; printf '%s\\n' "$@"; }} >> {recorded_live}
{command}
"""
    subprocess.run(["sh", "-c", executable_command], env=live_env, check=True)
    calls = recorded_live.read_text().split("CALL\n")
    live_call = next(call for call in calls if "/tasks/t2/live" in call)
    assert WRITE_TOKEN not in live_call
    assert "Authorization: Bearer" in recorded_live_input.read_text()
    assert WRITE_TOKEN in recorded_live_input.read_text()


def test_container_python_callers_and_shell_library_use_injected_auth_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-034.17.1
    from panopticon.container.agent import _default_client
    from panopticon.container.entrypoint import _make_client
    from panopticon.harnesses.claude import write_mcp_config
    from panopticon.harnesses.codex import render_config
    from panopticon.harnesses.pi import TURN_EXTENSION, operation_instructions
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
    assert "Authorization: Bearer $PANOPTICON_SERVICE_AUTH_TOKEN" in operation_instructions(
        "advance", "COMPLETE", "task", "http://service", authenticated=True
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
