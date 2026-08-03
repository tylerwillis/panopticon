"""Wait for an agent CLI input box and deliver text through its host tmux pane.

The pane emits ``ESC[?2004h`` when bracketed-paste input is ready. Watching that raw signal avoids
depending on harness UI wording and gives paste-buffer a safe multi-line input surface. Delivery
is best-effort: a missing/vanished pane, timeout, or tmux error returns ``False`` and never raises.
"""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Protocol

DEFAULT_TIMEOUT = 300.0
DEFAULT_WAKE_TIMEOUT = 5.0
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


def readiness_log(session: str) -> str:
    """Return the stable raw-output log used to remember one task pane's readiness."""
    root = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir()))
    directory = root / f"panopticon-prefill-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    details = directory.stat()
    if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o700:
        raise OSError(f"unsafe readiness directory: {directory}")
    return str(directory / f"{session}.raw")


def readiness_watch_command(raw: str) -> str:
    """Consume pane output only until readiness, then persist one owner-only marker."""
    code = (
        "import os, sys\n"
        "marker = b'\\x1b[?2004h'\n"
        "seen = b''\n"
        "while True:\n"
        "    chunk = sys.stdin.buffer.read1(4096)\n"
        "    if not chunk:\n"
        "        raise SystemExit(1)\n"
        "    seen += chunk\n"
        "    if marker in seen:\n"
        "        break\n"
        "    seen = seen[-(len(marker) - 1):]\n"
        "flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)\n"
        f"fd = os.open({raw!r}, flags, 0o600)\n"
        "os.write(fd, marker)\n"
        "os.close(fd)"
    )
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def watch_pane(
    session: str,
    *,
    run: CommandRunner,
    prefix: Sequence[str] = ("tmux",),
    raw_log: str | None = None,
) -> str:
    """Start a persistent readiness watch and return the pane id, or ``""`` on failure."""
    pane = _pane_id(session, prefix=prefix, run=run)
    if not pane:
        return ""
    raw = raw_log or readiness_log(session)
    try:
        _unlink(raw)
        run(
            _tmux(
                prefix,
                "pipe-pane",
                "-O",
                "-t",
                pane,
                readiness_watch_command(raw),
            ),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return pane


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
    watch: bool = True,
    settle_delay: float = 1.0,
) -> bool:
    """Paste ``prompt_file`` after bracketed-paste readiness and optionally submit it."""
    prompt = Path(prompt_file)
    try:
        if not prompt.is_file() or not prompt.read_text():
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
            if watch:
                _unlink(raw)
                run(
                    _tmux(
                        prefix,
                        "pipe-pane",
                        "-O",
                        "-t",
                        pane,
                        readiness_watch_command(raw),
                    ),
                    check=False,
                )
            ready = False
            for _ in range(max(0, int(timeout))):
                if not _pane_id(session, prefix=prefix, run=run):
                    return False
                if Path(raw).is_file() and BRACKETED_PASTE_ON in Path(raw).read_bytes():
                    ready = True
                    break
                sleep(1.0)
            if not ready:
                return False
            if settle_delay:
                sleep(settle_delay)
            run(_tmux(prefix, "load-buffer", "-b", buf, str(prompt)))
            run(_tmux(prefix, "paste-buffer", "-p", "-d", "-b", buf, "-t", pane))
            if submit:
                run(_tmux(prefix, "send-keys", "-t", pane, "Enter"))
            return True
        finally:
            if watch:
                run(_tmux(prefix, "pipe-pane", "-t", pane), check=False)
            if created_raw:
                _unlink(raw)
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        _unlink(prompt_file)
