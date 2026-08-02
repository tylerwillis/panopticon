"""Host-side task provisioning (ADR 0011): branch the per-task clone, record it back.

The session service runs **where the container runs**, so it owns the host git. Each task works in
a writable per-task ``git clone --local`` created at spawn (a self-contained checkout under
``<clones_root>/<task_id>``, mounted at ``/workspace``). When the agent acquires a slug, this
**branches whatever's there** — ``git checkout -b panopticon/<slug>`` — and records ``(branch,
clone path)`` on the task service (`PUT /tasks/{id}/provisioning`). (``origin`` is already the forge:
spawn-prep points it there so the agent has a correct remote before it ever has a slug.) The task
service does no filesystem work, so
the split stays correct when the runner is remote (ADR 0009). LLM-free: pure git + REST.

Provisioning is **observed, not pushed** (ADR 0010): the session service spots the slug over its
work-pull loop (`ProvisionDaemon`) and calls :meth:`Provisioner.provision`. The call is
**idempotent** — it no-ops a task with no slug yet or one already branched — so the loop can call it
on every task it sees. There is no worktree, no symlink, and no repoint: the agent keeps working in
the same ``/workspace``, now on its feature branch (ADR 0011 §2/§3).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.git import GitClones, branch_name
from panopticon.sessionservice.executions import WorkflowExecutions


class Provisioner:
    """Branches each task's per-task clone once it has a slug, and records it on the task service.

    ``clones_root`` holds the per-task clones (``<clones_root>/<task_id>``, created at spawn-prep and
    mounted at ``/workspace``). ``git`` is injectable so the emitted commands are unit-testable
    without a real repo. ``executions`` is the shared "how is this workflow run" cache (see the
    shell-skip in :meth:`provision`); shared with the spawner so both agree which tasks are shell.
    """

    def __init__(
        self,
        client: TaskServiceClient,
        *,
        clones_root: str,
        git: GitClones | None = None,
        executions: WorkflowExecutions | None = None,
        workspace_exists: Callable[[str], bool] = os.path.isdir,
        inspect_repository: Callable[[str], tuple[str, str]] | None = None,
    ) -> None:
        self._client = client
        self._clones_root = clones_root.rstrip("/")
        self._git = git or GitClones()
        self._executions = executions or WorkflowExecutions(client)
        self._workspace_exists = workspace_exists
        self._inspect_repository = inspect_repository

    def provision(self, task: JsonObj, *, runner_id: str | None = None) -> str | None:
        """Provision ``task`` if it is ready, returning the created branch (else ``None``).

        Ready means it has a slug but isn't provisioned yet; otherwise this no-ops (idempotent, so
        the pull loop can call it on every task). Branches the per-task clone off its current HEAD,
        then records the branch + clone path on the task service. (``origin`` was pointed at the
        forge at spawn-prep, so there's nothing to repoint here.)

        A **shell** workflow's task is skipped: it runs on the host with no per-task clone, so there
        is nothing to branch — the guarantee that ``runner_type = "shell"`` means *no clone* holds
        even if such a task somehow acquires a slug.
        """
        if not task.get("slug"):
            return None
        if self._executions.is_shell(task.get("workflow")):
            return None
        clone = f"{self._clones_root}/{task['id']}"
        recorded = task.get("branch")
        foreign = runner_id is not None and task.get("provisioned_by") not in (None, runner_id)
        migration = task.get("migration")
        forge_first = bool(
            foreign
            and isinstance(migration, dict)
            and migration.get("destination_runner") == runner_id
            and migration.get("workspace_disposition") == "accepted"
            and migration.get("workspace_method", "forge-first") == "forge-first"
        )
        if task.get("provisioned") and not foreign:
            return None
        if foreign and not forge_first:
            return None
        branch = str(recorded or branch_name(task["slug"]))
        if forge_first:
            if not self._workspace_exists(clone):
                return None
            self._git.checkout_remote_branch(repo_path=clone, branch=branch)
            actual_branch = subprocess.run(
                ["git", "-C", clone, "branch", "--show-current"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        elif recorded:
            # Adopt/re-verify an existing branch (including legacy upgraded rows) without trying
            # to create it again. A mismatch is rejected below.
            actual_branch = subprocess.run(
                ["git", "-C", clone, "branch", "--show-current"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        else:
            self._git.create_branch(repo_path=clone, branch=branch)
        if not recorded and not forge_first:
            actual_branch = branch
        if actual_branch != branch:
            from panopticon.sessionservice.migration import MigrationConflict

            raise MigrationConflict("branch mismatch")
        effective_runner = runner_id or (
            str(task["claimed_by"]) if task.get("claimed_by") is not None else ""
        )
        if not effective_runner:
            return None
        self._client.record_provisioning(task["id"], branch, clone, effective_runner, True)
        return branch
