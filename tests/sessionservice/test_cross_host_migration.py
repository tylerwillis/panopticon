"""Executable contract for safe cross-host task migration (REQ-034).

The production seams named here are intentionally host-side and deterministic. Archive tests use
real temporary files, while git and task-service facts are represented as explicit inputs; no
Docker daemon, network service, agent, or LLM is used.
"""

from __future__ import annotations

import inspect
import io
import os
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

from panopticon.core.git import GitClones
from panopticon.core.models import Actor, Repo, Task
from panopticon.harnesses import LaunchContext
from panopticon.harnesses.codex import CodexHarness
from panopticon.sessionservice.clones import CloneCache
from panopticon.sessionservice.migration import (
    MigrationConflict,
    MigrationRequest,
    accept_workspace,
    change_session_history_disposition,
    config_volume_name,
    create_config_archive,
    create_workspace_archive,
    export_config_volume,
    inspect_forge_first,
    migrate_task,
    migration_claim_allowed,
    provisioning_ready,
    restore_config_archive,
    restore_config_volume,
    spawn_allowed,
    stage_workspace_archive,
    validate_migration_record,
    validate_reported_facts,
    verify_canonical_workspace,
)
from panopticon.sessionservice.provisioner import Provisioner
from panopticon.sessionservice.spawn import MigrationRequired, prepare_workspace
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import NotReady, TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike


# 2119: REQ-034.6.1
# 2119: REQ-034.6.4
def test_production_migration_never_invokes_docker_container_snapshot_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def run(args: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        assert check
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", run)
    exported = tmp_path / "config.tar.gz"
    export_config_volume("t1", exported, image="migration-helper")
    with tarfile.open(exported, "w:gz"):
        pass
    restore_config_volume("t1", exported, image="migration-helper")

    assert len(calls) == 2
    assert all(command[:2] == ["docker", "run"] for command in calls)
    assert all("panopticon-config-t1" in " ".join(command) for command in calls)
    assert all(
        forbidden not in command
        for command in calls
        for forbidden in ("commit", "cp", "export", "import")
    )


# 2119: REQ-034.6.6
def test_full_migration_never_invokes_docker_container_snapshot_at_subprocess_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that the full migrate_task lifecycle never invokes docker container snapshot commands
    (commit, cp, export, import) at the subprocess.run level, not just through host interface."""
    calls: list[list[str]] = []
    real_run = subprocess.run

    def run(args: list[str], *, check: bool = True, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        # Verify no forbidden docker container operations in the command
        if len(args) > 1 and args[0] == "docker":
            assert args[1] not in ("commit", "cp", "export", "import"), (
                f"Docker container snapshot operation '{args[1]}' invoked during migration: {args}"
            )
        # For non-docker commands (git, etc), use the real subprocess.run
        if not (len(args) > 0 and args[0] == "docker"):
            return real_run(args, check=check, **kwargs)  # type: ignore[arg-type]
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", run)

    # Set up a minimal full migration scenario
    source = _RecordingHost("host-a", tmp_path / "a-tasks", tmp_path / "a-config")
    destination = _RecordingHost("host-b", tmp_path / "b-tasks", tmp_path / "b-config")
    _source_checkout(source.tasks_root)
    control = _RecordingControlPlane(_task(claimed_by=None))

    # Run full archive migration (exercises workspace archiving, transfer, staging, acceptance)
    migrate_task(
        control,
        source,
        destination,
        MigrationRequest(
            task_id="t1",
            destination_runner="host-b",
            workspace="archive",
            session_history="omit",
        ),
    )

    # Verify that docker commands were only for config volume ops (or none if no config transfer)
    docker_commands = [cmd for cmd in calls if cmd and cmd[0] == "docker"]
    for docker_cmd in docker_commands:
        # Only docker run commands are allowed (for config volume operations)
        assert len(docker_cmd) > 1 and docker_cmd[1] == "run", (
            f"Unexpected docker command during migration: {docker_cmd}"
        )


def _task(**updates: object) -> dict[str, object]:
    task: dict[str, object] = {
        "id": "t1",
        "repo_id": "r1",
        "branch": "panopticon/safe-move",
        "clone": "/host-a/tasks/t1",
        "provisioned_by": "host-a",
        "workspace_verified_by": "host-a",
        "claimed_by": "host-a",
        "migration": None,
        "git_url": "forge:r1",
    }
    task.update(updates)
    return task


class _RecordingControlPlane:
    """Persistent-fact fake: intentionally has no filesystem/git dependency or clock."""

    def __init__(self, task: dict[str, object], audit: list[str] | None = None) -> None:
        self.task = task
        self.events: list[tuple[str, object]] = []
        self.audit = audit if audit is not None else []

    def read(self, _task_id: str) -> dict[str, object]:
        return dict(self.task)

    def record_migration(self, migration: dict[str, object]) -> None:
        self.task["migration"] = dict(migration)
        self.events.append(("migration", dict(migration)))
        self.audit.append(f"persist-migration:{migration['workspace_disposition']}")

    def record_provisioning(self, *, runner_id: str, branch: str, clone: str) -> None:
        self.task.update(provisioned_by=runner_id, branch=branch, clone=clone)
        self.events.append(("provisioning", (runner_id, branch, clone)))
        self.audit.append("persist-provisioning")

    def move_claim(self, runner_id: str) -> None:
        self.task["claimed_by"] = runner_id
        self.events.append(("claim", runner_id))
        self.audit.append("persist-claim")


class _RecordingHost:
    def __init__(
        self,
        runner_id: str,
        tasks_root: Path,
        config_root: Path,
        audit: list[str] | None = None,
    ) -> None:
        self.runner_id = runner_id
        self.tasks_root = tasks_root
        self.config_root = config_root
        self.events: list[str] = []
        self.spawned: list[str] = []
        self.launch_modes: list[str] = []
        self.container_operations: list[str] = []
        self.audit = audit if audit is not None else []

    def spawn(self, task_id: str) -> None:
        self.events.append("spawn")
        self.spawned.append(task_id)
        self.audit.append("spawn")
        sessions = self.config_root / config_volume_name(task_id) / "sessions"
        self.launch_modes.append("resume" if any(sessions.glob("**/*.jsonl")) else "fresh")

    def verify_workspace(self, task_id: str, branch: str) -> None:
        head = (self.tasks_root / task_id / ".git" / "HEAD").read_text()
        if not head.rstrip().endswith(branch):
            raise MigrationConflict("canonical branch mismatch")
        self.audit.append("verify-canonical")

    def checkout_forge(self, task_id: str, git_url: str, branch: str) -> None:
        assert git_url == "forge:r1"
        checkout = self.tasks_root / task_id
        (checkout / ".git").mkdir(parents=True)
        (checkout / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
        self.audit.append("checkout-forge")

    def commit_container(self, _task_id: str) -> None:
        self.container_operations.append("commit")

    def export_container(self, _task_id: str) -> None:
        self.container_operations.append("export")

    def restore_container(self, _task_id: str) -> None:
        self.container_operations.append("restore")

    def copy_container(self, _task_id: str) -> None:
        self.container_operations.append("copy")

    def export_writable_layer(self, _task_id: str) -> None:
        self.container_operations.append("export-writable-layer")

    def restore_writable_layer(self, _task_id: str) -> None:
        self.container_operations.append("restore-writable-layer")

    def copy_writable_layer(self, _task_id: str) -> None:
        self.container_operations.append("copy-writable-layer")

    def commit_writable_layer(self, _task_id: str) -> None:
        self.container_operations.append("commit-writable-layer")


def _source_checkout(root: Path) -> Path:
    checkout = root / "t1"
    checkout.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch=panopticon/safe-move", str(checkout)],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", "forge:r1"], check=True)
    (checkout / ".git" / "objects" / "object-a").write_bytes(b"object-a")
    (checkout / "tracked.txt").write_text("tracked\n")
    (checkout / "untracked.txt").write_text("uncommitted\n")
    return checkout


def _recording_git() -> tuple[list[list[str]], object]:
    calls: list[list[str]] = []

    def run(args: object, *, check: bool = True) -> str:
        calls.append(list(args))  # type: ignore[arg-type]
        return ""

    return calls, run


class _ProvisioningClient:
    def __init__(self) -> None:
        self.recorded: list[tuple[str, str, str]] = []

    def workflow_execution(self, _name: str | None) -> dict[str, object]:
        return {"runner_type": "docker", "script": "", "clone_repo": False, "workdir": None}

    def record_provisioning(
        self,
        task_id: str,
        branch: str,
        clone: str,
        _runner_id: str,
        _workspace_verified: bool,
    ) -> dict[str, str]:
        self.recorded.append((task_id, branch, clone))
        return {"id": task_id, "branch": branch, "clone": clone}


# 2119: REQ-034.1.1
# 2119: REQ-034.1.2
def test_provisioning_is_runner_qualified_local_and_deterministic() -> None:
    task = _task()

    assert provisioning_ready(task, runner_id="host-a", workspace_exists=True)
    assert not provisioning_ready(task, runner_id="host-b", workspace_exists=True)
    assert not provisioning_ready(task, runner_id="host-a", workspace_exists=False)
    assert not provisioning_ready(
        dict(task, workspace_verified_by="host-b"),
        runner_id="host-a",
        workspace_exists=True,
    )
    assert not provisioning_ready(
        dict(task, provisioned_by="host-b"),
        runner_id="host-a",
        workspace_exists=True,
    )
    assert not provisioning_ready(
        dict(task, workspace_verified_by="host-b", provisioned_by="host-a"),
        runner_id="host-a",
        workspace_exists=True,
    )
    assert not provisioning_ready(
        dict(task, branch=None),
        runner_id="host-a",
        workspace_exists=True,
    )
    assert not provisioning_ready(
        dict(task, clone=None),
        runner_id="host-a",
        workspace_exists=True,
    )
    assert not provisioning_ready(
        dict(task, claimed_by=None),
        runner_id="host-a",
        workspace_exists=True,
    )

    # The decision is a pure fold of recorded facts plus the runner's local existence probe. It
    # receives no filesystem object, command runner, liveness set, timestamp, or clock callback.
    assert provisioning_ready(dict(task), runner_id="host-a", workspace_exists=True)


# 2119: REQ-034.1.2
def test_provisioned_property_gates_on_workspace_verified_by_match() -> None:
    """Verify Task.provisioned gates on all three fields matching, not just some."""
    task = Task(
        id="t1",
        repo_id="r1",
        workflow="spike",
        state="WORKING",
        turn=Actor.AGENT,
        branch="panopticon/safe-move",
        clone="/host-a/tasks/t1",
        claimed_by="host-a",
        provisioned_by="host-a",
        workspace_verified_by="host-a",
    )
    assert task.provisioned

    task.workspace_verified_by = "host-b"
    assert not task.provisioned
    task.workspace_verified_by = "host-a"
    task.claimed_by = "host-b"
    assert not task.provisioned

    # workspace_verified_by==claimed_by but both differ from provisioned_by — also not provisioned
    task.claimed_by = "host-b"
    task.workspace_verified_by = "host-b"
    task.provisioned_by = "host-a"
    assert not task.provisioned

    # None values: missing any required field means not provisioned
    task.claimed_by = "host-a"
    task.provisioned_by = None
    task.workspace_verified_by = "host-a"
    assert not task.provisioned

    task.provisioned_by = "host-a"
    task.workspace_verified_by = None
    assert not task.provisioned

    task.workspace_verified_by = "host-a"
    task.claimed_by = None
    assert not task.provisioned


# 2119: REQ-034.2.4
def test_destination_acceptance_rejects_missing_wrong_branch_and_wrong_repository(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "tasks" / "t1"

    with pytest.raises(MigrationConflict, match=r"workspace.*missing"):
        accept_workspace(
            _task(), runner_id="host-b", staged=None, canonical=canonical, repository_id="r1"
        )

    staged = tmp_path / "staged"
    staged.mkdir()
    with pytest.raises(MigrationConflict, match="branch"):
        accept_workspace(
            _task(),
            runner_id="host-b",
            staged=staged,
            canonical=canonical,
            repository_id="r1",
            inspected_branch="trunk",
        )
    with pytest.raises(MigrationConflict, match="repository"):
        accept_workspace(
            _task(),
            runner_id="host-b",
            staged=staged,
            canonical=canonical,
            repository_id="different-repo",
            inspected_branch="panopticon/safe-move",
        )


# 2119: REQ-034.4.1
# 2119: REQ-034.4.4
def test_forge_first_requires_clean_fully_pushed_recorded_branch() -> None:
    assert inspect_forge_first(
        recorded_branch="panopticon/safe-move",
        checked_out_branch="panopticon/safe-move",
        dirty_paths=(),
        head="abc123",
        forge_reachable_commits={"abc123"},
    ) == {"branch": "panopticon/safe-move", "commit": "abc123"}

    with pytest.raises(MigrationConflict, match="uncommitted"):
        inspect_forge_first(
            recorded_branch="panopticon/safe-move",
            checked_out_branch="panopticon/safe-move",
            dirty_paths=("notes.txt",),
            head="abc123",
            forge_reachable_commits={"abc123"},
        )
    with pytest.raises(MigrationConflict, match="unpushed"):
        inspect_forge_first(
            recorded_branch="panopticon/safe-move",
            checked_out_branch="panopticon/safe-move",
            dirty_paths=(),
            head="local-only",
            forge_reachable_commits={"abc123"},
        )


# 2119: REQ-034.3.2
# 2119: REQ-034.3.3
# 2119: REQ-034.3.1
# 2119: REQ-034.4.2
def test_cross_host_claim_requires_observable_acceptance_or_explicit_discard() -> None:
    released = _task(claimed_by=None)
    assert migration_claim_allowed(released, runner_id="host-a")  # local restart
    assert not migration_claim_allowed(released, runner_id="host-b")

    pending = _task(
        claimed_by=None,
        migration={
            "source_runner": "host-a",
            "destination_runner": "host-b",
            "workspace_disposition": "pending",
            "session_history_disposition": "requested",
            "discarded_changes": [],
        },
    )
    assert not migration_claim_allowed(pending, runner_id="host-b")
    assert not migration_claim_allowed(
        {
            **pending,
            "migration": {**pending["migration"], "workspace_disposition": "failed"},
        },
        runner_id="host-b",
    )

    accepted = _task(
        claimed_by=None,
        migration={
            "source_runner": "host-a",
            "destination_runner": "host-b",
            "workspace_disposition": "accepted",
            "session_history_disposition": "omitted",
            "discarded_changes": ["notes.txt", "commit:local-only"],
        },
    )
    assert migration_claim_allowed(accepted, runner_id="host-b")
    assert accepted["migration"] == {
        "source_runner": "host-a",
        "destination_runner": "host-b",
        "workspace_disposition": "accepted",
        "session_history_disposition": "omitted",
        "discarded_changes": ["notes.txt", "commit:local-only"],
    }

    for missing in (
        "source_runner",
        "destination_runner",
        "workspace_disposition",
        "session_history_disposition",
    ):
        incomplete = dict(accepted["migration"])
        incomplete.pop(missing)
        with pytest.raises(MigrationConflict, match=missing):
            validate_migration_record(incomplete)


# 2119: REQ-034.5.1
# 2119: REQ-034.5.2
# 2119: REQ-034.5.3
# 2119: REQ-034.5.4
def test_workspace_archive_preserves_git_and_dirty_files_and_installs_atomically(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    subprocess.run(
        ["git", "init", "--initial-branch=panopticon/safe-move", str(source)],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(source), "remote", "add", "origin", "forge:r1"], check=True)
    (source / "tracked-modified.txt").write_text("modified but not committed\n")
    (source / "dirty.txt").write_text("not committed\n")
    executable = source / "tool.sh"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    (source / "tool-link").symlink_to("tool.sh")
    archive = create_workspace_archive(source)

    canonical = tmp_path / "tasks" / "t1"
    staged = stage_workspace_archive(archive, canonical=canonical)
    assert staged != canonical
    assert not canonical.exists()
    assert (staged / ".git" / "HEAD").read_text().endswith("safe-move\n")
    assert (staged / "dirty.txt").read_text() == "not committed\n"

    def entries(root: Path) -> dict[str, tuple[bool, bool, int, str | bytes]]:
        return {
            path.relative_to(root).as_posix(): (
                path.is_symlink(),
                path.is_dir(),
                path.lstat().st_mode & 0o777,
                str(path.readlink())
                if path.is_symlink()
                else path.read_bytes()
                if path.is_file()
                else b"",
            )
            for path in root.rglob("*")
        }

    source_entries = entries(source)
    staged_entries = entries(staged)
    assert staged_entries == source_entries
    assert source.exists()  # destination staging cannot delete source state

    installed = accept_workspace(
        _task(),
        runner_id="host-b",
        staged=staged,
        canonical=canonical,
        repository_id="r1",
        inspected_branch="panopticon/safe-move",
    )
    assert installed == canonical
    assert entries(canonical) == source_entries

    # A retry recognizes the accepted canonical workspace instead of overlaying it or deleting it.
    assert (
        accept_workspace(
            _task(),
            runner_id="host-b",
            staged=None,
            canonical=canonical,
            repository_id="r1",
            inspected_branch="panopticon/safe-move",
        )
        == canonical
    )


# 2119: REQ-034.6.1
# 2119: REQ-034.6.4
# 2119: REQ-034.6.5
def test_config_archive_restores_standard_volume_without_credentials(tmp_path: Path) -> None:
    config = tmp_path / "config"
    sessions = config / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "rollout.jsonl").write_text('{"originator":"codex-tui"}\n')
    (config / "auth.json").symlink_to("/panopticon/credentials/auth.json")
    secret_dir = tmp_path / "credential-dir"
    secret_dir.mkdir()
    (secret_dir / "token").write_text("never archive me")
    (config / "credentials.d").symlink_to(secret_dir, target_is_directory=True)

    archive = create_config_archive(config, credential_mount=secret_dir)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as bundle:
        names = bundle.getnames()
        auth = bundle.getmember("auth.json")
        credential_dir = bundle.getmember("credentials.d")
        assert "sessions/rollout.jsonl" in names
        assert auth.issym()
        assert auth.linkname == "/panopticon/credentials/auth.json"
        assert credential_dir.issym()
        assert credential_dir.linkname == str(secret_dir)
        assert not any(name.startswith("credentials.d/") for name in names)
        assert not any(name.startswith("panopticon/credentials") for name in names)
        assert "never archive me" not in b"".join(
            bundle.extractfile(member).read()
            for member in bundle.getmembers()
            if member.isfile() and bundle.extractfile(member) is not None
        ).decode(errors="ignore")

    restored: list[tuple[str, bytes]] = []
    restore_config_archive(
        "t1", archive, restore=lambda volume, payload: restored.append((volume, payload))
    )
    assert restored == [(config_volume_name("t1"), archive)]
    assert config_volume_name("t1") == "panopticon-config-t1"


# 2119: REQ-034.7.3
def test_spawn_gate_allows_safe_fresh_agent_but_never_container_copy() -> None:
    accepted_without_history = _task(
        claimed_by="host-b",
        provisioned_by="host-b",
        migration={
            "source_runner": "host-a",
            "destination_runner": "host-b",
            "workspace_disposition": "accepted",
            "session_history_disposition": "omitted",
            "discarded_changes": [],
        },
    )
    assert spawn_allowed(
        accepted_without_history,
        runner_id="host-b",
        workspace_exists=True,
        inspected_branch="panopticon/safe-move",
    )

    requested_history = _task(
        claimed_by="host-b",
        provisioned_by="host-b",
        migration={
            "source_runner": "host-a",
            "destination_runner": "host-b",
            "workspace_disposition": "accepted",
            "session_history_disposition": "requested",
            "discarded_changes": [],
        },
    )
    assert not spawn_allowed(
        requested_history,
        runner_id="host-b",
        workspace_exists=True,
        inspected_branch="panopticon/safe-move",
    )
    assert not spawn_allowed(
        accepted_without_history,
        runner_id="host-b",
        workspace_exists=False,
        inspected_branch="panopticon/safe-move",
    )
    assert not spawn_allowed(
        accepted_without_history,
        runner_id="host-b",
        workspace_exists=True,
        inspected_branch="trunk",
    )

    # Migration state has no container artifact/disposition field: images are rebuilt and only
    # workspace + optional session history participate in the gate.
    assert "container" not in accepted_without_history["migration"]


# 2119: REQ-034.1.1
# 2119: REQ-034.3.2
# 2119: REQ-034.3.3
# 2119: REQ-034.4.2
# 2119: REQ-034.2.4
# 2119: REQ-034.5.2
# 2119: REQ-034.5.3
# 2119: REQ-034.6.1
# 2119: REQ-034.6.4
# 2119: REQ-034.6.5
# 2119: REQ-034.6.6
# 2119: REQ-034.7.1
# 2119: REQ-034.7.2
# 2119: REQ-034.7.3
def test_archive_migration_persists_then_spawns_identical_workspace_and_session(
    tmp_path: Path,
) -> None:
    audit: list[str] = []
    source = _RecordingHost("host-a", tmp_path / "a-tasks", tmp_path / "a-config", audit)
    destination = _RecordingHost("host-b", tmp_path / "b-tasks", tmp_path / "b-config", audit)
    source_checkout = _source_checkout(source.tasks_root)
    source_session = source.config_root / config_volume_name("t1")
    (source_session / "sessions").mkdir(parents=True)
    (source_session / "sessions" / "interactive.jsonl").write_text('{"originator":"codex-tui"}\n')
    secret = tmp_path / "source-secret.json"
    secret.write_text("SOURCE SECRET MUST NOT MOVE")
    (source_session / "auth.json").symlink_to(secret)
    control = _RecordingControlPlane(_task(claimed_by=None), audit)

    result = migrate_task(
        control,
        source,
        destination,
        MigrationRequest(
            task_id="t1",
            destination_runner="host-b",
            workspace="archive",
            session_history="transfer",
        ),
    )

    destination_checkout = destination.tasks_root / "t1"
    destination_session = destination.config_root / config_volume_name("t1")
    assert (destination_checkout / ".git" / "objects" / "object-a").read_bytes() == b"object-a"
    assert "url = forge:r1" in (destination_checkout / ".git" / "config").read_text()
    assert (destination_checkout / "tracked.txt").read_text() == "tracked\n"
    assert (destination_checkout / "untracked.txt").read_text() == "uncommitted\n"
    assert (destination_session / "sessions" / "interactive.jsonl").is_file()
    assert (destination_session / "auth.json").is_symlink()
    assert (destination_session / "auth.json").readlink() == secret
    assert not any(
        path.is_file() and not path.is_symlink() and path.read_bytes() == secret.read_bytes()
        for path in destination_session.rglob("*")
    )
    assert source_checkout.exists()  # retained through destination acceptance

    assert result["provisioned_by"] == "host-b"
    assert result["branch"] == "panopticon/safe-move"
    assert result["clone"] == str(destination_checkout)
    assert result["claimed_by"] == "host-b"
    assert result["migration"] == {
        "source_runner": "host-a",
        "destination_runner": "host-b",
        "workspace_disposition": "accepted",
        "session_history_disposition": "accepted",
        "discarded_changes": [],
    }
    assert destination.spawned == ["t1"]
    assert destination.launch_modes == ["resume"]
    # Acceptance is persisted and ownership/claim move before execution; no container artifact or
    # docker-commit operation participates in migration.
    event_names = [name for name, _value in control.events]
    assert event_names.index("migration") < event_names.index("provisioning")
    assert event_names.index("provisioning") < event_names.index("claim")
    assert set(event_names) == {"migration", "provisioning", "claim"}
    assert audit.index("persist-migration:accepted") < audit.index("persist-provisioning")
    assert audit.index("persist-provisioning") < audit.index("persist-claim")
    assert audit.index("persist-claim") < audit.index("verify-canonical") < audit.index("spawn")
    # REQ-034.6.6: verify migration never attempts to snapshot, commit, export, or restore the container
    assert source.container_operations == []
    assert destination.container_operations == []
    # Verify no container snapshot operations (commit, cp, export, import) are attempted during
    # the complete migration lifecycle — workspace archiving, transfer, staging, acceptance, and spawn
    assert source.container_operations == []
    assert destination.container_operations == []


# 2119: REQ-034.1.1
# 2119: REQ-034.2.4
def test_missing_owner_and_wrong_destination_acceptance_never_authorize_claim() -> None:
    assert not provisioning_ready(
        _task(provisioned_by=None), runner_id="host-a", workspace_exists=True
    )
    accepted_for_c = _task(
        claimed_by=None,
        migration={
            "source_runner": "host-a",
            "destination_runner": "host-c",
            "workspace_disposition": "accepted",
            "session_history_disposition": "omitted",
            "discarded_changes": [],
        },
    )
    assert not migration_claim_allowed(accepted_for_c, runner_id="host-b")


# 2119: REQ-034.4.1
# 2119: REQ-034.4.2
# 2119: REQ-034.4.4
# 2119: REQ-034.6.6
def test_forge_first_is_refused_until_portable_or_explicitly_discarded(tmp_path: Path) -> None:
    source = _RecordingHost("host-a", tmp_path / "a-tasks", tmp_path / "a-config")
    destination = _RecordingHost("host-b", tmp_path / "b-tasks", tmp_path / "b-config")
    _source_checkout(source.tasks_root)
    task = _task(claimed_by=None)

    for dirty, pushed in [(["untracked.txt"], True), ([], False)]:
        control = _RecordingControlPlane(dict(task))
        with pytest.raises(MigrationConflict):
            migrate_task(
                control,
                source,
                destination,
                MigrationRequest(
                    task_id="t1",
                    destination_runner="host-b",
                    workspace="forge-first",
                    session_history="omit",
                ),
                inspected_dirty_paths=dirty,
                recorded_head="local-head",
                forge_reachable_commits={"local-head"} if pushed else set(),
            )
        assert control.task["provisioned_by"] == "host-a"
        assert control.task["claimed_by"] is None
        assert destination.spawned == []

    with pytest.raises(MigrationConflict, match="branch"):
        inspect_forge_first(
            recorded_branch="panopticon/safe-move",
            checked_out_branch="trunk",
            dirty_paths=(),
            head="portable",
            forge_reachable_commits={"portable"},
        )

    # Naming only one of two identified losses is not explicit authorization for both.
    partial = _RecordingControlPlane(dict(task))
    with pytest.raises(MigrationConflict, match="discard"):
        migrate_task(
            partial,
            source,
            destination,
            MigrationRequest(
                task_id="t1",
                destination_runner="host-b",
                workspace="forge-first",
                session_history="omit",
                discard_changes=("untracked.txt",),
            ),
            inspected_dirty_paths=["untracked.txt"],
            recorded_head="local-head",
            forge_reachable_commits=set(),
        )
    wrong_identity = _RecordingControlPlane(dict(task))
    with pytest.raises(MigrationConflict, match="discard"):
        migrate_task(
            wrong_identity,
            source,
            destination,
            MigrationRequest(
                task_id="t1",
                destination_runner="host-b",
                workspace="forge-first",
                session_history="omit",
                discard_changes=("different.txt", "commit:different"),
            ),
            inspected_dirty_paths=["untracked.txt"],
            recorded_head="local-head",
            forge_reachable_commits=set(),
        )

    control = _RecordingControlPlane(dict(task))
    result = migrate_task(
        control,
        source,
        destination,
        MigrationRequest(
            task_id="t1",
            destination_runner="host-b",
            workspace="forge-first",
            session_history="omit",
            discard_changes=("untracked.txt", "commit:local-head"),
        ),
        inspected_dirty_paths=["untracked.txt"],
        recorded_head="local-head",
        forge_reachable_commits=set(),
    )
    assert result["migration"]["discarded_changes"] == ["untracked.txt", "commit:local-head"]
    assert result["migration"]["session_history_disposition"] == "omitted"
    assert result["branch"] == "panopticon/safe-move"
    assert destination.spawned == ["t1"]
    assert source.container_operations == []
    assert destination.container_operations == []


# 2119: REQ-034.2.4
# 2119: REQ-034.5.2
# 2119: REQ-034.5.3
# 2119: REQ-034.5.4
# 2119: REQ-034.7.3
def test_failed_validation_preserves_source_owner_canonical_destination_and_nonrunnable_state(
    tmp_path: Path,
) -> None:
    source = _RecordingHost("host-a", tmp_path / "a-tasks", tmp_path / "a-config")
    destination = _RecordingHost("host-b", tmp_path / "b-tasks", tmp_path / "b-config")
    source_checkout = _source_checkout(source.tasks_root)
    existing = _source_checkout(destination.tasks_root)
    (existing / "sentinel").write_text("do not replace")
    control = _RecordingControlPlane(_task(claimed_by=None))

    with pytest.raises(MigrationConflict):
        migrate_task(
            control,
            source,
            destination,
            MigrationRequest(
                task_id="t1",
                destination_runner="host-b",
                workspace="archive",
                session_history="omit",
            ),
            destination_repository_id="wrong-repo",
        )

    assert source_checkout.exists()
    assert (existing / "sentinel").read_text() == "do not replace"
    assert control.task["provisioned_by"] == "host-a"
    assert control.task["claimed_by"] is None
    assert control.task["migration"]["workspace_disposition"] == "failed"
    assert destination.spawned == []


# 2119: REQ-034.6.1
# 2119: REQ-034.6.2
# 2119: REQ-034.6.5
# 2119: REQ-034.6.6
# 2119: REQ-034.7.2
def test_session_history_is_independent_and_requested_restore_cannot_be_silently_omitted(
    tmp_path: Path,
) -> None:
    audit: list[str] = []
    source = _RecordingHost("host-a", tmp_path / "a-tasks", tmp_path / "a-config", audit)
    destination = _RecordingHost("host-b", tmp_path / "b-tasks", tmp_path / "b-config", audit)
    _source_checkout(source.tasks_root)
    control = _RecordingControlPlane(_task(claimed_by=None))

    with pytest.raises(MigrationConflict, match="session"):
        migrate_task(
            control,
            source,
            destination,
            MigrationRequest(
                task_id="t1",
                destination_runner="host-b",
                workspace="archive",
                session_history="transfer",
            ),
        )
    assert destination.spawned == []

    source_session = source.config_root / config_volume_name("t1") / "sessions"
    source_session.mkdir(parents=True)
    (source_session / "old.jsonl").write_text('{"originator":"codex-tui"}\n')
    stale_destination = destination.config_root / config_volume_name("t1") / "sessions"
    stale_destination.mkdir(parents=True)
    (stale_destination / "stale.jsonl").write_text(
        '{"type":"session_meta","payload":{"id":"stale-destination",'
        '"originator":"codex-tui","thread_source":"user"}}\n'
    )

    fresh = migrate_task(
        _RecordingControlPlane(_task(claimed_by=None)),
        source,
        destination,
        MigrationRequest(
            task_id="t1",
            destination_runner="host-b",
            workspace="archive",
            session_history="omit",
        ),
    )
    assert fresh["migration"]["session_history_disposition"] == "omitted"
    assert fresh["branch"] == "panopticon/safe-move"
    assert destination.launch_modes[-1] == "fresh"
    assert not (destination.config_root / config_volume_name("t1")).exists()
    actual_home = tmp_path / "omitted-home"
    actual_home.mkdir()
    (actual_home / ".codex").symlink_to(
        destination.config_root / config_volume_name("t1"), target_is_directory=True
    )
    assert CodexHarness().argv(LaunchContext(home=actual_home, cwd=Path("/workspace")))[0:2] != [
        "codex",
        "resume",
    ]
    assert audit.index("verify-canonical") < audit.index("spawn")
    assert source.container_operations == []
    assert destination.container_operations == []

    bad_source = _RecordingHost("host-a", tmp_path / "bad-a", tmp_path / "bad-a-config")
    bad_destination = _RecordingHost("host-b", tmp_path / "bad-b", tmp_path / "bad-b-config")
    bad_checkout = _source_checkout(bad_source.tasks_root)
    (bad_checkout / ".git" / "HEAD").write_text("ref: refs/heads/trunk\n")
    with pytest.raises(MigrationConflict, match="branch"):
        migrate_task(
            _RecordingControlPlane(_task(claimed_by=None)),
            bad_source,
            bad_destination,
            MigrationRequest(
                task_id="t1",
                destination_runner="host-b",
                workspace="archive",
                session_history="omit",
            ),
        )
    assert bad_destination.spawned == []


# 2119: REQ-034.6.3
def test_transferred_config_uses_real_codex_resume_path_and_omission_uses_first_run(
    tmp_path: Path,
) -> None:
    source_config = tmp_path / "source-config"
    rollout = source_config / "sessions" / "2026" / "08" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        '{"type":"session_meta","payload":{"id":"interactive-1",'
        '"originator":"codex-tui","thread_source":"user"}}\n'
    )
    archive = create_config_archive(source_config)
    resumed_home = tmp_path / "resumed-home"
    mounted_config = resumed_home / ".codex"
    mounted_config.mkdir(parents=True)

    def restore(volume: str, payload: bytes) -> None:
        assert volume == "panopticon-config-t1"
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as bundle:
            bundle.extractall(mounted_config, filter="data")

    restore_config_archive("t1", archive, restore=restore)
    harness = CodexHarness()
    assert harness.argv(LaunchContext(home=resumed_home, cwd=Path("/workspace")))[:3] == [
        "codex",
        "resume",
        "interactive-1",
    ]

    fresh_home = tmp_path / "fresh-home"
    assert harness.argv(LaunchContext(home=fresh_home, cwd=Path("/workspace")))[0:2] != [
        "codex",
        "resume",
    ]


# 2119: REQ-034.7.2
def test_requested_session_transfer_requires_explicit_operator_omission() -> None:
    requested = {
        "source_runner": "host-a",
        "destination_runner": "host-b",
        "workspace_disposition": "accepted",
        "session_history_disposition": "requested",
        "session_history_was_requested": True,
        "discarded_changes": [],
    }
    with pytest.raises(MigrationConflict, match="operator"):
        change_session_history_disposition(requested, disposition="omitted", actor="agent")
    requested_task = _task(claimed_by="host-b", provisioned_by="host-b", migration=dict(requested))
    assert not spawn_allowed(
        requested_task,
        runner_id="host-b",
        workspace_exists=True,
        inspected_branch="panopticon/safe-move",
    )
    silently_omitted = dict(requested, session_history_disposition="omitted")
    silent_task = _task(claimed_by="host-b", provisioned_by="host-b", migration=silently_omitted)
    assert not spawn_allowed(
        silent_task,
        runner_id="host-b",
        workspace_exists=True,
        inspected_branch="panopticon/safe-move",
    )
    omitted = change_session_history_disposition(requested, disposition="omitted", actor="user")
    assert omitted["session_history_disposition"] == "omitted"
    assert omitted["session_history_changed_by"] == "user"
    omitted_task = _task(claimed_by="host-b", provisioned_by="host-b", migration=omitted)
    assert spawn_allowed(
        omitted_task,
        runner_id="host-b",
        workspace_exists=True,
        inspected_branch="panopticon/safe-move",
    )
    restored = dict(requested, session_history_disposition="accepted")
    restored_task = _task(claimed_by="host-b", provisioned_by="host-b", migration=restored)
    assert spawn_allowed(
        restored_task,
        runner_id="host-b",
        workspace_exists=True,
        inspected_branch="panopticon/safe-move",
    )


# 2119: REQ-034.7.2
def test_spawn_gate_raises_migration_conflict_when_session_history_not_accepted() -> None:
    """Verify that spawn_allowed() correctly rejects incomplete session history dispositions,
    which would cause the spawner to raise MigrationConflict at lines 238-244 of spawner.py."""
    requested = {
        "source_runner": "host-a",
        "destination_runner": "host-b",
        "workspace_disposition": "accepted",
        "session_history_disposition": "requested",
        "session_history_was_requested": True,
        "discarded_changes": [],
    }

    # Scenario 1: session_history_disposition="requested" should be rejected (would cause spawner to raise MigrationConflict)
    task_requested = _task(claimed_by="host-b", provisioned_by="host-b", migration=dict(requested))
    assert not spawn_allowed(
        task_requested,
        runner_id="host-b",
        workspace_exists=True,
        inspected_branch="panopticon/safe-move",
    ), "spawn_allowed should reject when session_history_disposition='requested'"

    # Scenario 2: silently omitted without operator approval should also be rejected
    silently_omitted = dict(requested, session_history_disposition="omitted")
    task_silent = _task(claimed_by="host-b", provisioned_by="host-b", migration=silently_omitted)
    assert not spawn_allowed(
        task_silent,
        runner_id="host-b",
        workspace_exists=True,
        inspected_branch="panopticon/safe-move",
    ), "spawn_allowed should reject when session history was silently omitted"

    # Scenario 3: operator-approved omission should be accepted (would allow spawner to proceed)
    operator_approved = {
        "source_runner": "host-a",
        "destination_runner": "host-b",
        "workspace_disposition": "accepted",
        "session_history_disposition": "omitted",
        "session_history_was_requested": True,
        "session_history_changed_by": "user",
        "discarded_changes": [],
    }
    task_approved = _task(claimed_by="host-b", provisioned_by="host-b", migration=operator_approved)
    assert spawn_allowed(
        task_approved,
        runner_id="host-b",
        workspace_exists=True,
        inspected_branch="panopticon/safe-move",
    ), "spawn_allowed should accept when operator explicitly approved omission"


# 2119: REQ-034.1.2
# 2119: REQ-034.1.3
# 2119: REQ-034.3.1
# 2119: REQ-034.3.4
# 2119: REQ-034.4.3
async def test_real_task_service_persists_and_gates_host_qualified_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite:///{tmp_path / 'tasks.db'}"
    svc = TaskService(
        SqlAlchemyStore(database_url),
        {"spike": Spike()},
        FilesystemArtifactStore(tmp_path / "artifacts"),
        clock=iter([f"t{i}" for i in range(100)]).__next__,
        id_factory=lambda: "t1",
    )
    await svc.init()
    await svc.create_repo(Repo(id="r1", name="acme/r1", git_url="https://forge/r1.git"))
    task = await svc.create_task("r1", "spike")
    task = await svc.set_slug(task.id, "safe-move")
    assert not task.provisioned
    assert task.provisioned_by is None
    assert task.workspace_verified_by is None
    await svc.claim(task.id, "host-a")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("control plane ran host I/O")),
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("control plane spawned command")),
    )
    monkeypatch.setattr(
        Path,
        "open",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("control plane opened host file")),
    )
    monkeypatch.setattr(
        os,
        "listdir",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("control plane listed host fs")),
    )
    with pytest.raises(TypeError, match="serializable"):
        await svc.record_provisioning(
            task.id,
            branch="panopticon/safe-move",
            clone=Path("/host-a/tasks/t1"),  # type: ignore[arg-type]
            runner_id="host-a",
            workspace_verified=True,
        )
    with pytest.raises(TypeError, match="serializable"):
        await svc.record_migration(
            task.id,
            source_runner="host-a",
            destination_runner="host-b",
            workspace_disposition="pending",
            session_history_disposition="requested",
            discarded_changes=[GitClones()],  # type: ignore[list-item]
            discard_authorized_by=None,
        )
    monkeypatch.setattr(
        time,
        "time",
        lambda: (_ for _ in ()).throw(AssertionError("transition read wall clock")),
    )
    await svc.record_provisioning(
        task.id,
        branch="panopticon/safe-move",
        clone="/host-a/tasks/t1",
        runner_id="host-a",
        workspace_verified=False,
    )
    assert not (await svc.get_task(task.id)).provisioned
    await svc.record_provisioning(
        task.id,
        branch="panopticon/safe-move",
        clone="/host-a/tasks/t1",
        runner_id="host-a",
        workspace_verified=True,
    )
    verified = await svc.get_task(task.id)
    assert verified.workspace_verified_by == "host-a"
    assert verified.provisioned_by == "host-a"
    assert verified.provisioned
    await svc.release(task.id)
    released = await svc.get_task(task.id)
    assert released.provisioned_by == "host-a"
    assert released.claimed_by is None
    assert not released.provisioned
    with pytest.raises(NotReady, match="migration"):
        await svc.claim(task.id, "host-b")

    with pytest.raises(NotReady, match="source"):
        await svc.record_migration(
            task.id,
            source_runner="host-c",
            destination_runner="host-b",
            workspace_disposition="accepted",
            session_history_disposition="omitted",
            discarded_changes=[],
            discard_authorized_by=None,
        )

    await svc.record_migration(
        task.id,
        source_runner="host-a",
        destination_runner="host-b",
        workspace_disposition="pending",
        session_history_disposition="requested",
        discarded_changes=[],
        discard_authorized_by=None,
    )
    pending = await svc.get_task(task.id)
    assert pending.migration is not None
    assert pending.migration.source_runner == "host-a"
    assert pending.migration.destination_runner == "host-b"
    assert pending.migration.workspace_disposition == "pending"
    assert pending.migration.session_history_disposition == "requested"
    monkeypatch.undo()
    pending_service = TaskService(
        SqlAlchemyStore(database_url),
        {"spike": Spike()},
        FilesystemArtifactStore(tmp_path / "artifacts-pending"),
    )
    await pending_service.init()
    pending_reloaded = await pending_service.get_task(task.id)
    assert pending_reloaded.migration is not None
    assert (
        pending_reloaded.migration.source_runner,
        pending_reloaded.migration.destination_runner,
        pending_reloaded.migration.workspace_disposition,
        pending_reloaded.migration.session_history_disposition,
    ) == ("host-a", "host-b", "pending", "requested")
    with pytest.raises(NotReady, match="pending"):
        await svc.claim(task.id, "host-b")
    await svc.record_migration(
        task.id,
        source_runner="host-a",
        destination_runner="host-c",
        workspace_disposition="pending",
        session_history_disposition="omitted",
        discarded_changes=[],
        discard_authorized_by=None,
    )
    await svc.record_migration(
        task.id,
        source_runner="host-a",
        destination_runner="host-c",
        workspace_disposition="accepted",
        session_history_disposition="omitted",
        discarded_changes=[],
        discard_authorized_by=None,
    )
    with pytest.raises(NotReady, match="destination"):
        await svc.claim(task.id, "host-b")
    await svc.record_migration(
        task.id,
        source_runner="host-a",
        destination_runner="host-b",
        workspace_disposition="pending",
        session_history_disposition="omitted",
        discarded_changes=["notes.txt"],
        discard_authorized_by="user",
    )
    await svc.record_migration(
        task.id,
        source_runner="host-a",
        destination_runner="host-b",
        workspace_disposition="accepted",
        session_history_disposition="omitted",
        discarded_changes=["notes.txt"],
        discard_authorized_by="user",
    )
    persisted = await svc.get_task(task.id)
    assert persisted.migration is not None
    assert (
        persisted.migration.source_runner,
        persisted.migration.destination_runner,
        persisted.migration.workspace_disposition,
        persisted.migration.session_history_disposition,
    ) == ("host-a", "host-b", "accepted", "omitted")
    assert persisted.migration.discarded_changes == ["notes.txt"]
    assert persisted.migration.discard_authorized_by == "user"
    claimed = await svc.claim(task.id, "host-b")
    assert claimed.claimed_by == "host-b"
    assert not claimed.provisioned
    after_claim = await svc.get_task(task.id)
    assert after_claim.migration is not None
    assert after_claim.migration.discarded_changes == ["notes.txt"]
    reloaded_service = TaskService(
        SqlAlchemyStore(database_url),
        {"spike": Spike()},
        FilesystemArtifactStore(tmp_path / "artifacts-reloaded"),
    )
    await reloaded_service.init()
    reloaded = await reloaded_service.get_task(task.id)
    assert reloaded.migration is not None
    assert reloaded.migration.discarded_changes == ["notes.txt"]
    assert reloaded.migration.discard_authorized_by == "user"
    assert (
        reloaded.migration.source_runner,
        reloaded.migration.destination_runner,
        reloaded.migration.workspace_disposition,
        reloaded.migration.session_history_disposition,
    ) == ("host-a", "host-b", "accepted", "omitted")


def test_task_service_migration_surface_accepts_only_reported_facts() -> None:
    assert set(inspect.signature(TaskService.record_provisioning).parameters) == {
        "self",
        "task_id",
        "branch",
        "clone",
        "runner_id",
        "workspace_verified",
    }
    assert set(inspect.signature(TaskService.record_migration).parameters) == {
        "self",
        "task_id",
        "source_runner",
        "destination_runner",
        "workspace_disposition",
        "workspace_method",
        "session_history_disposition",
        "discarded_changes",
        "discard_authorized_by",
    }


# 2119: REQ-034.1.3
def test_reported_facts_reject_executable_or_nonserializable_collaborators() -> None:
    assert validate_reported_facts(
        {"clone": "/host-a/tasks/t1", "runner_id": "host-a", "workspace_verified": True}
    )
    for forbidden in (Path("/host-a/tasks/t1"), lambda: True, GitClones()):
        with pytest.raises(TypeError, match="serializable"):
            validate_reported_facts({"clone": forbidden})


# 2119: REQ-034.1.4
def test_migration_decision_ignores_timestamp_and_liveness_context() -> None:
    facts = _task(
        claimed_by=None,
        migration={
            "source_runner": "host-a",
            "destination_runner": "host-b",
            "workspace_disposition": "accepted",
            "session_history_disposition": "omitted",
            "discarded_changes": [],
        },
    )
    early_dead = dict(facts, updated_at="1900-01-01", live_runners=[])
    late_live = dict(facts, updated_at="2999-12-31", live_runners=["host-a", "host-b"])
    assert migration_claim_allowed(early_dead, runner_id="host-b")
    assert migration_claim_allowed(late_live, runner_id="host-b")
    assert migration_claim_allowed(
        dict(facts, updated_at="1900", live_runners=["host-b"]), runner_id="host-b"
    )
    assert migration_claim_allowed(
        dict(facts, updated_at="2999", live_runners=[]), runner_id="host-b"
    )
    denied_early = dict(
        early_dead, migration={**facts["migration"], "workspace_disposition": "pending"}
    )
    denied_late = dict(
        late_live, migration={**facts["migration"], "workspace_disposition": "pending"}
    )
    assert not migration_claim_allowed(denied_early, runner_id="host-b")
    assert not migration_claim_allowed(denied_late, runner_id="host-b")
    provisioned_early = dict(facts, claimed_by="host-a", updated_at="1900", live_runners=[])
    provisioned_late = dict(facts, claimed_by="host-a", updated_at="2999", live_runners=["host-a"])
    assert provisioning_ready(provisioned_early, runner_id="host-a", workspace_exists=True)
    assert provisioning_ready(provisioned_late, runner_id="host-a", workspace_exists=True)
    assert provisioning_ready(
        dict(facts, claimed_by="host-a", updated_at="1900", live_runners=["host-a"]),
        runner_id="host-a",
        workspace_exists=True,
    )
    assert provisioning_ready(
        dict(facts, claimed_by="host-a", updated_at="2999", live_runners=[]),
        runner_id="host-a",
        workspace_exists=True,
    )
    assert not provisioning_ready(provisioned_early, runner_id="host-a", workspace_exists=False)
    assert not provisioning_ready(provisioned_late, runner_id="host-a", workspace_exists=False)


# 2119: REQ-034.2.1
def test_actual_spawn_preparation_refuses_foreign_workspace_record() -> None:
    calls, run = _recording_git()
    with pytest.raises(MigrationRequired, match=r"host-a.*host-b"):
        prepare_workspace(
            "t1",
            {"id": "r1", "git_url": "https://forge/r1.git"},
            cache=CloneCache("/cache", run=run, exists=lambda _p: True),  # type: ignore[arg-type]
            tasks_root="/host-b/tasks",
            git=GitClones(run=run),  # type: ignore[arg-type]
            exists=lambda _p: False,
            task={"branch": "panopticon/safe-move", "provisioned_by": "host-a"},
            runner_id="host-b",
            makedirs=lambda _p: None,
        )
    assert calls == []

    local_calls, local_run = _recording_git()
    assert (
        prepare_workspace(
            "t1",
            {"id": "r1", "git_url": "https://forge/r1.git"},
            cache=CloneCache("/cache", run=local_run, exists=lambda _p: True),  # type: ignore[arg-type]
            tasks_root="/host-a/tasks",
            git=GitClones(run=local_run),  # type: ignore[arg-type]
            exists=lambda _p: False,
            task={"provisioned_by": "host-a"},
            runner_id="host-a",
            makedirs=lambda _p: None,
        )
        == "/host-a/tasks/t1"
    )
    assert local_calls


# 2119: REQ-034.2.2
def test_actual_provisioner_reestablishes_and_inspects_recorded_forge_branch(
    tmp_path: Path,
) -> None:
    calls, run = _recording_git()
    client = _ProvisioningClient()
    clone = tmp_path / "tasks" / "t1"
    clone.mkdir(parents=True)
    subprocess.run(["git", "init", str(clone)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(clone), "remote", "add", "origin", "https://forge/r1.git"],
        check=True,
    )
    subprocess.run(["git", "-C", str(clone), "checkout", "-b", "panopticon/safe-move"], check=True)
    provisioner = Provisioner(
        client,  # type: ignore[arg-type]
        clones_root=str(tmp_path / "tasks"),
        git=GitClones(run=run),  # type: ignore[arg-type]
    )
    task = _task(
        slug="different-slug",
        claimed_by="host-b",
        provisioned=False,
        migration={
            "destination_runner": "host-b",
            "workspace_disposition": "accepted",
            "workspace_method": "forge-first",
        },
    )
    assert provisioner.provision(task, runner_id="host-b") == "panopticon/safe-move"
    assert calls == [
        ["git", "-C", str(clone), "fetch", "origin", "panopticon/safe-move"],
        ["git", "-C", str(clone), "checkout", "--detach", "origin/panopticon/safe-move"],
        ["git", "-C", str(clone), "checkout", "-B", "panopticon/safe-move"],
    ]
    assert client.recorded == [("t1", "panopticon/safe-move", str(clone))]


# 2119: REQ-034.2.2
def test_provisioner_does_not_record_destination_left_on_wrong_branch(tmp_path: Path) -> None:
    _calls, run = _recording_git()
    client = _ProvisioningClient()
    clone = tmp_path / "tasks" / "t1"
    clone.mkdir(parents=True)
    subprocess.run(["git", "init", str(clone)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(clone), "remote", "add", "origin", "https://forge/r1.git"],
        check=True,
    )
    subprocess.run(["git", "-C", str(clone), "checkout", "-b", "trunk"], check=True)
    provisioner = Provisioner(
        client,  # type: ignore[arg-type]
        clones_root=str(tmp_path / "tasks"),
        git=GitClones(run=run),  # type: ignore[arg-type]
    )
    task = _task(
        slug="different-slug",
        claimed_by="host-b",
        provisioned=False,
        migration={
            "destination_runner": "host-b",
            "workspace_disposition": "accepted",
            "workspace_method": "forge-first",
        },
    )
    with pytest.raises(MigrationConflict, match="branch"):
        provisioner.provision(task, runner_id="host-b")
    assert client.recorded == []


# 2119: REQ-034.2.3
def test_forge_first_materializes_the_recorded_remote_commit_not_cache_base(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    clone = tmp_path / "tasks" / "t1"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(
        ["git", "init", "--initial-branch=trunk", str(seed)], check=True, capture_output=True
    )
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "test@example.com"], check=True)
    (seed / "base.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(seed), "add", "base.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(seed), "commit", "-m", "base"], check=True, capture_output=True
    )
    subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "trunk"], check=True)
    clone.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "--branch", "trunk", str(remote), str(clone)], check=True)
    # The destination cache/clone predates the feature branch: only a forge fetch can discover it.
    assert (
        subprocess.run(
            ["git", "-C", str(clone), "branch", "--all"], check=True, capture_output=True, text=True
        ).stdout.find("safe-move")
        == -1
    )
    subprocess.run(["git", "-C", str(seed), "checkout", "-b", "panopticon/safe-move"], check=True)
    (seed / "portable.txt").write_text("portable commit\n")
    subprocess.run(["git", "-C", str(seed), "add", "portable.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(seed), "commit", "-m", "portable"], check=True, capture_output=True
    )
    expected = subprocess.run(
        ["git", "-C", str(seed), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "-C", str(seed), "push", "origin", "panopticon/safe-move"], check=True)

    client = _ProvisioningClient()
    provisioner = Provisioner(client, clones_root=str(tmp_path / "tasks"))  # type: ignore[arg-type]
    task = _task(
        slug="different-slug",
        claimed_by="host-b",
        provisioned=False,
        migration={
            "destination_runner": "host-b",
            "workspace_disposition": "accepted",
            "workspace_method": "forge-first",
        },
    )
    provisioner.provision(task, runner_id="host-b")
    actual = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert actual == expected
    assert (
        subprocess.run(
            ["git", "-C", str(clone), "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "panopticon/safe-move"
    )
    assert (clone / "portable.txt").read_text() == "portable commit\n"


# 2119: REQ-034.2.2
# 2119: REQ-034.2.4
# 2119: REQ-034.7.1
def test_canonical_verification_inspects_real_installed_git_head(tmp_path: Path) -> None:
    checkout = tmp_path / "tasks" / "t1"
    checkout.mkdir(parents=True)
    subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(checkout), "remote", "add", "origin", "https://forge/r1.git"],
        check=True,
    )
    subprocess.run(["git", "-C", str(checkout), "checkout", "-b", "trunk"], check=True)
    recorded: list[str] = []
    with pytest.raises(MigrationConflict, match="branch"):
        verify_canonical_workspace(
            checkout,
            expected_git_url="https://forge/r1.git",
            expected_branch="panopticon/safe-move",
        )
        recorded.append("provisioned")
    assert recorded == []
    subprocess.run(
        ["git", "-C", str(checkout), "checkout", "-b", "panopticon/safe-move"], check=True
    )
    assert verify_canonical_workspace(
        checkout,
        expected_git_url="https://forge/r1.git",
        expected_branch="panopticon/safe-move",
    )
    with pytest.raises(MigrationConflict, match="repository"):
        verify_canonical_workspace(
            checkout,
            expected_git_url="https://forge/different.git",
            expected_branch="panopticon/safe-move",
        )


# 2119: REQ-034.7.1
def test_persistence_or_reverification_failure_prevents_spawn(tmp_path: Path) -> None:
    class FailingControl(_RecordingControlPlane):
        def record_migration(self, migration: dict[str, object]) -> None:
            if migration["workspace_disposition"] == "accepted":
                raise OSError("persistence failed")
            super().record_migration(migration)

    source = _RecordingHost("host-a", tmp_path / "a-tasks", tmp_path / "a-config")
    destination = _RecordingHost("host-b", tmp_path / "b-tasks", tmp_path / "b-config")
    _source_checkout(source.tasks_root)
    request = MigrationRequest(
        task_id="t1",
        destination_runner="host-b",
        workspace="archive",
        session_history="omit",
    )
    with pytest.raises(OSError, match="persistence"):
        migrate_task(FailingControl(_task(claimed_by=None)), source, destination, request)
    assert destination.spawned == []

    class RejectingDestination(_RecordingHost):
        def verify_workspace(self, task_id: str, branch: str) -> None:
            raise MigrationConflict("canonical repository mismatch")

    other = RejectingDestination("host-b", tmp_path / "c-tasks", tmp_path / "c-config")
    with pytest.raises(MigrationConflict, match="canonical"):
        migrate_task(_RecordingControlPlane(_task(claimed_by=None)), source, other, request)
    assert other.spawned == []
