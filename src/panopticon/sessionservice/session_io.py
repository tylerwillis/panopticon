"""Runner-owned delivery to and capture from task tmux sessions."""

from __future__ import annotations

import re
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from panopticon.sessionservice.prefill import DEFAULT_WAKE_TIMEOUT, prefill_pane

FAILURE_REASON = "tmux-delivery-failed"
MAX_TRANSCRIPT_BYTES = 64 * 1024
MAX_TRANSCRIPT_LINES = 200

_ANSI = re.compile(
    r"\x1b(?:"
    r"\[[0-?]*[ -/]*[@-~]|"
    r"\][^\x07\x1b]*(?:\x07|\x1b\\)|"
    r"[P\^_X][\s\S]*?\x1b\\|"
    r"[@-_0-?]"
    r")"
)


class _Client(Protocol):
    def pending_session_input(self, task_id: str, runner_id: str) -> list[dict[str, Any]]: ...

    def settle_session_input(
        self, task_id: str, delivery_id: str, status: str, failure_reason: str | None
    ) -> None: ...

    def publish_session_transcript(self, task_id: str, snapshot: dict[str, Any]) -> None: ...


class _Runner(Protocol):
    def deliver_session_input(
        self, task_id: str, text: str, *, submit: bool
    ) -> tuple[bool, str | None]: ...

    def capture_session_transcript(self, task_id: str) -> dict[str, Any] | None: ...


def deliver_pane_input(
    session: str,
    text: str,
    *,
    submit: bool,
    run: Callable[..., str],
    raw_log: str,
    timeout: float = DEFAULT_WAKE_TIMEOUT,
    sleep: Callable[[float], None],
    prefix: Sequence[str] = ("tmux",),
) -> tuple[bool, str | None]:
    """Use the pre-armed bracketed-paste watcher and map all failures to one public reason."""
    prompt: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as handle:
            handle.write(text)
            prompt = Path(handle.name)
        delivered = prefill_pane(
            session,
            str(prompt),
            run=run,
            sleep=sleep,
            raw_log=raw_log,
            timeout=timeout,
            submit=submit,
            watch=False,
            settle_delay=0,
            prefix=prefix,
        )
        return (True, None) if delivered else (False, FAILURE_REASON)
    except (OSError, ValueError):
        return False, FAILURE_REASON
    finally:
        if prompt is not None:
            prompt.unlink(missing_ok=True)


def capture_pane_snapshot(
    session: str,
    *,
    run: Callable[..., str],
    prefix: Sequence[str] = ("tmux",),
) -> dict[str, Any] | None:
    """Capture and bound a plain-text pane suffix, preserving valid Unicode."""
    try:
        captured = run([*prefix, "capture-pane", "-p", "-S", "-200", "-t", session])
        dimensions = run(
            [
                *prefix,
                "display-message",
                "-p",
                "-t",
                session,
                "#{pane_width}\t#{pane_height}",
            ]
        ).strip()
        columns_text, rows_text = dimensions.split("\t", 1)
        columns, rows = int(columns_text), int(rows_text)
    except (OSError, ValueError):
        return None

    lines = captured.splitlines()
    newest = lines[-MAX_TRANSCRIPT_LINES:]
    plain = "\n".join(_ANSI.sub("", line) for line in newest)
    encoded = plain.encode("utf-8")
    truncated = len(lines) > MAX_TRANSCRIPT_LINES or len(encoded) > MAX_TRANSCRIPT_BYTES
    text = encoded[-MAX_TRANSCRIPT_BYTES:].decode("utf-8", errors="ignore")
    return {
        "text": text,
        "columns": columns,
        "rows": rows,
        "truncated": truncated,
    }


class SessionIOWorker:
    """Asynchronously drain pending input and publish snapshots for this runner's live tasks."""

    def __init__(
        self,
        client: _Client,
        runner: _Runner,
        *,
        runner_id: str,
        dispatch: Callable[[Callable[[], None]], object] | None = None,
    ) -> None:
        self._client = client
        self._runner = runner
        self._runner_id = runner_id
        self._dispatch = dispatch or (
            lambda call: threading.Thread(target=call, daemon=True).start()
        )
        self._inflight: set[str] = set()
        self._lock = threading.Lock()

    def process(self, task: Mapping[str, Any]) -> None:
        if (
            task.get("claimed_by") != self._runner_id
            or task.get("container_status") != "live"
            or task.get("turn") != "user"
        ):
            return
        task_id = str(task["id"])
        with self._lock:
            if task_id in self._inflight:
                return
            self._inflight.add(task_id)

        def work() -> None:
            try:
                for delivery in self._client.pending_session_input(task_id, self._runner_id):
                    ok, reason = self._runner.deliver_session_input(
                        task_id, str(delivery["text"]), submit=bool(delivery["submit"])
                    )
                    self._client.settle_session_input(
                        task_id,
                        str(delivery["id"]),
                        "delivered" if ok else "failed",
                        None if ok else (reason or FAILURE_REASON),
                    )
                if snapshot := self._runner.capture_session_transcript(task_id):
                    self._client.publish_session_transcript(task_id, snapshot)
            finally:
                with self._lock:
                    self._inflight.discard(task_id)

        self._dispatch(work)
