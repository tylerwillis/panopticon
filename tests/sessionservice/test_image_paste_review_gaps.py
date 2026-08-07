"""Focused counterexamples required by REQ-053's fresh-context honesty review."""

# ruff: noqa: B023

from __future__ import annotations

import contextlib
import os
import pty
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from panopticon.sessionservice.image_paste import (
    MAX_IMAGE_BYTES,
    CapturedImage,
    capture_clipboard_image,
    image_paste_binding,
    paste_clipboard_image,
)
from panopticon.sessionservice.tmux_defaults import server_default_config_text


def _which(present: set[str]):
    return lambda tool: f"/usr/bin/{tool}" if tool in present else None


# 2119: attached-session-image-paste.1.1
# 2119: attached-session-image-paste.1.2
@pytest.mark.skipif(not shutil.which("tmux"), reason="needs tmux")
def test_real_tmux_ctrl_v_invokes_loaded_bridge_with_originating_session_and_pane(
    tmp_path: Path,
) -> None:
    socket = "panopticon-image-paste-itest"
    session = "panopticon-task-bridge"
    marker = tmp_path / "bridge-call"
    helper = tmp_path / "record_bridge.py"
    helper.write_text(
        f"import pathlib, sys\npathlib.Path({str(marker)!r}).write_text('|'.join(sys.argv[1:]))\n"
    )
    command = f"python {shlex.quote(str(helper))}"
    tmux_env = {**os.environ, "TERM": "xterm-256color"}
    default_binding = image_paste_binding("python -m panopticon.sessionservice.image_paste")
    test_binding = image_paste_binding(command)
    config = tmp_path / "tmux.conf"
    config.write_text(
        server_default_config_text(clipboard=None).replace(default_binding, test_binding)
    )
    subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, env=tmux_env)
    master = -1
    client: subprocess.Popen[bytes] | None = None
    try:
        created = subprocess.run(
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
                "sleep",
                "30",
            ],
            capture_output=True,
            check=False,
            env=tmux_env,
        )
        assert created.returncode == 0, created.stderr
        pane = subprocess.run(
            ["tmux", "-L", socket, "display-message", "-t", session, "-p", "#{pane_id}"],
            capture_output=True,
            text=True,
            check=True,
            env=tmux_env,
        ).stdout.strip()
        keys = subprocess.run(
            ["tmux", "-L", socket, "list-keys", "-T", "root"],
            capture_output=True,
            text=True,
            check=True,
            env=tmux_env,
        ).stdout
        ctrl_v = next(line for line in keys.splitlines() if "C-v" in line)
        assert command in ctrl_v
        assert "#{session_name}" in ctrl_v and "#{pane_id}" in ctrl_v

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
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.read_text() == f"{session}|{pane}"
    finally:
        if master >= 0:
            with contextlib.suppress(OSError):
                os.write(master, b"\x02d")
            with contextlib.suppress(OSError):
                os.close(master)
        if client is not None:
            client.wait(timeout=3)
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, env=tmux_env)


# 2119: attached-session-image-paste.2.2
def test_wayland_capture_requires_every_antecedent() -> None:
    scenarios = (
        ("darwin", {"WAYLAND_DISPLAY": "wayland-0"}, {"wl-paste", "osascript"}),
        ("linux", {"WAYLAND_DISPLAY": ""}, {"wl-paste", "xclip"}),
        ("linux", {"DISPLAY": ":0"}, {"xclip"}),
    )
    for platform, environ, present in scenarios:
        calls: list[tuple[str, ...]] = []

        def run(argv: tuple[str, ...], **_kwargs: object):
            calls.append(argv)
            output = "«data PNGf706e67»".encode() if platform == "darwin" else b"png"
            return subprocess.CompletedProcess(argv, 0, stdout=output, stderr=b"")

        capture_clipboard_image(
            platform=platform,
            environ=environ,
            which=_which(present),
            run=run,
        )
        assert calls
        assert calls[0][0] != "/usr/bin/wl-paste"


# 2119: attached-session-image-paste.5.1
def test_failure_never_uses_an_alternate_path_delivery_command() -> None:
    for failure in ("capture", "empty", "oversize", "staging", "loading", "delivery"):
        calls: list[list[str]] = []

        def capture() -> CapturedImage:
            if failure == "capture":
                raise RuntimeError("unavailable")
            if failure == "empty":
                return CapturedImage(b"", "png")
            if failure == "oversize":
                return CapturedImage(b"x" * (MAX_IMAGE_BYTES + 1), "png")
            return CapturedImage(b"png", "png")

        def run(argv: list[str], **_kwargs: object):
            calls.append(argv)
            fails = (
                (failure == "staging" and argv[:2] == ["docker", "exec"])
                or (failure == "loading" and "load-buffer" in argv)
                or (failure == "delivery" and "paste-buffer" in argv)
            )
            return subprocess.CompletedProcess(argv, 1 if fails else 0, stdout=b"", stderr=b"")

        result = paste_clipboard_image(
            "panopticon-task",
            "%4",
            capture=capture,
            run=run,
            token=lambda: "failed",
        )
        assert not result.ok
        failure_index = next(
            (
                index
                for index, argv in enumerate(calls)
                if (failure == "staging" and argv[:2] == ["docker", "exec"])
                or (failure == "loading" and "load-buffer" in argv)
                or (failure == "delivery" and "paste-buffer" in argv)
            ),
            -1,
        )
        delivery_tail = calls[failure_index + 1 :] if failure_index >= 0 else calls
        assert delivery_tail == [
            [
                "tmux",
                "display-message",
                "-t",
                "%4",
                "Image paste is unavailable; save the image under the task workspace and paste its path",
            ]
        ]
