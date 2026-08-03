"""Private no-follow log sink used by integrated tmux sessions."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO


def open_private_log(path: Path) -> BinaryIO:
    """Open an append-only private log without following its directory or leaf symlinks."""
    directory_fd = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        log_fd = os.open(
            path.name,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory_fd,
        )
    finally:
        os.close(directory_fd)
    os.fchmod(log_fd, 0o600)
    return os.fdopen(log_fd, "ab", buffering=0)


def copy_available(input_fd: int, output: BinaryIO, log: BinaryIO) -> None:
    """Forward available input immediately; a failed log sink must not stop pane output."""
    persist = True
    while chunk := os.read(input_fd, 65536):
        output.write(chunk)
        output.flush()
        if persist:
            try:
                log.write(chunk)
            except OSError:
                persist = False


def main(argv: Sequence[str] | None = None) -> int:
    """Copy stdin to stdout and the securely opened private log."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        raise SystemExit("usage: python -m panopticon.terminal.log_tee PATH")
    with open_private_log(Path(arguments[0])) as log:
        copy_available(sys.stdin.fileno(), sys.stdout.buffer, log)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
