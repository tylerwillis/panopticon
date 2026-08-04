"""Regression contract for representation-independent transcript validation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from panopticon.client import TaskServiceClient
from panopticon.core.models import Repo
from panopticon.sessionservice.session_io import capture_pane_snapshot
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.auth import scoped_task_token
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike

READ = "session-reader-token"
WRITE = "session-writer-token"


def _app(tmp_path: Path) -> tuple[TaskService, TestClient]:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    credential = secrets / "auth.json"
    credential.write_text(json.dumps({"read": [READ], "write": [WRITE]}))
    credential.chmod(0o600)
    service = TaskService(
        SqlAlchemyStore(f"sqlite:///{tmp_path / 'task.db'}"),
        {"spike": Spike()},
        FilesystemArtifactStore(tmp_path / "artifacts"),
    )
    asyncio.run(service.init())
    asyncio.run(service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1")))
    return service, TestClient(
        create_app(service, auth_file="auth.json", auth_mode="enforced", secrets_dir=secrets)
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _live_task(service: TaskService, http: TestClient) -> str:
    asyncio.run(service.register_runner("host-1"))
    task_id = str(
        http.post(
            "/tasks",
            headers=_auth(WRITE),
            json={"repo_id": "r1", "workflow": "spike", "depends_on_task_ids": []},
        ).json()["id"]
    )
    claimed = http.put(
        f"/tasks/{task_id}/claim",
        headers=_auth(WRITE),
        json={"runner_id": "host-1"},
    )
    assert claimed.status_code == 200
    asyncio.run(service.register(task_id, "panopticon-test", runner_id="host-1"))
    return task_id


@pytest.mark.parametrize(
    ("valid_text", "invalid_text"),
    [
        ("visible pwned", "visible\x1bpwned"),
        ("A" * 65536, "A" * 65537),
        ("é" * 32768, "é" * 32769),
        ("line\n" * 200, "line\n" * 201),
    ],
    ids=["terminal-escape", "ascii-byte-bound", "multibyte-bound", "logical-line-bound"],
)
def test_client_validates_decoded_transcript_before_replacing_snapshot(
    tmp_path: Path, valid_text: str, invalid_text: str
) -> None:
    # 2119: REQ-047.1.1
    # 2119: REQ-047.1.2
    # 2119: REQ-047.1.3
    # 2119: REQ-047.1.4
    # 2119: REQ-047.2.1
    # 2119: REQ-047.2.2
    # 2119: REQ-047.3.2
    service, client = _app(tmp_path)
    with client as http:
        task_id = _live_task(service, http)
        published_bodies: list[dict[str, object]] = []

        def record_publication(request: httpx.Request) -> None:
            if request.method == "PUT" and request.url.path.endswith("/session/transcript"):
                published_bodies.append(json.loads(request.content))

        http.event_hooks["request"].append(record_publication)
        publisher = TaskServiceClient(http, token=WRITE)
        safe_snapshot = {
            "text": valid_text,
            "columns": 80,
            "rows": 24,
            "truncated": False,
        }
        publisher.publish_session_transcript(task_id, safe_snapshot, runner_id="host-1")

        with pytest.raises(httpx.HTTPStatusError) as rejected:
            publisher.publish_session_transcript(
                task_id,
                {**safe_snapshot, "text": invalid_text},
                runner_id="host-1",
            )

        client_bodies = tuple(published_bodies)
        stored_after_client_rejection = http.get(
            f"/tasks/{task_id}/session/transcript", headers=_auth(READ)
        )
        plain_accepted = http.put(
            f"/tasks/{task_id}/session/transcript",
            headers=_auth(WRITE),
            json={**safe_snapshot, "runner_id": "host-1"},
        )
        plain_rejected = http.put(
            f"/tasks/{task_id}/session/transcript",
            headers=_auth(WRITE),
            json={**safe_snapshot, "text": invalid_text, "runner_id": "host-1"},
        )
        stored = http.get(f"/tasks/{task_id}/session/transcript", headers=_auth(READ))

    assert rejected.value.response.status_code == 422
    assert plain_accepted.status_code == 200
    assert plain_rejected.status_code == 422
    assert stored_after_client_rejection.status_code == 200
    assert stored_after_client_rejection.json()["text"] == valid_text
    assert client_bodies
    assert all("text_b64" in body and "text" not in body for body in client_bodies)
    assert stored.status_code == 200
    assert stored.json()["text"] == valid_text


def test_transcript_publication_keeps_auth_and_runner_ansi_layers(tmp_path: Path) -> None:
    # 2119: REQ-047.3.1
    # 2119: REQ-047.3.2
    service, client = _app(tmp_path)
    with client as http:
        task_id = _live_task(service, http)
        scoped = TaskServiceClient(http, token=scoped_task_token(WRITE, task_id))
        ansi_sequences = [
            "\x1b[31m",
            "\x1b]0;title\x07",
            "\x1bPdata\x1b\\",
            "\x1bXx\x1b\\",
            "\x1b^x\x1b\\",
            "\x1b_x\x1b\\",
            "\x1b7",
        ]
        ansi = "".join(ansi_sequences)
        pane_values = iter([f"visible{ansi}plain", "80\t24"])
        snapshot = capture_pane_snapshot(
            task_id,
            run=lambda *_args, **_kwargs: next(pane_values),
        )
        assert snapshot is not None
        assert snapshot["text"] == "visibleplain"

        with pytest.raises(httpx.HTTPStatusError) as forbidden:
            scoped.publish_session_transcript(task_id, snapshot, runner_id="host-1")

        owner = TaskServiceClient(http, token=WRITE)
        owner.publish_session_transcript(task_id, snapshot, runner_id="host-1")
        with pytest.raises(httpx.HTTPStatusError) as wrong_runner:
            owner.publish_session_transcript(task_id, snapshot, runner_id="host-2")
        missing_runner = http.put(
            f"/tasks/{task_id}/session/transcript",
            headers=_auth(WRITE),
            json=snapshot,
        )
        unstripped = [
            http.put(
                f"/tasks/{task_id}/session/transcript",
                headers=_auth(WRITE),
                json={
                    **snapshot,
                    "text": f"visible{sequence}plain",
                    "runner_id": "host-1",
                },
            )
            for sequence in ansi_sequences
        ]

    assert forbidden.value.response.status_code == 401
    assert wrong_runner.value.response.status_code == 409
    assert missing_runner.status_code == 422
    assert all(response.status_code == 422 for response in unstripped)
