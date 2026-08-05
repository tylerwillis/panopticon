"""Executable contract for REQ-048's unified credential-scoping model.

The tests describe the public authorization seam before implementation. They deliberately use the
same credential file, REST application, MCP mount, and runner command surface used in production.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from panopticon.core.models import Repo, Responsibility, Skill
from panopticon.core.state import Complete, InitialState, State
from panopticon.core.workflow import Workflow
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.auth import derive_task_capability, load_client_token
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Orchestrator, Spike

WRITE_TOKEN = "fleet-writer-token"
ROTATED_WRITE_TOKEN = "rotated-fleet-writer-token"
OLD_WRITE_TOKEN = "old-fleet-writer-token"
READ_TOKEN = "phone-reader-token"
SECOND_READ_TOKEN = "second-phone-reader-token"
SCOPE_FAILURE = {"detail": "credential scope forbids operation"}
GENERIC_FAILURE = {"detail": "authentication required"}
PHONE_ORIGIN = "https://phone.example"


class _ScopedWorkflow(Workflow):
    name = "scoped"

    class Working(InitialState):
        label = "WORKING"
        responsibilities = (Responsibility(key="ready", description="Ready."),)
        transitions = ("REVIEW",)

    class Review(State):
        label = "REVIEW"
        transitions = (Complete,)

    initial = Working

    def skills(self) -> tuple[Skill, ...]:
        return (
            Skill(
                name="scoped-only",
                description="A workflow-specific marker skill.",
                instructions="Exercise the scoped workflow.",
            ),
        )


class _AlternateOrchestrator(Workflow):
    name = "alternate-orchestrator"
    orchestrates = True

    class Coordinating(InitialState):
        label = "COORDINATING"
        transitions = (Complete,)

    initial = Coordinating


class _PlannedScopedWorkflow(Workflow):
    name = "planned-scoped"

    class Planning(InitialState):
        label = "PLANNING"
        responsibilities = (Responsibility(key="ready", description="Ready."),)
        transitions = ("ITERATING",)

    class Iterating(State):
        label = "ITERATING"
        responsibilities = (Responsibility(key="implemented", description="Implemented."),)
        transitions = (Complete,)

    initial = Planning


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _wire_response(response: object) -> tuple[int, dict[str, str], bytes]:
    return response.status_code, dict(response.headers), response.content  # type: ignore[attr-defined]


def _service(tmp_path: Path) -> TaskService:
    tmp_path.mkdir(parents=True, exist_ok=True)
    service = TaskService(
        SqlAlchemyStore(f"sqlite:///{tmp_path / 'task.db'}"),
        {
            "spike": Spike(),
            "orchestrator": Orchestrator(),
            "alternate-orchestrator": _AlternateOrchestrator(),
            "planned-scoped": _PlannedScopedWorkflow(),
            "scoped": _ScopedWorkflow(),
        },
        FilesystemArtifactStore(tmp_path / "artifacts"),
    )
    asyncio.run(service.init())
    asyncio.run(service.create_repo(Repo(id="r1", name="acme/one", git_url="https://x/r1")))
    asyncio.run(service.create_repo(Repo(id="r2", name="acme/two", git_url="https://x/r2")))
    return service


def _reloaded_service(tmp_path: Path) -> TaskService:
    service = TaskService(
        SqlAlchemyStore(f"sqlite:///{tmp_path / 'task.db'}"),
        {
            "spike": Spike(),
            "orchestrator": Orchestrator(),
            "alternate-orchestrator": _AlternateOrchestrator(),
            "planned-scoped": _PlannedScopedWorkflow(),
            "scoped": _ScopedWorkflow(),
        },
        FilesystemArtifactStore(tmp_path / "artifacts"),
    )
    asyncio.run(service.init())
    return service


def _reloaded_client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            _reloaded_service(tmp_path),
            auth_file="task-service-auth.json",
            auth_mode="enforced",
            secrets_dir=tmp_path / "secrets",
        )
    )


def _credential_file(
    tmp_path: Path, *, read: list[str] | None = None, write: list[str] | None = None
) -> str:
    secrets = tmp_path / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    body: dict[str, list[str]] = {"write": [WRITE_TOKEN] if write is None else write}
    if read is not None:
        body["read"] = read
    path = secrets / "task-service-auth.json"
    path.write_text(json.dumps(body))
    path.chmod(0o600)
    return path.name


def _client(
    tmp_path: Path,
    *,
    read: list[str] | None = None,
    write: list[str] | None = None,
    browser_origins: list[str] | None = None,
) -> TestClient:
    return TestClient(
        create_app(
            _service(tmp_path),
            auth_file=_credential_file(tmp_path, read=read, write=write),
            auth_mode="enforced",
            secrets_dir=tmp_path / "secrets",
            browser_origins=browser_origins,
        )
    )


def _create_task(
    client: TestClient,
    *,
    repo_id: str = "r1",
    workflow: str = "spike",
    governor_task_id: str | None = None,
) -> dict[str, object]:
    response = client.post(
        "/tasks",
        headers=_bearer(WRITE_TOKEN),
        json={
            "repo_id": repo_id,
            "workflow": workflow,
            "governor_task_id": governor_task_id,
        },
    )
    assert response.status_code == 201
    return response.json()


def _task_token(task_id: object, *, root: str = WRITE_TOKEN) -> str:
    return derive_task_capability(root, str(task_id))


def _stream_start_status(
    app: object, path: str, token: str, query: bytes = b"container_id=test-container"
) -> int:
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

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "client": ("testclient", 12345),
        "server": ("testserver", 80),
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))  # type: ignore[operator]
    start = next(message for message in sent if message["type"] == "http.response.start")
    return int(start["status"])


def _stream_start_headers(app: object, path: str, token: str, origin: str) -> dict[str, str]:
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

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"container_id=test-container",
        "headers": [
            (b"authorization", f"Bearer {token}".encode()),
            (b"origin", origin.encode()),
        ],
        "client": ("testclient", 12345),
        "server": ("testserver", 80),
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))  # type: ignore[operator]
    start = next(message for message in sent if message["type"] == "http.response.start")
    return {
        name.decode().lower(): value.decode()
        for name, value in start.get("headers", [])  # type: ignore[union-attr]
    }


def _asgi_denial(app: object, path: str, token: str) -> tuple[int, dict[str, object]]:
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

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"container_id=test-container",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "client": ("testclient", 12345),
        "server": ("testserver", 80),
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))  # type: ignore[operator]
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")  # type: ignore[arg-type]
        for message in sent
        if message["type"] == "http.response.body"
    )
    return int(start["status"]), json.loads(body)


def _assert_capability_generation_revoked_everywhere(
    client: TestClient, *, removed: str, remaining: str
) -> None:
    for route in client.app.routes:
        if not hasattr(route, "methods") or route.path in {
            "/healthz",
            "/docs",
            "/docs/oauth2-redirect",
            "/openapi.json",
        }:
            continue
        path = route.path
        for parameter in ("task_id", "repo_id", "runner_id", "registration_id", "name"):
            path = path.replace("{" + parameter + "}", "missing")
        path = path.replace("{entry_index}", "0").replace("{operation}", "drop")
        for method in route.methods:
            revoked = client.request(method, path, headers=_bearer(removed), json={})
            retained = client.request(method, path, headers=_bearer(remaining), json={})
            assert revoked.status_code == 401, (method, path, revoked.text)
            assert retained.status_code != 401, (method, path, retained.text)


def test_task_capability_is_deterministic_bound_and_forgery_resistant(tmp_path: Path) -> None:
    # 2119: REQ-048.2.1
    # 2119: REQ-048.2.3
    # 2119: REQ-048.2.4
    with _client(tmp_path) as client:
        first = _create_task(client)
        second = _create_task(client)
        token = _task_token(first["id"])
        assert token == _task_token(first["id"])
        assert token != _task_token(second["id"])
        version, encoded_subject, profile, encoded_mac = token.split(".")
        subject = base64.urlsafe_b64decode(encoded_subject + "==").decode()
        mac = base64.urlsafe_b64decode(encoded_mac + "==")
        expected_mac = hmac.new(
            WRITE_TOKEN.encode(),
            b"panopticon-task-capability-v1\0" + subject.encode() + b"\0self",
            hashlib.sha256,
        ).digest()
        assert "=" not in encoded_subject
        assert "=" not in encoded_mac
        assert encoded_subject == base64.urlsafe_b64encode(subject.encode()).decode().rstrip("=")
        assert encoded_mac == base64.urlsafe_b64encode(expected_mac).decode().rstrip("=")
        assert (version, subject, profile, mac) == (
            "ptc1",
            str(first["id"]),
            "self",
            expected_mac,
        )
        for edge_subject, expected_subject in (("???", "Pz8_"), ("~~~", "fn5-")):
            edge_token = derive_task_capability(WRITE_TOKEN, edge_subject)
            edge_encoded_subject = edge_token.split(".")[1]
            assert edge_encoded_subject == expected_subject
            assert set(edge_encoded_subject) <= set(
                "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            )
        assert client.get(f"/tasks/{first['id']}", headers=_bearer(token)).status_code == 200
        assert (
            client.get(
                f"/tasks/{first['id']}", headers=_bearer(_task_token(second["id"]))
            ).status_code
            == 403
        )
        differently_bound = _task_token(second["id"])
        different_existing = client.get(f"/tasks/{first['id']}", headers=_bearer(differently_bound))
        different_missing = client.get("/tasks/missing", headers=_bearer(differently_bound))
        assert _wire_response(different_existing) == _wire_response(different_missing)
        assert (different_existing.status_code, different_existing.json()) == (
            403,
            SCOPE_FAILURE,
        )
        nonexistent_subject = _task_token("never-created-task")
        nonexistent_assertion = client.get(
            f"/tasks/{first['id']}", headers=_bearer(nonexistent_subject)
        )
        assert _wire_response(different_existing) == _wire_response(nonexistent_assertion)
        nonexistent_target = client.get("/tasks/missing", headers=_bearer(nonexistent_subject))
        assert _wire_response(nonexistent_assertion) == _wire_response(nonexistent_target)
        assert (nonexistent_assertion.status_code, nonexistent_assertion.json()) == (
            403,
            SCOPE_FAILURE,
        )
        forged = f"{token[:-1]}{'a' if token[-1] != 'a' else 'b'}"
        nonexistent_valid = _task_token("never-created-task")
        nonexistent_forged = (
            f"{nonexistent_valid[:-1]}{'a' if nonexistent_valid[-1] != 'a' else 'b'}"
        )
        existing = client.get(f"/tasks/{first['id']}", headers=_bearer(forged))
        nonexistent_asserted = client.get(
            f"/tasks/{first['id']}", headers=_bearer(nonexistent_forged)
        )
        missing = client.get("/tasks/not-a-task", headers=_bearer(forged))
        assert existing.status_code == 401
        assert missing.status_code == 401
        assert _wire_response(existing) == _wire_response(missing)
        assert _wire_response(nonexistent_asserted) == _wire_response(existing)
        existing_parts = token.split(".")
        nonexistent_parts = nonexistent_valid.split(".")
        existing_malformed = ".".join([*existing_parts[:2], "operator", existing_parts[3]])
        nonexistent_malformed = ".".join([*nonexistent_parts[:2], "operator", nonexistent_parts[3]])
        malformed_existing_subject = client.get(
            f"/tasks/{first['id']}", headers=_bearer(existing_malformed)
        )
        malformed_nonexistent_subject = client.get(
            f"/tasks/{first['id']}", headers=_bearer(nonexistent_malformed)
        )
        assert (
            malformed_existing_subject.status_code,
            malformed_existing_subject.json(),
        ) == (
            malformed_nonexistent_subject.status_code,
            malformed_nonexistent_subject.json(),
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda parts: ["ptc0", *parts[1:]],
        lambda parts: [parts[0], "bm90LXRoZS10YXNr", *parts[2:]],
        lambda parts: [*parts[:2], "operator", parts[3]],
        lambda parts: [*parts[:3], "not_base64!"],
        lambda parts: parts[:3],
        lambda parts: [*parts, "extra"],
        lambda parts: [parts[0], "", *parts[2:]],
        lambda parts: [*parts[:3], ""],
        lambda parts: [parts[0], f"{parts[1]}=", *parts[2:]],
    ],
)
def test_malformed_or_differently_bound_task_capabilities_are_rejected(
    tmp_path: Path, mutate: object
) -> None:
    # 2119: REQ-048.2.3
    with _client(tmp_path) as client:
        own = _create_task(client)
        token = _task_token(own["id"])
        invalid = ".".join(mutate(token.split(".")))  # type: ignore[operator]
        nonexistent_token = _task_token("never-created-task")
        invalid_nonexistent_subject = ".".join(  # type: ignore[operator]
            mutate(nonexistent_token.split("."))
        )
        response = client.get(f"/tasks/{own['id']}", headers=_bearer(invalid))
        nonexistent_subject_response = client.get(
            f"/tasks/{own['id']}", headers=_bearer(invalid_nonexistent_subject)
        )
        assert response.status_code == 401
        missing = client.get("/tasks/missing", headers=_bearer(invalid))
        assert (response.status_code, dict(response.headers), response.content) == (
            missing.status_code,
            dict(missing.headers),
            missing.content,
        )
        assert (response.status_code, dict(response.headers), response.content) == (
            nonexistent_subject_response.status_code,
            dict(nonexistent_subject_response.headers),
            nonexistent_subject_response.content,
        )
        _assert_capability_generation_revoked_everywhere(client, removed=invalid, remaining=token)
        for payload in (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_task", "arguments": {"task_id": own["id"]}},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": f"task://{own['id']}/artifacts/plan.md"},
            },
        ):
            assert client.post("/mcp", headers=_bearer(invalid), json=payload).status_code == 401


@pytest.mark.parametrize(
    ("root", "task_id"),
    [
        (WRITE_TOKEN, "task-1"),
        (WRITE_TOKEN, "task-2"),
        (OLD_WRITE_TOKEN, "task-1"),
        ("third-fleet-writer-token", "unicode-safe-id"),
    ],
)
def test_capability_derivation_is_deterministic_for_each_input_pair(
    root: str, task_id: str
) -> None:
    # 2119: REQ-048.2.4
    values = {derive_task_capability(root, task_id) for _ in range(20)}
    assert values == {derive_task_capability(root, task_id)}
    script = (
        "from panopticon.taskservice.auth import derive_task_capability; "
        f"print(derive_task_capability({root!r}, {task_id!r}))"
    )
    fresh = subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True
    )
    assert fresh.stdout.strip() == next(iter(values))


def test_overlap_rotation_accepts_each_generation_then_revokes_removed_generation(
    tmp_path: Path,
) -> None:
    # 2119: REQ-048.2.2
    # 2119: REQ-048.3.3
    middle_token = "middle-fleet-writer-token"
    with _client(tmp_path, write=[OLD_WRITE_TOKEN, middle_token, WRITE_TOKEN]) as client:
        task = _create_task(client)
        second_task = _create_task(client)
        old = _task_token(task["id"], root=OLD_WRITE_TOKEN)
        middle = _task_token(task["id"], root=middle_token)
        new = _task_token(task["id"])
        second_old = _task_token(second_task["id"], root=OLD_WRITE_TOKEN)
        second_new = _task_token(second_task["id"])
        assert client.get(f"/tasks/{task['id']}", headers=_bearer(old)).status_code == 200
        assert client.get(f"/tasks/{task['id']}", headers=_bearer(middle)).status_code == 200
        assert client.get(f"/tasks/{task['id']}", headers=_bearer(new)).status_code == 200
        credential = tmp_path / "secrets/task-service-auth.json"
        credential.write_text(json.dumps({"write": [middle_token, WRITE_TOKEN]}))
        credential.chmod(0o600)
        restarted = TestClient(
            create_app(
                _reloaded_service(tmp_path),
                auth_file=credential.name,
                auth_mode="enforced",
                secrets_dir=credential.parent,
            )
        )
        assert restarted.get(f"/tasks/{task['id']}", headers=_bearer(old)).status_code == 401
        assert restarted.get(f"/tasks/{task['id']}", headers=_bearer(middle)).status_code == 200
        assert restarted.get(f"/tasks/{task['id']}", headers=_bearer(new)).status_code == 200
        assert (
            restarted.get(f"/tasks/{second_task['id']}", headers=_bearer(second_old)).status_code
            == 401
        )
        assert (
            restarted.get(f"/tasks/{second_task['id']}", headers=_bearer(second_new)).status_code
            == 200
        )
        assert (
            restarted.put(
                f"/tasks/{task['id']}/slug",
                headers=_bearer(old),
                json={"slug": "revoked-old"},
            ).status_code
            == 401
        )
        for label, capability in (("middle", middle), ("new", new)):
            assert (
                restarted.put(
                    f"/tasks/{task['id']}/slug",
                    headers=_bearer(capability),
                    json={"slug": f"active-{label}"},
                ).status_code
                == 200
            )
        _assert_capability_generation_revoked_everywhere(restarted, removed=old, remaining=middle)
        credential.write_text(json.dumps({"write": [WRITE_TOKEN]}))
        credential.chmod(0o600)
        restarted_again = TestClient(
            create_app(
                _reloaded_service(tmp_path),
                auth_file=credential.name,
                auth_mode="enforced",
                secrets_dir=credential.parent,
            )
        )
        assert (
            restarted_again.get(f"/tasks/{task['id']}", headers=_bearer(middle)).status_code == 401
        )
        assert restarted_again.get(f"/tasks/{task['id']}", headers=_bearer(new)).status_code == 200
        assert (
            restarted_again.put(
                f"/tasks/{task['id']}/slug",
                headers=_bearer(middle),
                json={"slug": "revoked-middle"},
            ).status_code
            == 401
        )
        assert (
            restarted_again.put(
                f"/tasks/{task['id']}/slug",
                headers=_bearer(new),
                json={"slug": "active-new-only"},
            ).status_code
            == 200
        )
        _assert_capability_generation_revoked_everywhere(
            restarted_again, removed=middle, remaining=new
        )


def test_task_token_reads_only_its_task_repo_and_collection_view(tmp_path: Path) -> None:
    # 2119: REQ-048.4.1
    # 2119: REQ-048.4.2
    # 2119: REQ-048.4.3
    with _client(tmp_path) as client:
        own = _create_task(client)
        sibling = _create_task(client, repo_id="r2")
        headers = _bearer(_task_token(own["id"]))
        own_id = own["id"]
        registration = client.post(
            f"/tasks/{own_id}/registrations",
            headers=_bearer(WRITE_TOKEN),
            json={"container_id": "readable-registration", "runner_id": None},
        ).json()
        readable = [
            f"/tasks/{own_id}",
            f"/tasks/{own_id}/transitions",
            f"/tasks/{own_id}/operations",
            f"/tasks/{own_id}/states",
            f"/tasks/{own_id}/skills",
            f"/tasks/{own_id}/briefing",
            f"/tasks/{own_id}/history/0/wake",
            f"/tasks/{own_id}/workflow-overview",
            f"/tasks/{own_id}/artifacts",
            f"/tasks/{own_id}/registrations",
        ]
        for path in readable:
            response = client.get(path, headers=headers)
            assert response.status_code == 200, (path, response.text)
            fleet_response = client.get(path, headers=_bearer(WRITE_TOKEN))
            assert response.content == fleet_response.content
            body = response.json()
            if path == f"/tasks/{own_id}":
                assert body["id"] == own_id
            elif path.endswith("/transitions"):
                assert body == ["COMPLETE", "DROPPED"]
            elif path.endswith("/operations"):
                assert body == {"advance": "COMPLETE", "drop": "DROPPED"}
            elif path.endswith("/states"):
                assert set(body) == {"ITERATING", "COMPLETE", "DROPPED"}
            elif path.endswith("/skills"):
                assert {skill["name"] for skill in body} == {"provision", "artifacts"}
            elif path.endswith(("/briefing", "/wake")):
                assert isinstance(body["briefing"], str) and body["briefing"]
            elif path.endswith("/workflow-overview"):
                assert isinstance(body["overview"], str) and "ITERATING" in body["overview"]
            elif path.endswith("/artifacts"):
                assert body == []
            elif path.endswith("/registrations"):
                assert body == [registration]
        briefing = client.get(f"/tasks/{own_id}/briefing", headers=headers).json()["briefing"]
        wake = client.get(f"/tasks/{own_id}/history/0/wake", headers=headers).json()["briefing"]
        assert briefing.startswith("You are in the **ITERATING** phase of the `spike` workflow.")
        assert wake == (
            f"You have entered ITERATING.\n\n{briefing}\n\n"
            "Relevant agent skills: `/provision`, `/artifacts`."
        )
        assert [task["id"] for task in client.get("/tasks", headers=headers).json()] == [own["id"]]
        repo = client.get("/repos/r1", headers=headers)
        assert repo.status_code == 200
        assert {key: repo.json()[key] for key in ("id", "name", "git_url")} == {
            "id": "r1",
            "name": "acme/one",
            "git_url": "https://x/r1",
        }
        assert client.get("/repos/r2", headers=headers).status_code == 403
        assert client.get(f"/tasks/{sibling['id']}", headers=headers).status_code == 403


def test_orchestrator_collection_includes_governed_descendants_but_not_siblings(
    tmp_path: Path,
) -> None:
    # 2119: REQ-048.4.2
    with _client(tmp_path) as client:
        ancestor = _create_task(client)
        governor = _create_task(
            client, workflow="orchestrator", governor_task_id=str(ancestor["id"])
        )
        peer = _create_task(client, governor_task_id=str(ancestor["id"]))
        child = _create_task(client, governor_task_id=str(governor["id"]))
        grandchild = _create_task(client, governor_task_id=str(child["id"]))
        sibling = _create_task(client)
        response = client.get("/tasks", headers=_bearer(_task_token(governor["id"])))
        assert response.status_code == 200
        assert {task["id"] for task in response.json()} == {
            governor["id"],
            child["id"],
            grandchild["id"],
        }
        assert sibling["id"] not in {task["id"] for task in response.json()}
        assert peer["id"] not in {task["id"] for task in response.json()}
        assert ancestor["id"] not in {task["id"] for task in response.json()}


def test_collection_scope_uses_orchestrates_flag_not_workflow_name(tmp_path: Path) -> None:
    # 2119: REQ-048.4.2
    with _client(tmp_path) as client:
        governor = _create_task(client, workflow="alternate-orchestrator")
        child = _create_task(client, governor_task_id=str(governor["id"]))
        response = client.get("/tasks", headers=_bearer(_task_token(governor["id"])))
        assert {task["id"] for task in response.json()} == {governor["id"], child["id"]}


def test_non_orchestrator_collection_excludes_its_governed_descendants(tmp_path: Path) -> None:
    # 2119: REQ-048.4.2
    # 2119: REQ-048.7.4
    with _client(tmp_path) as client:
        ordinary = _create_task(client)
        _create_task(client, governor_task_id=str(ordinary["id"]))
        response = client.get("/tasks", headers=_bearer(_task_token(ordinary["id"])))
        assert response.status_code == 200
        assert [task["id"] for task in response.json()] == [ordinary["id"]]


def test_each_task_capability_resolves_its_own_repository(tmp_path: Path) -> None:
    # 2119: REQ-048.4.3
    with _client(tmp_path) as client:
        task_r1 = _create_task(client, repo_id="r1")
        task_r2 = _create_task(client, repo_id="r2")
        for task, expected_repo, denied_repo in (
            (
                task_r1,
                {"id": "r1", "name": "acme/one", "git_url": "https://x/r1"},
                "r2",
            ),
            (
                task_r2,
                {"id": "r2", "name": "acme/two", "git_url": "https://x/r2"},
                "r1",
            ),
        ):
            headers = _bearer(_task_token(task["id"]))
            own = client.get(f"/repos/{expected_repo['id']}", headers=headers)
            assert own.status_code == 200
            assert {key: own.json()[key] for key in expected_repo} == expected_repo
            assert client.get(f"/repos/{denied_repo}", headers=headers).status_code == 403


def test_own_workflow_reads_cannot_be_substituted_from_a_sibling_workflow(tmp_path: Path) -> None:
    # 2119: REQ-048.4.1
    with _client(tmp_path) as client:
        own = _create_task(client, workflow="scoped")
        sibling = _create_task(client, workflow="spike")
        headers = _bearer(_task_token(own["id"]))
        suffixes = (
            "transitions",
            "operations",
            "states",
            "skills",
            "briefing",
            "history/0/wake",
            "workflow-overview",
        )
        for suffix in suffixes:
            own_path = f"/tasks/{own['id']}/{suffix}"
            sibling_path = f"/tasks/{sibling['id']}/{suffix}"
            scoped = client.get(own_path, headers=headers)
            fleet_scoped = client.get(own_path, headers=_bearer(WRITE_TOKEN))
            fleet_sibling = client.get(sibling_path, headers=_bearer(WRITE_TOKEN))
            assert scoped.status_code == 200
            assert scoped.content == fleet_scoped.content
            assert scoped.content != fleet_sibling.content
            if suffix == "skills":
                assert {skill["name"] for skill in scoped.json()} == {
                    "provision",
                    "artifacts",
                    "scoped-only",
                }
            assert client.get(sibling_path, headers=headers).status_code == 403


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("put", "/tasks/{id}/slug", {"slug": "mine"}),
        ("put", "/tasks/{id}/url", {"url": "https://example.test/pr/1"}),
        ("put", "/tasks/{id}/turn", {"turn": "agent"}),
        ("put", "/tasks/{id}/blocked", {"blocked": True}),
        ("put", "/tasks/{id}/attention", {"attention": True}),
        ("put", "/tasks/{id}/tokens-used", {"tokens_used": 10}),
        ("put", "/tasks/{id}/token-estimate", {"token_estimate": 20}),
        ("put", "/tasks/{id}/dependencies", {"dep_ids": []}),
        ("put", "/tasks/{id}/state", {"state": "COMPLETE"}),
        ("post", "/tasks/{id}/transition", {"to_state": "COMPLETE"}),
        ("post", "/tasks/{id}/operations/drop", None),
    ],
)
def test_task_token_reaches_its_agent_mutation_surface(
    tmp_path: Path, method: str, path: str, body: dict[str, object]
) -> None:
    # 2119: REQ-048.5.1
    # 2119: REQ-048.8.1
    with _client(tmp_path) as client:
        if path.endswith("/dependencies"):
            # A dependency id is itself a secondary authorization target
            # (mcp-credential-uri-normalization.3.1), so it must be in ``task``'s own
            # self-or-governed scope, not an arbitrary other task.
            task = _create_task(client, workflow="orchestrator")
            dependency = _create_task(client, governor_task_id=str(task["id"]))
            body = {"dep_ids": [dependency["id"]]}
        else:
            task = _create_task(client)
        headers = _bearer(_task_token(task["id"]))
        response = client.request(method, path.format(id=task["id"]), headers=headers, json=body)
        assert response.status_code == 200, response.text
        result = client.get(f"/tasks/{task['id']}", headers=headers).json()
        if path.endswith("/slug"):
            assert result["slug"] == body["slug"]
        elif path.endswith("/url"):
            assert result["url"] == body["url"]
        elif path.endswith("/turn"):
            assert result["turn"] == body["turn"]
        elif path.endswith("/blocked"):
            assert result["blocked"] is body["blocked"]
        elif path.endswith("/attention"):
            assert result["attention"] is body["attention"]
        elif path.endswith("/tokens-used"):
            assert result["tokens_used"] == body["tokens_used"]
        elif path.endswith("/token-estimate"):
            assert result["token_estimate"] == body["token_estimate"]
        elif path.endswith("/dependencies"):
            assert result["depends_on_task_ids"] == body["dep_ids"]
        elif "/operations/" in path:
            assert result["state"] == "DROPPED"
        else:
            assert result["state"] == "COMPLETE"
        with _reloaded_client(tmp_path) as reloaded:
            durable = reloaded.get(f"/tasks/{task['id']}", headers=_bearer(_task_token(task["id"])))
            assert durable.status_code == 200
            assert durable.json() == result


def test_task_capability_resolves_a_real_responsibility_and_then_advances(tmp_path: Path) -> None:
    # 2119: REQ-048.5.1
    with _client(tmp_path) as client:
        task = _create_task(client, workflow="scoped")
        headers = _bearer(_task_token(task["id"]))
        resolved = client.post(
            f"/tasks/{task['id']}/responsibilities",
            headers=headers,
            json={"key": "ready", "status": "met"},
        )
        assert resolved.status_code == 200
        assert resolved.json()["history"][-1]["responsibilities"][0]["status"] == "met"
        advanced = client.post(f"/tasks/{task['id']}/operations/advance", headers=headers)
        assert advanced.status_code == 200
        assert advanced.json()["state"] == "REVIEW"
        assert client.get(f"/tasks/{task['id']}", headers=headers).json()["state"] == "REVIEW"
        with _reloaded_client(tmp_path) as reloaded:
            durable = reloaded.get(f"/tasks/{task['id']}", headers=headers)
            assert durable.status_code == 200
            assert durable.json()["state"] == "REVIEW"
            assert durable.json()["history"][0]["responsibilities"][0]["status"] == "met"


def test_task_capability_durably_persists_alternate_responsibility_effects(
    tmp_path: Path,
) -> None:
    # 2119: REQ-048.5.1
    with _client(tmp_path) as client:
        task = _create_task(client, workflow="scoped")
        task_id = str(task["id"])
        headers = _bearer(_task_token(task_id))
        failed = client.post(
            f"/tasks/{task_id}/responsibilities",
            headers=headers,
            json={"key": "ready", "status": "failed", "comment": "blocked by fixture"},
        )
        assert failed.status_code == 200
        with _reloaded_client(tmp_path) as reloaded:
            durable = reloaded.get(f"/tasks/{task_id}", headers=headers).json()
            responsibility = durable["history"][0]["responsibilities"][0]
            assert responsibility["status"] == "failed"
            assert responsibility["comment"] == "blocked by fixture"


def test_task_capability_persists_both_directions_of_boolean_and_turn_setters(
    tmp_path: Path,
) -> None:
    # 2119: REQ-048.5.1
    with _client(tmp_path) as client:
        task = _create_task(client)
        task_id = str(task["id"])
        headers = _bearer(_task_token(task_id))
        for field in ("blocked", "attention"):
            for value in (True, False):
                response = client.put(
                    f"/tasks/{task_id}/{field}", headers=headers, json={field: value}
                )
                assert response.status_code == 200
                assert client.get(f"/tasks/{task_id}", headers=headers).json()[field] is value
                with _reloaded_client(tmp_path) as reloaded:
                    assert reloaded.get(f"/tasks/{task_id}", headers=headers).json()[field] is value
        for turn in ("agent", "user", "agent"):
            response = client.put(f"/tasks/{task_id}/turn", headers=headers, json={"turn": turn})
            assert response.status_code == 200
            assert client.get(f"/tasks/{task_id}", headers=headers).json()["turn"] == turn
            with _reloaded_client(tmp_path) as reloaded:
                assert reloaded.get(f"/tasks/{task_id}", headers=headers).json()["turn"] == turn


def test_task_capability_durably_clears_dependencies(tmp_path: Path) -> None:
    # 2119: REQ-048.5.1
    with _client(tmp_path) as client:
        task = _create_task(client, workflow="orchestrator")
        dependency = _create_task(client, governor_task_id=str(task["id"]))
        task_id = str(task["id"])
        headers = _bearer(_task_token(task_id))
        assert (
            client.put(
                f"/tasks/{task_id}/dependencies",
                headers=headers,
                json={"dep_ids": [dependency["id"]]},
            ).status_code
            == 200
        )
        cleared = client.put(
            f"/tasks/{task_id}/dependencies", headers=headers, json={"dep_ids": []}
        )
        assert cleared.status_code == 200
        with _reloaded_client(tmp_path) as reloaded:
            assert (
                reloaded.get(f"/tasks/{task_id}", headers=headers).json()["depends_on_task_ids"]
                == []
            )


def test_task_capability_persists_distinct_scalar_and_state_effects(tmp_path: Path) -> None:
    # 2119: REQ-048.5.1
    with _client(tmp_path) as client:
        task = _create_task(client, workflow="scoped")
        task_id = str(task["id"])
        headers = _bearer(_task_token(task_id))
        scalar_cases = (
            ("slug", "slug", ("first-slug", "second-slug")),
            ("url", "url", ("https://example.test/one", "https://example.test/two")),
            ("tokens-used", "tokens_used", (11, 29)),
            ("token-estimate", "token_estimate", (101, 307)),
        )
        for route_field, json_field, values in scalar_cases:
            for value in values:
                response = client.put(
                    f"/tasks/{task_id}/{route_field}",
                    headers=headers,
                    json={json_field: value},
                )
                assert response.status_code == 200
                with _reloaded_client(tmp_path) as reloaded:
                    assert (
                        reloaded.get(f"/tasks/{task_id}", headers=headers).json()[json_field]
                        == value
                    )
        for state in ("REVIEW", "WORKING"):
            response = client.put(f"/tasks/{task_id}/state", headers=headers, json={"state": state})
            assert response.status_code == 200
            with _reloaded_client(tmp_path) as reloaded:
                assert reloaded.get(f"/tasks/{task_id}", headers=headers).json()["state"] == state

        transition_task = _create_task(client, workflow="scoped")
        transition_id = str(transition_task["id"])
        transition_headers = _bearer(_task_token(transition_id))
        assert (
            client.post(
                f"/tasks/{transition_id}/responsibilities",
                headers=transition_headers,
                json={"key": "ready", "status": "met"},
            ).status_code
            == 200
        )
        for state in ("REVIEW", "COMPLETE"):
            response = client.post(
                f"/tasks/{transition_id}/transition",
                headers=transition_headers,
                json={"to_state": state},
            )
            assert response.status_code == 200
            with _reloaded_client(tmp_path) as reloaded:
                assert (
                    reloaded.get(f"/tasks/{transition_id}", headers=transition_headers).json()[
                        "state"
                    ]
                    == state
                )


def test_task_token_can_publish_and_read_only_its_artifacts(tmp_path: Path) -> None:
    # 2119: REQ-048.4.1
    # 2119: REQ-048.5.2
    # 2119: REQ-048.8.1
    with _client(tmp_path) as client:
        own = _create_task(client)
        sibling = _create_task(client)
        headers = _bearer(_task_token(own["id"]))
        own_path = f"/tasks/{own['id']}/artifacts/design.md"
        assert client.put(own_path, headers=headers, content=b"design").status_code == 204
        assert client.get(own_path, headers=headers).content == b"design"
        assert client.get(f"/tasks/{own['id']}/artifacts", headers=headers).json() == ["design.md"]
        assert client.put(own_path, headers=headers, content=b"replacement").status_code == 204
        assert client.get(own_path, headers=headers).content == b"replacement"
        sibling_path = f"/tasks/{sibling['id']}/artifacts/stolen.md"
        assert (
            client.put(
                sibling_path, headers=_bearer(WRITE_TOKEN), content=b"sibling-original"
            ).status_code
            == 204
        )
        assert client.put(sibling_path, headers=headers, content=b"bad").status_code == 403
        assert client.get(sibling_path, headers=headers).status_code == 403
        assert client.get(f"/tasks/{sibling['id']}/artifacts", headers=headers).status_code == 403
        assert client.get(sibling_path, headers=_bearer(WRITE_TOKEN)).content == b"sibling-original"
        new_sibling_path = f"/tasks/{sibling['id']}/artifacts/new.md"
        assert (
            client.put(new_sibling_path, headers=headers, content=b"forbidden-create").status_code
            == 403
        )
        assert client.get(new_sibling_path, headers=_bearer(WRITE_TOKEN)).status_code == 404


def test_task_token_can_hold_only_its_registration_and_liveness(tmp_path: Path) -> None:
    # 2119: REQ-048.4.1
    # 2119: REQ-048.5.3
    with _client(tmp_path) as client:
        own = _create_task(client)
        sibling = _create_task(client)
        headers = _bearer(_task_token(own["id"]))
        registration = client.post(
            f"/tasks/{own['id']}/registrations",
            headers=headers,
            json={"container_id": "own-container", "runner_id": None},
        )
        assert registration.status_code == 201
        assert client.get(f"/tasks/{own['id']}/registrations", headers=headers).json() == [
            registration.json()
        ]
        assert (
            _stream_start_status(client.app, f"/tasks/{own['id']}/live", _task_token(own["id"]))
            == 200
        )
        assert (
            _stream_start_status(client.app, f"/tasks/{sibling['id']}/live", _task_token(own["id"]))
            == 403
        )
        assert (
            client.post(
                f"/tasks/{sibling['id']}/registrations",
                headers=headers,
                json={"container_id": "wrong-container", "runner_id": None},
            ).status_code
            == 403
        )
        assert (
            client.delete(
                f"/registrations/{registration.json()['id']}", headers=headers
            ).status_code
            == 204
        )
        assert client.get(f"/tasks/{own['id']}/registrations", headers=headers).json() == []
        sibling_registration = client.post(
            f"/tasks/{sibling['id']}/registrations",
            headers=_bearer(WRITE_TOKEN),
            json={"container_id": "sibling-container", "runner_id": None},
        ).json()
        assert (
            client.delete(
                f"/registrations/{sibling_registration['id']}", headers=headers
            ).status_code
            == 403
        )


def test_task_capability_liveness_stream_remains_open_for_keepalives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-048.4.1
    # 2119: REQ-048.5.3
    import panopticon.taskservice.api as api_module

    async def immediate_keepalive() -> None:
        return None

    monkeypatch.setattr(api_module, "_wait_for_liveness_keepalive", immediate_keepalive)
    with _client(tmp_path) as client:
        task = _create_task(client)
        messages: list[dict[str, object]] = []
        first = True

        async def receive() -> dict[str, object]:
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": b"", "more_body": False}
            while (
                len([message for message in messages if message["type"] == "http.response.body"])
                < 2
            ):
                await asyncio.sleep(0)
            return {"type": "http.disconnect"}

        async def send(message: dict[str, object]) -> None:
            messages.append(message)

        task_id = str(task["id"])
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": f"/tasks/{task_id}/live",
            "raw_path": f"/tasks/{task_id}/live".encode(),
            "query_string": b"container_id=held-open",
            "headers": [(b"authorization", f"Bearer {_task_token(task_id)}".encode())],
            "client": ("testclient", 12345),
            "server": ("testserver", 80),
            "root_path": "",
        }
        asyncio.run(client.app(scope, receive, send))
        bodies = [
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        ]
        assert bodies[:2] == [b":ok\n", b":keepalive\n"]
        assert all(
            message.get("more_body") is True
            for message in messages
            if message["type"] == "http.response.body" and message.get("body")
        )


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/tasks/{id}", None),
        ("post", "/tasks/{id}/operations/drop", None),
        ("put", "/tasks/{id}/slug", {"slug": "stolen"}),
    ],
)
def test_sibling_and_missing_targets_have_identical_scope_denials(
    tmp_path: Path, method: str, path: str, body: dict[str, object] | None
) -> None:
    # 2119: REQ-048.6.1
    # 2119: REQ-048.6.3
    with _client(tmp_path) as client:
        own = _create_task(client)
        sibling = _create_task(client)
        headers = _bearer(_task_token(own["id"]))
        sibling_before = client.get(f"/tasks/{sibling['id']}", headers=_bearer(WRITE_TOKEN)).json()
        existing = client.request(method, path.format(id=sibling["id"]), headers=headers, json=body)
        missing = client.request(method, path.format(id="missing"), headers=headers, json=body)
        assert (existing.status_code, existing.json()) == (403, SCOPE_FAILURE)
        assert (missing.status_code, missing.json()) == (403, SCOPE_FAILURE)
        sibling_after = client.get(f"/tasks/{sibling['id']}", headers=_bearer(WRITE_TOKEN)).json()
        assert sibling_after == sibling_before


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/repos", {"id": "r3", "name": "x/r3", "git_url": "https://x/r3"}),
        ("patch", "/repos/r1", {"name": "stolen"}),
        ("get", "/workflow-files", None),
        ("put", "/tasks/{id}/claim", {"runner_id": "runner"}),
        ("delete", "/tasks/{id}/claim", None),
        (
            "put",
            "/tasks/{id}/provisioning",
            {
                "branch": "panopticon/task",
                "clone": "/tasks/task",
                "runner_id": "runner",
                "workspace_verified": True,
            },
        ),
        (
            "put",
            "/tasks/{id}/migration",
            {
                "source_runner": "source",
                "destination_runner": "destination",
                "workspace_disposition": "transferred",
                "workspace_method": "archive",
                "session_history_disposition": "transferred",
                "discarded_changes": [],
            },
        ),
        (
            "put",
            "/tasks/{id}/lifecycle",
            {"runner_id": "runner", "phase": "starting", "detail": None},
        ),
        ("delete", "/tasks/{id}/lifecycle", None),
        ("get", "/runners", None),
        ("get", "/runners/runner", None),
        ("get", "/runners/runner/live", None),
        ("post", "/runners/runner/reclaim", None),
    ],
)
def test_task_token_cannot_reach_fleet_control_plane(
    tmp_path: Path, method: str, path: str, body: dict[str, object] | None
) -> None:
    # 2119: REQ-048.6.2
    with _client(tmp_path) as client:
        own = _create_task(client)
        response = client.request(
            method,
            path.format(id=own["id"]),
            headers=_bearer(_task_token(own["id"])),
            json=body,
        )
        assert (response.status_code, response.json()) == (403, SCOPE_FAILURE)


def test_fleet_administration_route_inventory_is_complete_and_task_denied(tmp_path: Path) -> None:
    # 2119: REQ-048.6.2
    expected = {
        ("POST", "/repos"),
        ("PATCH", "/repos/{repo_id}"),
        ("GET", "/workflow-files"),
        ("PUT", "/tasks/{task_id}/claim"),
        ("DELETE", "/tasks/{task_id}/claim"),
        ("PUT", "/tasks/{task_id}/provisioning"),
        ("PUT", "/tasks/{task_id}/migration"),
        ("PUT", "/tasks/{task_id}/lifecycle"),
        ("DELETE", "/tasks/{task_id}/lifecycle"),
        ("PUT", "/tasks/{task_id}/governor"),
        ("PUT", "/tasks/{task_id}/snooze"),
        ("GET", "/tasks/{task_id}/session/input"),
        ("PUT", "/tasks/{task_id}/session/input/{delivery_id}"),
        ("PUT", "/tasks/{task_id}/session/transcript"),
        ("GET", "/runners"),
        ("GET", "/runners/{runner_id}"),
        ("GET", "/runners/{runner_id}/live"),
        ("POST", "/runners/{runner_id}/reclaim"),
    }
    with _client(tmp_path) as client:
        task = _create_task(client)
        policy = client.app.state.credential_scope_policy
        registered = {
            (method, route.path)
            for route in client.app.routes
            if hasattr(route, "methods")
            for method in route.methods
        }
        independently_derived_admin = {
            entry
            for entry in registered
            if (
                (entry[1].startswith("/repos") and entry[0] not in {"GET", "HEAD"})
                or entry[1] == "/workflow-files"
                or entry[1].endswith(
                    ("/claim", "/provisioning", "/migration", "/lifecycle", "/governor", "/snooze")
                )
                or (
                    entry[1].startswith("/tasks/{task_id}/session/")
                    and entry
                    not in {
                        ("POST", "/tasks/{task_id}/session/input"),
                        ("GET", "/tasks/{task_id}/session/input/{delivery_id}"),
                        ("GET", "/tasks/{task_id}/session/transcript"),
                    }
                )
                or entry[1].startswith("/runners")
            )
        }
        assert independently_derived_admin == expected
        assert policy.fleet_administration_rest_surfaces() == expected
        token = _task_token(task["id"])
        for method, template in expected:
            path = (
                template.replace("{task_id}", str(task["id"]))
                .replace("{repo_id}", "r1")
                .replace("{runner_id}", "missing")
            )
            if path.endswith("/live"):
                status, body = _asgi_denial(client.app, path, token)
            else:
                response = client.request(method, path, headers=_bearer(token), json={})
                status, body = response.status_code, response.json()
            assert (status, body) == (403, SCOPE_FAILURE), (method, template)


def test_task_capability_cannot_combine_with_operator_token_for_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-048.6.2
    operator_token = "operator-migration-token"
    monkeypatch.setenv("PANOPTICON_OPERATOR_TOKEN", operator_token)
    with _client(tmp_path) as client:
        task = _create_task(client)
        headers = {
            **_bearer(_task_token(task["id"])),
            "X-Panopticon-Operator-Token": operator_token,
        }
        response = client.put(
            f"/tasks/{task['id']}/migration",
            headers=headers,
            json={
                "source_runner": "one",
                "destination_runner": "two",
                "workspace_disposition": "transferred",
                "workspace_method": "archive",
                "session_history_disposition": "transferred",
                "discarded_changes": [],
            },
        )
        assert (response.status_code, response.json()) == (403, SCOPE_FAILURE)


def test_task_scope_action_table_is_exhaustive_and_relationship_sensitive() -> None:
    # 2119: REQ-048.5.1
    # 2119: REQ-048.6.1
    # 2119: REQ-048.6.2
    # 2119: REQ-048.6.3
    # 2119: REQ-048.7.2
    # 2119: REQ-048.7.3
    # 2119: REQ-048.7.4
    from panopticon.taskservice.auth_scope import (
        Action,
        Principal,
        Relation,
        Target,
        authorize,
        task_targeted_actions,
    )

    expected_task_targeted = {
        "read_task",
        "read_task_metadata",
        "invoke_operation",
        "request_transition",
        "set_state",
        "resolve_responsibility",
        "set_slug",
        "set_url",
        "set_tokens_used",
        "set_token_estimate",
        "set_turn",
        "set_blocked",
        "set_attention",
        "set_dependencies",
        "record_stage_entry_wake",
        "put_artifact",
        "list_artifacts",
        "read_artifact",
        "create_session_input",
        "read_session_input_status",
        "read_session_transcript",
        "register_container",
        "deregister_container",
        "task_liveness",
        "claim_task",
        "provision_task",
        "migrate_task",
        "report_lifecycle",
        "set_governor",
        "snooze_task",
    }
    assert {action.value for action in task_targeted_actions()} == expected_task_targeted
    principal = Principal.task("subject")
    self_target = Target("subject", Relation.SELF, orchestrates=False)
    governed = Target("child", Relation.GOVERNED, orchestrates=True)
    governed_non_orchestrator = Target("child", Relation.GOVERNED, orchestrates=False)
    unrelated = Target("sibling", Relation.UNRELATED, orchestrates=False)
    missing = Target("missing", Relation.MISSING, orchestrates=False)
    expected_self = {
        "read_task",
        "read_task_metadata",
        "invoke_operation",
        "request_transition",
        "set_state",
        "resolve_responsibility",
        "set_slug",
        "set_url",
        "set_tokens_used",
        "set_token_estimate",
        "set_turn",
        "set_blocked",
        "set_attention",
        "set_dependencies",
        "put_artifact",
        "list_artifacts",
        "read_artifact",
        "create_session_input",
        "read_session_input_status",
        "read_session_transcript",
        "register_container",
        "deregister_container",
        "task_liveness",
    }
    expected_child = {
        "read_task",
        "read_task_metadata",
        "put_artifact",
        "list_artifacts",
        "read_artifact",
        "set_slug",
        "set_token_estimate",
        "set_turn",
        "set_dependencies",
    }
    assert {
        action.value
        for action in task_targeted_actions()
        if authorize(principal, action, self_target).allowed
    } == expected_self
    assert {
        action.value
        for action in task_targeted_actions()
        if authorize(principal, action, governed).allowed
    } == expected_child
    assert not any(
        authorize(principal, action, governed_non_orchestrator).allowed
        for action in task_targeted_actions()
    )
    for action in task_targeted_actions():
        existing = authorize(principal, action, unrelated)
        absent = authorize(principal, action, missing)
        assert (existing.allowed, existing.status, existing.body) == (False, 403, SCOPE_FAILURE)
        assert (absent.allowed, absent.status, absent.body) == (False, 403, SCOPE_FAILURE)
    admin_actions = {
        Action.CLAIM_TASK,
        Action.PROVISION_TASK,
        Action.MIGRATE_TASK,
        Action.REPORT_LIFECYCLE,
        Action.SET_GOVERNOR,
        Action.SNOOZE_TASK,
    }
    assert not any(authorize(principal, action, self_target).allowed for action in admin_actions)


def test_every_task_targeted_rest_route_rejects_sibling_and_missing_targets_identically(
    tmp_path: Path,
) -> None:
    # 2119: REQ-048.4.1
    # 2119: REQ-048.6.1
    # 2119: REQ-048.6.3
    # 2119: REQ-048.8.1
    with _client(tmp_path) as client:
        own = _create_task(client)
        sibling = _create_task(client)
        token = _task_token(own["id"])
        headers = _bearer(token)
        surfaces = client.app.state.credential_scope_policy.classified_surfaces()
        task_routes = {
            (method, route.path)
            for route in client.app.routes
            if hasattr(route, "methods") and "{task_id}" in route.path
            for method in route.methods
            if method in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}
        }
        runner_session_surfaces = {
            ("GET", "/tasks/{task_id}/session/input"),
            ("PUT", "/tasks/{task_id}/session/input/{delivery_id}"),
            ("PUT", "/tasks/{task_id}/session/transcript"),
        }
        expected = (task_routes - runner_session_surfaces) | {
            ("DELETE", "/registrations/{registration_id}")
        }
        assert surfaces.task_targeted_rest == expected
        sibling_registration = client.post(
            f"/tasks/{sibling['id']}/registrations",
            headers=_bearer(WRITE_TOKEN),
            json={"container_id": "sibling", "runner_id": None},
        ).json()["id"]
        for method, template in expected:
            if template == "/registrations/{registration_id}":
                paths = [
                    template.replace("{registration_id}", sibling_registration),
                    template.replace("{registration_id}", "missing"),
                ]
            else:
                paths = [
                    template.replace("{task_id}", str(sibling["id"])),
                    template.replace("{task_id}", "missing"),
                ]
                paths = [
                    path.replace("{entry_index}", "0")
                    .replace("{operation}", "drop")
                    .replace("{name}", "artifact.md")
                    for path in paths
                ]
            responses: list[tuple[int, object]] = []
            for path in paths:
                if path.endswith("/live"):
                    responses.append(_asgi_denial(client.app, path, token))
                else:
                    response = client.request(method, path, headers=headers, json={})
                    responses.append((response.status_code, response.json()))
            assert responses == [(403, SCOPE_FAILURE), (403, SCOPE_FAILURE)], (method, template)


def test_orchestrator_can_create_and_preplan_only_its_governed_child(tmp_path: Path) -> None:
    # 2119: REQ-048.7.1
    # 2119: REQ-048.7.2
    # 2119: REQ-048.7.3
    with _client(tmp_path) as client:
        governor = _create_task(client, workflow="orchestrator")
        unrelated = _create_task(client)
        # A dependency id is itself a secondary authorization target (mcp-credential-uri-
        # normalization.3.1): governor's capability is in scope for itself, so this stands in
        # for "some existing task id" in the preplan dependency-setting assertions below without
        # exercising the separately-tested out-of-scope rejection.
        dependency_target = str(governor["id"])
        headers = _bearer(_task_token(governor["id"]))
        child = client.post(
            "/tasks",
            headers=headers,
            json={
                "repo_id": "r1",
                "workflow": "planned-scoped",
                "governor_task_id": governor["id"],
            },
        )
        assert child.status_code == 201
        child_id = child.json()["id"]
        grandchild = _create_task(client, workflow="planned-scoped", governor_task_id=str(child_id))
        great_grandchild = _create_task(
            client, workflow="planned-scoped", governor_task_id=str(grandchild["id"])
        )
        assert client.get(f"/tasks/{child_id}", headers=headers).status_code == 200
        assert client.get(f"/tasks/{grandchild['id']}", headers=headers).status_code == 200
        assert client.get(f"/tasks/{great_grandchild['id']}", headers=headers).status_code == 200
        grandchild_id = str(grandchild["id"])
        descendant_registration = client.post(
            f"/tasks/{grandchild_id}/registrations",
            headers=_bearer(WRITE_TOKEN),
            json={"container_id": "descendant-container", "runner_id": None},
        ).json()
        client.put(
            f"/tasks/{grandchild_id}/artifacts/plan.md",
            headers=_bearer(WRITE_TOKEN),
            content=b"descendant-plan",
        )
        descendant_reads = [
            f"/tasks/{grandchild_id}/transitions",
            f"/tasks/{grandchild_id}/operations",
            f"/tasks/{grandchild_id}/states",
            f"/tasks/{grandchild_id}/skills",
            f"/tasks/{grandchild_id}/briefing",
            f"/tasks/{grandchild_id}/history/0/wake",
            f"/tasks/{grandchild_id}/workflow-overview",
            f"/tasks/{grandchild_id}/artifacts",
            f"/tasks/{grandchild_id}/artifacts/plan.md",
            f"/tasks/{grandchild_id}/registrations",
        ]
        for path in descendant_reads:
            response = client.get(path, headers=headers)
            assert response.status_code == 200, (path, response.text)
        assert (
            client.get(f"/tasks/{grandchild_id}/artifacts/plan.md", headers=headers).content
            == b"descendant-plan"
        )
        assert client.get(f"/tasks/{grandchild_id}/registrations", headers=headers).json() == [
            descendant_registration
        ]
        assert (
            _stream_start_status(
                client.app,
                f"/tasks/{grandchild_id}/live",
                _task_token(governor["id"]),
            )
            == 403
        )
        assert (
            client.put(
                f"/tasks/{grandchild_id}/artifacts/plan.md",
                headers=headers,
                content=b"updated-descendant-plan",
            ).status_code
            == 204
        )
        for field, value in (
            ("slug", "planned-grandchild"),
            ("token-estimate", 200),
            ("turn", "user"),
            ("dependencies", [dependency_target]),
        ):
            json_key = {
                "token-estimate": "token_estimate",
                "dependencies": "dep_ids",
            }.get(field, field)
            response = client.put(
                f"/tasks/{grandchild_id}/{field}",
                headers=headers,
                json={json_key: value},
            )
            assert response.status_code == 200, (field, response.text)
        descendant_responsibility = client.post(
            f"/tasks/{grandchild_id}/responsibilities",
            headers=headers,
            json={"key": "ready", "status": "met"},
        )
        assert descendant_responsibility.status_code == 200
        descendant_persisted = client.get(f"/tasks/{grandchild_id}", headers=headers).json()
        assert descendant_persisted["slug"] == "planned-grandchild"
        assert descendant_persisted["token_estimate"] == 200
        assert descendant_persisted["turn"] == "user"
        assert descendant_persisted["depends_on_task_ids"] == [dependency_target]
        assert (
            client.get(f"/tasks/{grandchild_id}/artifacts/plan.md", headers=headers).content
            == b"updated-descendant-plan"
        )
        great_grandchild_id = str(great_grandchild["id"])
        assert (
            client.put(
                f"/tasks/{great_grandchild_id}/artifacts/plan.md",
                headers=headers,
                content=b"great-grandchild-plan",
            ).status_code
            == 204
        )
        for field, value in (
            ("slug", "planned-great-grandchild"),
            ("token-estimate", 300),
            ("turn", "user"),
            ("dependencies", [dependency_target]),
        ):
            json_key = {
                "token-estimate": "token_estimate",
                "dependencies": "dep_ids",
            }.get(field, field)
            response = client.put(
                f"/tasks/{great_grandchild_id}/{field}",
                headers=headers,
                json={json_key: value},
            )
            assert response.status_code == 200, (field, response.text)
        assert (
            client.post(
                f"/tasks/{great_grandchild_id}/responsibilities",
                headers=headers,
                json={"key": "ready", "status": "met"},
            ).status_code
            == 200
        )
        assert (
            client.put(
                f"/tasks/{child_id}/artifacts/plan.md", headers=headers, content=b"plan"
            ).status_code
            == 204
        )
        assert (
            client.put(
                f"/tasks/{child_id}/token-estimate",
                headers=headers,
                json={"token_estimate": 100},
            ).status_code
            == 200
        )
        assert (
            client.put(
                f"/tasks/{child_id}/slug", headers=headers, json={"slug": "planned-child"}
            ).status_code
            == 200
        )
        assert (
            client.put(
                f"/tasks/{child_id}/turn", headers=headers, json={"turn": "user"}
            ).status_code
            == 200
        )
        assert (
            client.put(
                f"/tasks/{child_id}/dependencies",
                headers=headers,
                json={"dep_ids": [dependency_target]},
            ).status_code
            == 200
        )
        responsibility = client.post(
            f"/tasks/{child_id}/responsibilities",
            headers=headers,
            json={"key": "ready", "status": "met"},
        )
        assert responsibility.status_code == 200
        assert responsibility.json()["history"][-1]["responsibilities"][0]["status"] == "met"
        persisted = client.get(f"/tasks/{child_id}", headers=headers).json()
        assert persisted["slug"] == "planned-child"
        assert persisted["token_estimate"] == 100
        assert persisted["turn"] == "user"
        assert persisted["depends_on_task_ids"] == [dependency_target]
        assert (
            client.get(f"/tasks/{child_id}/artifacts/plan.md", headers=headers).content == b"plan"
        )
        client.put(
            f"/tasks/{child_id}/state",
            headers=_bearer(WRITE_TOKEN),
            json={"state": "ITERATING"},
        )
        post_planning = client.post(
            f"/tasks/{child_id}/responsibilities",
            headers=headers,
            json={"key": "implemented", "status": "met"},
        )
        assert (post_planning.status_code, post_planning.json()) == (403, SCOPE_FAILURE)
        denied = [
            ("post", f"/tasks/{child_id}/operations/drop", None),
            ("post", f"/tasks/{child_id}/operations/advance", None),
            ("post", f"/tasks/{child_id}/transition", {"to_state": "COMPLETE"}),
            ("put", f"/tasks/{child_id}/state", {"state": "COMPLETE"}),
            ("put", f"/tasks/{child_id}/url", {"url": "https://forbidden.example"}),
            ("put", f"/tasks/{child_id}/tokens-used", {"tokens_used": 1}),
            ("put", f"/tasks/{child_id}/blocked", {"blocked": True}),
            ("put", f"/tasks/{child_id}/attention", {"attention": True}),
            ("put", f"/tasks/{child_id}/snooze", {"until": "2030-01-01T00:00:00Z"}),
            ("put", f"/tasks/{child_id}/snooze", {"until": None}),
            ("put", f"/tasks/{child_id}/history/0/wake", {"status": "delivered"}),
            (
                "post",
                f"/tasks/{child_id}/registrations",
                {"container_id": "forbidden", "runner_id": None},
            ),
            ("delete", f"/registrations/{descendant_registration['id']}", None),
            ("put", f"/tasks/{child_id}/claim", {"runner_id": "bad"}),
            ("delete", f"/tasks/{child_id}/claim", None),
            (
                "put",
                f"/tasks/{child_id}/provisioning",
                {
                    "branch": "panopticon/child",
                    "clone": "/tasks/child",
                    "runner_id": "runner",
                    "workspace_verified": True,
                },
            ),
            (
                "put",
                f"/tasks/{child_id}/migration",
                {
                    "source_runner": "source",
                    "destination_runner": "destination",
                    "workspace_disposition": "transferred",
                    "workspace_method": "archive",
                    "session_history_disposition": "transferred",
                    "discarded_changes": [],
                },
            ),
            (
                "put",
                f"/tasks/{child_id}/lifecycle",
                {"runner_id": "runner", "phase": "starting", "detail": None},
            ),
            ("delete", f"/tasks/{child_id}/lifecycle", None),
            ("put", f"/tasks/{child_id}/governor", {"governor_task_id": None}),
            (
                "put",
                f"/tasks/{child_id}/governor",
                {"governor_task_id": unrelated["id"]},
            ),
        ]
        for method, path, body in denied:
            response = client.request(method, path, headers=headers, json=body)
            assert (response.status_code, response.json()) == (403, SCOPE_FAILURE)
        assert client.get(f"/tasks/{unrelated['id']}", headers=headers).status_code == 403
        for wrong_governor in (None, unrelated["id"], child_id):
            response = client.post(
                "/tasks",
                headers=headers,
                json={
                    "repo_id": "r1",
                    "workflow": "spike",
                    "governor_task_id": wrong_governor,
                },
            )
            assert (response.status_code, response.json()) == (403, SCOPE_FAILURE)
        wrong_repo = client.post(
            "/tasks",
            headers=headers,
            json={
                "repo_id": "r2",
                "workflow": "planned-scoped",
                "governor_task_id": governor["id"],
            },
        )
        assert (wrong_repo.status_code, wrong_repo.json()) == (403, SCOPE_FAILURE)


def test_non_orchestrator_cannot_create_a_governed_child(tmp_path: Path) -> None:
    # 2119: REQ-048.7.4
    with _client(tmp_path) as client:
        ordinary = _create_task(client)
        response = client.post(
            "/tasks",
            headers=_bearer(_task_token(ordinary["id"])),
            json={
                "repo_id": "r1",
                "workflow": "spike",
                "governor_task_id": ordinary["id"],
            },
        )
        assert (response.status_code, response.json()) == (403, SCOPE_FAILURE)
        child = _create_task(client, governor_task_id=str(ordinary["id"]))
        child_id = child["id"]
        grandchild = _create_task(client, governor_task_id=str(child_id))
        grandchild_id = grandchild["id"]
        delegated = [
            ("get", f"/tasks/{child_id}", None),
            ("get", f"/tasks/{child_id}/transitions", None),
            ("get", f"/tasks/{child_id}/operations", None),
            ("get", f"/tasks/{child_id}/states", None),
            ("get", f"/tasks/{child_id}/skills", None),
            ("get", f"/tasks/{child_id}/briefing", None),
            ("get", f"/tasks/{child_id}/history/0/wake", None),
            ("get", f"/tasks/{child_id}/workflow-overview", None),
            ("get", f"/tasks/{child_id}/registrations", None),
            ("get", f"/tasks/{child_id}/artifacts", None),
            ("get", f"/tasks/{child_id}/artifacts/plan.md", None),
            ("put", f"/tasks/{child_id}/artifacts/plan.md", None),
            ("put", f"/tasks/{child_id}/slug", {"slug": "no"}),
            ("put", f"/tasks/{child_id}/token-estimate", {"token_estimate": 1}),
            ("post", f"/tasks/{child_id}/responsibilities", {"key": "x", "status": "met"}),
            ("put", f"/tasks/{child_id}/turn", {"turn": "user"}),
            ("put", f"/tasks/{child_id}/dependencies", {"dep_ids": []}),
        ]
        for method, path, body in delegated:
            result = client.request(
                method,
                path,
                headers=_bearer(_task_token(ordinary["id"])),
                json=body,
                content=b"plan" if "artifacts" in path else None,
            )
            assert (result.status_code, result.json()) == (403, SCOPE_FAILURE)
            deep_path = path.replace(str(child_id), str(grandchild_id))
            deep_result = client.request(
                method,
                deep_path,
                headers=_bearer(_task_token(ordinary["id"])),
                json=body,
                content=b"plan" if "artifacts" in deep_path else None,
            )
            assert (deep_result.status_code, deep_result.json()) == (403, SCOPE_FAILURE)
        assert _asgi_denial(
            client.app,
            f"/tasks/{child_id}/live",
            _task_token(ordinary["id"]),
        ) == (403, SCOPE_FAILURE)
        for tool_name, arguments in {
            "get_task": {},
            "set_slug": {"slug": "no"},
            "set_token_estimate": {"token_estimate": 1},
            "resolve_responsibility": {"key": "x", "status": "met"},
            "set_turn": {"turn": "user"},
            "set_dependencies": {"dep_ids": []},
            "put_artifact": {"name": "plan.md", "content": "no"},
            "list_artifacts": {},
        }.items():
            for target_id in (child_id, grandchild_id):
                call = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": tool_name,
                        "arguments": {"task_id": target_id, **arguments},
                    },
                }
                assert (
                    client.post(
                        "/mcp",
                        headers=_bearer(_task_token(ordinary["id"])),
                        json=call,
                    ).status_code
                    == 403
                )
        for target_id in (child_id, grandchild_id):
            resource = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": f"panopticon://tasks/{target_id}/artifacts/plan.md"},
            }
            assert (
                client.post(
                    "/mcp",
                    headers=_bearer(_task_token(ordinary["id"])),
                    json=resource,
                ).status_code
                == 403
            )


def test_orchestrator_authority_uses_workflow_flag_not_a_hard_coded_name(tmp_path: Path) -> None:
    # 2119: REQ-048.7.1
    with _client(tmp_path) as client:
        governor = _create_task(client, workflow="alternate-orchestrator")
        response = client.post(
            "/tasks",
            headers=_bearer(_task_token(governor["id"])),
            json={
                "repo_id": "r1",
                "workflow": "spike",
                "governor_task_id": governor["id"],
            },
        )
        assert response.status_code == 201
        assert response.json()["governor_task_id"] == governor["id"]


def test_stale_orchestrator_workflow_fails_closed_without_authentication_500(
    tmp_path: Path,
) -> None:
    # 2119: REQ-048.12.1
    with _client(tmp_path) as client:
        governor = _create_task(client, workflow="orchestrator")
        child = _create_task(client, governor_task_id=str(governor["id"]))
        policy = client.app.state.credential_scope_policy
        policy._service._workflows.pop("orchestrator")
        headers = _bearer(_task_token(governor["id"]))

        assert client.get(f"/tasks/{governor['id']}", headers=headers).status_code == 200
        listed = client.get("/tasks", headers=headers)
        assert listed.status_code == 200
        assert [task["id"] for task in listed.json()] == [governor["id"]]
        create = client.post(
            "/tasks",
            headers=headers,
            json={
                "repo_id": "r1",
                "workflow": "spike",
                "governor_task_id": governor["id"],
            },
        )
        assert (create.status_code, create.json()) == (403, SCOPE_FAILURE)
        assert client.get(f"/tasks/{child['id']}", headers=headers).status_code == 403


def test_sync_mcp_policy_delegates_to_the_enforced_async_policy(tmp_path: Path) -> None:
    # 2119: REQ-048.8.1
    with _client(tmp_path) as client:
        orchestrator = _create_task(client, workflow="orchestrator")
        policy = client.app.state.credential_scope_policy
        for name, arguments in (
            ("create_task", {"orchestrator_task_id": orchestrator["id"]}),
            ("list_workflows", {"orchestrator_task_id": orchestrator["id"]}),
        ):
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
            asynchronous = asyncio.run(
                policy.authorize_mcp_async(_task_token(orchestrator["id"]), request)
            )
            synchronous = policy.authorize_mcp(_task_token(orchestrator["id"]), request)
            assert synchronous == asynchronous
            assert synchronous.allowed is True


def test_mcp_uses_the_same_scope_for_tool_arguments_and_artifact_resources(tmp_path: Path) -> None:
    # 2119: REQ-048.8.1
    # 2119: REQ-048.8.2
    with _client(tmp_path) as client:
        own = _create_task(client)
        sibling = _create_task(client)
        headers = _bearer(_task_token(own["id"]))
        tool_call = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "set_slug", "arguments": {"task_id": sibling["id"], "slug": "x"}},
        }
        resource_read = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "resources/read",
            "params": {"uri": f"task://{sibling['id']}/artifacts/plan.md"},
        }
        tool_decision = client.app.state.credential_scope_policy.authorize_mcp(
            _task_token(own["id"]), tool_call
        )
        resource_decision = client.app.state.credential_scope_policy.authorize_mcp(
            _task_token(own["id"]), resource_read
        )
        for decision in (tool_decision, resource_decision):
            assert decision.subject_task_id == own["id"]
            assert decision.target_task_id == sibling["id"]
            assert decision.allowed is False
        assert client.post("/mcp", headers=headers, json=tool_call).status_code == 403
        assert client.post("/mcp", headers=headers, json=resource_read).status_code == 403
        own_call = json.loads(json.dumps(tool_call))
        own_call["params"]["arguments"]["task_id"] = own["id"]
        assert client.post("/mcp", headers=headers, json=own_call).status_code != 403
        conflicting_actor = json.loads(json.dumps(own_call))
        conflicting_actor["params"]["arguments"]["actor_task_id"] = sibling["id"]
        decision = client.app.state.credential_scope_policy.authorize_mcp(
            _task_token(own["id"]), conflicting_actor
        )
        assert decision.subject_task_id == own["id"]
        assert decision.allowed is True


def test_task_capability_drives_a_real_mcp_session_and_keeps_cross_task_denied(
    tmp_path: Path,
) -> None:
    # 2119: REQ-048.5.1
    # 2119: REQ-048.6.3
    # 2119: REQ-048.8.1
    with _client(tmp_path) as client:
        own = _create_task(client)
        sibling = _create_task(client)
        token = _task_token(own["id"])

        async def exercise() -> None:
            transport = httpx.ASGITransport(app=client.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                headers=_bearer(token),
            ) as http_client:
                with pytest.raises(ExceptionGroup) as denied:
                    async with streamable_http_client(
                        "http://testserver/mcp/", http_client=http_client
                    ) as (read_stream, write_stream, _):
                        async with ClientSession(read_stream, write_stream) as session:
                            await session.initialize()
                            tools = {tool.name for tool in (await session.list_tools()).tools}
                            assert "set_slug" in tools
                            result = await session.call_tool(
                                "set_slug", {"task_id": own["id"], "slug": "mcp-self"}
                            )
                            assert result.isError is False
                            await session.call_tool(
                                "set_slug", {"task_id": sibling["id"], "slug": "forbidden"}
                            )
                assert any(
                    isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 403
                    for error in denied.value.exceptions
                )

        asyncio.run(exercise())
        assert (
            client.get(f"/tasks/{own['id']}", headers=_bearer(token)).json()["slug"] == "mcp-self"
        )
        assert sibling["slug"] is None


def test_task_capability_rejects_non_object_json_without_crashing(tmp_path: Path) -> None:
    # 2119: REQ-048.2.2
    # 2119: REQ-048.2.4
    with _client(tmp_path) as client:
        orchestrator = _create_task(client, workflow="orchestrator")
        headers = _bearer(_task_token(orchestrator["id"]))
        for body in ([], "not-an-object", 1, None):
            create = client.post("/tasks", headers=headers, json=body)
            mcp = client.post("/mcp", headers=headers, json=body)
            assert (create.status_code, create.json()) == (403, SCOPE_FAILURE)
            assert (mcp.status_code, mcp.json()) == (403, SCOPE_FAILURE)


def test_rest_and_mcp_resolve_equivalent_actions_to_identical_scope_decisions(
    tmp_path: Path,
) -> None:
    # 2119: REQ-048.8.1
    with _client(tmp_path) as client:
        own = _create_task(client)
        sibling = _create_task(client)
        orchestrator = _create_task(client, workflow="orchestrator")
        governed = _create_task(client, governor_task_id=str(orchestrator["id"]))
        grandchild = _create_task(client, governor_task_id=str(governed["id"]))
        great_grandchild = _create_task(client, governor_task_id=str(grandchild["id"]))
        policy = client.app.state.credential_scope_policy
        pairs = [
            (("GET", "/tasks/{task_id}"), ("tool", "get_task")),
            (("PUT", "/tasks/{task_id}/slug"), ("tool", "set_slug")),
            (("PUT", "/tasks/{task_id}/url"), ("tool", "set_url")),
            (("PUT", "/tasks/{task_id}/tokens-used"), ("tool", "set_tokens_used")),
            (("PUT", "/tasks/{task_id}/token-estimate"), ("tool", "set_token_estimate")),
            (("POST", "/tasks/{task_id}/operations/{operation}"), ("tool", "apply_operation")),
            (("PUT", "/tasks/{task_id}/state"), ("tool", "set_state")),
            (("POST", "/tasks/{task_id}/responsibilities"), ("tool", "resolve_responsibility")),
            (("PUT", "/tasks/{task_id}/turn"), ("tool", "set_turn")),
            (("PUT", "/tasks/{task_id}/blocked"), ("tool", "set_blocked")),
            (("PUT", "/tasks/{task_id}/attention"), ("tool", "set_attention")),
            (("PUT", "/tasks/{task_id}/dependencies"), ("tool", "set_dependencies")),
            (("PUT", "/tasks/{task_id}/artifacts/{name}"), ("tool", "put_artifact")),
            (("GET", "/tasks/{task_id}/artifacts"), ("tool", "list_artifacts")),
            (("GET", "/tasks/{task_id}/artifacts/{name}"), ("resource", "artifact")),
        ]
        classified = policy.classified_surfaces()
        equivalent_mcp = {surface for _, surface in pairs}
        assert equivalent_mcp == classified.mcp_surfaces_with_rest_equivalents
        actual_mcp = client.app.state.panopticon_mcp
        actual_equivalent = {
            ("tool", name)
            for name in actual_mcp._tool_manager._tools
            if name not in {"create_task", "list_workflows"}
        } | {("resource", "artifact")}
        assert equivalent_mcp == actual_equivalent
        for rest_surface, mcp_surface in pairs:
            assert policy.action_for_rest(*rest_surface) == policy.action_for_mcp(*mcp_surface)
            for subject, target in (
                (own, own["id"]),
                (orchestrator, governed["id"]),
                (orchestrator, grandchild["id"]),
                (orchestrator, great_grandchild["id"]),
                (own, sibling["id"]),
                (own, "missing"),
            ):
                concrete_path = (
                    rest_surface[1]
                    .replace("{task_id}", str(target))
                    .replace("{operation}", "advance")
                    .replace("{name}", "plan.md")
                )
                rest = policy.authorize_rest_request(
                    _task_token(subject["id"]), rest_surface[0], concrete_path
                )
                if mcp_surface[0] == "tool":
                    mcp_request = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": mcp_surface[1],
                            "arguments": {"task_id": target},
                        },
                    }
                else:
                    mcp_request = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "resources/read",
                        "params": {"uri": f"panopticon://tasks/{target}/artifacts/plan.md"},
                    }
                mcp = policy.authorize_mcp(_task_token(subject["id"]), mcp_request)
                assert rest.subject_task_id == subject["id"]
                assert rest.target_task_id == target
                assert mcp == rest


def test_every_task_targeted_mcp_surface_uses_capability_subject_and_decoded_target(
    tmp_path: Path,
) -> None:
    # 2119: REQ-048.8.1
    # 2119: REQ-048.8.2
    with _client(tmp_path) as client:
        own = _create_task(client)
        sibling = _create_task(client)
        headers = _bearer(_task_token(own["id"]))
        arguments = {
            "get_task": {},
            "set_slug": {"slug": "mcp"},
            "set_url": {"url": "https://example.test"},
            "set_tokens_used": {"tokens_used": 1},
            "set_token_estimate": {"token_estimate": 2},
            "apply_operation": {"operation": "drop"},
            "set_state": {"state": "COMPLETE"},
            "resolve_responsibility": {"key": "missing", "status": "met"},
            "set_turn": {"turn": "agent"},
            "set_blocked": {"blocked": True},
            "set_attention": {"attention": True},
            "set_dependencies": {"dep_ids": []},
            "put_artifact": {"name": "mcp.md", "content": "mcp"},
            "list_artifacts": {},
        }
        mcp = client.app.state.panopticon_mcp
        assert set(arguments) == set(mcp._tool_manager._tools) - {"create_task", "list_workflows"}
        for tool_name, extra in arguments.items():
            for target, denied in ((own["id"], False), (sibling["id"], True)):
                call = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": tool_name,
                        "arguments": {"task_id": target, **extra},
                    },
                }
                response = client.post("/mcp", headers=headers, json=call)
                assert (response.status_code == 403) is denied, (tool_name, target, response.text)
        client.put(f"/tasks/{own['id']}/artifacts/mcp.md", headers=headers, content=b"own")
        for target, denied in ((own["id"], False), (sibling["id"], True)):
            read = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": f"panopticon://tasks/{target}/artifacts/mcp.md"},
            }
            response = client.post("/mcp", headers=headers, json=read)
            assert (response.status_code == 403) is denied


def test_every_rest_and_mcp_surface_has_a_scope_classification(tmp_path: Path) -> None:
    # 2119: REQ-048.8.3
    from panopticon.taskservice.auth_scope import AuthorizationClass

    with _client(tmp_path) as client:
        policy = client.app.state.credential_scope_policy
        classified = policy.classified_surfaces()
        actual_rest_entries = [
            (method, route.path)
            for route in client.app.routes
            if hasattr(route, "methods")
            and route.path not in {"/healthz", "/docs", "/docs/oauth2-redirect", "/openapi.json"}
            for method in route.methods
        ]
        actual_rest = set(actual_rest_entries)
        assert len(actual_rest_entries) == len(actual_rest)
        assert classified.rest == actual_rest
        assert set(classified.rest_classes) == actual_rest
        mcp = client.app.state.panopticon_mcp
        actual_tools = set(mcp._tool_manager._tools)
        actual_resources = {
            *map(str, mcp._resource_manager._resources),
            *map(str, mcp._resource_manager._templates),
        }
        assert classified.mcp_tools == actual_tools
        assert classified.mcp_resources == actual_resources
        assert set(classified.mcp_tool_classes) == actual_tools
        assert set(classified.mcp_resource_classes) == actual_resources
        assignments = [
            *classified.rest_classes.values(),
            *classified.mcp_tool_classes.values(),
            *classified.mcp_resource_classes.values(),
        ]
        assert all(isinstance(assignment, AuthorizationClass) for assignment in assignments)
        assert {assignment.value for assignment in assignments} <= {
            "public",
            "fleet-read",
            "fleet-write",
            "task-scoped",
            "operator-migration",
        }
        assert policy.classification_for_rest("GET", "/__unregistered") is None
        assert policy.classification_for_mcp_tool("not-a-tool") is None
        assert policy.classification_for_mcp_resource("not-a-resource://missing") is None


def test_read_array_is_optional_but_configured_read_token_stays_read_only(tmp_path: Path) -> None:
    # 2119: REQ-035.14.1
    # 2119: REQ-048.9.1
    # 2119: REQ-048.9.2
    with _client(tmp_path) as no_reader:
        assert no_reader.get("/tasks", headers=_bearer(WRITE_TOKEN)).status_code == 200
    empty = tmp_path / "empty"
    with _client(empty, read=[]) as no_reader:
        assert no_reader.get("/tasks", headers=_bearer(WRITE_TOKEN)).status_code == 200
    empty_reference = _credential_file(tmp_path / "empty-client", read=[])
    with pytest.raises(ValueError, match="no configured read token"):
        load_client_token(
            empty_reference,
            privilege="read",
            secrets_dir=tmp_path / "empty-client" / "secrets",
        )
    invalid = tmp_path / "invalid"
    with pytest.raises(ValueError, match="authentication credential file is invalid"):
        _client(invalid, read=[], write=[])
    missing_write = tmp_path / "missing-write"
    reference = _credential_file(missing_write, read=[])
    (missing_write / "secrets" / reference).write_text(json.dumps({"read": []}))
    with pytest.raises(ValueError, match="authentication credential file is invalid"):
        TestClient(
            create_app(
                _service(missing_write),
                auth_file=reference,
                auth_mode="enforced",
                secrets_dir=missing_write / "secrets",
            )
        )
    wrong_type = tmp_path / "wrong-write-type"
    reference = _credential_file(wrong_type, read=[])
    (wrong_type / "secrets" / reference).write_text(json.dumps({"write": WRITE_TOKEN}))
    with pytest.raises(ValueError, match="authentication credential file is invalid"):
        TestClient(
            create_app(
                _service(wrong_type),
                auth_file=reference,
                auth_mode="enforced",
                secrets_dir=wrong_type / "secrets",
            )
        )
    configured = tmp_path / "configured"
    with _client(configured, read=[READ_TOKEN]) as client:
        assert client.get("/tasks", headers=_bearer(READ_TOKEN)).status_code == 200
        assert client.post("/tasks", headers=_bearer(READ_TOKEN), json={}).status_code == 401
        assert client.get("/tasks/missing/live", headers=_bearer(READ_TOKEN)).status_code == 401
        assert client.post("/mcp", headers=_bearer(READ_TOKEN), json={}).status_code == 401


def test_read_token_classification_covers_the_complete_registered_surface(tmp_path: Path) -> None:
    # 2119: REQ-048.9.2
    with _client(tmp_path, read=[READ_TOKEN]) as client:
        headers = _bearer(READ_TOKEN)
        for route in client.app.routes:
            if not hasattr(route, "methods") or route.path.startswith(("/mcp", "/healthz")):
                continue
            path = route.path
            for parameter in (
                "task_id",
                "repo_id",
                "runner_id",
                "registration_id",
                "delivery_id",
                "name",
            ):
                path = path.replace("{" + parameter + "}", "missing")
            path = path.replace("{entry_index}", "not-an-integer").replace(
                "{operation}", "not-an-operation"
            )
            for method in route.methods & {"GET", "HEAD"}:
                if path.endswith(("/live", "/session/input")):
                    response = client.get(path, headers=headers)
                    assert response.status_code == 401
                    assert response.json() == GENERIC_FAILURE
                    assert response.headers["www-authenticate"] == "Bearer"
                else:
                    response = client.request(method, path, headers=headers)
                    assert response.status_code not in {401, 403}, (method, path, response.text)
            if "GET" in route.methods and not path.endswith("/live"):
                response = client.head(path, headers=headers)
                assert response.status_code not in {401, 403}, ("HEAD", path, response.text)
            for method in route.methods & {"POST", "PUT", "PATCH", "DELETE"}:
                response = client.request(method, path, headers=headers, json={})
                assert response.status_code == 401, (method, path, response.text)
                assert response.json() == GENERIC_FAILURE
                assert response.headers["www-authenticate"] == "Bearer"
        mcp = client.app.state.panopticon_mcp
        for tool_name in mcp._tool_manager._tools:
            call = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": {}},
            }
            response = client.post("/mcp", headers=headers, json=call)
            assert response.status_code == 401
            assert response.json() == GENERIC_FAILURE
            assert response.headers["www-authenticate"] == "Bearer"
        for uri in [
            *map(str, mcp._resource_manager._resources),
            *map(str, mcp._resource_manager._templates),
        ]:
            read = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/read",
                "params": {"uri": uri},
            }
            response = client.post("/mcp", headers=headers, json=read)
            assert response.status_code == 401
            assert response.json() == GENERIC_FAILURE
            assert response.headers["www-authenticate"] == "Bearer"
        unknown_tool = {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {"name": "not-a-tool", "arguments": {"malformed": True}},
        }
        malformed_resource = {
            "jsonrpc": "2.0",
            "id": 100,
            "method": "resources/read",
            "params": {"uri": "not a valid resource uri"},
        }
        for invalid_request in (unknown_tool, malformed_resource):
            response = client.post("/mcp", headers=headers, json=invalid_request)
            assert response.status_code == 401
            assert response.json() == GENERIC_FAILURE
            assert response.headers["www-authenticate"] == "Bearer"
        for live_path in ("/tasks/missing/live", "/runners/missing/live"):
            response = client.head(live_path, headers=headers)
            assert response.status_code == 401
            assert response.content == b""
            assert response.headers["www-authenticate"] == "Bearer"
        for malformed_query in (
            "/tasks?wait=-1",
            "/tasks?wait=not-a-number",
            "/tasks?since=not-an-integer",
            "/tasks?terminal=not-a-boolean",
        ):
            response = client.get(malformed_query, headers=headers)
            assert response.status_code == 422


def test_browser_read_uses_bearer_header_without_cookie_credentials(tmp_path: Path) -> None:
    # 2119: REQ-048.1.3
    # 2119: REQ-048.10.1
    # 2119: REQ-048.10.2
    with _client(tmp_path, read=[READ_TOKEN], browser_origins=[PHONE_ORIGIN]) as client:
        response = client.get("/tasks", headers={**_bearer(READ_TOKEN), "Origin": PHONE_ORIGIN})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == PHONE_ORIGIN
        for allowed_header in ("Authorization", "Content-Type"):
            single = client.options(
                "/tasks",
                headers={
                    "Origin": PHONE_ORIGIN,
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": allowed_header,
                },
            )
            assert single.status_code == 200
        assert "access-control-allow-credentials" not in response.headers
        assert client.get(f"/tasks?access_token={READ_TOKEN}").status_code == 401
        assert client.get("/tasks", cookies={"authorization": READ_TOKEN}).status_code == 401
        task = _create_task(client)
        assert (
            client.get(
                "/tasks", headers={**_bearer(WRITE_TOKEN), "Origin": PHONE_ORIGIN}
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/tasks",
                headers={**_bearer(_task_token(task["id"])), "Origin": PHONE_ORIGIN},
            ).status_code
            == 403
        )


def test_cors_preflight_accepts_each_allowed_header_individually(tmp_path: Path) -> None:
    # 2119: REQ-048.11.3
    with _client(tmp_path, read=[READ_TOKEN], browser_origins=[PHONE_ORIGIN]) as client:
        for allowed_header in ("Authorization", "Content-Type"):
            response = client.options(
                "/tasks",
                headers={
                    "Origin": PHONE_ORIGIN,
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": allowed_header,
                },
            )
            assert response.status_code == 200
            assert allowed_header.lower() in {
                value.strip().lower()
                for value in response.headers["access-control-allow-headers"].split(",")
            }


@pytest.mark.parametrize(
    ("channel", "name"),
    [
        *(
            ("query", name)
            for name in (
                "token",
                "access_token",
                "access-token",
                "accessToken",
                "auth_token",
                "auth-token",
                "authToken",
                "api_key",
                "api-key",
                "apiKey",
                "authorization",
            )
        ),
        *(
            ("cookie", name)
            for name in (
                "token",
                "access_token",
                "access-token",
                "accessToken",
                "auth_token",
                "auth-token",
                "authToken",
                "api_key",
                "api-key",
                "apiKey",
                "authorization",
            )
        ),
        *(
            ("json", name)
            for name in (
                "token",
                "access_token",
                "access-token",
                "accessToken",
                "auth_token",
                "auth-token",
                "authToken",
                "api_key",
                "api-key",
                "apiKey",
                "authorization",
            )
        ),
        *(
            ("header", name)
            for name in (
                "X-API-Key",
                "X-Auth-Token",
                "X-Access-Token",
                "Authentication",
                "Proxy-Authorization",
            )
        ),
    ],
)
@pytest.mark.parametrize("alongside_valid_header", [False, True])
def test_cross_origin_rejects_every_alternate_credential_channel_even_with_valid_header(
    tmp_path: Path, channel: str, name: str, alongside_valid_header: bool
) -> None:
    # 2119: REQ-048.10.1
    with _client(tmp_path, read=[READ_TOKEN], browser_origins=[PHONE_ORIGIN]) as client:
        base_headers = {"Origin": PHONE_ORIGIN}
        if alongside_valid_header:
            base_headers.update(_bearer(READ_TOKEN))
        kwargs: dict[str, object] = {"headers": base_headers}
        if channel == "query":
            kwargs["params"] = {name: READ_TOKEN}
        elif channel == "cookie":
            kwargs["cookies"] = {name: READ_TOKEN}
        elif channel == "json":
            kwargs["json"] = {name: READ_TOKEN}
        else:
            kwargs["headers"] = {**kwargs["headers"], name: READ_TOKEN}  # type: ignore[dict-item]
        response = client.request("GET", "/tasks", **kwargs)
        assert response.status_code == 401


@pytest.mark.parametrize(
    "authorization",
    [
        READ_TOKEN,
        f"Basic {READ_TOKEN}",
        f"bearer {READ_TOKEN}",
        f"Bearer  {READ_TOKEN}",
        f"Bearer {READ_TOKEN} trailing",
        f" Bearer {READ_TOKEN}",
        f"Bearer {READ_TOKEN} ",
        f"Bearer junk{READ_TOKEN}",
        f"Bearer\t{READ_TOKEN}",
    ],
)
def test_cross_origin_requires_the_exact_bearer_header_shape(
    tmp_path: Path, authorization: str
) -> None:
    # 2119: REQ-048.10.1
    with _client(tmp_path, read=[READ_TOKEN], browser_origins=[PHONE_ORIGIN]) as client:
        response = client.get(
            "/tasks", headers={"Authorization": authorization, "Origin": PHONE_ORIGIN}
        )
        assert response.status_code == 401


def test_cors_preflight_is_data_free_and_read_only(tmp_path: Path) -> None:
    # 2119: REQ-048.10.2
    # 2119: REQ-048.10.3
    # 2119: REQ-048.11.3
    with _client(tmp_path, read=[READ_TOKEN], browser_origins=[PHONE_ORIGIN]) as client:
        response = client.options(
            "/tasks",
            headers={
                "Origin": PHONE_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization, Content-Type",
            },
        )
        assert response.status_code == 200
        assert response.content in {b"", b"OK"}
        assert "access-control-allow-credentials" not in response.headers
        assert not any(
            word in " ".join(response.headers.values()).lower()
            for word in ("task", "repo", "workflow", "runner", "registration", "artifact")
        )
        assert set(
            response.headers["access-control-allow-methods"].replace(" ", "").split(",")
        ) == {
            "GET",
            "HEAD",
            "OPTIONS",
        }
        assert set(
            response.headers["access-control-allow-headers"].replace(" ", "").lower().split(",")
        ) == {"authorization", "content-type"}
        assert response.headers["access-control-allow-origin"] == PHONE_ORIGIN
        for method in ("HEAD", "OPTIONS"):
            allowed = client.options(
                "/tasks",
                headers={"Origin": PHONE_ORIGIN, "Access-Control-Request-Method": method},
            )
            assert allowed.status_code == 200
        for method in ("POST", "PUT", "PATCH", "DELETE", "CONNECT", "TRACE", "PROPFIND"):
            denied = client.options(
                "/tasks",
                headers={"Origin": PHONE_ORIGIN, "Access-Control-Request-Method": method},
            )
            assert denied.status_code >= 400
        for extra in ("Accept", "X-Extra", "X-API-Key", "Cookie", "Proxy-Authorization"):
            extra_header = client.options(
                "/tasks",
                headers={
                    "Origin": PHONE_ORIGIN,
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": f"Authorization, {extra}",
                },
            )
            assert extra_header.status_code >= 400


@pytest.mark.parametrize(
    "path",
    [
        "/tasks/task-1",
        "/repos/r1",
        "/workflows/spike/execution",
        "/runners/runner-1",
        "/tasks/task-1/registrations",
        "/tasks/task-1/artifacts/plan.md",
    ],
)
def test_preflight_never_contains_protected_resource_data(tmp_path: Path, path: str) -> None:
    # 2119: REQ-048.10.3
    with _client(tmp_path, read=[READ_TOKEN], browser_origins=[PHONE_ORIGIN]) as client:
        response = client.options(
            path,
            headers={"Origin": PHONE_ORIGIN, "Access-Control-Request-Method": "GET"},
        )
        assert response.status_code == 200
        assert response.content in {b"", b"OK"}
        allowed_headers = {
            "access-control-allow-origin",
            "access-control-allow-methods",
            "access-control-allow-headers",
            "access-control-max-age",
            "vary",
            "content-type",
            "content-length",
        }
        assert set(response.headers) <= allowed_headers
        assert response.headers.get("access-control-max-age") in {None, "600"}
        assert response.headers.get("content-type") == "text/plain; charset=utf-8"
        assert response.headers.get("content-length") == str(len(response.content))
        assert response.headers["access-control-allow-origin"] == PHONE_ORIGIN
        assert (
            response.headers["access-control-allow-methods"].replace(" ", "") == "GET,HEAD,OPTIONS"
        )
        assert response.headers.get("access-control-allow-headers", "").replace(
            " ", ""
        ).lower() in {"", "authorization,content-type"}
        assert set(response.headers.get("vary", "Origin").replace(" ", "").split(",")) <= {
            "Origin",
            "Access-Control-Request-Headers",
        }


def test_preflight_headers_and_body_never_echo_real_protected_values(tmp_path: Path) -> None:
    # 2119: REQ-048.10.3
    with _client(tmp_path, read=[READ_TOKEN], browser_origins=[PHONE_ORIGIN]) as client:
        task = _create_task(client)
        task_id = str(task["id"])
        client.put(
            f"/tasks/{task_id}/artifacts/plan.md",
            headers=_bearer(WRITE_TOKEN),
            content=b"plan-secret-value",
        )
        protected_values = {task_id, "acme/one", "https://x/r1", "plan-secret-value"}
        fixed_response: tuple[bytes, tuple[tuple[str, str], ...]] | None = None
        for path in (
            f"/tasks/{task_id}",
            "/repos/r1",
            f"/tasks/{task_id}/registrations",
            f"/tasks/{task_id}/artifacts/plan.md",
        ):
            response = client.options(
                path,
                headers={"Origin": PHONE_ORIGIN, "Access-Control-Request-Method": "GET"},
            )
            wire = (
                response.text
                + "\n"
                + "\n".join(f"{name}: {value}" for name, value in response.headers.items())
            )
            assert response.status_code == 200
            assert response.content in {b"", b"OK"}
            normalized_response = (response.content, tuple(sorted(response.headers.items())))
            if fixed_response is None:
                fixed_response = normalized_response
            else:
                assert normalized_response == fixed_response
            assert not any(value in wire for value in protected_values)
            for name, value in response.headers.items():
                if name == "access-control-allow-origin":
                    assert value == PHONE_ORIGIN
                elif name == "access-control-allow-methods":
                    assert value.replace(" ", "") == "GET,HEAD,OPTIONS"
                elif name == "access-control-allow-headers":
                    assert value.replace(" ", "").lower() in {
                        "",
                        "authorization,content-type",
                    }
                elif name == "access-control-max-age":
                    assert value == "600"
                elif name == "vary":
                    assert set(value.replace(" ", "").split(",")) <= {
                        "Origin",
                        "Access-Control-Request-Headers",
                    }
                elif name == "content-type":
                    assert value == "text/plain; charset=utf-8"
                elif name == "content-length":
                    assert int(value) == len(response.content)
                else:
                    raise AssertionError(f"unexpected preflight header {name}: {value}")


def test_cors_is_off_by_default_and_echoes_only_an_allowed_exact_origin(tmp_path: Path) -> None:
    # 2119: REQ-048.11.1
    # 2119: REQ-048.11.4
    with _client(tmp_path, read=[READ_TOKEN]) as disabled:
        live_task = _create_task(disabled)
        for origin in (PHONE_ORIGIN, "https://evil.example", "null"):
            response = disabled.get("/tasks", headers={**_bearer(READ_TOKEN), "Origin": origin})
            assert "access-control-allow-origin" not in response.headers
            preflight = disabled.options(
                "/tasks",
                headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
            )
            assert "access-control-allow-origin" not in preflight.headers
            live_headers = _stream_start_headers(
                disabled.app,
                f"/tasks/{live_task['id']}/live",
                _task_token(live_task["id"]),
                origin,
            )
            assert "access-control-allow-origin" not in live_headers
    empty = tmp_path / "empty-origin-list"
    with _client(empty, read=[READ_TOKEN], browser_origins=[]) as disabled:
        assert (
            "access-control-allow-origin"
            not in disabled.get(
                "/tasks", headers={**_bearer(READ_TOKEN), "Origin": PHONE_ORIGIN}
            ).headers
        )
    methods = tmp_path / "disabled-methods"
    with _client(methods, read=[READ_TOKEN]) as disabled:
        head = disabled.head("/tasks", headers={**_bearer(READ_TOKEN), "Origin": PHONE_ORIGIN})
        post = disabled.post(
            "/tasks", headers={**_bearer(WRITE_TOKEN), "Origin": PHONE_ORIGIN}, json={}
        )
        assert "access-control-allow-origin" not in head.headers
        assert "access-control-allow-origin" not in post.headers
    configured = tmp_path / "configured"
    with _client(configured, read=[READ_TOKEN], browser_origins=[PHONE_ORIGIN]) as client:
        allowed = client.get("/tasks", headers={**_bearer(READ_TOKEN), "Origin": PHONE_ORIGIN})
        assert allowed.headers["access-control-allow-origin"] == PHONE_ORIGIN
        assert "origin" in {value.strip().lower() for value in allowed.headers["vary"].split(",")}
        for denied_origin in (
            f"{PHONE_ORIGIN}/",
            "https://evil.example",
            "http://phone.example",
            "https://phone.example:8443",
            "null",
        ):
            denied = client.get("/tasks", headers={**_bearer(READ_TOKEN), "Origin": denied_origin})
            assert "access-control-allow-origin" not in denied.headers
        failure = client.get(
            "/tasks", headers={"Authorization": "Bearer wrong", "Origin": PHONE_ORIGIN}
        )
        assert "access-control-allow-credentials" not in failure.headers
        assert failure.headers["access-control-allow-origin"] == PHONE_ORIGIN
        assert "origin" in {value.strip().lower() for value in failure.headers["vary"].split(",")}
        not_found = client.get(
            "/tasks/missing",
            headers={**_bearer(READ_TOKEN), "Origin": PHONE_ORIGIN},
        )
        assert not_found.status_code == 404
        assert not_found.headers["access-control-allow-origin"] == PHONE_ORIGIN
        assert "origin" in {value.strip().lower() for value in not_found.headers["vary"].split(",")}


def test_disabled_cors_emits_no_allow_origin_on_any_registered_route(tmp_path: Path) -> None:
    # 2119: REQ-048.11.1
    with _client(tmp_path, read=[READ_TOKEN]) as client:
        paths: set[str] = set()
        for route in client.app.routes:
            if not hasattr(route, "methods"):
                continue
            path = route.path
            for parameter in ("task_id", "repo_id", "runner_id", "registration_id", "name"):
                path = path.replace("{" + parameter + "}", "missing")
            paths.add(path.replace("{entry_index}", "0").replace("{operation}", "drop"))
        for path in paths:
            for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
                preflight = client.options(
                    path,
                    headers={"Origin": PHONE_ORIGIN, "Access-Control-Request-Method": method},
                )
                assert "access-control-allow-origin" not in preflight.headers, (path, method)
                actual = client.request(
                    method,
                    path,
                    headers={**_bearer(READ_TOKEN), "Origin": PHONE_ORIGIN},
                    json={} if method in {"POST", "PUT", "PATCH"} else None,
                )
                assert "access-control-allow-origin" not in actual.headers, (path, method)
        for route in client.app.routes:
            if (
                not hasattr(route, "methods")
                or "GET" not in route.methods
                or route.path.endswith("/live")
            ):
                continue
            path = route.path
            for parameter in ("task_id", "repo_id", "runner_id", "registration_id", "name"):
                path = path.replace("{" + parameter + "}", "missing")
            path = path.replace("{entry_index}", "0").replace("{operation}", "drop")
            response = client.get(path, headers={**_bearer(READ_TOKEN), "Origin": PHONE_ORIGIN})
            assert "access-control-allow-origin" not in response.headers, path


@pytest.mark.parametrize(
    "origin",
    [
        "",
        "*",
        "*://phone.example",
        "//phone.example",
        "phone.example",
        "https://",
        "https:///phone.example",
        "https://*.example",
        "https://phone.example:*",
        "https://phone.example/",
        "https://phone.example/path",
        "https://phone.example?query=1",
        "https://phone.example#fragment",
        "https://user:pass@phone.example",
        "https://user@phone.example",
        "null",
        "data:text/plain,opaque",
        "https://phone.example:bad",
        "https://[::1",
        "not an origin",
    ],
)
def test_cors_rejects_non_origin_allowlist_entries(tmp_path: Path, origin: str) -> None:
    # 2119: REQ-048.11.2
    with pytest.raises(ValueError, match="browser origin configuration is invalid"):
        _client(tmp_path, read=[READ_TOKEN], browser_origins=[origin])


@pytest.mark.parametrize(
    "invalid_origin",
    ["*", "phone.example", "https://phone.example/path", "https://user@phone.example", "null"],
)
def test_cors_rejects_a_forbidden_entry_mixed_with_a_valid_origin(
    tmp_path: Path, invalid_origin: str
) -> None:
    # 2119: REQ-048.11.2
    with pytest.raises(ValueError, match="browser origin configuration is invalid"):
        _client(
            tmp_path,
            read=[READ_TOKEN],
            browser_origins=[PHONE_ORIGIN, invalid_origin],
        )


def test_cors_accepts_an_exact_origin_with_an_explicit_valid_port(tmp_path: Path) -> None:
    # 2119: REQ-048.11.2
    origin = "https://phone.example:8443"
    with _client(tmp_path, read=[READ_TOKEN], browser_origins=[origin]) as client:
        response = client.get("/tasks", headers={**_bearer(READ_TOKEN), "Origin": origin})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin


@pytest.mark.parametrize("origin", ["http://localhost:3000", "https://[::1]:8443"])
def test_cors_accepts_other_valid_exact_origin_forms(tmp_path: Path, origin: str) -> None:
    # 2119: REQ-048.11.2
    with _client(tmp_path, read=[READ_TOKEN], browser_origins=[origin]) as client:
        response = client.get("/tasks", headers={**_bearer(READ_TOKEN), "Origin": origin})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin


def test_cors_policy_is_identical_for_each_allowed_origin_and_route(tmp_path: Path) -> None:
    # 2119: REQ-048.11.3
    origins = [PHONE_ORIGIN, "https://tablet.example:8443"]
    with _client(tmp_path, read=[READ_TOKEN], browser_origins=origins) as client:
        for origin in origins:
            for path in ("/tasks", "/repos/r1", "/workflows"):
                allowed = client.options(
                    path,
                    headers={
                        "Origin": origin,
                        "Access-Control-Request-Method": "GET",
                        "Access-Control-Request-Headers": "Authorization, Content-Type",
                    },
                )
                assert allowed.status_code == 200
                assert allowed.headers["access-control-allow-origin"] == origin
                for method in ("POST", "PUT", "PATCH", "DELETE", "CONNECT", "TRACE", "PROPFIND"):
                    denied = client.options(
                        path,
                        headers={"Origin": origin, "Access-Control-Request-Method": method},
                    )
                    assert denied.status_code >= 400
                for extra in (
                    "Accept",
                    "X-Extra",
                    "X-API-Key",
                    "Cookie",
                    "Proxy-Authorization",
                ):
                    denied = client.options(
                        path,
                        headers={
                            "Origin": origin,
                            "Access-Control-Request-Method": "GET",
                            "Access-Control-Request-Headers": f"Authorization, {extra}",
                        },
                    )
                    assert denied.status_code >= 400
                allowed_methods = ("GET", "HEAD", "OPTIONS")
                forbidden_methods = ("POST", "PUT", "PATCH", "DELETE", "PROPFIND")
                allowed_header_sets = (
                    "Authorization",
                    "Content-Type",
                    "Authorization, Content-Type",
                )
                for method in (*allowed_methods, *forbidden_methods):
                    for requested_headers in allowed_header_sets:
                        result = client.options(
                            path,
                            headers={
                                "Origin": origin,
                                "Access-Control-Request-Method": method,
                                "Access-Control-Request-Headers": requested_headers,
                            },
                        )
                        assert (result.status_code == 200) is (method in allowed_methods)
                    for requested_headers in (
                        "X-Extra",
                        "Authorization, X-Extra",
                        "Content-Type, X-Extra",
                    ):
                        result = client.options(
                            path,
                            headers={
                                "Origin": origin,
                                "Access-Control-Request-Method": method,
                                "Access-Control-Request-Headers": requested_headers,
                            },
                        )
                        assert result.status_code >= 400


def test_cors_never_allows_browser_credentials_on_any_response(tmp_path: Path) -> None:
    # 2119: REQ-048.10.2
    with _client(tmp_path, read=[READ_TOKEN], browser_origins=[PHONE_ORIGIN]) as client:
        task = _create_task(client)
        successful_if_not_browser_blocked = (
            (
                "POST",
                "/tasks",
                _bearer(WRITE_TOKEN),
                {"repo_id": "r1", "workflow": "spike", "governor_task_id": None},
            ),
            (
                "PUT",
                f"/tasks/{task['id']}/slug",
                _bearer(_task_token(task["id"])),
                {"slug": "cross-origin-mutation"},
            ),
        )
        for origin in (PHONE_ORIGIN, "https://evil.example"):
            for method, path, auth_headers, body in successful_if_not_browser_blocked:
                response = client.request(
                    method,
                    path,
                    headers={**auth_headers, "Origin": origin},
                    json=body,
                )
                assert response.status_code == 403
                assert "true" not in {
                    value.strip().lower()
                    for value in response.headers.get("access-control-allow-credentials", "").split(
                        ","
                    )
                }
        paths: set[str] = set()
        for route in client.app.routes:
            if not hasattr(route, "methods"):
                continue
            path = route.path
            for parameter in ("task_id", "repo_id", "runner_id", "registration_id", "name"):
                path = path.replace("{" + parameter + "}", "missing")
            paths.add(path.replace("{entry_index}", "0").replace("{operation}", "drop"))
        actual_cases = (
            *(
                (method, _bearer(READ_TOKEN if method in {"GET", "HEAD"} else WRITE_TOKEN))
                for method in (
                    "GET",
                    "HEAD",
                    "OPTIONS",
                    "POST",
                    "PUT",
                    "PATCH",
                    "DELETE",
                    "CONNECT",
                    "TRACE",
                    "PROPFIND",
                )
            ),
            ("GET", {"Authorization": "Bearer invalid"}),
        )
        for path in paths:
            for origin in (PHONE_ORIGIN, "https://evil.example", "null"):
                for method, auth_headers in actual_cases:
                    response = client.request(
                        method,
                        path,
                        headers={**auth_headers, "Origin": origin},
                        json={} if method == "POST" else None,
                    )
                    assert "true" not in {
                        value.strip().lower()
                        for value in response.headers.get(
                            "access-control-allow-credentials", ""
                        ).split(",")
                    }
                for requested_method in (
                    "GET",
                    "HEAD",
                    "OPTIONS",
                    "POST",
                    "PUT",
                    "PATCH",
                    "DELETE",
                    "CONNECT",
                    "TRACE",
                    "PROPFIND",
                ):
                    preflight = client.options(
                        path,
                        headers={
                            "Origin": origin,
                            "Access-Control-Request-Method": requested_method,
                            "Access-Control-Request-Headers": "Authorization, Content-Type",
                        },
                    )
                    assert "true" not in {
                        value.strip().lower()
                        for value in preflight.headers.get(
                            "access-control-allow-credentials", ""
                        ).split(",")
                    }


def test_task_scope_policy_is_clock_free_and_each_declared_input_changes_decisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 2119: REQ-048.12.1
    import time

    from panopticon.taskservice.auth_scope import Action, Principal, Relation, Target, authorize

    assert authorize.__code__.co_varnames[: authorize.__code__.co_argcount] == (
        "principal",
        "action",
        "target",
    )
    signature = inspect.signature(authorize)
    assert [(name, parameter.kind) for name, parameter in signature.parameters.items()] == [
        ("principal", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("action", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("target", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ]

    principal = Principal.task("task-1")
    self_target = Target(task_id="task-1", relation=Relation.SELF, orchestrates=False)
    other_target = Target(task_id="task-2", relation=Relation.UNRELATED, orchestrates=False)
    cases = [
        (
            Principal.task(principal_id),
            Target(
                task_id=principal_id if relation is Relation.SELF else f"target-{principal_id}",
                relation=relation,
                orchestrates=orchestrates,
            ),
        )
        for principal_id in (
            "task-1",
            "task-99",
            "00000000000000000000000000000000",
            "ffffffffffffffffffffffffffffffff",
        )
        for relation in Relation
        for orchestrates in (False, True)
    ]

    def decide() -> dict[tuple[object, int], object]:
        current = {
            (action, index): authorize(case_principal, action, target)
            for action in Action
            for index, (case_principal, target) in enumerate(cases)
        }
        for action in Action:
            for index, (case_principal, target) in enumerate(cases):
                assert authorize(case_principal, action, target) == current[(action, index)]
        return current

    original_wall_clock = time.time
    original_monotonic_clock = time.monotonic
    closure = inspect.getclosurevars(authorize)
    bound_values = [
        *closure.globals.values(),
        *closure.nonlocals.values(),
        *(authorize.__defaults__ or ()),
        *(authorize.__kwdefaults__ or {}).values(),
    ]
    assert not any(
        value is original_wall_clock or value is original_monotonic_clock for value in bound_values
    )
    observed_clock_calls: list[object] = []

    def trace_clock_calls(frame: object, event: str, argument: object) -> None:
        del frame
        if event == "c_call" and argument in {
            original_wall_clock,
            original_monotonic_clock,
        }:
            observed_clock_calls.append(argument)

    sys.setprofile(trace_clock_calls)
    try:
        decide()
    finally:
        sys.setprofile(None)
    assert observed_clock_calls == []
    baseline = decide()
    for wall_clock, monotonic_clock in (
        (lambda: 0.0, original_monotonic_clock),
        (original_wall_clock, lambda: 0.0),
        (lambda: 10**15, original_monotonic_clock),
        (original_wall_clock, lambda: 10**15),
        (lambda: 1.0, lambda: 2.0),
        (lambda: 2.0, lambda: 1.0),
    ):
        monkeypatch.setattr(time, "time", wall_clock)
        monkeypatch.setattr(time, "monotonic", monotonic_clock)
        assert decide() == baseline

    def clock_access_is_forbidden() -> float:
        raise AssertionError("task-scope authorization consulted a clock")

    for wall_clock, monotonic_clock in (
        (clock_access_is_forbidden, original_monotonic_clock),
        (original_wall_clock, clock_access_is_forbidden),
        (clock_access_is_forbidden, clock_access_is_forbidden),
    ):
        monkeypatch.setattr(time, "time", wall_clock)
        monkeypatch.setattr(time, "monotonic", monotonic_clock)
        assert decide() == baseline
    assert authorize(principal, Action.READ_TASK, self_target).allowed is True
    assert authorize(Principal.task("task-2"), Action.READ_TASK, self_target).allowed is False
    with pytest.raises(TypeError):
        authorize("task-1", Action.READ_TASK, self_target)  # type: ignore[arg-type]
    assert authorize(principal, Action.CLAIM_TASK, self_target).allowed is False
    assert authorize(principal, Action.READ_TASK, other_target).allowed is False
    governed = Target(task_id="task-2", relation=Relation.GOVERNED, orchestrates=True)
    assert authorize(principal, Action.PREPLAN_CHILD, governed).allowed is True
    assert (
        authorize(
            principal,
            Action.PREPLAN_CHILD,
            Target(task_id="task-2", relation=Relation.GOVERNED, orchestrates=False),
        ).allowed
        is False
    )
    auth_scope_source = Path(__file__).parents[2] / "src/panopticon/taskservice/auth_scope.py"
    tree = ast.parse(auth_scope_source.read_text())
    imported_roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert imported_roots.isdisjoint({"time", "datetime"})


def test_credential_scoping_packages_have_no_container_or_llm_sdk_imports() -> None:
    # 2119: REQ-048.12.2
    script = """
import builtins, importlib, json, sys
attempts = []
original_import = builtins.__import__
original_import_module = importlib.import_module
def tracked_import(name, *args, **kwargs):
    attempts.append(name)
    return original_import(name, *args, **kwargs)
def tracked_import_module(name, *args, **kwargs):
    attempts.append(name)
    return original_import_module(name, *args, **kwargs)
builtins.__import__ = tracked_import
importlib.import_module = tracked_import_module
from panopticon.taskservice.auth import derive_task_capability
from panopticon.taskservice.auth_scope import Action, Principal, Relation, Target, authorize
for root in ("fleet-writer-token", "rotated-token"):
    for task_id in ("task-1", "task-2", "???"):
        derive_task_capability(root, task_id)
for principal_id in ("task-1", "task-99"):
    principal = Principal.task(principal_id)
    for action in Action:
        for relation in Relation:
            for orchestrates in (False, True):
                target_id = principal_id if relation is Relation.SELF else "target"
                authorize(principal, action, Target(target_id, relation, orchestrates))
forbidden = sorted(name for name in {*attempts, *sys.modules} if name == "panopticon.container" or name.startswith(("panopticon.container.", "anthropic", "openai")))
print(json.dumps(forbidden))
"""
    process = subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True
    )
    assert json.loads(process.stdout) == []


def test_scoping_packages_contain_no_literal_forbidden_imports() -> None:
    # 2119: REQ-048.12.2
    source_root = Path(__file__).parents[2] / "src/panopticon"
    forbidden = {"anthropic", "openai"}
    violations: list[tuple[str, str]] = []
    for source in (
        source_root / "taskservice/auth.py",
        source_root / "taskservice/auth_scope.py",
    ):
        tree = ast.parse(source.read_text(), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
                if node.module == "panopticon":
                    names.extend(f"panopticon.{alias.name}" for alias in node.names)
                if node.level and (node.module or "").split(".", 1)[0] == "container":
                    names.append("panopticon.container")
                if (
                    node.level
                    and not node.module
                    and any(alias.name == "container" for alias in node.names)
                ):
                    names.append("panopticon.container")
            elif (
                isinstance(node, ast.Call)
                and (
                    (
                        node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)
                    )
                    or (
                        node.keywords
                        and isinstance(node.keywords[0].value, ast.Constant)
                        and isinstance(node.keywords[0].value.value, str)
                    )
                )
                and (
                    (isinstance(node.func, ast.Name) and node.func.id == "__import__")
                    or (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module")
                )
            ):
                names = [
                    node.args[0].value if node.args else node.keywords[0].value.value  # type: ignore[union-attr]
                ]
            else:
                continue
            for module in names:
                root = module.split(".", 1)[0]
                if root in forbidden or module.startswith("panopticon.container"):
                    violations.append((str(source.relative_to(source_root)), module))
    assert violations == []


def test_fleet_write_retains_host_duties_while_task_token_cannot_claim(tmp_path: Path) -> None:
    # 2119: REQ-048.1.2
    with _client(tmp_path) as client:
        task = _create_task(client)
        second = _create_task(client)
        fleet = _bearer(WRITE_TOKEN)
        for target in (task, second):
            path = f"/tasks/{target['id']}/claim"
            assert client.put(path, headers=fleet, json={"runner_id": "r1"}).status_code == 200
            assert (
                client.put(
                    f"/tasks/{target['id']}/lifecycle",
                    headers=fleet,
                    json={"runner_id": "r1", "phase": "starting", "detail": None},
                ).status_code
                == 200
            )
            assert (
                client.put(
                    f"/tasks/{target['id']}/slug",
                    headers=fleet,
                    json={"slug": f"operator-{target['id']}"},
                ).status_code
                == 200
            )
        assert client.get("/runners", headers=fleet).status_code == 200
        assert (
            client.post(
                "/repos",
                headers=fleet,
                json={"id": "r3", "name": "acme/three", "git_url": "https://x/r3"},
            ).status_code
            == 201
        )
        assert (
            client.patch("/repos/r1", headers=fleet, json={"name": "acme/renamed"}).status_code
            == 200
        )
        reclaim = client.post("/runners/r1/reclaim", headers=fleet)
        assert reclaim.status_code == 200
        path = f"/tasks/{task['id']}/claim"
        assert (
            client.put(
                path,
                headers=_bearer(_task_token(task["id"])),
                json={"runner_id": "r2"},
            ).status_code
            == 403
        )


def test_runner_injects_only_the_subject_task_capability(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-035.17.1
    # 2119: REQ-035.39.1
    # 2119: REQ-048.1.1
    # 2119: REQ-048.3.1
    # 2119: REQ-048.3.2
    from panopticon.sessionservice.local_runner import LocalRunner

    calls: list[list[str]] = []
    mounted_snapshot: dict[str, str] = {}
    mounted_files: dict[str, str] = {}
    mounted_targets: list[tuple[str, str, bool]] = []
    shared_client_headers: list[str] = []
    caplog.set_level(1)

    def run(args: list[str], **_: object) -> str:
        calls.append(args)
        if args[:2] == ["docker", "run"]:
            for index, value in enumerate(args[:-1]):
                if value == "--volume":
                    pieces = args[index + 1].split(":")
                    source = Path(pieces[0])
                    target = pieces[1]
                    readonly = pieces[2:] == ["ro"]
                elif value == "--mount":
                    fields = dict(
                        field.split("=", 1) for field in args[index + 1].split(",") if "=" in field
                    )
                    source = Path(fields.get("source", fields.get("src", "")))
                    target = fields.get("target", fields.get("dst", fields.get("destination", "")))
                    readonly = "readonly" in args[index + 1].split(",")
                elif value == "--env-file":
                    source = Path(args[index + 1])
                    target = "docker-env-file"
                    readonly = True
                else:
                    continue
                mounted_targets.append((str(source), target, readonly))
                if source.is_file():
                    mounted_files[str(source)] = source.read_text()
            designated = [
                source
                for source, target, readonly in mounted_targets
                if target == "/run/secrets/panopticon-service-auth" and readonly
            ]
            assert len(designated) == 1
            mounted_snapshot.update(json.loads(Path(designated[0]).read_text()))
            monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_FILE", designated[0])
            from panopticon.container.agent import _default_client

            shared_client_headers.append(
                _default_client("http://service")._http.headers["authorization"]
            )
        return ""

    with _client(
        tmp_path,
        read=[READ_TOKEN, SECOND_READ_TOKEN],
        write=[OLD_WRITE_TOKEN, WRITE_TOKEN],
    ) as client:
        task = _create_task(client)
        sibling = _create_task(client)
        task_id = str(task["id"])
        secrets = tmp_path / "secrets"
        reference = "task-service-auth.json"
        runner = LocalRunner(
            "http://service",
            auth_file=reference,
            secrets_dir=secrets,
            user="1000:1000",
            run=run,
        )
        credential = secrets / reference
        credential.write_text(
            json.dumps({"read": [SECOND_READ_TOKEN], "write": [WRITE_TOKEN, ROTATED_WRITE_TOKEN]})
        )
        credential.chmod(0o600)
        runner.spawn(task_id)
        docker = next(call for call in calls if call[:2] == ["docker", "run"])
        rendered = " ".join(docker)
        encoded_subject = base64.urlsafe_b64encode(task_id.encode()).decode().rstrip("=")
        independent_mac = hmac.new(
            ROTATED_WRITE_TOKEN.encode(),
            b"panopticon-task-capability-v1\0" + task_id.encode() + b"\0self",
            hashlib.sha256,
        ).digest()
        encoded_mac = base64.urlsafe_b64encode(independent_mac).decode().rstrip("=")
        expected = f"ptc1.{encoded_subject}.self.{encoded_mac}"
        assert WRITE_TOKEN not in rendered
        assert ROTATED_WRITE_TOKEN not in rendered
        assert OLD_WRITE_TOKEN not in rendered
        assert READ_TOKEN not in rendered
        assert SECOND_READ_TOKEN not in rendered
        assert expected not in rendered
        assert all(expected not in " ".join(call) for call in calls)
        assert expected not in caplog.text
        assert mounted_snapshot == {"task": expected}
        assert shared_client_headers == [f"Bearer {expected}"]
        auth_environment = [
            value for value in docker if value.startswith("PANOPTICON_SERVICE_AUTH_FILE=")
        ]
        assert auth_environment == [
            "PANOPTICON_SERVICE_AUTH_FILE=/run/secrets/panopticon-service-auth"
        ]
        auth_env_option_indices = [
            index
            for index, value in enumerate(docker[:-1])
            if value == "--env"
            and docker[index + 1]
            == "PANOPTICON_SERVICE_AUTH_FILE=/run/secrets/panopticon-service-auth"
        ]
        assert len(auth_env_option_indices) == 1
        assert auth_env_option_indices[0] + 1 < len(docker) - 1
        for index, value in enumerate(docker[:-1]):
            if value == "--env":
                assert "=" in docker[index + 1]
        auth_mounts = [
            mount
            for mount in mounted_targets
            if mount[1:] == ("/run/secrets/panopticon-service-auth", True)
        ]
        assert len(auth_mounts) == 1
        assert sum(mount[0] == auth_mounts[0][0] for mount in mounted_targets) == 1
        assert all(
            not target.startswith("/run/secrets/")
            or Path(source).is_file()
            or source in mounted_files
            for source, target, _ in mounted_targets
        )
        assert any(
            docker[index] in {"--volume", "--mount"}
            and index + 1 < len(docker) - 1
            and str(auth_mounts[0][0]) in docker[index + 1]
            for index in range(len(docker) - 1)
        )
        credential_snapshots: list[dict[str, object]] = []
        capability_bearing_mounts: list[dict[str, object]] = []
        for contents in mounted_files.values():
            assert WRITE_TOKEN not in contents
            assert ROTATED_WRITE_TOKEN not in contents
            assert OLD_WRITE_TOKEN not in contents
            assert READ_TOKEN not in contents
            assert SECOND_READ_TOKEN not in contents
            try:
                decoded = json.loads(contents)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict) and set(decoded) & {"task", "read", "write"}:
                credential_snapshots.append(decoded)
            if "ptc1." in contents:
                assert isinstance(decoded, dict)
                capability_bearing_mounts.append(decoded)
        assert credential_snapshots == [{"task": expected}]
        assert capability_bearing_mounts == [{"task": expected}]
    with TestClient(
        create_app(
            _reloaded_service(tmp_path),
            auth_file=reference,
            auth_mode="enforced",
            secrets_dir=secrets,
        )
    ) as restarted:
        assert restarted.get(f"/tasks/{task_id}", headers=_bearer(expected)).status_code == 200
        assert _stream_start_status(restarted.app, f"/tasks/{task_id}/live", expected) == 200
        assert (
            restarted.get(f"/tasks/{sibling['id']}", headers=_bearer(expected)).status_code == 403
        )


def test_capability_plaintext_is_absent_from_persistent_and_diagnostic_surfaces(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # 2119: REQ-048.3.2
    with _client(tmp_path, read=[READ_TOKEN]) as client:
        task = _create_task(client)
        capability = _task_token(task["id"])
        assert capability not in json.dumps(task)
        task_response = client.get(f"/tasks/{task['id']}", headers=_bearer(capability))
        repo_response = client.get("/repos/r1", headers=_bearer(capability))
        assert capability not in task_response.text
        assert capability not in repo_response.text
        assert capability not in client.get("/tasks", headers=_bearer(WRITE_TOKEN)).text
        for path in ("/repos", "/repos/r1", "/repos/r1/workflows", "/repos/r1/image-layer"):
            assert capability not in client.get(path, headers=_bearer(WRITE_TOKEN)).text
        client.put(
            f"/tasks/{task['id']}/artifacts/safe.md", headers=_bearer(capability), content=b"safe"
        )
        artifact = client.get(f"/tasks/{task['id']}/artifacts/safe.md", headers=_bearer(capability))
        assert artifact.content == b"safe"
        assert capability.encode() not in (tmp_path / "task.db").read_bytes()
        artifact_bytes = b"".join(
            path.read_bytes() for path in (tmp_path / "artifacts").rglob("*") if path.is_file()
        )
        assert capability.encode() not in artifact_bytes
        client.get("/tasks", headers={"Authorization": f"Bearer {capability}x"})
        assert capability not in caplog.text
