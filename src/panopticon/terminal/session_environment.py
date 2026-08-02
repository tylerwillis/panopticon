"""Current-process environment pinned into newly created integrated tmux sessions."""

from __future__ import annotations

import os
import shlex
from collections.abc import Sequence

SESSION_ENVIRONMENT = (
    "PANOPTICON_SERVICE_AUTH_FILE",
    "PANOPTICON_SERVICE_AUTH_MODE",
    "PANOPTICON_CONFIG",
)


def session_environment_argv(command: Sequence[str]) -> list[str]:
    """Run ``command`` with current auth settings and stale tmux-server values cleared."""
    arguments = ["env"]
    for name in SESSION_ENVIRONMENT:
        arguments.extend(["-u", name])
    arguments.extend(
        f"{name}={os.environ[name]}" for name in SESSION_ENVIRONMENT if name in os.environ
    )
    return [*arguments, *command]


def session_environment_command(command: str) -> str:
    """Shell form of :func:`session_environment_argv` for tmux pane commands."""
    prefix = " ".join(shlex.quote(argument) for argument in session_environment_argv([]))
    return f"{prefix} {command}"
