"""Live host tmux→shipped bridge→running container proof for REQ-050.1.1."""

from __future__ import annotations

import os
import pty
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from panopticon.sessionservice.tmux_defaults import write_default_config


def _docker_and_tmux_work() -> bool:
    return bool(
        shutil.which("docker")
        and shutil.which("tmux")
        and subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    )


# 2119: REQ-050.1.1
@pytest.mark.skipif(not _docker_and_tmux_work(), reason="needs a working docker daemon + tmux")
def test_real_ctrl_v_runs_the_shipped_bridge_against_a_matching_live_container(
    tmp_path: Path,
) -> None:
    # Use Panopticon's exact dedicated socket name.  TMUX_TMPDIR isolates this proof from a
    # developer's live Panopticon server without weakening the socket-name contract.
    socket = "panopticon"
    tmux_tmpdir = tmp_path / "tmux"
    tmux_tmpdir.mkdir(mode=0o700)
    tmux_env = {**os.environ, "TMUX_TMPDIR": str(tmux_tmpdir)}
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
        os.write(master, b"\x16")

        deadline = time.monotonic() + 5
        observed = ""
        while time.monotonic() < deadline:
            pane_bytes = pane_input.read_text() if pane_input.exists() else ""
            messages = subprocess.run(
                ["tmux", "-L", socket, "show-messages", "-t", session],
                capture_output=True,
                text=True,
                check=False,
                env=tmux_env,
            ).stdout
            if pane_bytes.startswith("/tmp/panopticon-clipboard-"):
                observed = "path delivered"
                break
            if "Image paste is unavailable" in messages:
                observed = "failure surfaced"
                break
            time.sleep(0.05)
        assert observed in {"path delivered", "failure surfaced"}
    finally:
        if master >= 0:
            os.write(master, b"\x02d")
            os.close(master)
        if client is not None:
            client.wait(timeout=3)
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, env=tmux_env)
        subprocess.run(["docker", "rm", "--force", session], capture_output=True)
        subprocess.run(["docker", "rmi", "--force", image], capture_output=True)
