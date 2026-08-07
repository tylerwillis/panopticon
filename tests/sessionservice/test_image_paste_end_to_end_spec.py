"""Live host tmux→shipped bridge→running container proof for attached-session-image-paste.1.1."""

from __future__ import annotations

import contextlib
import os
import pty
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

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
def test_real_ctrl_v_runs_the_shipped_bridge_against_a_matching_live_container(
    tmp_path: Path,
) -> None:
    # Use Panopticon's exact dedicated socket name.  TMUX_TMPDIR isolates this proof from a
    # developer's live Panopticon server without weakening the socket-name contract.
    socket = "panopticon"
    tmux_tmpdir = Path(tempfile.mkdtemp(prefix="pt-"))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    wl_paste = fake_bin / "wl-paste"
    wl_paste.write_text("#!/bin/sh\nprintf '\\211PNG\\r\\n\\032\\ncontent'\n")
    wl_paste.chmod(0o755)
    tmux_env = {
        **os.environ,
        "TMUX_TMPDIR": str(tmux_tmpdir),
        "TERM": "xterm-256color",
        "WAYLAND_DISPLAY": "panopticon-test",
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    session = "panopticon-image-paste-e2e-task"
    image = "panopticon-image-paste-e2e:latest"
    pane_input = tmp_path / "pane-input"
    subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, env=tmux_env)
    subprocess.run(["docker", "rm", "--force", session], capture_output=True)
    master = -1
    client: subprocess.Popen[bytes] | None = None
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
                f"cat > {pane_input}",
            ],
            check=True,
            capture_output=True,
            env=tmux_env,
        )
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
        os.write(master, b"\x16")

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pane_bytes = pane_input.read_text() if pane_input.exists() else ""
            if pane_bytes.startswith("/tmp/panopticon-clipboard-"):
                break
            time.sleep(0.05)
        assert pane_bytes.startswith("/tmp/panopticon-clipboard-")
        container_path = pane_bytes.strip()
        staged = subprocess.run(
            ["docker", "exec", session, "cat", container_path],
            capture_output=True,
            check=True,
        )
        assert staged.stdout == b"\x89PNG\r\n\x1a\ncontent"
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
