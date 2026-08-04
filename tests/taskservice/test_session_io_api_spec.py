"""Executable REST contract for deterministic, runner-mediated session I/O."""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from panopticon.client import TaskServiceClient
from panopticon.container import hook
from panopticon.core.models import Repo
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.auth import scoped_task_token
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike

READ = "session-reader-token"
WRITE = "session-writer-token"
STATUS_FIELDS = {
    "id",
    "status",
    "submit",
    "byte_count",
    "created_at",
    "settled_at",
    "failure_reason",
}


def test_session_io_authority_is_bound_to_task_scoped_or_master_principal(
    tmp_path: Path,
) -> None:
    # 2119: REQ-045.1.1
    # 2119: REQ-045.1.3
    # 2119: REQ-045.6.2
    service, client = _app(tmp_path)
    with client as http:
        task_id = _live_user_task(service, http)
        other_id = _live_user_task(service, http)
        scoped = scoped_task_token(WRITE, task_id)
        created = http.post(
            f"/tasks/{task_id}/session/input",
            headers=_auth(scoped),
            json={"text": "own task", "submit": False, "idempotency_key": "scoped-own-1"},
        )
        cross_task = http.post(
            f"/tasks/{other_id}/session/input",
            headers=_auth(scoped),
            json={"text": "cross task", "submit": True, "idempotency_key": "scoped-cross-1"},
        )
        forged = scoped[:-1] + ("0" if scoped[-1] != "0" else "1")
        forged_scope = http.post(
            f"/tasks/{task_id}/session/input",
            headers=_auth(forged),
            json={"text": "forged", "submit": False, "idempotency_key": "scoped-forged-1"},
        )
        delivery_id = created.json()["id"]
        spoofed_poll = http.get(
            f"/tasks/{task_id}/session/input",
            headers=_auth(scoped),
            params={"runner_id": "host-1"},
        )
        anonymous_poll = http.get(f"/tasks/{task_id}/session/input", params={"runner_id": "host-1"})
        read_poll = http.get(
            f"/tasks/{task_id}/session/input", headers=_auth(READ), params={"runner_id": "host-1"}
        )
        omitted_runner = http.get(
            f"/tasks/{task_id}/session/input",
            headers=_auth(WRITE),
        )
        spoofed_settle = http.put(
            f"/tasks/{task_id}/session/input/{delivery_id}",
            headers=_auth(scoped),
            json={"runner_id": "host-1", "status": "delivered"},
        )
        omitted_settle = http.put(
            f"/tasks/{task_id}/session/input/{delivery_id}",
            headers=_auth(WRITE),
            json={"status": "delivered"},
        )
        spoofed_publish = http.put(
            f"/tasks/{task_id}/session/transcript",
            headers=_auth(scoped),
            json={
                "runner_id": "host-1",
                "text": "forged pane",
                "columns": 80,
                "rows": 24,
                "truncated": False,
            },
        )
        omitted_publish = http.put(
            f"/tasks/{task_id}/session/transcript",
            headers=_auth(WRITE),
            json={"text": "x", "columns": 80, "rows": 24, "truncated": False},
        )
        master_poll = http.get(
            f"/tasks/{task_id}/session/input",
            headers=_auth(WRITE),
            params={"runner_id": "host-1"},
        )
        http.delete(f"/tasks/{task_id}/claim", headers=_auth(WRITE))
        http.put(f"/tasks/{task_id}/claim", headers=_auth(WRITE), json={"runner_id": "host-2"})
        former_owner_poll = http.get(
            f"/tasks/{task_id}/session/input",
            headers=_auth(WRITE),
            params={"runner_id": "host-1"},
        )
        former_owner_settle = http.put(
            f"/tasks/{task_id}/session/input/{delivery_id}",
            headers=_auth(WRITE),
            json={"runner_id": "host-1", "status": "delivered"},
        )
        former_owner_publish = http.put(
            f"/tasks/{task_id}/session/transcript",
            headers=_auth(WRITE),
            json={
                "runner_id": "host-1",
                "text": "stale owner",
                "columns": 80,
                "rows": 24,
                "truncated": False,
            },
        )
    assert created.status_code == 202
    assert cross_task.status_code == 403
    assert forged_scope.status_code == 401
    assert spoofed_poll.status_code == 403
    assert anonymous_poll.status_code == 401
    assert read_poll.status_code == 401
    assert omitted_runner.status_code == 422
    assert spoofed_settle.status_code == 403
    assert omitted_settle.status_code == 422
    assert spoofed_publish.status_code == 403
    assert omitted_publish.status_code == 422
    assert master_poll.status_code == 200
    assert master_poll.json()[0]["id"] == delivery_id
    assert former_owner_poll.status_code == 409
    assert former_owner_settle.status_code == 409
    assert former_owner_publish.status_code == 409


def _app(
    tmp_path: Path, *, clock: Callable[[], str] | None = None
) -> tuple[TaskService, TestClient]:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    credential = secrets / "auth.json"
    credential.write_text(json.dumps({"read": [READ], "write": [WRITE]}))
    credential.chmod(0o600)
    service = TaskService(
        SqlAlchemyStore(f"sqlite:///{tmp_path / 'task.db'}"),
        {"spike": Spike()},
        FilesystemArtifactStore(tmp_path / "artifacts"),
        **({"clock": clock} if clock is not None else {}),
    )
    asyncio.run(service.init())
    asyncio.run(service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1")))
    return service, TestClient(
        create_app(service, auth_file="auth.json", auth_mode="enforced", secrets_dir=secrets)
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _live_user_task(
    service: TaskService, http: TestClient, *, depends_on_task_ids: list[str] | None = None
) -> str:
    if "host-1" not in service.live_runners():
        asyncio.run(service.register_runner("host-1"))
    task_id = str(
        http.post(
            "/tasks",
            headers=_auth(WRITE),
            json={
                "repo_id": "r1",
                "workflow": "spike",
                "depends_on_task_ids": [],
            },
        ).json()["id"]
    )
    assert (
        http.put(
            f"/tasks/{task_id}/claim", headers=_auth(WRITE), json={"runner_id": "host-1"}
        ).status_code
        == 200
    )
    asyncio.run(service.register(task_id, "panopticon-test", runner_id="host-1"))
    if depends_on_task_ids:
        assert (
            http.put(
                f"/tasks/{task_id}/dependencies",
                headers=_auth(WRITE),
                json={"dep_ids": depends_on_task_ids},
            ).status_code
            == 200
        )
    return task_id


def test_session_input_requires_write_and_202_means_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-045.1.1
    # 2119: REQ-045.2.1
    # 2119: REQ-045.4.1
    # 2119: REQ-045.5.4
    # 2119: REQ-045.5.8
    # 2119: REQ-045.6.1
    service, client = _app(tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("control plane ran a process")),
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("control plane launched a process")),
    )
    monkeypatch.setattr(os, "system", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)
    with client as http:
        blocker_id = str(
            http.post(
                "/tasks", headers=_auth(WRITE), json={"repo_id": "r1", "workflow": "spike"}
            ).json()["id"]
        )
        task_id = _live_user_task(service, http, depends_on_task_ids=[blocker_id])
        http.put(f"/tasks/{task_id}/blocked", headers=_auth(WRITE), json={"blocked": True})
        http.put(f"/tasks/{task_id}/attention", headers=_auth(WRITE), json={"attention": True})
        before = http.get(f"/tasks/{task_id}", headers=_auth(WRITE)).json()
        body = {"text": "hello λ", "submit": True, "idempotency_key": "phone-0001"}
        with monkeypatch.context() as unavailable:
            unavailable.setattr(
                socket, "socket", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError())
            )
            assert http.post(f"/tasks/{task_id}/session/input", json=body).status_code == 401
            assert (
                http.post(
                    f"/tasks/{task_id}/session/input", headers=_auth(READ), json=body
                ).status_code
                == 401
            )
            accepted = http.post(f"/tasks/{task_id}/session/input", headers=_auth(WRITE), json=body)
            status_without_host = http.get(
                f"/tasks/{task_id}/session/input/{accepted.json()['id']}", headers=_auth(READ)
            )
            second = http.post(
                f"/tasks/{task_id}/session/input",
                headers=_auth(WRITE),
                json={**body, "idempotency_key": "phone-no-host-2"},
            )
            with sqlite3.connect(tmp_path / "task.db") as database:
                pending_count = database.execute(
                    "SELECT count(*) FROM session_input WHERE task_id = ? AND status = 'pending'",
                    (task_id,),
                ).fetchone()
            settlement_without_host = http.put(
                f"/tasks/{task_id}/session/input/{accepted.json()['id']}",
                headers=_auth(WRITE),
                json={"runner_id": "host-1", "status": "delivered"},
            )
            publication_without_host = http.put(
                f"/tasks/{task_id}/session/transcript",
                headers=_auth(WRITE),
                json={
                    "runner_id": "host-1",
                    "text": "pane",
                    "columns": 80,
                    "rows": 24,
                    "truncated": False,
                },
            )
            transcript_without_host = http.get(
                f"/tasks/{task_id}/session/transcript", headers=_auth(READ)
            )
            after = http.get(f"/tasks/{task_id}", headers=_auth(WRITE)).json()
    assert accepted.status_code == 202
    assert second.status_code == 409
    assert pending_count == (1,)
    assert status_without_host.status_code == 200
    assert settlement_without_host.status_code == 200
    assert publication_without_host.status_code == 200
    assert transcript_without_host.status_code == 200
    assert accepted.json()["status"] == "pending"
    assert accepted.json()["submit"] is True
    assert accepted.json()["id"]
    assert "delivered" not in accepted.json()
    assert (after["turn"], after["blocked"], after["attention"]) == (
        before["turn"],
        before["blocked"],
        before["attention"],
    )

    reloaded_service = TaskService(
        SqlAlchemyStore(f"sqlite:///{tmp_path / 'task.db'}"),
        {"spike": Spike()},
        FilesystemArtifactStore(tmp_path / "artifacts"),
    )
    with TestClient(
        create_app(
            reloaded_service,
            auth_file="auth.json",
            auth_mode="enforced",
            secrets_dir=tmp_path / "secrets",
        )
    ) as reloaded:
        durable = reloaded.get(
            f"/tasks/{task_id}/session/input/{accepted.json()['id']}", headers=_auth(READ)
        )
    assert durable.status_code == 200
    assert durable.json()["status"] == "delivered"


@pytest.mark.parametrize("unavailable", ["run", "popen", "system", "which", "socket"])
def test_control_plane_session_io_is_independent_of_each_host_facility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unavailable: str
) -> None:
    # 2119: REQ-045.6.1
    service, client = _app(tmp_path)
    failure = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError(unavailable))
    if unavailable == "run":
        monkeypatch.setattr(subprocess, "run", failure)
    elif unavailable == "popen":
        monkeypatch.setattr(subprocess, "Popen", failure)
    elif unavailable == "system":
        monkeypatch.setattr(os, "system", failure)
    elif unavailable == "which":
        monkeypatch.setattr(shutil, "which", failure)
    with client as http:
        task_id = _live_user_task(service, http)
        if unavailable == "socket":
            monkeypatch.setattr(socket, "socket", failure)
        accepted = http.post(
            f"/tasks/{task_id}/session/input",
            headers=_auth(WRITE),
            json={"text": "x", "submit": False, "idempotency_key": f"facility-{unavailable}"},
        )
        delivery_id = accepted.json()["id"]
        assert (
            http.get(
                f"/tasks/{task_id}/session/input/{delivery_id}", headers=_auth(READ)
            ).status_code
            == 200
        )
        assert (
            http.get(
                f"/tasks/{task_id}/session/input",
                headers=_auth(WRITE),
                params={"runner_id": "host-1"},
            ).status_code
            == 200
        )
        assert (
            http.put(
                f"/tasks/{task_id}/session/input/{delivery_id}",
                headers=_auth(WRITE),
                json={"runner_id": "host-1", "status": "delivered"},
            ).status_code
            == 200
        )
        assert (
            http.put(
                f"/tasks/{task_id}/session/transcript",
                headers=_auth(WRITE),
                json={
                    "runner_id": "host-1",
                    "text": "pane",
                    "columns": 80,
                    "rows": 24,
                    "truncated": False,
                },
            ).status_code
            == 200
        )
        assert (
            http.get(f"/tasks/{task_id}/session/transcript", headers=_auth(READ)).status_code == 200
        )


def test_concurrent_distinct_inputs_leave_exactly_one_pending(tmp_path: Path) -> None:
    # 2119: REQ-045.5.8
    service, client = _app(tmp_path)
    barrier = threading.Barrier(3)
    statuses: list[int] = []

    def submit(index: int, http: TestClient) -> None:
        barrier.wait()
        response = http.post(
            f"/tasks/{task_id}/session/input",
            headers=_auth(WRITE),
            json={
                "text": f"concurrent-{index}",
                "submit": False,
                "idempotency_key": f"concurrent-{index:02d}",
            },
        )
        statuses.append(response.status_code)

    with client as http:
        task_id = _live_user_task(service, http)
        threads = [threading.Thread(target=submit, args=(index, http)) for index in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(2)
    assert sorted(statuses) == [202, 409]
    pending = asyncio.run(service.list_session_inputs(task_id))
    assert len(pending) == 1
    assert pending[0].status.value == "pending"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"text": "", "submit": True, "idempotency_key": "phone-0001"}, 422),
        ({"text": "x" * 65537, "submit": True, "idempotency_key": "phone-0002"}, 422),
        ({"text": "λ" * 32769, "submit": True, "idempotency_key": "phone-0002b"}, 422),
        (
            {"text": "safe\x1b[201~\r", "submit": False, "idempotency_key": "phone-control1"},
            422,
        ),
        (
            {"text": "safe\x1b[201~", "submit": False, "idempotency_key": "phone-terminator"},
            422,
        ),
        ({"text": "safe\r", "submit": False, "idempotency_key": "phone-control2"}, 422),
        ({"text": "safe\x00", "submit": False, "idempotency_key": "phone-control3"}, 422),
        ({"text": "safe\x9b", "submit": False, "idempotency_key": "phone-control4"}, 422),
        *[
            (
                {
                    "text": f"safe{chr(code)}",
                    "submit": False,
                    "idempotency_key": f"control-{code:03d}",
                },
                422,
            )
            for code in (*range(9), *range(11, 32), *range(127, 160))
        ],
        ({"text": "safe\n\t", "submit": False, "idempotency_key": "control-allowed"}, 202),
        ({"text": "x", "submit": "yes", "idempotency_key": "phone-0003"}, 422),
        ({"text": "x", "submit": 1, "idempotency_key": "phone-bool1"}, 422),
        ({"text": "x", "submit": None, "idempotency_key": "phone-bool2"}, 422),
        ({"text": "x", "submit": [], "idempotency_key": "phone-bool3"}, 422),
        ({"text": "x", "submit": {}, "idempotency_key": "phone-bool4"}, 422),
        ({"text": "x", "submit": True, "idempotency_key": "short"}, 422),
        ({"text": "x", "submit": True, "idempotency_key": "seven77"}, 422),
        ({"text": "x", "submit": True, "idempotency_key": "eight888"}, 202),
        ({"text": "x", "submit": True, "idempotency_key": "phone space"}, 422),
        ({"text": "x", "submit": True, "idempotency_key": "phone!bad"}, 422),
        ({"text": "x", "submit": True, "idempotency_key": "phone@bad"}, 422),
        ({"text": "x", "submit": True, "idempotency_key": "phone/bad"}, 422),
        ({"text": "x", "submit": True, "idempotency_key": "λ-phone-key"}, 422),
        ({"text": "x", "submit": True, "idempotency_key": "x" * 129}, 422),
        ({"text": "x", "submit": True, "idempotency_key": "._~-AZaz09" + "x" * 118}, 202),
        ({"text": "x" * 65536, "submit": True, "idempotency_key": "phone-limit"}, 202),
    ],
)
def test_session_input_validation(tmp_path: Path, body: dict[str, object], expected: int) -> None:
    # 2119: REQ-045.2.3
    # 2119: REQ-045.3.3
    service, client = _app(tmp_path)
    with client as http:
        task_id = _live_user_task(service, http)
        response = http.post(f"/tasks/{task_id}/session/input", headers=_auth(WRITE), json=body)
    assert response.status_code == expected
    if expected == 422:
        assert asyncio.run(service.list_session_inputs(task_id)) == []


def test_session_input_rejects_absent_busy_and_non_live_without_record(tmp_path: Path) -> None:
    # 2119: REQ-045.2.1
    # 2119: REQ-045.2.2
    service, client = _app(tmp_path)
    body = {"text": "do not queue", "submit": True, "idempotency_key": "phone-0004"}
    with client as http:
        assert (
            http.post("/tasks/missing/session/input", headers=_auth(WRITE), json=body).status_code
            == 404
        )
        with sqlite3.connect(tmp_path / "task.db") as database:
            assert database.execute("SELECT count(*) FROM session_input").fetchone() == (0,)
        task_id = str(
            http.post(
                "/tasks", headers=_auth(WRITE), json={"repo_id": "r1", "workflow": "spike"}
            ).json()["id"]
        )
        assert (
            http.post(
                f"/tasks/{task_id}/session/input", headers=_auth(WRITE), json=body
            ).status_code
            == 409
        )
        assert (
            http.get(f"/tasks/{task_id}", headers=_auth(WRITE)).json()["container_status"]
            == "queued"
        )
        asyncio.run(service.register(task_id, "orphan-container", runner_id=None))
        assert (
            http.post(
                f"/tasks/{task_id}/session/input",
                headers=_auth(WRITE),
                json={**body, "idempotency_key": "phone-unowned-live"},
            ).status_code
            == 409
        )

        runner_registration = asyncio.run(service.register_runner("host-1"))
        down_id = str(
            http.post(
                "/tasks", headers=_auth(WRITE), json={"repo_id": "r1", "workflow": "spike"}
            ).json()["id"]
        )
        http.put(f"/tasks/{down_id}/claim", headers=_auth(WRITE), json={"runner_id": "host-1"})
        assert (
            http.get(f"/tasks/{down_id}", headers=_auth(WRITE)).json()["container_status"] == "down"
        )
        assert (
            http.post(
                f"/tasks/{down_id}/session/input",
                headers=_auth(WRITE),
                json={**body, "idempotency_key": "phone-down"},
            ).status_code
            == 409
        )
        asyncio.run(service.deregister_runner(runner_registration.id))
        assert (
            http.get(f"/tasks/{down_id}", headers=_auth(WRITE)).json()["container_status"]
            == "disconnected"
        )
        assert (
            http.post(
                f"/tasks/{down_id}/session/input",
                headers=_auth(WRITE),
                json={**body, "idempotency_key": "phone-disconnected"},
            ).status_code
            == 409
        )

        disconnected_live_id = _live_user_task(service, http)
        current_runner = next(
            registration
            for registration in service.live_runner_registrations()
            if registration.runner_id == "host-1"
        )
        asyncio.run(service.deregister_runner(current_runner.id))
        assert service.registrations(disconnected_live_id)
        assert (
            http.post(
                f"/tasks/{disconnected_live_id}/session/input",
                headers=_auth(WRITE),
                json={**body, "idempotency_key": "phone-runner-dead"},
            ).status_code
            == 409
        )
        assert asyncio.run(service.list_session_inputs(disconnected_live_id)) == []

        live_id = _live_user_task(service, http)
        assert (
            http.put(
                f"/tasks/{live_id}/turn", headers=_auth(WRITE), json={"turn": "agent"}
            ).status_code
            == 200
        )
        assert (
            http.post(
                f"/tasks/{live_id}/session/input",
                headers=_auth(WRITE),
                json={**body, "idempotency_key": "phone-agent"},
            ).status_code
            == 409
        )
        terminal_id = _live_user_task(service, http)
        assert (
            http.put(
                f"/tasks/{terminal_id}/state", headers=_auth(WRITE), json={"state": "COMPLETE"}
            ).status_code
            == 200
        )
        assert (
            http.post(
                f"/tasks/{terminal_id}/session/input",
                headers=_auth(WRITE),
                json={**body, "idempotency_key": "phone-terminal"},
            ).status_code
            == 409
        )
        assert asyncio.run(service.list_session_inputs(task_id)) == []
        assert asyncio.run(service.list_session_inputs(down_id)) == []
        assert asyncio.run(service.list_session_inputs(live_id)) == []
        assert asyncio.run(service.list_session_inputs(terminal_id)) == []


def test_input_idempotency_status_shape_and_auth_survive_store_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-045.1.2
    # 2119: REQ-045.1.3
    # 2119: REQ-045.4.1
    # 2119: REQ-045.5.4
    # 2119: REQ-045.5.5
    # 2119: REQ-045.5.6
    # 2119: REQ-045.6.1
    # 2119: REQ-045.6.2
    service, client = _app(tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("host command unavailable")),
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("host command unavailable")),
    )
    monkeypatch.setattr(os, "system", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)
    body = {"text": " secret λ prompt\n", "submit": False, "idempotency_key": "phone-0005"}
    with client as http:
        blocker_id = str(
            http.post(
                "/tasks", headers=_auth(WRITE), json={"repo_id": "r1", "workflow": "spike"}
            ).json()["id"]
        )
        task_id = _live_user_task(service, http, depends_on_task_ids=[blocker_id])
        first = http.post(f"/tasks/{task_id}/session/input", headers=_auth(WRITE), json=body)
        same = http.post(f"/tasks/{task_id}/session/input", headers=_auth(WRITE), json=body)
        delivery_id = first.json()["id"]
        original_before_conflict = http.get(
            f"/tasks/{task_id}/session/input/{delivery_id}", headers=_auth(READ)
        ).json()
        conflict = http.post(
            f"/tasks/{task_id}/session/input",
            headers=_auth(WRITE),
            json={**body, "text": "different"},
        )
        original_after_text_conflict = http.get(
            f"/tasks/{task_id}/session/input/{delivery_id}", headers=_auth(READ)
        ).json()
        submit_conflict = http.post(
            f"/tasks/{task_id}/session/input",
            headers=_auth(WRITE),
            json={**body, "submit": True},
        )
        original_after_submit_conflict = http.get(
            f"/tasks/{task_id}/session/input/{delivery_id}", headers=_auth(READ)
        ).json()
        pending_after_conflicts = http.get(
            f"/tasks/{task_id}/session/input",
            headers=_auth(WRITE),
            params={"runner_id": "host-1"},
        )
        wrong_runner_pending = http.get(
            f"/tasks/{task_id}/session/input",
            headers=_auth(WRITE),
            params={"runner_id": "host-2"},
        )
        status = http.get(f"/tasks/{task_id}/session/input/{delivery_id}", headers=_auth(READ))
        status_with_write = http.get(
            f"/tasks/{task_id}/session/input/{delivery_id}", headers=_auth(WRITE)
        )
        invalid_status = http.get(
            f"/tasks/{task_id}/session/input/{delivery_id}", headers=_auth("invalid-token-value")
        )
        forbidden = http.put(
            f"/tasks/{task_id}/session/input/{delivery_id}",
            headers=_auth(READ),
            json={"runner_id": "host-1", "status": "delivered"},
        )
        missing_settlement = http.put(
            f"/tasks/{task_id}/session/input/{delivery_id}",
            json={"runner_id": "host-1", "status": "delivered"},
        )
        wrong_runner = http.put(
            f"/tasks/{task_id}/session/input/{delivery_id}",
            headers=_auth(WRITE),
            json={"runner_id": "host-2", "status": "delivered"},
        )
        pending_settlement = http.put(
            f"/tasks/{task_id}/session/input/{delivery_id}",
            headers=_auth(WRITE),
            json={"runner_id": "host-1", "status": "pending"},
        )
        missing_status = http.get(f"/tasks/{task_id}/session/input/{delivery_id}")
        missing_transcript = http.get(f"/tasks/{task_id}/session/transcript")
        assert (
            http.put(
                f"/tasks/{task_id}/blocked", headers=_auth(WRITE), json={"blocked": True}
            ).status_code
            == 200
        )
        assert (
            http.put(
                f"/tasks/{task_id}/attention", headers=_auth(WRITE), json={"attention": True}
            ).status_code
            == 200
        )
        before_settle = http.get(f"/tasks/{task_id}", headers=_auth(WRITE)).json()
        assert before_settle["blocked"] is True
        assert before_settle["attention"] is True
        settled = http.put(
            f"/tasks/{task_id}/session/input/{delivery_id}",
            headers=_auth(WRITE),
            json={
                "runner_id": "host-1",
                "status": "failed",
                "failure_reason": "tmux-delivery-failed",
            },
        )
        after_settle = http.get(f"/tasks/{task_id}", headers=_auth(WRITE)).json()
        settled_status = http.get(
            f"/tasks/{task_id}/session/input/{delivery_id}", headers=_auth(READ)
        )
        retried_after_settlement = http.post(
            f"/tasks/{task_id}/session/input", headers=_auth(WRITE), json=body
        )
    assert first.status_code == same.status_code == 202
    assert first.json() == same.json()
    assert conflict.status_code == 409
    assert submit_conflict.status_code == 409
    assert original_after_text_conflict == original_before_conflict
    assert original_after_submit_conflict == original_before_conflict
    assert pending_after_conflicts.json()[0]["text"] == body["text"]
    assert wrong_runner_pending.status_code == 409
    assert status.json() == original_before_conflict
    assert status_with_write.status_code == 200
    assert invalid_status.status_code == 401
    assert status.status_code == 200 and body["text"] not in status.text
    assert "text" not in status.json()
    assert set(status.json()) == STATUS_FIELDS
    assert status.json()["id"] == delivery_id
    assert status.json()["status"] == "pending"
    assert status.json()["submit"] is False
    assert status.json()["byte_count"] == len(body["text"].encode())
    assert status.json()["created_at"]
    assert status.json()["settled_at"] is None
    assert status.json()["failure_reason"] is None
    assert forbidden.status_code == 401
    assert missing_settlement.status_code == 401
    assert wrong_runner.status_code == 409
    assert pending_settlement.status_code == 422
    assert missing_status.status_code == missing_transcript.status_code == 401
    assert settled.status_code == 200
    assert (after_settle["turn"], after_settle["blocked"], after_settle["attention"]) == (
        before_settle["turn"],
        before_settle["blocked"],
        before_settle["attention"],
    )
    assert settled_status.json()["status"] == "failed"
    assert settled_status.json()["id"] == delivery_id
    assert settled_status.json()["submit"] is False
    assert settled_status.json()["byte_count"] == len(body["text"].encode())
    assert settled_status.json()["created_at"] == status.json()["created_at"]
    assert settled_status.json()["settled_at"]
    assert settled_status.json()["failure_reason"] == "tmux-delivery-failed"
    assert retried_after_settlement.status_code == 202
    assert retried_after_settlement.json() == settled_status.json()
    assert body["text"] not in settled_status.text
    assert "text" not in settled_status.json()
    assert set(settled_status.json()) == STATUS_FIELDS
    assert body["text"] not in settled_status.json().values()


def test_delivered_settlement_preserves_turn_blocked_and_attention(tmp_path: Path) -> None:
    # 2119: REQ-045.4.1
    service, client = _app(tmp_path)
    with client as http:
        blocker_id = str(
            http.post(
                "/tasks", headers=_auth(WRITE), json={"repo_id": "r1", "workflow": "spike"}
            ).json()["id"]
        )
        task_id = _live_user_task(service, http, depends_on_task_ids=[blocker_id])
        http.put(f"/tasks/{task_id}/blocked", headers=_auth(WRITE), json={"blocked": True})
        http.put(f"/tasks/{task_id}/attention", headers=_auth(WRITE), json={"attention": True})
        before = http.get(f"/tasks/{task_id}", headers=_auth(READ)).json()
        delivery = http.post(
            f"/tasks/{task_id}/session/input",
            headers=_auth(WRITE),
            json={"text": "send", "submit": True, "idempotency_key": "delivered-0001"},
        ).json()
        after_acceptance = http.get(f"/tasks/{task_id}", headers=_auth(READ)).json()
        settled = http.put(
            f"/tasks/{task_id}/session/input/{delivery['id']}",
            headers=_auth(WRITE),
            json={"runner_id": "host-1", "status": "delivered"},
        )
        after = http.get(f"/tasks/{task_id}", headers=_auth(READ)).json()
    assert (
        after_acceptance["turn"],
        after_acceptance["blocked"],
        after_acceptance["attention"],
    ) == (
        before["turn"],
        before["blocked"],
        before["attention"],
    )
    assert settled.status_code == 200
    assert (after["turn"], after["blocked"], after["attention"]) == (
        before["turn"],
        before["blocked"],
        before["attention"],
    )


def test_staged_acceptance_and_settlement_preserve_false_markers(tmp_path: Path) -> None:
    # 2119: REQ-045.4.1
    service, client = _app(tmp_path)
    with client as http:
        task_id = _live_user_task(service, http)
        before = http.get(f"/tasks/{task_id}", headers=_auth(READ)).json()
        assert (before["turn"], before["blocked"], before["attention"]) == (
            "user",
            False,
            False,
        )
        accepted = http.post(
            f"/tasks/{task_id}/session/input",
            headers=_auth(WRITE),
            json={"text": "stage", "submit": False, "idempotency_key": "staged-0001"},
        )
        after_acceptance = http.get(f"/tasks/{task_id}", headers=_auth(READ)).json()
        settled = http.put(
            f"/tasks/{task_id}/session/input/{accepted.json()['id']}",
            headers=_auth(WRITE),
            json={"runner_id": "host-1", "status": "delivered"},
        )
        after = http.get(f"/tasks/{task_id}", headers=_auth(READ)).json()
    assert accepted.status_code == 202
    assert (
        after_acceptance["turn"],
        after_acceptance["blocked"],
        after_acceptance["attention"],
    ) == (
        before["turn"],
        before["blocked"],
        before["attention"],
    )
    assert settled.status_code == 200
    assert (after["turn"], after["blocked"], after["attention"]) == (
        before["turn"],
        before["blocked"],
        before["attention"],
    )


@pytest.mark.parametrize(("blocked", "attention"), [(True, False), (False, True)])
def test_turn_to_agent_clears_each_marker_independently(
    tmp_path: Path, blocked: bool, attention: bool
) -> None:
    # 2119: REQ-045.4.2
    service, client = _app(tmp_path)
    with client as http:
        blocker_id = str(
            http.post(
                "/tasks", headers=_auth(WRITE), json={"repo_id": "r1", "workflow": "spike"}
            ).json()["id"]
        )
        task_id = _live_user_task(service, http, depends_on_task_ids=[blocker_id])
        if blocked:
            http.put(f"/tasks/{task_id}/blocked", headers=_auth(WRITE), json={"blocked": True})
        if attention:
            http.put(f"/tasks/{task_id}/attention", headers=_auth(WRITE), json={"attention": True})
        prompted = http.put(f"/tasks/{task_id}/turn", headers=_auth(WRITE), json={"turn": "agent"})
    assert prompted.status_code == 200
    assert prompted.json()["blocked"] is False
    assert prompted.json()["attention"] is False


@pytest.mark.parametrize("submit", [False, True])
def test_retried_client_request_causes_one_runner_delivery(tmp_path: Path, submit: bool) -> None:
    # 2119: REQ-045.5.5
    # 2119: REQ-045.5.6
    from panopticon.sessionservice.session_io import SessionIOWorker

    class Runner:
        def __init__(self) -> None:
            self.deliveries: list[tuple[str, bool]] = []

        def deliver_session_input(
            self, task_id: str, delivery_id: str, text: str, *, submit: bool
        ) -> tuple[bool, str | None]:
            del task_id, delivery_id
            self.deliveries.append((text, submit))
            return True, None

        def capture_session_transcript(self, task_id: str) -> None:
            del task_id

    service, client = _app(tmp_path)
    with client as http:
        task_id = _live_user_task(service, http)
        other_task_id = _live_user_task(service, http)
        body = {"text": "once", "submit": submit, "idempotency_key": "phone-once"}
        first = http.post(f"/tasks/{task_id}/session/input", headers=_auth(WRITE), json=body)
        retry = http.post(f"/tasks/{task_id}/session/input", headers=_auth(WRITE), json=body)
        other_task = http.post(
            f"/tasks/{other_task_id}/session/input", headers=_auth(WRITE), json=body
        )
        assert first.json()["id"] == retry.json()["id"]
        assert other_task.status_code == 202
        assert other_task.json()["id"] != first.json()["id"]
        runner = Runner()
        worker = SessionIOWorker(
            TaskServiceClient(http, token=WRITE),
            runner,
            runner_id="host-1",
            dispatch=lambda call: call(),
        )
        task = http.get(f"/tasks/{task_id}", headers=_auth(WRITE)).json()
        worker.process(task)
        worker.process(task)
    assert runner.deliveries == [("once", submit)]


def test_submitted_request_retry_after_settlement_returns_original(tmp_path: Path) -> None:
    # 2119: REQ-045.5.5
    service, client = _app(tmp_path)
    body = {"text": "submit once", "submit": True, "idempotency_key": "submit-retry-1"}
    with client as http:
        task_id = _live_user_task(service, http)
        first = http.post(f"/tasks/{task_id}/session/input", headers=_auth(WRITE), json=body)
        settled = http.put(
            f"/tasks/{task_id}/session/input/{first.json()['id']}",
            headers=_auth(WRITE),
            json={"runner_id": "host-1", "status": "delivered"},
        )
        http.put(f"/tasks/{task_id}/turn", headers=_auth(WRITE), json={"turn": "agent"})
        retry = http.post(f"/tasks/{task_id}/session/input", headers=_auth(WRITE), json=body)
    assert settled.status_code == 200
    assert retry.status_code == 202
    assert retry.json() == settled.json()


def test_agent_turn_worker_publishes_with_explicit_runner_identity(tmp_path: Path) -> None:
    # 2119: REQ-045.6.2
    # 2119: REQ-045.7.2
    # 2119: REQ-045.7.1
    # 2119: REQ-045.7.5
    from panopticon.sessionservice.session_io import SessionIOWorker, capture_pane_snapshot

    class Runner:
        def deliver_session_input(
            self, task_id: str, delivery_id: str, text: str, *, submit: bool
        ) -> tuple[bool, str | None]:
            raise AssertionError((task_id, delivery_id, text, submit))

        def capture_session_transcript(self, task_id: str) -> dict[str, object]:
            assert task_id
            values = iter([f"\x1b[31m{WRITE}\x1b[0m λ é 🚀", "80\t24"])
            snapshot = capture_pane_snapshot(task_id, run=lambda *_a, **_k: next(values))
            assert snapshot is not None
            return snapshot

    service, client = _app(tmp_path)
    with client as http:
        task_id = _live_user_task(service, http)
        task = http.put(
            f"/tasks/{task_id}/turn", headers=_auth(WRITE), json={"turn": "agent"}
        ).json()
        SessionIOWorker(
            TaskServiceClient(http, token=WRITE),
            Runner(),
            runner_id="host-1",
            dispatch=lambda call: call(),
        ).process(task)
        transcript = http.get(f"/tasks/{task_id}/session/transcript", headers=_auth(READ))
    assert transcript.status_code == 200
    assert transcript.json()["text"] == f"{WRITE} λ é 🚀"
    assert transcript.json()["runner_id"] == "host-1"
    assert transcript.json()["truncated"] is False


def test_submitted_delivery_uses_existing_prompt_hook_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-045.4.2
    service, client = _app(tmp_path)

    class HookClient:
        def __init__(self) -> None:
            self.turns: list[tuple[str, str]] = []

        def set_turn(self, task_id: str, turn: str) -> dict[str, object]:
            self.turns.append((task_id, turn))
            return {"id": task_id, "slug": "session-io"}

    hook_client = HookClient()
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    assert hook.main(["agent", "prompt"], client=hook_client, stdin=io.StringIO("")) == 0  # type: ignore[arg-type]
    assert hook_client.turns == [("t1", "agent")]
    with client as http:
        task_id = _live_user_task(service, http)
        http.put(f"/tasks/{task_id}/blocked", headers=_auth(WRITE), json={"blocked": True})
        http.put(f"/tasks/{task_id}/attention", headers=_auth(WRITE), json={"attention": True})
        prompted = http.put(f"/tasks/{task_id}/turn", headers=_auth(WRITE), json={"turn": "agent"})
    assert prompted.json()["turn"] == "agent"
    assert prompted.json()["blocked"] is False
    assert prompted.json()["attention"] is False


def test_transcript_is_readable_bounded_structured_stale_and_unredacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-045.1.2
    # 2119: REQ-045.1.3
    # 2119: REQ-045.7.2
    # 2119: REQ-045.7.3
    # 2119: REQ-045.7.4
    # 2119: REQ-045.7.5
    # 2119: REQ-045.8.1
    # 2119: REQ-045.8.2
    # 2119: REQ-045.5.7
    # 2119: REQ-045.6.2
    # 2119: REQ-045.6.1
    service, client = _app(tmp_path, clock=lambda: "2026-08-03T12:34:56Z")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("host command unavailable")),
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("host command unavailable")),
    )
    monkeypatch.setattr(os, "system", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)
    with client as http:
        task_id = _live_user_task(service, http)
        empty = http.get(f"/tasks/{task_id}/session/transcript", headers=_auth(READ))
        version_before_publish = service.tasks_version()
        published = http.put(
            f"/tasks/{task_id}/session/transcript",
            headers=_auth(WRITE),
            json={
                "runner_id": "host-1",
                "text": "password=hunter2 Authorization: Bearer secret sk-test λ",
                "columns": 90,
                "rows": 30,
                "truncated": False,
            },
        )
        version_after_publish = service.tasks_version()
        latest_text = (
            "\r\n newest password=hunter2 Authorization: Bearer secret sk-test λ é U0001f680 \n"
        )
        latest = http.put(
            f"/tasks/{task_id}/session/transcript",
            headers=_auth(WRITE),
            json={
                "runner_id": "host-1",
                "text": latest_text,
                "columns": 100,
                "rows": 40,
                "truncated": True,
            },
        )
        forbidden_publish = http.put(
            f"/tasks/{task_id}/session/transcript",
            headers=_auth(READ),
            json={"runner_id": "host-1", "text": "x", "columns": 1, "rows": 1, "truncated": False},
        )
        missing_publish = http.put(
            f"/tasks/{task_id}/session/transcript",
            json={"runner_id": "host-1", "text": "x", "columns": 1, "rows": 1, "truncated": False},
        )
        wrong_runner_publish = http.put(
            f"/tasks/{task_id}/session/transcript",
            headers=_auth(WRITE),
            json={
                "runner_id": "host-2",
                "text": "wrong host",
                "columns": 1,
                "rows": 1,
                "truncated": False,
            },
        )
        transcript = http.get(f"/tasks/{task_id}/session/transcript", headers=_auth(READ))
        transcript_with_write = http.get(
            f"/tasks/{task_id}/session/transcript", headers=_auth(WRITE)
        )
        invalid_transcript = http.get(
            f"/tasks/{task_id}/session/transcript", headers=_auth("invalid-token-value")
        )
        openapi = http.get("/openapi.json", headers=_auth(READ)).json()
        registration = service.registrations(task_id)[0]
        asyncio.run(service.deregister(registration.id))
        stale = http.get(f"/tasks/{task_id}/session/transcript", headers=_auth(READ))
    assert empty.status_code == 503
    assert empty.json() == {"detail": "session transcript unavailable"}
    assert published.status_code == latest.status_code == 200
    assert version_after_publish == version_before_publish
    assert forbidden_publish.status_code == 401
    assert missing_publish.status_code == 401
    assert wrong_runner_publish.status_code == 409
    assert transcript_with_write.status_code == 200
    assert invalid_transcript.status_code == 401
    assert transcript.json()["text"] == stale.json()["text"]
    assert transcript.json()["text"] == latest_text
    assert transcript.json()["source"] == "pane"
    assert transcript.json()["runner_id"] == "host-1"
    assert transcript.json()["columns"] == 100
    assert transcript.json()["rows"] == 40
    assert transcript.json()["truncated"] is True
    assert transcript.json()["received_at"] == "2026-08-03T12:34:56Z"
    assert stale.json()["stale"] is True
    assert {key: value for key, value in stale.json().items() if key != "stale"} == {
        key: value for key, value in transcript.json().items() if key != "stale"
    }
    transcript_docs = openapi["paths"]["/tasks/{task_id}/session/transcript"]["get"][
        "description"
    ].lower()
    input_docs = openapi["paths"]["/tasks/{task_id}/session/input"]["post"]["description"].lower()
    assert transcript_docs == (
        "return unredacted pane text. it may contain arbitrary terminal output, including "
        "credentials or other secrets printed in the pane."
    )
    assert input_docs == (
        "accept input for runner delivery. client retries are idempotent. a runner crash or "
        "settlement-write failure after the tmux side effect can cause a duplicate delivery."
    )


@pytest.mark.parametrize(
    "control",
    ["\x1b[31mred", "\x1b]0;title\x07", "\x1bPdata\x1b\\", "\x1b_payload\x1b\\"],
)
def test_transcript_publication_rejects_terminal_escape_sequences(
    tmp_path: Path, control: str
) -> None:
    # 2119: REQ-045.7.5
    service, client = _app(tmp_path)
    with client as http:
        task_id = _live_user_task(service, http)
        rejected = http.put(
            f"/tasks/{task_id}/session/transcript",
            headers=_auth(WRITE),
            json={
                "runner_id": "host-1",
                "text": f"visible{control} λ",
                "columns": 80,
                "rows": 24,
                "truncated": False,
            },
        )
        unavailable = http.get(f"/tasks/{task_id}/session/transcript", headers=_auth(READ))
    assert rejected.status_code == 422
    assert unavailable.status_code == 503
