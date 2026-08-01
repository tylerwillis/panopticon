"""Shipped defaults for panopticon's dedicated ``-L panopticon`` tmux server (REQ-030): mouse
reporting, deep scrollback, and mouse-drag/double-click copy wired to the system clipboard.

The server is never explicitly created; whichever panopticon-owned session-creating call happens
to be first against a not-yet-running socket implicitly starts it. Separate ``tmux -L socket
set-option ...`` invocations do NOT work for this: tmux's ``exit-empty`` (on by default) tears the
server back down the moment a client that leaves it with zero sessions disconnects, so nothing set
by one bare command survives to the next — confirmed against a real tmux binary. The only reliable
mechanism is tmux's ``-f <file>`` flag, honored **only** when a client's command is what starts a
brand-new server (silently ignored once one is already running): every ``new-session`` call that
might be the first to touch the socket must load :func:`write_default_config`'s file via ``-f``, so
the defaults land atomically with that very session — including ``history-limit``, which binds to
a pane at creation and is never applied retroactively, and so **must** be in place before that
pane, not merely before the socket has *some* session on it. ``-f`` also replaces tmux's normal
``~/.tmux.conf``/``/etc/tmux.conf`` search entirely (REQ-030.5.2), rather than merely selecting a
different socket the way ``-L`` alone does — so an operator's personal tmux customizations never
reach this socket's server, confirmed against a real tmux binary.

The double-click binding chains three tmux commands (enter copy-mode, select the word, copy it) as
one action bound to one key. Passed as bare ``;``-separated tokens on a plain CLI argv, tmux's
flat argument parser (``cmd_parse_from_arguments``) treats every ``;`` as a top-level separator
unconditionally — splitting the chain into three separate immediate commands instead of binding
them together (confirmed: only ``copy-mode -M`` ends up bound; the other two fire immediately and
error). Written as a config-file line instead — parsed by ``cmd_parse_from_file`` via ``-f`` — the
same ``\\;``-separated text correctly nests as the key's one chained action.

The config path is deterministic per socket (so repeated spawns overwrite one file rather than
littering temp dirs) and shared by every process on the host, so :func:`write_default_config`
writes to a fresh uniquely-named file in the same directory and atomically renames it onto that
path rather than truncating it in place — an in-place write racing a concurrent writer (two
processes independently starting up against the same fresh socket) could otherwise hand tmux a
half-written file, and renaming onto the path (rather than opening it directly) never follows a
pre-existing symlink there either.

LLM-free: the config text is pure; :func:`write_default_config` is the one bit of real I/O (a
config file must be a real path on disk for ``-f`` to load), deliberately kept separate.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

#: Scrollback lines per pane — tmux's stock default (2000) discards most of what REQ-009's inline
#: (``--no-alt-screen``) rendering puts into scrollback.
HISTORY_LIMIT = "50000"


def clipboard_tool(
    *, platform: str = sys.platform, which: Callable[[str], str | None] | None = None
) -> str | None:
    """The shell command a copy-pipe bind should run, or ``None`` when no system clipboard tool is
    found (REQ-030.4). ``pbcopy`` on darwin; on other platforms, the first of ``wl-copy`` / ``xclip
    -selection clipboard`` / ``xsel --clipboard --input`` found on ``PATH``.

    ``which`` defaults to ``shutil.which`` resolved **inside the call** (not as an eager default
    argument): a default bound at function-definition time would capture that reference once at
    import and never see a test's ``monkeypatch.setattr("shutil.which", ...)`` again."""
    resolve = which if which is not None else shutil.which
    if platform == "darwin":
        return "pbcopy" if resolve("pbcopy") else None
    for tool, command in (
        ("wl-copy", "wl-copy"),
        ("xclip", "xclip -selection clipboard"),
        ("xsel", "xsel --clipboard --input"),
    ):
        if resolve(tool):
            return command
    return None


def server_default_config_text(*, clipboard: str | None) -> str:
    """The tmux config file text (REQ-030.1, REQ-030.2) applying panopticon's shipped server
    defaults: mouse reporting, :data:`HISTORY_LIMIT` scrollback, ``set-clipboard``, and
    drag/double-click copy bound to ``clipboard`` (see :func:`clipboard_tool`) when given, else a
    plain in-tmux copy (the paste buffer, plus OSC 52 via ``set-clipboard``, still reaches a
    remote/odd setup)."""
    copy = (
        f'send-keys -X copy-pipe-and-cancel "{clipboard}"'
        if clipboard
        else "send-keys -X copy-selection-and-cancel"
    )
    lines = [
        "set-option -g mouse on",
        f"set-option -g history-limit {HISTORY_LIMIT}",
        "set-option -g set-clipboard on",
        f"bind-key -T copy-mode MouseDragEnd1Pane {copy}",
        f"bind-key -T copy-mode-vi MouseDragEnd1Pane {copy}",
        f"bind-key -T root DoubleClick1Pane copy-mode -M \\; send-keys -X select-word \\; {copy}",
    ]
    return "\n".join(lines) + "\n"


def default_config_path(socket: str, *, directory: str | Path | None = None) -> Path:
    """Where the generated defaults config for ``socket`` lives — deterministic per socket, so
    repeated calls (every spawn/attach) overwrite the same file instead of littering temp dirs."""
    return Path(directory or tempfile.gettempdir()) / f"panopticon-{socket}-tmux.conf"


def write_default_config(
    socket: str,
    *,
    directory: str | Path | None = None,
    clipboard: str | None = None,
    which: Callable[[str], str | None] | None = None,
    platform: str = sys.platform,
) -> Path:
    """Resolve the clipboard tool (unless ``clipboard`` is given) and write the shipped defaults
    config for ``socket``, returning its path for the caller's own ``-f`` flag — prepended to
    whichever ``new-session`` call might be the first to touch the socket (REQ-030.3). Written
    atomically (a unique temp file, then renamed onto the target path) so a concurrent writer can
    never leave tmux reading a half-written file."""
    resolved = (
        clipboard if clipboard is not None else clipboard_tool(platform=platform, which=which)
    )
    path = default_config_path(socket, directory=directory)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(server_default_config_text(clipboard=resolved))
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return path


def defaults_argv(socket: str | None) -> list[str]:
    """The ``-f <config>`` args to prepend to a ``new-session`` call so a fresh socket boots with
    panopticon's shipped tmux defaults (REQ-030), regardless of which panopticon-owned process
    happens to touch it first — ``-f`` only takes effect when tmux is starting a brand-new server,
    so passing it unconditionally on every ``new-session`` is harmless (silently ignored once a
    server already exists — including one left running from before this socket adopted these
    defaults; restart it, e.g. ``make stop``, to pick them up) and is the only mechanism that
    reliably applies for the first-touch case (see the module docstring). Empty without a
    dedicated socket (``socket=None``) — that means talking to the ambient default tmux server,
    which may be an operator's own, and these defaults must never reach it (REQ-030.5)."""
    if not socket:
        return []
    return ["-f", str(write_default_config(socket))]
