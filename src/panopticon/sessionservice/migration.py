"""Deterministic host-side primitives for explicit cross-host task migration."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class MigrationConflict(RuntimeError):
    """A migration cannot safely proceed from the supplied facts."""


@dataclass(frozen=True)
class MigrationRequest:
    task_id: str
    destination_runner: str
    workspace: str
    session_history: str
    discard_changes: tuple[str, ...] = ()


def config_volume_name(task_id: str) -> str:
    return f"panopticon-config-{task_id}"


def validate_reported_facts(facts: Mapping[str, object]) -> bool:
    try:
        json.dumps(facts)
    except (TypeError, ValueError) as exc:
        raise TypeError("runner-reported facts must be JSON serializable") from exc
    return True


def validate_migration_record(record: Mapping[str, object]) -> bool:
    for field in (
        "source_runner",
        "destination_runner",
        "workspace_disposition",
        "session_history_disposition",
    ):
        if field not in record:
            raise MigrationConflict(f"migration record missing {field}")
    validate_reported_facts(record)
    return True


def provisioning_ready(
    task: Mapping[str, object], *, runner_id: str, workspace_exists: bool
) -> bool:
    return bool(
        workspace_exists
        and task.get("claimed_by") == runner_id
        and task.get("provisioned_by") == runner_id
        and task.get("workspace_verified_by") == runner_id
        and task.get("branch")
        and task.get("clone")
    )


def migration_claim_allowed(task: Mapping[str, object], *, runner_id: str) -> bool:
    owner = task.get("provisioned_by")
    if owner in (None, runner_id):
        return True
    migration = task.get("migration")
    if not isinstance(migration, Mapping):
        return False
    return bool(
        migration.get("source_runner") == owner
        and migration.get("destination_runner") == runner_id
        and migration.get("workspace_disposition") == "accepted"
    )


def change_session_history_disposition(
    record: Mapping[str, object], *, disposition: str, actor: str
) -> dict[str, object]:
    if disposition == "omitted" and actor != "user":
        raise MigrationConflict("session history omission requires an explicit operator decision")
    changed = dict(record)
    changed["session_history_disposition"] = disposition
    changed["session_history_changed_by"] = actor
    return changed


def spawn_allowed(
    task: Mapping[str, object],
    *,
    runner_id: str,
    workspace_exists: bool,
    inspected_branch: str | None,
) -> bool:
    migration = task.get("migration")
    if not isinstance(migration, Mapping):
        return provisioning_ready(task, runner_id=runner_id, workspace_exists=workspace_exists)
    session = migration.get("session_history_disposition")
    session_ready = session == "accepted" or (
        session == "omitted"
        and (
            not migration.get("session_history_was_requested")
            or migration.get("session_history_changed_by") == "user"
        )
    )
    return bool(
        migration.get("destination_runner") == runner_id
        and migration.get("workspace_disposition") == "accepted"
        and task.get("claimed_by") == runner_id
        and task.get("provisioned_by") == runner_id
        and workspace_exists
        and inspected_branch == task.get("branch")
        and session_ready
    )


def inspect_forge_first(
    *,
    recorded_branch: str,
    checked_out_branch: str,
    dirty_paths: tuple[str, ...] | list[str],
    head: str,
    forge_reachable_commits: set[str],
) -> dict[str, str]:
    if checked_out_branch != recorded_branch:
        raise MigrationConflict("recorded branch is not checked out")
    if dirty_paths:
        raise MigrationConflict("uncommitted work is not forge-portable")
    if head not in forge_reachable_commits:
        raise MigrationConflict("unpushed commit is not forge-portable")
    return {"branch": recorded_branch, "commit": head}


def _archive_directory(root: Path) -> bytes:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz", dereference=False) as bundle:
        for entry in sorted(root.rglob("*")):
            bundle.add(entry, arcname=entry.relative_to(root), recursive=False)
    return data.getvalue()


def create_workspace_archive(source: Path) -> bytes:
    if not source.is_dir():
        raise MigrationConflict("workspace missing")
    return _archive_directory(source)


def create_config_archive(source: Path, *, credential_mount: Path | None = None) -> bytes:
    del credential_mount  # deliberately outside the archive root; never traversed
    if not source.is_dir():
        raise MigrationConflict("session configuration missing")
    return _archive_directory(source)


def _safe_extract(bundle: tarfile.TarFile, destination: Path) -> None:
    destination_resolved = destination.resolve()
    for member in bundle.getmembers():
        target = destination / member.name
        if not target.resolve(strict=False).is_relative_to(destination_resolved):
            raise MigrationConflict("archive path escapes staging directory")
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
        elif member.issym():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(member.linkname)
        elif member.isfile():
            target.parent.mkdir(parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            if source is None:
                raise MigrationConflict(f"archive member has no data: {member.name}")
            with target.open("wb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(member.mode & 0o777)
        else:
            raise MigrationConflict(f"unsupported archive member: {member.name}")


def stage_workspace_archive(archive: bytes, *, canonical: Path) -> Path:
    canonical.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=f".{canonical.name}.migration-", dir=canonical.parent))
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as bundle:
            _safe_extract(bundle, staged)
    except Exception:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    return staged


def verify_canonical_workspace(
    checkout: Path, *, expected_git_url: str, expected_branch: str
) -> bool:
    if not checkout.is_dir():
        raise MigrationConflict("canonical workspace missing")
    branch = subprocess.run(
        ["git", "-C", str(checkout), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if branch != expected_branch:
        raise MigrationConflict(f"branch mismatch: expected {expected_branch}, got {branch}")
    origin = subprocess.run(
        ["git", "-C", str(checkout), "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if origin != expected_git_url:
        raise MigrationConflict("repository identity mismatch")
    return True


def accept_workspace(
    task: Mapping[str, object],
    *,
    runner_id: str,
    staged: Path | None,
    canonical: Path,
    repository_id: str,
    inspected_branch: str | None = None,
) -> Path:
    if staged is None and not canonical.exists():
        raise MigrationConflict("workspace is missing")
    if repository_id != task.get("repo_id"):
        raise MigrationConflict("repository identity mismatch")
    if inspected_branch != task.get("branch"):
        raise MigrationConflict("branch mismatch")
    if canonical.exists() and staged is None:
        return canonical
    if staged is None or not staged.is_dir():
        raise MigrationConflict("workspace is missing")
    if canonical.exists():
        raise MigrationConflict("canonical workspace already exists")
    os.replace(staged, canonical)
    return canonical


def restore_config_archive(
    task_id: str, archive: bytes, *, restore: Callable[[str, bytes], None]
) -> None:
    restore(config_volume_name(task_id), archive)


def migrate_task(
    control: Any,
    source: Any,
    destination: Any,
    request: MigrationRequest,
    *,
    inspected_dirty_paths: list[str] | None = None,
    recorded_head: str = "",
    forge_reachable_commits: set[str] | None = None,
    destination_repository_id: str | None = None,
) -> dict[str, object]:
    task = control.read(request.task_id)
    source_runner = task.get("provisioned_by")
    if source_runner != source.runner_id or request.destination_runner != destination.runner_id:
        raise MigrationConflict("migration runner ownership mismatch")
    discarded: list[str] = []
    source_checkout = source.tasks_root / request.task_id
    canonical = destination.tasks_root / request.task_id
    canonical_installed = False
    if request.workspace == "archive":
        archive = create_workspace_archive(source_checkout)
        staged = stage_workspace_archive(archive, canonical=canonical)
    elif request.workspace == "forge-first":
        dirty = inspected_dirty_paths or []
        losses = [*dirty]
        if recorded_head not in (forge_reachable_commits or set()):
            losses.append(f"commit:{recorded_head}")
        if losses and set(request.discard_changes) != set(losses):
            raise MigrationConflict("discard authorization must identify every omitted change")
        discarded = list(request.discard_changes)
        canonical.parent.mkdir(parents=True, exist_ok=True)
        if canonical.exists():
            shutil.rmtree(canonical)
        destination.checkout_forge(request.task_id, str(task["git_url"]), str(task["branch"]))
        staged = None
    else:
        raise MigrationConflict("unknown workspace migration mode")

    try:
        if request.workspace == "archive":
            accept_workspace(
                task,
                runner_id=destination.runner_id,
                staged=staged,
                canonical=canonical,
                repository_id=destination_repository_id or str(task["repo_id"]),
                inspected_branch=str(task["branch"]),
            )
            canonical_installed = True
        session_disposition = "omitted"
        session_changed_by: str | None = "user"
        source_config = source.config_root / config_volume_name(request.task_id)
        destination_config = destination.config_root / config_volume_name(request.task_id)
        if request.session_history == "transfer":
            config_archive = create_config_archive(source_config)
            if destination_config.exists():
                shutil.rmtree(destination_config)
            destination_config.mkdir(parents=True)
            with tarfile.open(fileobj=io.BytesIO(config_archive), mode="r:*") as bundle:
                _safe_extract(bundle, destination_config)
            session_disposition = "accepted"
            session_changed_by = None
        elif request.session_history == "omit":
            shutil.rmtree(destination_config, ignore_errors=True)
        else:
            raise MigrationConflict("unknown session history disposition")
        migration: dict[str, object] = {
            "source_runner": source.runner_id,
            "destination_runner": destination.runner_id,
            "workspace_disposition": "accepted",
            "session_history_disposition": session_disposition,
            "discarded_changes": discarded,
        }
        if session_changed_by:
            migration["session_history_changed_by"] = session_changed_by
        if discarded:
            migration["discard_authorized_by"] = "user"
        control.record_migration(migration)
        control.record_provisioning(
            runner_id=destination.runner_id, branch=str(task["branch"]), clone=str(canonical)
        )
        control.move_claim(destination.runner_id)
        destination.verify_workspace(request.task_id, str(task["branch"]))
        destination.spawn(request.task_id)
        return dict(control.read(request.task_id))
    except Exception:
        if canonical_installed:
            shutil.rmtree(canonical, ignore_errors=True)
        failed = {
            "source_runner": source.runner_id,
            "destination_runner": destination.runner_id,
            "workspace_disposition": "failed",
            "session_history_disposition": "requested"
            if request.session_history == "transfer"
            else "omitted",
            "discarded_changes": discarded,
        }
        with suppress(Exception):
            control.record_migration(failed)
        raise
