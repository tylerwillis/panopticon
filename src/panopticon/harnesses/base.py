"""The ``Harness`` interface — the agent CLI a task container runs, as a pluggable adapter.

A harness *describes and renders* one agent CLI's surface (ADR 0004's Skill specs, the
turn-flip hook wiring, MCP client config, system-prompt injection, launch argv) — it never
runs the CLI itself. The only place a harness's :meth:`Harness.argv` is *executed* is the
in-container agent launcher (:mod:`panopticon.container.agent`), which keeps the determinism
invariant's shape: this package writes files and computes argv (deterministic, unit-tested);
``container/`` remains the only LLM-bearing path.

Like the execution-backend ``Runner`` (and unlike ``Store``/``Workflow``), the interface lives
in its owning package rather than ``core``: harnesses aren't a control-plane dependency — the
task service only records a task's harness *name* (an opaque fact); the container and the
session service look the name up here when they need the CLI's mechanics.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from panopticon.core.models import Skill

#: Sent to the agent as the first message when a container restarts mid-task on the agent's
#: turn, so it picks up where it left off rather than waiting for user input.
INTERRUPT_PROMPT = "You were interrupted. Continue."

#: The in-container path of the mounted per-repo credential dir (the repo's ``credential_dir``
#: secrets reference, ADR 0007's directory-shaped sibling of ``env_file``). Shared read-write
#: across the repo's task containers — it holds credentials whose nature is shared (e.g. one
#: ChatGPT account = one rotating token chain, converged on by every session). The runner
#: exports the path as ``PANOPTICON_CREDENTIALS`` when the mount is present.
CREDENTIALS_MOUNT = "/panopticon/credentials"

#: Command both CLI hook formats invoke to update the task's turn through the control plane.
HOOK_COMMAND = "python -m panopticon.container.hook"


def task_id_note(task_id: str) -> str:
    """Guidance shared by every rendered skill for calling the task-scoped MCP tools."""
    return (
        f'\nThis is task `{task_id}` — pass `task_id="{task_id}"` to every panopticon MCP tool '
        f"you call here.\n"
    )


def operation_skill(name: str, target_state: str, task_id: str) -> Skill:
    """Represent a declared workflow operation on the same CLI-agnostic surface as a skill."""
    return Skill(
        name=name,
        description=f"Apply the workflow's '{name}' operation.",
        instructions=(
            f"Apply this workflow's `{name}` operation — it moves the task to **{target_state}**. "
            f'Invoke it with the `apply_operation` tool (`operation="{name}"`, '
            f"`task_id=\"{task_id}\"`); don't edit the state directly. It's gated on the current "
            "state's responsibilities and starts a new turn."
        ),
    )


@dataclass(frozen=True)
class BootstrapContext:
    """Everything a harness needs to render its CLI surface — plain data, no client.

    The agent launcher fetches the active workflow's skills/operations/overview from the task
    service and passes them in, so a harness's :meth:`Harness.bootstrap` stays a pure function
    of its inputs (deterministic, testable with no service).
    """

    home: Path  # the in-container user's home (config dirs hang off it)
    cwd: Path  # the task workspace (the per-task clone)
    service_url: str  # the task service as seen from the container
    task_id: str
    skills: Sequence[Skill] = ()
    operations: Mapping[str, str] = field(default_factory=dict)  # verb → target state
    overview: str = ""  # the whole-workflow map (→ the agent's system prompt)
    environ: Mapping[str, str] = field(default_factory=dict)  # the container's env (auth vars)

    def workflow_skills(self) -> Iterator[Skill]:
        """Skills followed by operations, ready for a harness's native skill renderer."""
        yield from self.skills
        for name, target_state in self.operations.items():
            yield operation_skill(name, target_state, self.task_id)


@dataclass(frozen=True)
class LaunchContext:
    """What the launch argv depends on: first-run inputs and the resume signal."""

    home: Path
    cwd: Path
    initial_prompt: str | None = None  # first message on a first run (unset on resume)
    turn: str | None = None  # "agent" → auto-resume with INTERRUPT_PROMPT on respawn
    starting_model: str | None = None  # model for the first run; resumes keep the session's


class Harness(ABC):
    """One agent CLI's mechanics. Subclasses are stateless; the registry holds one instance each.

    ``name`` is what :attr:`~panopticon.core.models.Task.harness` records — the control plane
    treats it as an opaque string; everything CLI-specific stays behind this interface.
    ``config_dirname`` names the per-task config-volume mountpoint under the container home
    (e.g. ``.claude``), where the CLI keeps the session state that must survive respawn.
    """

    name: ClassVar[str]
    config_dirname: ClassVar[str]
    field_label: ClassVar[str] = "model"

    def suggested_models(self) -> Sequence[tuple[str, str]]:
        """Static ``(value, label)`` suggestions for an advisory dashboard picker."""
        return ()

    def suggested_efforts(self, model: str | None = None) -> Sequence[tuple[str, str]]:
        """Static effort suggestions for ``model``; unknown and free-text values stay valid."""
        return ()

    def config_dir(self, home: Path) -> Path:
        """The CLI's config dir under ``home`` — the per-task volume's in-container path."""
        return home / self.config_dirname

    def image_layer(self) -> str:
        """Dockerfile fragment installing this CLI, composed as the **harness tier** of the task
        image (base → harness → workflow → repo, ADR 0005). Empty when the base image already
        carries the CLI (claude, today)."""
        return ""

    @abstractmethod
    def missing_auth(self, environ: Mapping[str, str], *, home: Path) -> str | None:
        """``None`` when the container can authenticate; else the operator-facing message the
        spawn failure carries (naming the exact variable/file to set for *this* harness)."""

    @abstractmethod
    def bootstrap(self, ctx: BootstrapContext) -> None:
        """Render the CLI's surface: skills/operations, turn-flip hook wiring, MCP client
        config, the workflow overview, and any first-run acceptance seeds. Pure file writes,
        idempotent — it runs on every container start (including respawns)."""

    @abstractmethod
    def argv(self, ctx: LaunchContext) -> list[str]:
        """The CLI's launch argv — resuming the prior session when the config volume holds one,
        else a first run carrying ``starting_model``/``initial_prompt``."""

    def env(self, ctx: LaunchContext) -> dict[str, str]:
        """Extra environment for the launch (e.g. pointing the CLI at its config dir)."""
        return {}
