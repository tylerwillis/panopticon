"""claude turn-flip hooks: the rendered settings and the callback that POSTs `set_turn`."""

from __future__ import annotations

import io
import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
import tomllib
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from panopticon.container import hook
from panopticon.harnesses import BootstrapContext, LaunchContext
from panopticon.harnesses.claude import settings, write_settings
from panopticon.harnesses.codex import render_config
from panopticon.harnesses.pi import EXTENSION_FILE, PiHarness

_CONTROL_PLANE_SENTINEL = "CONTROL_PLANE_FAILURE_SENTINEL"


def test_settings_wire_stop_to_user_and_prompt_to_agent() -> None:
    s = settings()
    assert s["hooks"]["Stop"][0]["hooks"][0]["command"].endswith("hook user stop")
    assert s["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"].endswith("hook agent prompt")


# 2119: REQ-009.1.1
def test_every_command_turn_hook_has_a_three_second_backstop() -> None:
    claude_hooks = settings()["hooks"]
    for entries in claude_hooks.values():
        for entry in entries:
            assert entry["hooks"][0]["timeout"] == 3

    codex_config = tomllib.loads(render_config("http://svc", "", Path("/workspace")))
    for event in ("Stop", "UserPromptSubmit"):
        assert codex_config["hooks"][event][0]["hooks"][0]["timeout"] == 3


def test_settings_flip_to_user_while_asking_a_question_and_back_when_answered() -> None:
    # AskUserQuestion is a mid-turn tool call (never fires Stop), so PreToolUse/PostToolUse matched
    # to it carry the turn: user while the question is pending, agent once it's answered.
    s = settings()
    pre = s["hooks"]["PreToolUse"][0]
    post = s["hooks"]["PostToolUse"][0]
    assert pre["matcher"] == "AskUserQuestion" and post["matcher"] == "AskUserQuestion"
    assert pre["hooks"][0]["command"].endswith("hook user")  # no event arg → pure turn flip
    assert post["hooks"][0]["command"].endswith("hook agent")


def test_settings_pre_accept_bypass_permissions_mode() -> None:
    # Without this, unattended claude (--dangerously-skip-permissions) hangs on the first-run
    # "Bypass Permissions mode" acceptance prompt — the task shows "stuck starting".
    assert settings()["skipDangerousModePermissionPrompt"] is True


def test_write_settings_writes_claude_settings(tmp_path: Path) -> None:
    path = write_settings(tmp_path)
    assert path == tmp_path / ".claude" / "settings.json"
    assert "Stop" in json.loads(path.read_text())["hooks"]


def test_write_settings_merges_without_clobbering_existing_keys(tmp_path: Path) -> None:
    # Routed through the read-merge-write helper: any unrelated settings already on disk survive.
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text('{"model": "opus"}')

    write_settings(tmp_path)

    data = json.loads(settings_path.read_text())
    assert data["model"] == "opus"  # preserved
    assert "Stop" in data["hooks"]  # turn-flip hooks merged in


class _FakeClient:
    def __init__(self, slug: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.tokens: list[tuple[str, int]] = []
        self._slug = slug

    def set_turn(self, task_id: str, turn: str) -> dict[str, object]:
        self.calls.append((task_id, turn))
        return {}

    def get_task(self, task_id: str) -> dict[str, object]:
        return {"id": task_id, "slug": self._slug}

    def get_briefing(self, task_id: str) -> str:
        return "PHASE BRIEFING: you are in PLANNING"

    def set_tokens_used(self, task_id: str, tokens_used: int) -> dict[str, object]:
        self.tokens.append((task_id, tokens_used))
        return {}


class _FailingClient(_FakeClient):
    def __init__(self, operation: str, error: httpx.HTTPError) -> None:
        super().__init__(slug=None)
        self._operation = operation
        self._error = error

    def _fail(self, operation: str) -> None:
        if operation == self._operation:
            raise self._error

    def set_turn(self, task_id: str, turn: str) -> dict[str, object]:
        self._fail("set_turn")
        return super().set_turn(task_id, turn)

    def set_tokens_used(self, task_id: str, tokens_used: int) -> dict[str, object]:
        self._fail("set_tokens_used")
        return super().set_tokens_used(task_id, tokens_used)

    def get_briefing(self, task_id: str) -> str:
        self._fail("get_briefing")
        return super().get_briefing(task_id)

    def get_task(self, task_id: str) -> dict[str, object]:
        self._fail("get_task")
        return super().get_task(task_id)


def _control_plane_error(kind: str) -> httpx.HTTPError:
    request = httpx.Request("PUT", "http://svc/tasks/t1/turn")
    if kind == "connection":
        return httpx.ConnectError(_CONTROL_PLANE_SENTINEL, request=request)
    if kind == "timeout":
        return httpx.ReadTimeout(_CONTROL_PLANE_SENTINEL, request=request)
    if kind == "protocol":
        return httpx.RemoteProtocolError(_CONTROL_PLANE_SENTINEL, request=request)
    response = httpx.Response(503, request=request)
    return httpx.HTTPStatusError(_CONTROL_PLANE_SENTINEL, request=request, response=response)


# 2119: REQ-009.1.1
# 2119: REQ-009.2.1
@pytest.mark.parametrize(
    ("argv", "payload"),
    [
        (["user"], ""),
        (["agent"], ""),
        (["agent", "prompt"], ""),
        (["user", "stop"], ""),
        (["user", "stop"], '{"transcript_path": "/missing"}'),
    ],
)
def test_hook_returns_success_within_bound_against_a_blackholed_service(
    argv: list[str], payload: str
) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(5)
    release = threading.Event()

    def blackhole() -> None:
        try:
            connection, _ = listener.accept()
        except OSError:
            return
        with connection:
            release.wait(timeout=5)

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
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "panopticon.container.hook", *argv],
            input=payload,
            text=True,
            capture_output=True,
            env=env,
            timeout=3.5,
            check=False,
        )
        elapsed = time.monotonic() - started
    finally:
        release.set()
        listener.close()
        thread.join(timeout=1)

    assert elapsed < 3
    assert completed.returncode == 0
    assert completed.stdout == "" and completed.stderr == ""


# 2119: REQ-009.2.1
@pytest.mark.parametrize(
    ("argv", "payload", "successful_requests", "expected_stdout"),
    [
        (["agent", "prompt"], "", 1, ""),
        (["agent", "prompt"], "", 2, "PHASE BRIEFING\n"),
        (["user", "stop"], '{"transcript_path": "/missing"}', 1, ""),
    ],
)
def test_hook_fails_open_when_a_later_control_plane_request_stalls(
    argv: list[str], payload: str, successful_requests: int, expected_stdout: str
) -> None:
    request_count = 0
    release = threading.Event()

    class _LaterBlackhole(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _handle_request(self) -> None:
            nonlocal request_count
            request_count += 1
            if request_count > successful_requests:
                release.wait(timeout=5)
                return
            length = int(self.headers.get("content-length", "0"))
            if length:
                self.rfile.read(length)
            response: dict[str, object] = {}
            if self.path.endswith("/briefing"):
                response = {"briefing": "PHASE BRIEFING"}
            elif self.command == "GET":
                response = {"id": "t1", "slug": "hook-fail-open"}
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _handle_request
        do_PUT = _handle_request

    service = ThreadingHTTPServer(("127.0.0.1", 0), _LaterBlackhole)
    service_thread = threading.Thread(target=service.serve_forever, daemon=True)
    service_thread.start()
    env = {
        key: value
        for key, value in os.environ.items()
        if key.lower() not in {"http_proxy", "https_proxy", "all_proxy"}
    }
    env.update(
        PANOPTICON_SERVICE_URL=f"http://127.0.0.1:{service.server_port}",
        PANOPTICON_TASK_ID="t1",
        NO_PROXY="127.0.0.1",
    )

    started = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "panopticon.container.hook", *argv],
            input=payload,
            text=True,
            capture_output=True,
            env=env,
            timeout=3.5,
            check=False,
        )
        elapsed = time.monotonic() - started
    finally:
        release.set()
        service.shutdown()
        service.server_close()
        service_thread.join(timeout=1)

    assert request_count == successful_requests + 1
    assert elapsed < 3
    assert completed.returncode == 0
    assert completed.stdout == expected_stdout and completed.stderr == ""


# 2119: REQ-009.1.1
# 2119: REQ-009.2.1
def test_hook_whole_callback_deadline_bounds_cumulative_slow_responses() -> None:
    request_count = 0

    class _SlowResponses(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _respond_slowly(self) -> None:
            nonlocal request_count
            request_count += 1
            length = int(self.headers.get("content-length", "0"))
            if length:
                self.rfile.read(length)
            time.sleep(1.1)
            body = json.dumps(
                {"briefing": "PHASE BRIEFING"} if self.path.endswith("/briefing") else {}
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            with suppress(BrokenPipeError):
                self.wfile.write(body)

        do_GET = _respond_slowly
        do_PUT = _respond_slowly

    service = ThreadingHTTPServer(("127.0.0.1", 0), _SlowResponses)
    service_thread = threading.Thread(target=service.serve_forever, daemon=True)
    service_thread.start()
    env = {
        key: value
        for key, value in os.environ.items()
        if key.lower() not in {"http_proxy", "https_proxy", "all_proxy"}
    }
    env.update(
        PANOPTICON_SERVICE_URL=f"http://127.0.0.1:{service.server_port}",
        PANOPTICON_TASK_ID="t1",
        NO_PROXY="127.0.0.1",
    )

    started = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "panopticon.container.hook", "agent", "prompt"],
            input="",
            text=True,
            capture_output=True,
            env=env,
            timeout=3.5,
            check=False,
        )
        elapsed = time.monotonic() - started
    finally:
        service.shutdown()
        service.server_close()
        service_thread.join(timeout=1)

    assert request_count == 2
    assert elapsed < 3
    assert completed.returncode == 0
    assert completed.stdout == "" and completed.stderr == ""


# 2119: REQ-009.2.1
@pytest.mark.parametrize(
    ("argv", "malformed_request", "malformed_body", "expected_stdout"),
    [
        (["user"], 1, b"not json", ""),
        (["agent", "prompt"], 3, b"[]", "PHASE BRIEFING\n"),
    ],
)
def test_hook_fails_open_on_malformed_control_plane_responses(
    argv: list[str], malformed_request: int, malformed_body: bytes, expected_stdout: str
) -> None:
    request_count = 0

    class _MalformedResponse(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _respond(self) -> None:
            nonlocal request_count
            request_count += 1
            length = int(self.headers.get("content-length", "0"))
            if length:
                self.rfile.read(length)
            if request_count == malformed_request:
                body = malformed_body
            elif self.path.endswith("/briefing"):
                body = b'{"briefing": "PHASE BRIEFING"}'
            else:
                body = b"{}"
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _respond
        do_PUT = _respond

    service = ThreadingHTTPServer(("127.0.0.1", 0), _MalformedResponse)
    service_thread = threading.Thread(target=service.serve_forever, daemon=True)
    service_thread.start()
    env = {
        key: value
        for key, value in os.environ.items()
        if key.lower() not in {"http_proxy", "https_proxy", "all_proxy"}
    }
    env.update(
        PANOPTICON_SERVICE_URL=f"http://127.0.0.1:{service.server_port}",
        PANOPTICON_TASK_ID="t1",
        NO_PROXY="127.0.0.1",
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "panopticon.container.hook", *argv],
            input="",
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
    finally:
        service.shutdown()
        service.server_close()
        service_thread.join(timeout=1)

    assert request_count == malformed_request
    assert completed.returncode == 0
    assert completed.stdout == expected_stdout and completed.stderr == ""


# 2119: REQ-009.2.1
@pytest.mark.parametrize(
    ("argv", "operation", "failure", "payload"),
    [
        (["user"], "set_turn", "connection", ""),
        (["agent"], "set_turn", "protocol", ""),
        (["user", "stop"], "set_tokens_used", "status", '{"transcript_path": "/missing"}'),
        (["user", "stop"], "set_turn", "timeout", ""),
        (["agent", "prompt"], "set_turn", "status", ""),
        (["agent", "prompt"], "get_briefing", "connection", ""),
        (["agent", "prompt"], "get_task", "protocol", ""),
    ],
)
def test_every_shared_callback_path_fails_open_on_control_plane_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    operation: str,
    failure: str,
    payload: str,
) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    client = _FailingClient(operation, _control_plane_error(failure))

    assert hook.main(argv, client=client, stdin=io.StringIO(payload)) == 0  # type: ignore[arg-type]
    captured = capsys.readouterr()
    expected_stdout = "PHASE BRIEFING: you are in PLANNING\n" if operation == "get_task" else ""
    assert captured.out == expected_stdout
    assert captured.err == ""


# 2119: REQ-009.3.1
def test_every_injected_turn_hook_preserves_its_event_mapping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    claude_hooks = settings()["hooks"]
    assert (
        claude_hooks["Stop"][0]["hooks"][0]["command"]
        == "python -m panopticon.container.hook user stop"
    )
    assert (
        claude_hooks["UserPromptSubmit"][0]["hooks"][0]["command"]
        == "python -m panopticon.container.hook agent prompt"
    )
    assert claude_hooks["PreToolUse"][0]["matcher"] == "AskUserQuestion"
    assert (
        claude_hooks["PreToolUse"][0]["hooks"][0]["command"]
        == "python -m panopticon.container.hook user"
    )
    assert claude_hooks["PostToolUse"][0]["matcher"] == "AskUserQuestion"
    assert (
        claude_hooks["PostToolUse"][0]["hooks"][0]["command"]
        == "python -m panopticon.container.hook agent"
    )

    codex_config = tomllib.loads(render_config("http://svc", "", Path("/workspace")))
    assert (
        codex_config["hooks"]["Stop"][0]["hooks"][0]["command"]
        == "python -m panopticon.container.hook user stop"
    )
    assert (
        codex_config["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        == "python -m panopticon.container.hook agent prompt"
    )

    subprocess_turns: list[str] = []
    subprocess_paths: list[str] = []

    class _ResponsiveService(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _respond(self, body: dict[str, object]) -> None:
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_PUT(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            body = json.loads(self.rfile.read(length))
            subprocess_paths.append(self.path)
            subprocess_turns.append(body["turn"])
            self._respond({})

        def do_GET(self) -> None:
            if self.path.endswith("/briefing"):
                self._respond({"briefing": "PHASE BRIEFING"})
            else:
                self._respond({"id": "t1", "slug": "hook-fail-open"})

    service = ThreadingHTTPServer(("127.0.0.1", 0), _ResponsiveService)
    service_thread = threading.Thread(target=service.serve_forever, daemon=True)
    service_thread.start()
    env = {
        key: value
        for key, value in os.environ.items()
        if key.lower() not in {"http_proxy", "https_proxy", "all_proxy"}
    }
    env.update(
        PANOPTICON_SERVICE_URL=f"http://127.0.0.1:{service.server_port}",
        PANOPTICON_TASK_ID="t1",
        NO_PROXY="127.0.0.1",
    )
    try:
        for command in (
            claude_hooks["Stop"][0]["hooks"][0]["command"],
            claude_hooks["UserPromptSubmit"][0]["hooks"][0]["command"],
        ):
            completed = subprocess.run(
                shlex.split(command),
                input="",
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            assert completed.returncode == 0 and completed.stderr == ""
    finally:
        service.shutdown()
        service.server_close()
        service_thread.join(timeout=1)
    assert subprocess_turns == ["user", "agent"]
    assert subprocess_paths == ["/tasks/t1/turn", "/tasks/t1/turn"]

    client = _FakeClient(slug="hook-fail-open")
    assert hook.main(["user", "stop"], client=client, stdin=io.StringIO("")) == 0  # type: ignore[arg-type]
    live = '{"background_tasks": [{"status": "running"}]}'
    assert hook.main(["user", "stop"], client=client, stdin=io.StringIO(live)) == 0  # type: ignore[arg-type]
    completed_background = '{"background_tasks": [{"status": "completed"}]}'
    assert (
        hook.main(  # type: ignore[arg-type]
            ["user", "stop"], client=client, stdin=io.StringIO(completed_background)
        )
        == 0
    )
    assert hook.main(["agent", "prompt"], client=client, stdin=io.StringIO("")) == 0  # type: ignore[arg-type]
    assert hook.main(["user"], client=client) == 0  # type: ignore[arg-type]
    assert hook.main(["agent"], client=client) == 0  # type: ignore[arg-type]
    assert client.calls == [
        ("t1", "user"),
        ("t1", "user"),
        ("t1", "agent"),
        ("t1", "user"),
        ("t1", "agent"),
    ]

    pi_harness = PiHarness()
    pi_harness.bootstrap(
        BootstrapContext(
            home=tmp_path,
            cwd=Path("/workspace"),
            service_url="http://svc",
            task_id="t1",
        )
    )
    extension_path = tmp_path / ".pi" / EXTENSION_FILE
    assert pi_harness.argv(LaunchContext(home=tmp_path, cwd=Path("/workspace"))) == [
        "pi",
        "--extension",
        str(extension_path),
    ]
    source = extension_path.read_text().replace(
        "export default function", "const extension = function"
    )
    probe = (
        source
        + """
const handlers = {};
const turns = [];
const pi = { on(event, handler) { handlers[event] = handler; } };
globalThis.fetch = (url, options) => {
  if (url !== "http://svc/tasks/t1/turn") throw new Error(`unexpected URL: ${url}`);
  if (options.method !== "PUT") throw new Error(`unexpected method: ${options.method}`);
  turns.push(JSON.parse(options.body).turn);
  return Promise.resolve({ ok: true });
};
extension(pi);
await handlers.agent_end();
await handlers.input();
if (JSON.stringify(turns) !== JSON.stringify(["user", "agent"])) {
  throw new Error(`unexpected pi turns: ${turns}`);
}
"""
    )
    subprocess.run(
        ["node", "--input-type=module", "--eval", probe],
        check=True,
        env={
            **os.environ,
            "PANOPTICON_SERVICE_URL": "http://svc",
            "PANOPTICON_TASK_ID": "t1",
        },
    )


def test_hook_flips_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    client = _FakeClient(slug="fix-widget")  # slugged → no nudge, just the turn flip
    assert hook.main(["user"], client=client, stdin=io.StringIO("")) == 0  # type: ignore[arg-type]
    assert hook.main(["agent"], client=client) == 0  # type: ignore[arg-type]
    assert client.calls == [("t1", "user"), ("t1", "agent")]


def test_bare_flip_is_a_pure_turn_change_with_no_side_effects(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The AskUserQuestion hooks pass no event arg: they only flip the turn — no briefing/nudge to
    # the agent's context (unslugged, which would otherwise nudge), no token report (stdin ignored).
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    client = _FakeClient(slug=None)
    assert hook.main(["agent"], client=client) == 0  # type: ignore[arg-type]
    assert hook.main(["user"], client=client, stdin=io.StringIO('{"transcript_path": "x"}')) == 0  # type: ignore[arg-type]
    assert client.calls == [("t1", "agent"), ("t1", "user")]  # turns flipped
    assert capsys.readouterr().out == "" and client.tokens == []  # nothing else happened


def test_hook_rejects_unknown_event() -> None:
    assert hook.main(["nonsense"], client=_FakeClient()) == 2  # type: ignore[arg-type]
    assert (
        hook.main(["user", "bogus"], client=_FakeClient()) == 2
    )  # bad event arg  # type: ignore[arg-type]
    assert (
        hook.main(["user", "prompt", "extra"], client=_FakeClient()) == 2
    )  # too many args  # type: ignore[arg-type]


def test_user_turn_briefs_the_phase_and_nudges_provision_while_unslugged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    assert hook.main(["agent", "prompt"], client=_FakeClient(slug=None)) == 0  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert "PHASE BRIEFING" in out  # the current-phase briefing reaches the agent's context
    assert "provision" in out  # and, unslugged, the provisioning nudge


def test_briefing_prints_but_no_nudge_once_slugged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    hook.main(["agent", "prompt"], client=_FakeClient(slug="fix-widget"))  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert (
        "PHASE BRIEFING" in out and "provision" not in out
    )  # briefing always; nudge only unslugged


def test_stop_hook_is_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    # Stop hook: flips the turn and reports tokens, but emits nothing to the agent's context.
    hook.main(["user", "stop"], client=_FakeClient(slug=None), stdin=io.StringIO(""))  # type: ignore[arg-type]
    assert capsys.readouterr().out == ""


def _transcript(tmp_path: Path) -> Path:
    """A small claude-style JSONL transcript: two assistant lines with usage (totalling 693),
    plus lines the summer must ignore — a non-assistant line, an assistant line with no usage,
    a blank line, and malformed JSON.

    Weighted totals (input×1 + output×5 + cache_creation×1.25 + cache_read×0.1):
      line 1: 100 + 250 + 12.5 + 0.5 = 363
      line 2: 200 + 100 + 0    + 30  = 330  → total 693
    """
    lines = [
        {
            "type": "assistant",
            "message": {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_creation_input_tokens": 10,
                    "cache_read_input_tokens": 5,
                }
            },
        },  # 363
        {"type": "user", "message": {"content": "hi"}},  # no usage
        "",
        "not json at all",
        {"type": "assistant", "message": {"role": "assistant"}},  # assistant, no usage
        {
            "type": "assistant",
            "message": {
                "usage": {"input_tokens": 200, "output_tokens": 20, "cache_read_input_tokens": 300}
            },
        },  # 330
    ]
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines))
    return path


def test_session_tokens_sums_all_tiers_across_assistant_lines(tmp_path: Path) -> None:
    assert hook.session_tokens(str(_transcript(tmp_path))) == 693  # 363 + 330


def test_session_tokens_is_zero_for_missing_or_empty_transcript(tmp_path: Path) -> None:
    assert hook.session_tokens(str(tmp_path / "nope.jsonl")) == 0  # no file → no crash
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert hook.session_tokens(str(empty)) == 0


def test_stop_hook_reports_session_tokens_from_the_transcript(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    client = _FakeClient(slug="fix-widget")
    stdin = io.StringIO(json.dumps({"transcript_path": str(_transcript(tmp_path))}))
    assert hook.main(["user", "stop"], client=client, stdin=stdin) == 0  # type: ignore[arg-type]
    assert client.calls == [("t1", "user")]  # turn still flipped
    assert client.tokens == [("t1", 693)]  # and the session total recorded


def test_stop_hook_tolerates_stdin_without_a_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    client = _FakeClient(slug="fix-widget")
    assert hook.main(["user", "stop"], client=client, stdin=io.StringIO("{}")) == 0  # type: ignore[arg-type]
    assert client.calls == [("t1", "user")] and client.tokens == []  # no transcript → no report


# -- background-task gate: don't hand the turn back while background work is still running -------


def _stop(client: _FakeClient, payload: str) -> int:
    """Run the Stop hook feeding `payload` as the hook's stdin JSON."""
    return hook.main(["user", "stop"], client=client, stdin=io.StringIO(payload))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "payload",
    [
        '{"background_tasks": [{"id": "t", "type": "shell", "status": "running"}]}',
        '{"background_tasks": [{"id": "m", "type": "monitor", "status": "running"}]}',  # Monitor tool
        '{"background_tasks": [{"id": "a", "type": "subagent", "status": "running"}]}',  # background agent
        '{"background_tasks": [{"id": "w", "type": "workflow", "status": "running"}]}',  # background workflow
        '{"background_tasks": [{"id": "t", "status": "completed"}, {"id": "u", "status": "running"}]}',
        '{"background_tasks": [{"id": "t"}]}',  # no status → treated as live (conservative)
    ],
)
def test_stop_does_not_flip_while_a_background_task_is_live(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    client = _FakeClient(slug="fix-widget")
    assert _stop(client, payload) == 0
    assert client.calls == []  # turn left on the agent — set_turn not called


@pytest.mark.parametrize(
    "payload",
    [
        "",  # empty stdin
        "   ",  # blank stdin
        "not json",  # unparseable
        "[]",  # JSON, but not an object
        "{}",  # object without the field
        '{"background_tasks": []}',  # field present, nothing running
        '{"background_tasks": [{"id": "t", "status": "completed"}]}',  # only terminal entries
        '{"background_tasks": [{"id": "t", "status": "FAILED"}]}',  # terminal, case-insensitive
        '{"background_tasks": "oops"}',  # field present but wrong type → degrade, flip
    ],
)
def test_stop_flips_to_user_when_no_live_background_task(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    client = _FakeClient(slug="fix-widget")
    assert _stop(client, payload) == 0
    assert client.calls == [("t1", "user")]  # degrades to the original turn flip


def test_background_task_does_not_suppress_the_askuserquestion_flip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The gate is the *Stop* event only. AskUserQuestion's bare `hook user` flip means the agent is
    # genuinely waiting on the user, so it must flip to user even while a background task runs.
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    client = _FakeClient(slug="fix-widget")
    payload = '{"background_tasks": [{"id": "t", "status": "running"}]}'
    assert hook.main(["user"], client=client, stdin=io.StringIO(payload)) == 0  # type: ignore[arg-type]
    assert client.calls == [("t1", "user")]


# 2119: REQ-008.4.1
# 2119: REQ-008.5.1
def test_user_prompt_submit_unaffected_by_background_tasks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The gate is Stop-only: UserPromptSubmit (agent) always flips and still briefs, even if the
    # payload carries running background tasks.
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    client = _FakeClient(slug="fix-widget")
    payload = '{"background_tasks": [{"id": "t", "status": "running"}]}'
    assert hook.main(["agent", "prompt"], client=client, stdin=io.StringIO(payload)) == 0  # type: ignore[arg-type]
    assert client.calls == [("t1", "agent")]
    assert "PHASE BRIEFING" in capsys.readouterr().out


# 2119: REQ-008.4.1
# 2119: REQ-008.5.1
def test_user_prompt_submit_waits_for_the_turn_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    entered = threading.Event()
    release = threading.Event()
    result: list[int] = []

    class _GatedClient(_FakeClient):
        def set_turn(self, task_id: str, turn: str) -> dict[str, object]:
            entered.set()
            assert release.wait(timeout=5), "test did not release the turn write"
            return super().set_turn(task_id, turn)

    client = _GatedClient(slug="fix-widget")
    thread = threading.Thread(
        target=lambda: result.append(
            hook.main(["agent", "prompt"], client=client, stdin=io.StringIO(""))  # type: ignore[arg-type]
        ),
        daemon=True,
    )
    thread.start()
    assert entered.wait(timeout=5), "turn write was never attempted"
    assert thread.is_alive(), "hook returned while the turn write was still pending"

    release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert result == [0]
    assert client.calls == [("t1", "agent")]
