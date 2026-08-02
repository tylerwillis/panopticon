from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from panopticon.sessionservice.migration import (
    MigrationConflict,
    accept_migration,
    request_migration,
)


class FakeClient:
    def __init__(self, task: dict[str, Any]) -> None:
        self.task = task
        self.migrations: list[dict[str, Any]] = []

    def get_task(self, _task_id: str) -> dict[str, Any]:
        return self.task

    def get_repo(self, _repo_id: str) -> dict[str, str]:
        return {"id": "r1", "git_url": "https://forge/r1.git"}

    def record_migration(self, _task_id: str, **facts: Any) -> dict[str, Any]:
        self.migrations.append(facts)
        return {**self.task, "migration": facts}


def test_forge_first_request_identifies_every_local_loss(tmp_path: Path) -> None:
    checkout = tmp_path / "t1"
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", origin], check=True)
    subprocess.run(["git", "init", "--initial-branch=panopticon/safe", checkout], check=True)
    subprocess.run(["git", "-C", checkout, "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", checkout, "config", "user.email", "test@example.com"], check=True)
    (checkout / "tracked.txt").write_text("base\n")
    subprocess.run(["git", "-C", checkout, "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", checkout, "commit", "--message", "base"], check=True)
    subprocess.run(["git", "-C", checkout, "remote", "add", "origin", str(origin)], check=True)
    subprocess.run(["git", "-C", checkout, "push", "origin", "panopticon/safe"], check=True)
    (checkout / "dirty.txt").write_text("dirty\n")
    client = FakeClient(
        {
            "id": "t1",
            "branch": "panopticon/safe",
            "provisioned_by": "host-a",
        }
    )
    with pytest.raises(MigrationConflict, match="explicit discard"):
        request_migration(
            client,  # type: ignore[arg-type]
            "t1",
            destination_runner="host-b",
            workspace_method="forge-first",
            transfer_session=False,
            tasks_root=tmp_path,
        )


def test_accept_refuses_stale_destination_and_live_source(tmp_path: Path) -> None:
    task = {
        "id": "t1",
        "repo_id": "r1",
        "branch": "panopticon/safe",
        "claimed_by": "host-a",
        "migration": {
            "source_runner": "host-a",
            "destination_runner": "host-b",
            "workspace_disposition": "pending",
            "workspace_method": "archive",
            "session_history_disposition": "omitted",
            "discarded_changes": [],
        },
    }
    client = FakeClient(task)
    (tmp_path / "t1").mkdir()
    with pytest.raises(MigrationConflict, match="already exists"):
        accept_migration(
            client,  # type: ignore[arg-type]
            "t1",
            runner_id="host-b",
            tasks_root=tmp_path,
            workspace_archive=None,
            session_archive=None,
        )
