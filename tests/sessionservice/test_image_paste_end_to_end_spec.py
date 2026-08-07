"""Live shipped bridge→host tmux→running container proof for image paste."""

from __future__ import annotations

import contextlib
import os
import pty
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from panopticon.sessionservice.image_paste import CapturedImage, paste_clipboard_image
from panopticon.sessionservice.tmux_defaults import write_default_config


def _docker_and_tmux_work() -> bool:
    return bool(
        sys.platform == "linux"
        and shutil.which("docker")
        and shutil.which("tmux")
        and subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    )


# 2119: attached-session-image-paste.1.1
@pytest.mark.skipif(not _docker_and_tmux_work(), reason="needs a working docker daemon + tmux")
def test_shipped_bridge_stages_known_png_and_delivers_its_container_path(
    tmp_path: Path,
) -> None:
    # Use Panopticon's exact dedicated socket name.  TMUX_TMPDIR isolates this proof from a
    # developer's live Panopticon server without weakening the socket-name contract.
    socket = "panopticon"
    tmux_tmpdir = Path(tempfile.mkdtemp(prefix="pt-"))
    tmux_env = {
        **os.environ,
        "TMUX_TMPDIR": str(tmux_tmpdir),
        "TERM": "xterm-256color",
    }
    session = "panopticon-image-paste-e2e-task"
    image = "panopticon-image-paste-e2e:latest"
    pane_input = tmp_path / "pane-input"
    pane_ready = tmp_path / "pane-ready"
    master = -1
    client: subprocess.Popen[bytes] | None = None
    subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, env=tmux_env)
    subprocess.run(["docker", "rm", "--force", session], capture_output=True)
    try:
        subprocess.run(
            ["docker", "build", "--tag", image, "-"],
            input=b'FROM alpine:3.20\nRUN adduser -D -u 1000 panopticon\nCMD ["sleep","30"]\n',
            check=True,
            capture_output=True,
            env=tmux_env,
        )
        subprocess.run(
            ["docker", "run", "--detach", "--name", session, image],
            check=True,
            capture_output=True,
            env=tmux_env,
        )
        config = write_default_config(socket, directory=tmp_path, clipboard=None)
        subprocess.run(
            [
                "tmux",
                "-L",
                socket,
                "-f",
                str(config),
                "new-session",
                "-d",
                "-s",
                session,
                "sh",
                "-c",
                (
                    "stty raw -echo; printf '\\033[?2004h'; sleep 0.5; "
                    f"touch {shlex.quote(str(pane_ready))}; "
                    f"exec cat > {shlex.quote(str(pane_input))}"
                ),
            ],
            check=True,
            capture_output=True,
            env=tmux_env,
        )
        pane = subprocess.run(
            ["tmux", "-L", socket, "display-message", "-t", session, "-p", "#{pane_id}"],
            capture_output=True,
            text=True,
            check=True,
            env=tmux_env,
        ).stdout.strip()

        deadline = time.monotonic() + 3
        pane_command = ""
        while time.monotonic() < deadline:
            pane_command = subprocess.run(
                [
                    "tmux",
                    "-L",
                    socket,
                    "display-message",
                    "-t",
                    session,
                    "-p",
                    "#{pane_current_command}",
                ],
                capture_output=True,
                text=True,
                check=True,
                env=tmux_env,
            ).stdout.strip()
            if pane_ready.exists() and pane_command == "cat":
                break
            time.sleep(0.02)
        assert pane_ready.exists()
        assert pane_command == "cat"

        master, slave = pty.openpty()
        client = subprocess.Popen(
            ["tmux", "-L", socket, "attach", "-t", session],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=tmux_env,
        )
        os.close(slave)
        deadline = time.monotonic() + 3
        clients = subprocess.CompletedProcess([], 1, stdout=b"")
        while time.monotonic() < deadline:
            clients = subprocess.run(
                ["tmux", "-L", socket, "list-clients", "-t", session],
                capture_output=True,
                check=False,
                env=tmux_env,
            )
            if clients.stdout:
                break
            time.sleep(0.02)
        assert clients.stdout
        time.sleep(0.5)

        def run(
            argv: list[str],
            *,
            input: bytes | None = None,
            **kwargs: Any,
        ) -> subprocess.CompletedProcess[bytes]:
            command = ["tmux", "-L", socket, *argv[1:]] if argv[0] == "tmux" else argv
            return subprocess.run(command, input=input, env=tmux_env, **kwargs)

        png = b"\x89PNG\r\n\x1a\ncontent"
        result = paste_clipboard_image(
            session,
            pane,
            capture=lambda: CapturedImage(png, "png"),
            run=run,
            token=lambda: "e2e",
        )
        assert result.ok

        bracketed_start = b"\x1b[200~"
        bracketed_end = b"\x1b[201~"
        pane_bytes = b""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pane_bytes = pane_input.read_bytes() if pane_input.exists() else b""
            if pane_bytes.startswith(bracketed_start) and pane_bytes.endswith(bracketed_end):
                break
            time.sleep(0.05)
        assert pane_bytes.startswith(bracketed_start)
        assert pane_bytes.endswith(bracketed_end)
        container_path = pane_bytes[len(bracketed_start) : -len(bracketed_end)].decode()
        assert container_path.startswith("/tmp/panopticon-clipboard-")
        staged = subprocess.run(
            ["docker", "exec", session, "cat", container_path],
            capture_output=True,
            check=True,
        )
        assert staged.stdout == png
    finally:
        if master >= 0:
            with contextlib.suppress(OSError):
                os.write(master, b"\x02d")
            with contextlib.suppress(OSError):
                os.close(master)
        if client is not None:
            client.wait(timeout=3)
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, env=tmux_env)
        subprocess.run(["docker", "rm", "--force", session], capture_output=True)
        subprocess.run(["docker", "rmi", "--force", image], capture_output=True)
        shutil.rmtree(tmux_tmpdir, ignore_errors=True)
