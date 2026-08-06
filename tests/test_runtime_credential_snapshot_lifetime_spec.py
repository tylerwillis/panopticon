"""Executable contract for REQ-050 runtime credential snapshot lifetime."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from panopticon.core.models import LifecyclePhase
from panopticon.sessionservice import local_runner as runner_module
from panopticon.sessionservice.local_runner import LocalRunner
from panopticon.taskservice.auth import derive_task_capability

WRITE_TOKEN = "runtime-snapshot-writer-token"


def _credential(root: Path) -> Path:
    credential = root / "auth.json"
    credential.write_text(json.dumps({"read": [], "write": [WRITE_TOKEN]}))
    credential.chmod(0o600)
    return credential


def test_successful_spawn_retains_a_path_resolvable_task_snapshot(tmp_path: Path) -> None:
    # 2119: REQ-050.1.1
    credential = _credential(tmp_path)
    mounted_sources: list[Path] = []

    def record(args: list[str], **_kwargs: object) -> str:
        if args[:3] == ["docker", "run", "--detach"]:
            mount = next(
                value for value in args if ":/run/secrets/panopticon-service-auth:ro" in value
            )
            source = Path(mount.rsplit(":", 2)[0])
            assert source.is_file()
            mounted_sources.append(source)
        return ""

    runner = LocalRunner(
        "http://service", auth_file=credential.name, secrets_dir=tmp_path, run=record
    )
    runner._snapshot_dir = tmp_path

    runner.spawn("task")

    assert len(mounted_sources) == 1
    snapshot = mounted_sources[0]
    assert snapshot.is_file()
    assert json.loads(snapshot.read_text()) == {"task": derive_task_capability(WRITE_TOKEN, "task")}
    runner.stop("panopticon-task")
    assert not snapshot.exists()


def test_later_runner_instances_remove_retained_task_snapshots(tmp_path: Path) -> None:
    # 2119: REQ-050.2.1
    snapshots = [
        tmp_path / "panopticon-service-auth-task-first.json",
        tmp_path / "panopticon-service-auth-task-second.json",
    ]
    for snapshot in snapshots:
        snapshot.write_text("task capability")
    unrelated = tmp_path / "panopticon-service-auth-other-stranded.json"
    unrelated.write_text("other task capability")

    stop_runner = LocalRunner("http://service", run=lambda *_args, **_kwargs: "")
    stop_runner._snapshot_dir = tmp_path
    stop_runner.stop("panopticon-task")

    assert all(not snapshot.exists() for snapshot in snapshots)
    assert unrelated.is_file()

    cleanup_snapshot = tmp_path / "panopticon-service-auth-task-terminal.json"
    cleanup_snapshot.write_text("task capability")
    cleanup_runner = LocalRunner("http://service", run=lambda *_args, **_kwargs: "")
    cleanup_runner._snapshot_dir = tmp_path
    cleanup_runner.cleanup_runtime_credentials("task")

    assert not cleanup_snapshot.exists()
    assert unrelated.is_file()


def test_replacement_removes_old_snapshots_before_creating_the_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-050.2.1
    credential = _credential(tmp_path)
    stale = [
        tmp_path / "panopticon-service-auth-task-first.json",
        tmp_path / "panopticon-service-auth-task-second.json",
    ]
    for snapshot in stale:
        snapshot.write_text("old task capability")
    original_snapshot = runner_module.snapshot_task_capability
    created: list[Path] = []

    def snapshot_after_cleanup(*args: object, **kwargs: object) -> Path:
        assert all(not snapshot.exists() for snapshot in stale)
        snapshot = original_snapshot(*args, **kwargs)  # type: ignore[arg-type]
        created.append(snapshot)
        return snapshot

    monkeypatch.setattr(runner_module, "snapshot_task_capability", snapshot_after_cleanup)
    runner = LocalRunner(
        "http://service", auth_file=credential.name, secrets_dir=tmp_path, run=lambda *_a, **_k: ""
    )
    runner._snapshot_dir = tmp_path

    runner.spawn("task")

    assert created and created[0].is_file()


def test_pre_docker_failure_removes_its_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-050.3.1
    credential = _credential(tmp_path)
    calls: list[list[str]] = []
    created: list[Path] = []
    original_snapshot = runner_module.snapshot_task_capability

    def observe_snapshot(*args: object, **kwargs: object) -> Path:
        snapshot = original_snapshot(*args, **kwargs)  # type: ignore[arg-type]
        created.append(snapshot)
        return snapshot

    monkeypatch.setattr(runner_module, "snapshot_task_capability", observe_snapshot)
    runner = LocalRunner(
        "not-a-url",
        auth_file=credential.name,
        secrets_dir=tmp_path,
        run=lambda args, **_kwargs: calls.append(args) or "",
    )
    runner._snapshot_dir = tmp_path

    with pytest.raises(ValueError, match="task-service URL has no host"):
        runner.spawn("task")

    assert len(created) == 1
    assert not created[0].exists()
    assert list(tmp_path.glob("panopticon-service-auth-task-*.json")) == []
    assert not any(call[:3] == ["docker", "run", "--detach"] for call in calls)


@pytest.mark.parametrize("failure_stage", ["docker", "tmux", "progress"])
def test_failed_spawn_removes_its_snapshot(
    tmp_path: Path, failure_stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-050.3.1
    credential = _credential(tmp_path)
    calls: list[list[str]] = []
    created: list[Path] = []
    original_snapshot = runner_module.snapshot_task_capability

    def observe_snapshot(*args: object, **kwargs: object) -> Path:
        snapshot = original_snapshot(*args, **kwargs)  # type: ignore[arg-type]
        created.append(snapshot)
        return snapshot

    monkeypatch.setattr(runner_module, "snapshot_task_capability", observe_snapshot)

    def assert_snapshot_was_created() -> None:
        assert len(created) == 1

    def fail_at_stage(args: list[str], **_kwargs: object) -> str:
        calls.append(args)
        if failure_stage == "docker" and args[:3] == ["docker", "run", "--detach"]:
            assert_snapshot_was_created()
            assert created[0].is_file()
            raise RuntimeError("docker failure")
        if failure_stage == "tmux" and "new-session" in args:
            assert_snapshot_was_created()
            raise RuntimeError("tmux failure")
        return ""

    def progress(phase: LifecyclePhase) -> None:
        if failure_stage == "progress" and phase == LifecyclePhase.AWAITING:
            assert_snapshot_was_created()
            raise RuntimeError("progress failure")

    runner = LocalRunner(
        "http://service", auth_file=credential.name, secrets_dir=tmp_path, run=fail_at_stage
    )
    runner._snapshot_dir = tmp_path

    with pytest.raises(RuntimeError, match=failure_stage):
        runner.spawn("task", progress=progress)

    assert len(created) == 1
    assert not created[0].exists()
    assert list(tmp_path.glob("panopticon-service-auth-task-*.json")) == []
    if failure_stage == "docker":
        assert not any("new-session" in call for call in calls)
    else:
        assert calls.count(["docker", "rm", "--force", "panopticon-task"]) == 2
