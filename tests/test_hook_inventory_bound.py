"""Inventory evidence for the closed set of injected command hooks."""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
import threading
import time
import tomllib
from pathlib import Path

from panopticon.harnesses import HARNESSES, BootstrapContext, LaunchContext
from panopticon.harnesses.claude import settings
from panopticon.harnesses.codex import render_config
from panopticon.harnesses.outfitter import EXTENSION_FILE as OUTFITTER_EXTENSION_FILE
from panopticon.harnesses.outfitter import OutfitterHarness
from panopticon.harnesses.pi import EXTENSION_FILE as PI_EXTENSION_FILE
from panopticon.harnesses.pi import TURN_EXTENSION, PiHarness


# 2119: REQ-016.1.1
def test_every_injected_command_hook_is_in_the_bounded_inventory() -> None:
    assert set(HARNESSES) == {"claude", "codex", "pi", "outfitter"}
    claude = settings()["hooks"]
    assert set(claude) == {"Stop", "UserPromptSubmit", "PreToolUse", "PostToolUse"}
    for entries in claude.values():
        for entry in entries:
            assert entry["hooks"]
            for hook in entry["hooks"]:
                assert hook["type"] == "command"
                assert hook["timeout"] == 3


# 2119: REQ-016.1.1
def test_originating_harness_commands_return_within_bound_when_dispatched() -> None:
    claude = settings()["hooks"]
    codex = tomllib.loads(render_config("http://service", "", Path("/workspace")))["hooks"]
    commands = [
        hook["command"]
        for inventory in (claude, codex)
        for entries in inventory.values()
        for entry in entries
        for hook in entry["hooks"]
    ]
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.1)
    release = threading.Event()

    def blackhole() -> None:
        connections: list[socket.socket] = []
        try:
            while not release.is_set():
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                connections.append(connection)
        finally:
            for connection in connections:
                connection.close()

    thread = threading.Thread(target=blackhole, daemon=True)
    thread.start()
    host, port = listener.getsockname()
    env = {
        key: value
        for key, value in os.environ.items()
        if key.lower() not in {"http_proxy", "https_proxy", "all_proxy"}
    }
    env.update(
        PANOPTICON_SERVICE_URL=f"http://{host}:{port}",
        PANOPTICON_TASK_ID="t1",
        NO_PROXY=host,
    )
    started = time.monotonic()
    processes = [
        subprocess.Popen(
            shlex.split(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for command in commands
    ]
    try:
        results = [process.communicate(input="", timeout=3.2) for process in processes]
        elapsed = time.monotonic() - started
    finally:
        release.set()
        listener.close()
        thread.join(timeout=1)
        for process in processes:
            if process.poll() is None:
                process.kill()

    assert elapsed < 3
    assert all(process.returncode == 0 for process in processes)
    assert results == [("", "")] * len(commands)


# 2119: REQ-016.1.1
def test_every_registered_pi_runtime_injects_the_bounded_extension(tmp_path: Path) -> None:
    ctx = BootstrapContext(
        home=tmp_path,
        cwd=Path("/workspace"),
        service_url="http://service",
        task_id="task-1",
    )
    PiHarness().bootstrap(ctx)
    OutfitterHarness().bootstrap(ctx)

    assert (tmp_path / ".pi" / "agent" / PI_EXTENSION_FILE).read_text() == TURN_EXTENSION
    assert (tmp_path / ".outfitter" / OUTFITTER_EXTENSION_FILE).read_text() == TURN_EXTENSION

    launch = LaunchContext(home=tmp_path, cwd=Path("/workspace"))
    for harness, expected_extension in (
        (PiHarness(), tmp_path / ".pi" / "agent" / PI_EXTENSION_FILE),
        (OutfitterHarness(), tmp_path / ".outfitter" / OUTFITTER_EXTENSION_FILE),
    ):
        argv = harness.argv(launch)
        assert argv.count("--extension") == 1
        extension_index = argv.index("--extension")
        assert argv[extension_index + 1] == str(expected_extension)
        assert expected_extension.read_text() == TURN_EXTENSION

    codex = tomllib.loads(render_config("http://service", "", Path("/workspace")))["hooks"]
    assert set(codex) == {"Stop", "UserPromptSubmit"}
    for entries in codex.values():
        for entry in entries:
            assert entry["hooks"]
            for hook in entry["hooks"]:
                assert hook["type"] == "command"
                assert hook["timeout"] == 3
