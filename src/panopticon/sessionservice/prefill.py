"""Wait for an agent CLI input box and deliver text through its host tmux pane.

The pane emits ``ESC[?2004h`` when bracketed-paste input is ready. Watching that raw signal avoids
depending on harness UI wording and gives paste-buffer a safe multi-line input surface. Delivery
is best-effort: a missing/vanished pane, timeout, or tmux error returns ``False`` and never raises.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Protocol

DEFAULT_TIMEOUT = 300.0
BRACKETED_PASTE_ON = b"\x1b[?2004h"


class CommandRunner(Protocol):
    def __call__(self, args: Sequence[str], *, check: bool = True) -> str: ...


def _tmux(prefix: Sequence[str], *args: str) -> list[str]:
    return [*prefix, *args]


def _pane_id(session: str, *, prefix: Sequence[str], run: CommandRunner) -> str:
    try:
        return run(
            _tmux(prefix, "display-message", "-p", "-t", session, "#{pane_id}"),
            check=False,
        ).strip()
    except OSError:
        return ""


def _unlink(path: str) -> None:
    with suppress(OSError):
        os.unlink(path)


def prefill_pane(
    session: str,
    prompt_file: str,
    *,
    run: CommandRunner,
    prefix: Sequence[str] = ("tmux",),
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = DEFAULT_TIMEOUT,
    raw_log: str | None = None,
    buffer: str | None = None,
    submit: bool = False,
) -> bool:
    """Paste ``prompt_file`` after bracketed-paste readiness and optionally submit it."""
    prompt = Path(prompt_file)
    try:
        if not prompt.is_file() or not prompt.read_text().strip():
            return False
        pane = _pane_id(session, prefix=prefix, run=run)
        if not pane:
            return False

        if raw_log is None:
            raw_fd, raw = tempfile.mkstemp(prefix="panopticon-prefill-raw-")
            os.close(raw_fd)
            created_raw = True
        else:
            raw = raw_log
            created_raw = False
        buf = buffer or f"panopticon-prefill-{session}"
        try:
            run(
                _tmux(
                    prefix,
                    "pipe-pane",
                    "-O",
                    "-t",
                    pane,
                    f"cat >> {shlex.quote(raw)}",
                ),
                check=False,
            )
            ready = False
            for _ in range(max(0, int(timeout))):
                if not _pane_id(session, prefix=prefix, run=run):
                    return False
                if BRACKETED_PASTE_ON in Path(raw).read_bytes():
                    ready = True
                    break
                sleep(1.0)
            if not ready:
                return False
            sleep(1.0)
            run(_tmux(prefix, "load-buffer", "-b", buf, str(prompt)))
            run(_tmux(prefix, "paste-buffer", "-p", "-d", "-b", buf, "-t", pane))
            if submit:
                run(_tmux(prefix, "send-keys", "-t", pane, "Enter"))
            return True
        finally:
            run(_tmux(prefix, "pipe-pane", "-t", pane), check=False)
            if created_raw:
                _unlink(raw)
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        _unlink(prompt_file)
