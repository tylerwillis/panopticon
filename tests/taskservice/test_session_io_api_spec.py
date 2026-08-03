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
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from panopticon.client import TaskServiceClient
from panopticon.container import hook
from panopticon.core.models import Repo
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
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
    # 2119: REQ-044.1.1
    # 2119: REQ-044.2.1
    # 2119: REQ-044.4.1
    # 2119: REQ-044.6.1
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
            settlement_without_host = http.put(
                f"/tasks/{task_id}/session/input/{second.json()['id']}",
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
    assert status_without_host.status_code == 200
    assert settlement_without_host.status_code == 200
    assert publication_without_host.status_code == 200
    assert transcript_without_host.status_code == 200
    assert accepted.json()["status"] == "pending"
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
    assert durable.json()["status"] == "pending"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"text": "", "submit": True, "idempotency_key": "phone-0001"}, 422),
        ({"text": "x" * 65537, "submit": True, "idempotency_key": "phone-0002"}, 422),
        ({"text": "λ" * 32769, "submit": True, "idempotency_key": "phone-0002b"}, 422),
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
        ({"text": "x", "submit": True, "idempotency_key": "phone/bad"}, 422),
        ({"text": "x", "submit": True, "idempotency_key": "λ-phone-key"}, 422),
        ({"text": "x", "submit": True, "idempotency_key": "x" * 129}, 422),
        ({"text": "x", "submit": True, "idempotency_key": "._~-AZaz09" + "x" * 118}, 202),
        ({"text": "x" * 65536, "submit": True, "idempotency_key": "phone-limit"}, 202),
    ],
)
def test_session_input_validation(tmp_path: Path, body: dict[str, object], expected: int) -> None:
    # 2119: REQ-044.2.3
    service, client = _app(tmp_path)
    with client as http:
        task_id = _live_user_task(service, http)
        response = http.post(f"/tasks/{task_id}/session/input", headers=_auth(WRITE), json=body)
    assert response.status_code == expected


def test_session_input_rejects_absent_busy_and_non_live_without_record(tmp_path: Path) -> None:
    # 2119: REQ-044.2.1
    # 2119: REQ-044.2.2
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
    # 2119: REQ-044.1.2
    # 2119: REQ-044.1.3
    # 2119: REQ-044.4.1
    # 2119: REQ-044.5.4
    # 2119: REQ-044.5.5
    # 2119: REQ-044.5.6
    # 2119: REQ-044.6.1
    # 2119: REQ-044.6.2
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
    body = {"text": " secret λ prompt\r\n", "submit": False, "idempotency_key": "phone-0005"}
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
    # 2119: REQ-044.4.1
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
        delivery = http.post(
            f"/tasks/{task_id}/session/input",
            headers=_auth(WRITE),
            json={"text": "send", "submit": True, "idempotency_key": "delivered-0001"},
        ).json()
        before = http.get(f"/tasks/{task_id}", headers=_auth(READ)).json()
        settled = http.put(
            f"/tasks/{task_id}/session/input/{delivery['id']}",
            headers=_auth(WRITE),
            json={"runner_id": "host-1", "status": "delivered"},
        )
        after = http.get(f"/tasks/{task_id}", headers=_auth(READ)).json()
    assert settled.status_code == 200
    assert (after["turn"], after["blocked"], after["attention"]) == (
        before["turn"],
        before["blocked"],
        before["attention"],
    )


def test_staged_acceptance_and_settlement_preserve_false_markers(tmp_path: Path) -> None:
    # 2119: REQ-044.4.1
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
    # 2119: REQ-044.4.2
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


def test_retried_client_request_causes_one_runner_delivery(tmp_path: Path) -> None:
    # 2119: REQ-044.5.5
    from panopticon.sessionservice.session_io import SessionIOWorker

    class Runner:
        def __init__(self) -> None:
            self.deliveries: list[str] = []

        def deliver_session_input(
            self, task_id: str, text: str, *, submit: bool
        ) -> tuple[bool, str | None]:
            del task_id, submit
            self.deliveries.append(text)
            return True, None

        def capture_session_transcript(self, task_id: str) -> None:
            del task_id

    service, client = _app(tmp_path)
    with client as http:
        task_id = _live_user_task(service, http)
        other_task_id = _live_user_task(service, http)
        body = {"text": "once", "submit": False, "idempotency_key": "phone-once"}
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
    assert runner.deliveries == ["once"]


def test_submitted_request_retry_after_settlement_returns_original(tmp_path: Path) -> None:
    # 2119: REQ-044.5.5
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
        retry = http.post(f"/tasks/{task_id}/session/input", headers=_auth(WRITE), json=body)
    assert settled.status_code == 200
    assert retry.status_code == 202
    assert retry.json() == settled.json()


def test_submitted_delivery_uses_existing_prompt_hook_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-044.4.2
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
    # 2119: REQ-044.1.2
    # 2119: REQ-044.1.3
    # 2119: REQ-044.7.2
    # 2119: REQ-044.7.3
    # 2119: REQ-044.7.4
    # 2119: REQ-044.7.5
    # 2119: REQ-044.8.1
    # 2119: REQ-044.8.2
    # 2119: REQ-044.5.7
    # 2119: REQ-044.6.2
    # 2119: REQ-044.6.1
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
        latest_text = "\r\n newest password=hunter2 Authorization: Bearer secret sk-test λ \n"
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
        "accept input for runner delivery. client retries are idempotent. a runner crash between "
        "the tmux side effect and its settlement write can cause a duplicate delivery."
    )
