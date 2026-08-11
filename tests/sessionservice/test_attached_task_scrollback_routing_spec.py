"""REQ-054 contract tests for input routing in attached Panopticon task panes."""

from __future__ import annotations

import contextlib
import fcntl
import os
import pty
import shlex
import shutil
import struct
import subprocess
import sys
import termios
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from panopticon.sessionservice.tmux_defaults import defaults_argv, write_default_config

_TASK_CONDITION = "#{m/r:^panopticon-,#{session_name}}"


def _config_lines(tmp_path: Path) -> list[str]:
    return (
        write_default_config("panopticon-scrollback-routing", directory=tmp_path, clipboard="cat")
        .read_text()
        .splitlines()
    )


def _binding(lines: list[str], table: str, key: str) -> str:
    prefix = f"bind-key -T {table} {key} "
    matches = [line for line in lines if line.startswith(prefix)]
    assert len(matches) == 1
    return matches[0]


# 2119: REQ-054.1.1
def test_task_wheel_up_enters_scrollback_instead_of_reaching_the_program(
    tmp_path: Path,
) -> None:
    binding = _binding(_config_lines(tmp_path), "root", "WheelUpPane")
    assert _TASK_CONDITION in binding
    assert "#{pane_in_mode}" in binding
    assert "copy-mode -e" in binding
    # The task branch may pass the mouse event only after copy mode already owns the pane. It may
    # not use tmux's stock `mouse_any_flag`, which forwards the event to a mouse-aware agent TUI.
    assert "send-keys -M" in binding
    assert "mouse_any_flag" not in binding


# 2119: REQ-054.1.2
# 2119: REQ-054.1.3
def test_task_wheel_down_moves_only_an_existing_scrollback_view(
    tmp_path: Path,
) -> None:
    binding = _binding(_config_lines(tmp_path), "root", "WheelDownPane")
    assert _TASK_CONDITION in binding
    assert "#{pane_in_mode}" in binding
    assert "send-keys -M" in binding
    # Outside copy mode the task branch is an explicit no-op, rather than another send-keys -M
    # that would deliver WheelDown to the foreground program at the live bottom.
    task_branch = binding.split(_TASK_CONDITION, 1)[1].rsplit("send-keys -M", 1)[0]
    assert task_branch.count("send-keys -M") == 1
    assert "select-pane -t =" in task_branch


# 2119: REQ-054.2.1
def test_task_page_up_enters_copy_mode_one_page_up(tmp_path: Path) -> None:
    lines = _config_lines(tmp_path)
    binding = _binding(lines, "root", "PageUp")
    assert _TASK_CONDITION in binding
    assert "copy-mode -u" in binding
    assert binding.endswith("'send-keys PageUp'")
    for table in ("copy-mode", "copy-mode-vi"):
        binding = _binding(lines, table, "PageUp")
        assert binding.endswith("send-keys -X page-up")


# 2119: REQ-054.2.2
def test_copy_mode_page_down_moves_toward_newer_content(tmp_path: Path) -> None:
    lines = _config_lines(tmp_path)
    for table in ("copy-mode", "copy-mode-vi"):
        binding = _binding(lines, table, "PageDown")
        assert binding.endswith("send-keys -X page-down")


# 2119: REQ-054.2.3
def test_task_page_down_at_live_bottom_is_consumed(tmp_path: Path) -> None:
    binding = _binding(_config_lines(tmp_path), "root", "PageDown")
    assert _TASK_CONDITION in binding
    assert "'copy-mode'" in binding
    assert binding.endswith("'send-keys PageDown'")


# 2119: REQ-054.3.1
def test_non_task_sessions_keep_foreground_scroll_input(tmp_path: Path) -> None:
    lines = _config_lines(tmp_path)
    assert _binding(lines, "root", "WheelUpPane").endswith("'send-keys -M'")
    assert _binding(lines, "root", "WheelDownPane").endswith("'send-keys -M'")
    assert _binding(lines, "root", "PageUp").endswith("'send-keys PageUp'")
    assert _binding(lines, "root", "PageDown").endswith("'send-keys PageDown'")


# 2119: REQ-054.3.2
def test_scrollback_bindings_are_absent_without_the_dedicated_socket() -> None:
    assert defaults_argv(None) == []


_HAVE_TMUX = bool(shutil.which("tmux"))


def _wait_for(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not reached before timeout")


def _pane_state(socket: str, target: str) -> tuple[bool, int]:
    result = subprocess.run(
        [
            "tmux",
            "-L",
            socket,
            "display-message",
            "-p",
            "-t",
            target,
            "#{pane_in_mode} #{scroll_position}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    return result[0] == "1", int(result[1] or "0")


def _attach(
    socket: str, session: str, *, socket_flag: str = "-L"
) -> tuple[int, subprocess.Popen[bytes]]:
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    environ = {**os.environ, "TERM": "xterm-256color"}
    process = subprocess.Popen(
        ["tmux", socket_flag, socket, "attach-session", "-t", session],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=environ,
        start_new_session=True,
    )
    os.close(slave)
    os.set_blocking(master, False)
    _wait_for(lambda: process.poll() is None)
    time.sleep(0.1)
    with contextlib.suppress(BlockingIOError):
        while os.read(master, 65536):
            pass
    return master, process


def _send_input(master: int, data: bytes) -> None:
    os.write(master, data)
    time.sleep(0.1)


def _detach(master: int, process: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(OSError):
        os.write(master, b"\x02d")
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.terminate()
        process.wait(timeout=2)
    os.close(master)


def _capture_program(tmp_path: Path) -> tuple[Path, Path, str]:
    script = tmp_path / "capture_input.py"
    capture = tmp_path / "captured-input"
    ready = tmp_path / "ready"
    script.write_text(
        "import os, pathlib, sys, tty\n"
        "for line in range(240): print(f'history-{line:03}', flush=True)\n"
        "tty.setraw(sys.stdin.fileno())\n"
        "sys.stdout.write('\\x1b[?1000h\\x1b[?1006h')\n"
        "sys.stdout.flush()\n"
        "pathlib.Path(sys.argv[2]).touch()\n"
        "fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)\n"
        "while True:\n"
        "    data = os.read(sys.stdin.fileno(), 4096)\n"
        "    if not data: break\n"
        "    os.write(fd, data)\n"
    )
    command = shlex.join([sys.executable, str(script), str(capture), str(ready)])
    return capture, ready, command


def _captured(path: Path) -> bytes:
    return path.read_bytes() if path.exists() else b""


# 2119: REQ-054.3.2
@pytest.mark.skipif(not _HAVE_TMUX, reason="needs tmux")
def test_real_tmux_without_panopticon_socket_does_not_receive_routing_bindings(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "operator.sock"
    operator_dir = tmp_path / "operator"
    operator_dir.mkdir()
    capture, ready, command = _capture_program(operator_dir)
    master: int | None = None
    client: subprocess.Popen[bytes] | None = None
    try:
        subprocess.run(
            [
                "tmux",
                "-S",
                str(socket_path),
                *defaults_argv(None),
                "new-session",
                "-d",
                "-s",
                "operator",
                command,
            ],
            check=True,
        )
        _wait_for(ready.exists)
        master, client = _attach(str(socket_path), "operator", socket_flag="-S")
        events = (b"\x1b[<64;5;5M", b"\x1b[<65;5;5M", b"\x1b[5~", b"\x1b[6~")
        for event in events:
            _send_input(master, event)
        _wait_for(lambda: all(event in _captured(capture) for event in events))
    finally:
        if master is not None and client is not None:
            _detach(master, client)
        subprocess.run(["tmux", "-S", str(socket_path), "kill-server"], capture_output=True)


# 2119: REQ-054.1.1
# 2119: REQ-054.1.2
# 2119: REQ-054.1.3
# 2119: REQ-054.2.1
# 2119: REQ-054.2.2
# 2119: REQ-054.2.3
# 2119: REQ-054.3.1
@pytest.mark.skipif(not _HAVE_TMUX, reason="needs tmux")
def test_real_tmux_routes_task_scrolling_to_scrollback_and_other_sessions_to_the_program(
    tmp_path: Path,
) -> None:
    socket = "panopticon-scrollback-routing-itest"
    subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)
    task_master: int | None = None
    task_client: subprocess.Popen[bytes] | None = None
    plain_master: int | None = None
    plain_client: subprocess.Popen[bytes] | None = None
    try:
        config = write_default_config(socket, directory=tmp_path, clipboard="cat")
        task_dir = tmp_path / "task"
        task_dir.mkdir()
        task_capture, task_ready, task_command = _capture_program(task_dir)
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
                "panopticon-test",
                task_command,
            ],
            capture_output=True,
            text=True,
        )
        assert created.returncode == 0, created.stderr
        _wait_for(task_ready.exists)
        target = "panopticon-test:0.0"
        history = subprocess.run(
            ["tmux", "-L", socket, "display-message", "-p", "-t", target, "#{history_size}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert int(history) > 100
        task_master, task_client = _attach(socket, "panopticon-test")

        wheel_up = b"\x1b[<64;5;5M"
        wheel_down = b"\x1b[<65;5;5M"
        page_up = b"\x1b[5~"
        page_down = b"\x1b[6~"

        # The first upward wheel event enters copy mode and moves off the live bottom; another
        # continues toward older history. Neither is available to the mouse-aware program.
        _send_input(task_master, wheel_up)
        _wait_for(lambda: _pane_state(socket, target)[0] and _pane_state(socket, target)[1] > 0)
        first_scroll = _pane_state(socket, target)[1]
        _send_input(task_master, wheel_up)
        _wait_for(lambda: _pane_state(socket, target)[1] > first_scroll)
        second_scroll = _pane_state(socket, target)[1]
        assert _captured(task_capture) == b""

        # WheelDown is owned by copy mode and moves toward newer content.
        _send_input(task_master, wheel_down)
        _wait_for(lambda: _pane_state(socket, target)[1] < second_scroll)
        assert _captured(task_capture) == b""

        # PageUp is tested from the live view, not merely from an already-active copy mode.
        _send_input(task_master, b"q")
        _wait_for(lambda: not _pane_state(socket, target)[0])
        _send_input(task_master, page_up)
        _wait_for(lambda: _pane_state(socket, target)[0] and _pane_state(socket, target)[1] > 0)
        page_scroll = _pane_state(socket, target)[1]
        _send_input(task_master, page_up)
        _wait_for(lambda: _pane_state(socket, target)[1] > page_scroll)
        continued_page_scroll = _pane_state(socket, target)[1]
        assert continued_page_scroll - page_scroll == page_scroll
        _send_input(task_master, page_down)
        _wait_for(lambda: _pane_state(socket, target)[1] < continued_page_scroll)
        assert _pane_state(socket, target)[1] == page_scroll
        assert _captured(task_capture) == b""

        # At the live bottom, downward wheel input remains a no-op and PageDown may enter copy
        # mode at position zero, but neither may leak into the foreground program.
        _send_input(task_master, b"q")
        _wait_for(lambda: not _pane_state(socket, target)[0])
        _send_input(task_master, wheel_down)
        assert not _pane_state(socket, target)[0]
        _send_input(task_master, page_down)
        _wait_for(lambda: _pane_state(socket, target)[0])
        assert _pane_state(socket, target)[1] == 0
        assert _captured(task_capture) == b""
        _detach(task_master, task_client)
        task_master = None
        task_client = None

        # The nearest scope counterexample is an otherwise identical mouse-aware pane whose
        # session lacks the task prefix. Every event is delivered to that program unchanged.
        plain_dir = tmp_path / "plain"
        plain_dir.mkdir()
        plain_capture, plain_ready, plain_command = _capture_program(plain_dir)
        subprocess.run(
            ["tmux", "-L", socket, "new-session", "-d", "-s", "not-panopticon", plain_command],
            check=True,
        )
        _wait_for(plain_ready.exists)
        plain_master, plain_client = _attach(socket, "not-panopticon")
        for event in (wheel_up, wheel_down, page_up, page_down):
            _send_input(plain_master, event)
        _wait_for(
            lambda: (
                wheel_up in _captured(plain_capture)
                and wheel_down in _captured(plain_capture)
                and page_up in _captured(plain_capture)
                and page_down in _captured(plain_capture)
            )
        )
        assert not _pane_state(socket, "not-panopticon:0.0")[0]
    finally:
        if task_master is not None and task_client is not None:
            _detach(task_master, task_client)
        if plain_master is not None and plain_client is not None:
            _detach(plain_master, plain_client)
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)
