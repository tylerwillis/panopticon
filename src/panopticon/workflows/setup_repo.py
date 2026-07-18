"""The SetupRepo workflow — a **shell** workflow (no container) for harness-aware repo auth.

The first example of ``runner_type = "shell"`` (ADR 0012 retired ``panopticon login``; container
auth is supplied from a repo's env-file or shared credential directory).
Rather than spawn a task container + agent, the session service runs :meth:`shell_script` directly
in a host tmux session: the operator attaches (``t`` in the dashboard), the script checks whether a
credential is already configured and guides them (collect one, or drop the task to add their own),
and on a final Enter it marks the task ``COMPLETE`` over REST and returns them to the dashboard. No
image, no per-task clone, no LLM — just a host shell doing one operator chore.

``RUNNING → {COMPLETE, DROPPED}``. The single state carries no responsibilities (a shell task has
no agent to gate); the script itself drives the advance to COMPLETE when the operator finishes (or
they drop the task instead to keep an existing credential).
"""

from __future__ import annotations

import importlib.resources
from typing import ClassVar

from panopticon.core.state import Complete, InitialState
from panopticon.core.workflow import Workflow
from panopticon.harnesses.pi import API_KEY_ENV_VARS

#: The shell script the workflow runs, kept in a sibling ``setup_repo.sh`` so it's edited (and
#: shell-linted) as a real script rather than a Python string. Its sourceable helpers live in
#: ``setup_repo_lib.sh`` (prepended, so they're defined before the interactive flow calls them —
#: and unit-testable in isolation). Read once at import.
_LIB = (importlib.resources.files("panopticon.workflows") / "setup_repo_lib.sh").read_text()
_SCRIPT = (importlib.resources.files("panopticon.workflows") / "setup_repo.sh").read_text()


class SetupRepo(Workflow):
    """A no-container utility workflow for the repo's selected harness auth flow.

    ``runner_type = "shell"`` routes it to the session service's shell runner instead of the
    Docker one. It's opt-out (``opt_in = False``) so it's enabled for every repo by default, and
    ``hidden`` keeps this operator utility out of both dashboard menus (the repo-form workflow
    list and the task-creation picker) — it's launched instead from the repos modal's setup
    hotkey, which creates a ``setup-repo`` task for the highlighted repo.
    """

    name: ClassVar[str] = "setup-repo"
    runner_type: ClassVar[str] = "shell"
    opt_in: ClassVar[bool] = False
    hidden: ClassVar[bool] = True
    when_to_use: ClassVar[str] = (
        "Run a repo's harness-aware auth setup in a host shell (no container); attach to complete "
        "the selected CLI's interactive flow and store its repo credentials privately."
    )

    class Running(InitialState):
        label = "RUNNING"
        description = "Configure the repo's selected harness auth in a host shell; the script completes the task when the operator finishes."
        transitions = (Complete,)  # advance → COMPLETE; + DROPPED inherited

    initial = Running

    def shell_script(self) -> str:
        """Guide the operator through harness auth, then complete the task on a final Enter.

        The script lives in the sibling ``setup_repo.sh``. The session service injects
        ``PANOPTICON_SERVICE_URL``/``PANOPTICON_TASK_ID`` (and sources the repo's secrets), so the
        script checks for an existing credential, optionally collects a new one, and — whatever route
        the operator takes — ends with a summary and a prompt to press Enter, which advances the task
        to COMPLETE over REST and returns them to the dashboard."""
        pi_vars = " ".join(API_KEY_ENV_VARS)
        return f"PANOPTICON_PI_API_KEY_ENV_VARS='{pi_vars}'\n{_LIB}\n{_SCRIPT}"
