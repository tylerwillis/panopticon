"""Acceptance tests for correctness-first Stop handling and deferred token telemetry."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from panopticon.container import hook
from panopticon.harnesses.claude import settings
from panopticon.harnesses.codex import render_config


class _RecordingClient:
    def __init__(self) -> None:
        self.turns: list[tuple[str, str]] = []
        self.tokens: list[tuple[str, int]] = []

    def set_turn(self, task_id: str, turn: str) -> dict[str, object]:
        self.turns.append((task_id, turn))
        return {}

    def set_tokens_used(self, task_id: str, tokens_used: int) -> dict[str, object]:
        self.tokens.append((task_id, tokens_used))
        return {}


# 2119: stop-hook-turn-before-telemetry.1.1
# 2119: stop-hook-turn-before-telemetry.1.2
# 2119: stop-hook-turn-before-telemetry.2.1
# 2119: stop-hook-turn-before-telemetry.2.2
# 2119: stop-hook-turn-before-telemetry.2.3
def test_long_transcript_cannot_delay_turn_and_is_reported_after_hook_exit(tmp_path: Path) -> None:
    """A >1.4 MB transcript remains open beyond the old deadline.

    A FIFO makes the expensive path deterministic on fast and slow CI machines: the complete
    transcript is valid JSONL, but EOF arrives only after the old three-second hook budget. The
    command receives no timeout signal or error from a harness. It must nevertheless update the
    turn and exit first, while detached accounting later consumes the complete transcript and
    reports its token total.
    """
    requests: list[tuple[str, dict[str, object]]] = []
    turn_seen = threading.Event()
    tokens_seen = threading.Event()
    transcript_reader_opened = threading.Event()
    parsing_had_started_at_turn: list[bool] = []

    class _TaskService(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_PUT(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            body = json.loads(self.rfile.read(length))
            requests.append((self.path, body))
            if self.path.endswith("/turn"):
                parsing_had_started_at_turn.append(transcript_reader_opened.is_set())
                turn_seen.set()
            elif self.path.endswith("/tokens-used"):
                tokens_seen.set()
            response = b"{}"
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    service = ThreadingHTTPServer(("127.0.0.1", 0), _TaskService)
    service_thread = threading.Thread(target=service.serve_forever, daemon=True)
    service_thread.start()

    transcript = tmp_path / "long-session.jsonl"
    os.mkfifo(transcript)
    transcript_lines = [
        json.dumps(
            {
                "type": "assistant",
                "padding": "x" * 1_500_000,
                "message": {
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cache_creation_input_tokens": 10,
                        "cache_read_input_tokens": 5,
                    }
                },
            }
        ),
        json.dumps({"type": "user", "message": {"content": "continue"}}),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 200,
                        "output_tokens": 20,
                        "cache_read_input_tokens": 300,
                    }
                },
            }
        ),
    ]
    transcript_bytes = ("\n".join(transcript_lines) + "\n").encode()
    writer_done = threading.Event()

    def write_slow_complete_transcript() -> None:
        try:
            with transcript.open("wb", buffering=0) as stream:
                # Opening the FIFO for writing completes only once transcript accounting has
                # opened the read side: this observes the exact "parsing begins" boundary.
                transcript_reader_opened.set()
                stream.write(transcript_bytes)
                time.sleep(3.2)
        except BrokenPipeError:
            pass
        finally:
            writer_done.set()

    writer = threading.Thread(target=write_slow_complete_transcript, daemon=True)
    writer.start()
    env = os.environ.copy()
    env.update(
        PANOPTICON_SERVICE_URL=f"http://127.0.0.1:{service.server_port}",
        PANOPTICON_TASK_ID="t1",
        NO_PROXY="127.0.0.1",
    )
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "panopticon.container.hook", "user", "stop"],
            input=json.dumps({"transcript_path": str(transcript)}),
            text=True,
            capture_output=True,
            env=env,
            timeout=4,
            check=False,
        )
        hook_elapsed = time.monotonic() - started

        assert completed.returncode == 0
        assert hook_elapsed < 3
        assert turn_seen.wait(timeout=0.2)
        assert requests[0] == ("/tasks/t1/turn", {"turn": "user"})
        assert parsing_had_started_at_turn == [False]
        assert not writer_done.is_set(), "accounting workload must still exceed the hook budget"

        assert tokens_seen.wait(timeout=5)
        # 363 for the first assistant line plus 330 for the second: complete cumulative,
        # cost-weighted accounting across input, output, cache-create, and cache-read tiers.
        assert requests[-1] == ("/tasks/t1/tokens-used", {"tokens_used": 693})
    finally:
        service.shutdown()
        service.server_close()
        service_thread.join(timeout=1)
        writer.join(timeout=5)


# 2119: stop-hook-turn-before-telemetry.1.3
def test_live_background_work_preserves_turn_without_dropping_token_accounting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The injected client keeps this unit test deterministic; production lifetime isolation is
    # exercised by the process-level long-transcript test above.
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(json.dumps({"message": {"usage": {"input_tokens": 11}}}) + "\n")
    client = _RecordingClient()
    payload = {
        "transcript_path": str(transcript),
        "background_tasks": [{"id": "bg", "status": "running"}],
    }

    assert (
        hook.main(  # type: ignore[arg-type]
            ["user", "stop"], client=client, stdin=io.StringIO(json.dumps(payload))
        )
        == 0
    )
    assert client.turns == []
    assert client.tokens == [("t1", 11)]


# 2119: stop-hook-turn-before-telemetry.3.1
def test_claude_stop_hook_independently_retains_bounded_correctness_first_callback() -> None:
    stop = settings()["hooks"]["Stop"][0]["hooks"][0]
    assert stop == {
        "type": "command",
        "command": "python -m panopticon.container.hook user stop",
        "timeout": 3,
    }


# 2119: stop-hook-turn-before-telemetry.3.2
def test_codex_stop_hook_independently_retains_bounded_correctness_first_callback() -> None:
    config = tomllib.loads(render_config("http://svc", "", Path("/workspace")))
    stop = config["hooks"]["Stop"][0]["hooks"][0]
    assert stop == {
        "type": "command",
        "command": "python -m panopticon.container.hook user stop",
        "timeout": 3,
    }
