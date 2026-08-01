"""Docker daemon reachability (REQ-031): the fail-loud preflight for `panopticon start`/`host`
and the per-host session-service daemon, plus the per-tick spawn-loop guard that distinguishes an
unreachable daemon (environmental, retried automatically once it returns) from a task-specific
crash — see `panopticon.sessionservice.spawner.Spawner.spawn_one`/`heal`.

Behind an injectable command-runner (the same pattern as `core.git`/`local_runner`'s
`CommandRunner`), so it's unit-testable without a real Docker daemon. LLM-free.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence

#: Run a command and return its exit status (mirrors `panopticon.terminal.doctor.Run`).
Run = Callable[[Sequence[str]], int]


def _subprocess_status(command: Sequence[str]) -> int:
    """Default :data:`Run`: run ``command`` quietly and return its exit status."""
    try:
        return subprocess.run(list(command), capture_output=True).returncode
    except OSError:
        return 127


def daemon_reachable(run: Run = _subprocess_status) -> bool:
    """Whether the Docker daemon answers ``docker info``."""
    return run(["docker", "info"]) == 0


#: The actionable fix, platform-agnostic (no `sys.platform` branch): names both the macOS app and
#: the Linux service manager in one line, so the message is useful regardless of host platform.
FIX_HINT = "start OrbStack or Docker Desktop (macOS), or `systemctl start docker` (Linux)"


def preflight_message(command: str, *, run: Run = _subprocess_status) -> str | None:
    """``None`` when the Docker daemon is reachable (clear to proceed); otherwise a
    human-readable, actionable refusal message for ``panopticon {command}`` naming the fix —
    the caller should refuse to start rather than spawn into failure (REQ-031.1/REQ-031.2)."""
    if daemon_reachable(run):
        return None
    return f"Docker daemon unreachable — {FIX_HINT}, then rerun `panopticon {command}`."
