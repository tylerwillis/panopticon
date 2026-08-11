"""Building the command to hand the terminal to a task's tmux session.

The terminal controller is a *supervisor* that owns the TTY (ADR 0009): it shows the dashboard,
and on `t` it leaves the dashboard and **attaches** the terminal to the chosen task's tmux
session, rejoining the dashboard when the operator detaches. Switching is therefore always
detach→attach — never `switch-client` — which is what lets a future remote task be reached the
same way, by prefixing the attach with ``ssh -t <host>`` (ADR 0009 §6). Sessions live on the
runner's dedicated `panopticon` tmux socket.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from typing import Any

CONTEXT_LABEL_LIMIT = 100
TASK_STATUS_RETURN_HINT = "Control+B and then D to get back to the dashboard"
TASK_STATUS_FORMAT = "#[align=left]#{T:status-left}#[align=right]#{T:status-right}"


def _one_line(value: object) -> str:
    """Trim and fold arbitrary Unicode whitespace for a compact status-line value."""
    return " ".join(str(value).split())


def task_context_label(task: Mapping[str, Any], session: str) -> str:
    """The current human context for a task's tmux footer, falling back to ``session``."""
    slug = _one_line(task.get("slug") or "")
    memo = _one_line(task.get("memo") or "")
    if slug and memo:
        label = f"{slug} [{memo}]"
    elif slug:
        label = slug
    elif memo:
        label = f"[{memo}]"
    else:
        label = session
    if len(label) > CONTEXT_LABEL_LIMIT:
        return f"{label[: CONTEXT_LABEL_LIMIT - 1]}…"
    return label


def _literal_tmux_format(value: str) -> str:
    """Escape tmux's format and strftime introducers so task text displays literally."""
    return value.replace("#", "##").replace("%", "%%")


def attach_command(
    session: str, *, socket: str, host: str | None = None, label: str | None = None
) -> list[str]:
    """The argv that attaches the current terminal to ``session`` on the panopticon socket.

    When ``label`` is supplied, the target session receives the task-focused status layout before
    attachment: literal context on the left, no central window list, and return guidance on the
    right. ``host`` wraps all operations in ``ssh -t <host> …`` so the same supervisor loop reaches
    a session on another machine.
    """
    tmux = ["tmux", "-L", socket]
    if label is not None:
        tmux += [
            "set-option",
            "-t",
            session,
            "status-left",
            _literal_tmux_format(label),
            ";",
            "set-option",
            "-t",
            session,
            "status-left-length",
            str(CONTEXT_LABEL_LIMIT),
            ";",
            "set-option",
            "-t",
            session,
            "status-right",
            TASK_STATUS_RETURN_HINT,
            ";",
            "set-option",
            "-t",
            session,
            "status-right-length",
            str(len(TASK_STATUS_RETURN_HINT)),
            ";",
            "set-option",
            "-t",
            session,
            "status-format[0]",
            TASK_STATUS_FORMAT,
            ";",
        ]
    tmux += ["attach", "-t", session]
    # ssh concatenates argv into a command interpreted by the remote shell. Pass one safely quoted
    # command string so spaces, quotes, and the tmux command separator in user context stay data.
    return ["ssh", "-t", host, shlex.join(tmux)] if host else tmux
