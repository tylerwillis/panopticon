"""Contract coverage for the runner-qualified provisioned state in issue #249."""

from __future__ import annotations

from panopticon.core import Task
from panopticon.core.models import Actor, MigrationRecord


def _ready_task() -> Task:
    return Task(
        id="t1",
        repo_id="r1",
        workflow="spike",
        state="ITERATING",
        turn=Actor.AGENT,
        branch="panopticon/fix-widget",
        clone="/tasks/t1",
        claimed_by="host-a",
        provisioned_by="host-a",
        workspace_verified_by="host-a",
    )


def test_provisioned_requires_every_runner_qualified_workspace_condition() -> None:
    task = _ready_task()

    # 2119-spec: skip-unclaimed-provisioning
    # 2119: 2.1
    assert task.provisioned is True  # an absent migration is ready

    task.migration = MigrationRecord("host-old", "host-a", "accepted", "discarded")
    assert task.provisioned is True

    for field in ("branch", "clone", "claimed_by", "provisioned_by", "workspace_verified_by"):
        original = getattr(task, field)
        setattr(task, field, None)
        assert task.provisioned is False, field
        setattr(task, field, original)

    task.provisioned_by = "host-b"
    assert task.provisioned is False
    task.provisioned_by = "host-a"
    task.workspace_verified_by = "host-b"
    assert task.provisioned is False
    task.workspace_verified_by = "host-a"
    task.migration = MigrationRecord("host-old", "host-a", "installed", "accepted")
    assert task.provisioned is False


def test_provisioned_docstring_states_every_truth_condition() -> None:
    # 2119: 2.2
    assert Task.provisioned.__doc__ == (
        "True only when branch and clone are recorded, migration is absent or its workspace "
        "disposition is accepted, and a current claim is present and matches both the "
        "provisioning and workspace-verification runners."
    )
