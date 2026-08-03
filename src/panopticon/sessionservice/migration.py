"""Deterministic host-side primitives for explicit cross-host task migration."""

from __future__ import annotations

import argparse
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

import httpx

from panopticon.client import TaskServiceClient
from panopticon.core.dirs import TASKS_DIR
from panopticon.sessionservice.local_runner import DEFAULT_IMAGE


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


def export_config_volume(task_id: str, destination: Path, *, image: str = DEFAULT_IMAGE) -> None:
    """Export the real Docker named volume without dereferencing credential symlinks."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--volume",
            f"{config_volume_name(task_id)}:/source:ro",
            "--volume",
            f"{destination.parent.resolve()}:/export",
            image,
            "tar",
            "--create",
            "--gzip",
            "--file",
            f"/export/{destination.name}",
            "--directory",
            "/source",
            ".",
        ],
        check=True,
    )


def restore_config_volume(task_id: str, archive: Path, *, image: str = DEFAULT_IMAGE) -> None:
    """Validate an archive locally, then replace the standard Docker volume contents."""
    with tempfile.TemporaryDirectory(prefix=f"panopticon-config-{task_id}-") as temp:
        staged = Path(temp)
        with tarfile.open(archive, mode="r:*") as bundle:
            _safe_extract(bundle, staged)
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--volume",
                f"{config_volume_name(task_id)}:/target",
                "--volume",
                f"{staged.resolve()}:/staged:ro",
                image,
                "sh",
                "-c",
                "find /target -mindepth 1 -delete && cp -a /staged/. /target/",
            ],
            check=True,
        )


def remove_config_volume(task_id: str) -> None:
    """Ensure an omitted session starts fresh instead of discovering stale host-local history."""
    subprocess.run(["docker", "volume", "rm", "--force", config_volume_name(task_id)], check=True)


def request_migration(
    client: TaskServiceClient,
    task_id: str,
    *,
    destination_runner: str,
    workspace_method: str,
    transfer_session: bool,
    tasks_root: Path = Path(TASKS_DIR),
    discard_changes: tuple[str, ...] = (),
) -> Mapping[str, object]:
    task = client.get_task(task_id)
    source = task.get("provisioned_by")
    if not isinstance(source, str):
        raise MigrationConflict("task has no recorded workspace owner")
    discarded: list[str] = []
    if workspace_method == "forge-first":
        checkout = tasks_root / task_id
        dirty = subprocess.run(
            ["git", "-C", str(checkout), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        dirty_paths = [line[3:] for line in dirty if len(line) > 3]
        unpushed = subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "rev-list",
                str(task["branch"]),
                "--not",
                f"origin/{task['branch']}",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        losses = [*dirty_paths, *(f"commit:{commit}" for commit in unpushed)]
        if set(discard_changes) != set(losses):
            raise MigrationConflict("forge-first requires explicit discard of every local change")
        discarded = losses
    return client.record_migration(
        task_id,
        source_runner=source,
        destination_runner=destination_runner,
        workspace_disposition="pending",
        workspace_method=workspace_method,
        session_history_disposition="requested" if transfer_session else "omitted",
        discarded_changes=discarded,
        discard_authorized_by="user" if discarded else None,
    )


def accept_migration(
    client: TaskServiceClient,
    task_id: str,
    *,
    runner_id: str,
    tasks_root: Path,
    workspace_archive: Path | None,
    session_archive: Path | None,
    image: str = DEFAULT_IMAGE,
) -> Mapping[str, object]:
    """Validate/install transferred state on the destination, then publish acceptance facts."""
    task = client.get_task(task_id)
    migration = task.get("migration")
    if not isinstance(migration, Mapping) or migration.get("workspace_disposition") not in {
        "pending",
        "installed",
        "accepted",
    }:
        raise MigrationConflict("task has no pending or accepted migration")
    if migration.get("destination_runner") != runner_id:
        raise MigrationConflict("pending migration names a different destination")
    repo = client.get_repo(str(task["repo_id"]))
    canonical = tasks_root / task_id
    method = migration.get("workspace_method", "archive")
    already_accepted = migration.get("workspace_disposition") == "accepted"
    already_installed = migration.get("workspace_disposition") == "installed"
    canonical_preexisting = canonical.exists()
    if already_accepted or already_installed:
        staged = canonical
    elif canonical_preexisting:
        raise MigrationConflict("canonical destination workspace already exists")
    elif method == "archive":
        if workspace_archive is None:
            raise MigrationConflict("archive migration requires --workspace-archive")
        staged = stage_workspace_archive(workspace_archive.read_bytes(), canonical=canonical)
    elif method == "forge-first":
        staged = Path(tempfile.mkdtemp(prefix=f".{task_id}.migration-", dir=canonical.parent))
        subprocess.run(
            [
                "git",
                "clone",
                "--branch",
                str(task["branch"]),
                str(repo["git_url"]),
                str(staged),
            ],
            check=True,
        )
    else:
        raise MigrationConflict("unknown workspace migration method")
    try:
        verify_canonical_workspace(
            staged,
            expected_git_url=str(repo["git_url"]),
            expected_branch=str(task["branch"]),
        )
        if not already_accepted and not canonical_preexisting:
            accept_workspace(
                task,
                runner_id=runner_id,
                staged=staged,
                canonical=canonical,
                repository_id=str(task["repo_id"]),
                inspected_branch=str(task["branch"]),
            )
        session = str(migration.get("session_history_disposition"))
        if session == "requested":
            if session_archive is None:
                raise MigrationConflict("requested session transfer requires --session-archive")
            restore_config_volume(task_id, session_archive, image=image)
            session = "accepted"
        elif session == "omitted":
            remove_config_volume(task_id)
        installed = (
            task
            if already_accepted or already_installed
            else client.record_migration(
                task_id,
                source_runner=str(migration["source_runner"]),
                destination_runner=runner_id,
                workspace_disposition="installed",
                workspace_method=str(method),
                session_history_disposition=session,
                discarded_changes=list(migration.get("discarded_changes", [])),
                discard_authorized_by=migration.get("discard_authorized_by"),
            )
        )
        if task.get("claimed_by") not in (None, runner_id):
            raise MigrationConflict("source claim must be released after its container is stopped")
        if task.get("claimed_by") != runner_id:
            client.claim(task_id, runner_id)
        client.record_provisioning(task_id, str(task["branch"]), str(canonical), runner_id, True)
        if already_accepted:
            return installed
        return client.record_migration(
            task_id,
            source_runner=str(migration["source_runner"]),
            destination_runner=runner_id,
            workspace_disposition="accepted",
            workspace_method=str(method),
            session_history_disposition=session,
            discarded_changes=list(migration.get("discarded_changes", [])),
            discard_authorized_by=migration.get("discard_authorized_by"),
        )
    except Exception:
        if (
            not already_accepted
            and not already_installed
            and not canonical_preexisting
            and staged.exists()
        ):
            shutil.rmtree(staged, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> None:
    """Operator CLI: request on either host, export on source, accept on destination."""
    parser = argparse.ArgumentParser(prog="python -m panopticon.sessionservice.migration")
    parser.add_argument(
        "--service-url",
        default=os.environ.get("PANOPTICON_SERVICE_URL", "http://localhost:8000"),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    request = sub.add_parser("request")
    request.add_argument("task_id")
    request.add_argument("--destination-runner", required=True)
    request.add_argument(
        "--workspace-method", choices=("archive", "forge-first"), default="archive"
    )
    request.add_argument("--transfer-session", action="store_true")
    request.add_argument("--tasks-root", default=TASKS_DIR, type=Path)
    request.add_argument("--discard-change", action="append", default=[])
    export = sub.add_parser("export")
    export.add_argument("task_id")
    export.add_argument("--tasks-root", default=TASKS_DIR)
    export.add_argument("--workspace-archive", required=True, type=Path)
    export.add_argument("--session-archive", type=Path)
    accept = sub.add_parser("accept")
    accept.add_argument("task_id")
    accept.add_argument("--runner-id", required=True)
    accept.add_argument("--tasks-root", default=TASKS_DIR, type=Path)
    accept.add_argument("--workspace-archive", type=Path)
    accept.add_argument("--session-archive", type=Path)
    accept.add_argument("--image", default=DEFAULT_IMAGE)
    args = parser.parse_args(argv)
    client = TaskServiceClient(httpx.Client(base_url=args.service_url, trust_env=False))
    if args.command == "request":
        request_migration(
            client,
            args.task_id,
            destination_runner=args.destination_runner,
            workspace_method=args.workspace_method,
            transfer_session=args.transfer_session,
            tasks_root=args.tasks_root,
            discard_changes=tuple(args.discard_change),
        )
    elif args.command == "export":
        args.workspace_archive.write_bytes(
            create_workspace_archive(Path(args.tasks_root) / args.task_id)
        )
        if args.session_archive is not None:
            export_config_volume(args.task_id, args.session_archive)
    else:
        accept_migration(
            client,
            args.task_id,
            runner_id=args.runner_id,
            tasks_root=args.tasks_root,
            workspace_archive=args.workspace_archive,
            session_archive=args.session_archive,
            image=args.image,
        )


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
            assert staged is not None
            verify_canonical_workspace(
                staged,
                expected_git_url=str(task["git_url"]),
                expected_branch=str(task["branch"]),
            )
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


if __name__ == "__main__":  # pragma: no cover - operator CLI wiring
    main()
