"""Executable contract for runner-owned session I/O."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from panopticon.sessionservice.prefill import BRACKETED_PASTE_ON, prefill_pane


@dataclass
class _Delivery:
    id: str = "delivery-1"
    text: str = "hello\nworld"
    submit: bool = False


class _Client:
    def __init__(self, delivery: _Delivery | None = None) -> None:
        self.delivery = delivery
        self.settlements: list[tuple[str, str, str | None]] = []
        self.transcripts: list[dict[str, Any]] = []
        self.task = {
            "id": "t1",
            "claimed_by": "host-1",
            "container_status": "live",
            "turn": "user",
        }

    def get_task(self, task_id: str) -> dict[str, Any]:
        del task_id
        return self.task

    def pending_session_input(self, task_id: str, runner_id: str) -> list[dict[str, Any]]:
        del task_id, runner_id
        return [] if self.delivery is None else [vars(self.delivery)]

    def settle_session_input(
        self,
        task_id: str,
        delivery_id: str,
        status: str,
        failure_reason: str | None,
        runner_id: str,
    ) -> None:
        del task_id, runner_id
        self.settlements.append((delivery_id, status, failure_reason))

    def publish_session_transcript(
        self, task_id: str, snapshot: dict[str, Any], runner_id: str
    ) -> None:
        del task_id, runner_id
        self.transcripts.append(snapshot)


class _Runner:
    def __init__(self, outcome: tuple[bool, str | None] = (True, None)) -> None:
        self.outcome = outcome
        self.deliveries: list[tuple[str, str, bool]] = []
        self.captures: list[str] = []
        self.capture = {"text": "recent λ", "columns": 80, "rows": 24, "truncated": False}

    def deliver_session_input(
        self, task_id: str, text: str, *, submit: bool
    ) -> tuple[bool, str | None]:
        self.deliveries.append((task_id, text, submit))
        return self.outcome

    def capture_session_transcript(self, task_id: str) -> dict[str, Any] | None:
        self.captures.append(task_id)
        return self.capture


@pytest.mark.parametrize("submit", [False, True])
def test_worker_delivers_only_for_live_owned_user_turn(submit: bool) -> None:
    # 2119: REQ-044.3.1
    # 2119: REQ-044.3.2
    # 2119: REQ-044.4.2
    # 2119: REQ-044.5.1
    # 2119: REQ-044.5.2
    # 2119: REQ-044.6.2
    from panopticon.sessionservice.session_io import SessionIOWorker

    delivery = _Delivery(submit=submit)
    client, runner = _Client(delivery), _Runner()
    worker = SessionIOWorker(client, runner, runner_id="host-1", dispatch=lambda call: call())
    eligible = {"id": "t1", "claimed_by": "host-1", "container_status": "live", "turn": "user"}
    worker.process(eligible)
    assert runner.deliveries == [("t1", "hello\nworld", submit)]
    assert client.settlements == [("delivery-1", "delivered", None)]

    for changed in ({"claimed_by": "host-2"}, {"container_status": "down"}, {"turn": "agent"}):
        blocked_runner = _Runner()
        SessionIOWorker(
            _Client(delivery), blocked_runner, runner_id="host-1", dispatch=lambda call: call()
        ).process(eligible | changed)
        assert blocked_runner.deliveries == []
        assert blocked_runner.captures == ([] if "turn" not in changed else ["t1"])


@pytest.mark.parametrize(
    "changed",
    [
        {"turn": "agent"},
        {"claimed_by": "host-2"},
        {"container_status": "down"},
    ],
)
def test_worker_revalidates_authoritative_task_before_each_delivery(
    changed: dict[str, str],
) -> None:
    # 2119: REQ-044.5.1
    from panopticon.sessionservice.session_io import SessionIOWorker

    class ChangingClient(_Client):
        def __init__(self) -> None:
            super().__init__()
            self.deliveries = [
                vars(_Delivery(id="delivery-1")),
                vars(_Delivery(id="delivery-2")),
            ]
            self.reads = 0

        def pending_session_input(self, task_id: str, runner_id: str) -> list[dict[str, Any]]:
            del task_id, runner_id
            return self.deliveries

        def get_task(self, task_id: str) -> dict[str, Any]:
            del task_id
            self.reads += 1
            if self.reads == 1:
                return self.task
            return self.task | changed

    client, runner = ChangingClient(), _Runner()
    SessionIOWorker(client, runner, runner_id="host-1", dispatch=lambda call: call()).process(
        client.task
    )
    assert runner.deliveries == [("t1", "hello\nworld", False)]
    assert client.settlements == [("delivery-1", "delivered", None)]


@pytest.mark.parametrize(
    "changed",
    [
        {"turn": "agent"},
        {"claimed_by": "host-2"},
        {"container_status": "down"},
    ],
)
def test_worker_rejects_stale_eligible_snapshot_before_first_delivery(
    changed: dict[str, str],
) -> None:
    # 2119: REQ-044.5.1
    from panopticon.sessionservice.session_io import SessionIOWorker

    client, runner = _Client(_Delivery()), _Runner()
    client.task = client.task | changed
    SessionIOWorker(client, runner, runner_id="host-1", dispatch=lambda call: call()).process(
        {"id": "t1", "claimed_by": "host-1", "container_status": "live", "turn": "user"}
    )
    assert runner.deliveries == []
    assert client.settlements == []


@pytest.mark.parametrize("submit", [False, True])
def test_prefill_stages_or_submits_with_exact_tmux_commands(tmp_path: Path, submit: bool) -> None:
    # 2119: REQ-044.3.1
    # 2119: REQ-044.3.2
    prompt, raw = tmp_path / "prompt", tmp_path / "ready"
    prompt.write_text("hello\nworld")
    raw.write_bytes(BRACKETED_PASTE_ON)
    calls: list[list[str]] = []

    def run(args: list[str], *, check: bool = True) -> str:
        del check
        calls.append(list(args))
        if "load-buffer" in args:
            assert Path(args[-1]).read_text() == "hello\nworld"
        return "%1\n" if "display-message" in args else ""

    assert prefill_pane(
        "sess",
        str(prompt),
        run=run,
        sleep=lambda _: None,
        raw_log=str(raw),
        timeout=1,
        submit=submit,
        watch=False,
        settle_delay=0,
    )
    assert sum("paste-buffer" in call for call in calls) == 1
    paste = next(call for call in calls if "paste-buffer" in call)
    assert "-p" in paste
    load = next(call for call in calls if "load-buffer" in call)
    buffer_name = load[load.index("-b") + 1]
    assert paste[paste.index("-b") + 1] == buffer_name
    assert calls.index(load) < calls.index(paste)
    assert sum("send-keys" in call for call in calls) == int(submit)
    if submit:
        enter = next(call for call in calls if "send-keys" in call)
        assert enter == ["tmux", "send-keys", "-t", "%1", "Enter"]
        assert enter.count("Enter") == 1
        assert calls.index(paste) < calls.index(enter)


def test_remote_delivery_preserves_nonempty_whitespace_input(tmp_path: Path) -> None:
    # 2119: REQ-044.2.3
    from panopticon.sessionservice.session_io import deliver_pane_input

    raw = tmp_path / "ready"
    raw.write_bytes(BRACKETED_PASTE_ON)
    loaded: list[str] = []

    def run(args: list[str], *, check: bool = True) -> str:
        del check
        if "display-message" in args:
            return "%1\n"
        if "load-buffer" in args:
            loaded.append(Path(args[-1]).read_text())
        return ""

    assert deliver_pane_input(
        "sess",
        " \n",
        submit=False,
        run=run,
        raw_log=str(raw),
        sleep=lambda _: None,
    ) == (True, None)
    assert loaded == [" \n"]


def test_worker_records_stable_delivery_failure() -> None:
    # 2119: REQ-044.5.3
    from panopticon.sessionservice.session_io import SessionIOWorker

    client, runner = _Client(_Delivery()), _Runner((False, "tmux-delivery-failed"))
    SessionIOWorker(client, runner, runner_id="host-1", dispatch=lambda call: call()).process(
        {"id": "t1", "claimed_by": "host-1", "container_status": "live", "turn": "user"}
    )
    assert client.settlements == [("delivery-1", "failed", "tmux-delivery-failed")]


@pytest.mark.parametrize("submit,fail", [(False, False), (True, False), (True, True)])
def test_worker_settlement_uses_local_runner_prefill_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, submit: bool, fail: bool
) -> None:
    # 2119: REQ-044.3.1
    # 2119: REQ-044.3.2
    # 2119: REQ-044.5.3
    from panopticon.sessionservice.local_runner import LocalRunner
    from panopticon.sessionservice.session_io import SessionIOWorker

    raw = tmp_path / "ready"
    raw.write_bytes(BRACKETED_PASTE_ON)
    calls: list[list[str]] = []

    def run(
        args: list[str],
        *,
        check: bool = True,
        interactive: bool = False,
        verbose: bool = False,
    ) -> str:
        del check, interactive, verbose
        calls.append(list(args))
        if "display-message" in args:
            return "%1\n"
        if fail and "paste-buffer" in args:
            raise OSError("tmux failed")
        return ""

    monkeypatch.setattr(
        "panopticon.sessionservice.local_runner.readiness_log", lambda _session: str(raw)
    )
    client = _Client(_Delivery(submit=submit))
    runner = LocalRunner("http://svc:8000", runner_id="host-1", run=run)
    SessionIOWorker(client, runner, runner_id="host-1", dispatch=lambda call: call()).process(
        client.task
    )
    assert any("load-buffer" in call for call in calls)
    assert any("paste-buffer" in call and "-p" in call for call in calls)
    assert sum("send-keys" in call for call in calls) == int(submit and not fail)
    expected = (
        ("delivery-1", "failed", "tmux-delivery-failed")
        if fail
        else ("delivery-1", "delivered", None)
    )
    assert client.settlements == [expected]


@pytest.mark.parametrize(
    "panes,failing_command",
    [
        ([""], None),
        (["%1", ""], None),
        (["%1"] * 4, None),
        (["%1"] * 2, "display-message"),
        (["%1"] * 2, "load-buffer"),
        (["%1"] * 2, "paste-buffer"),
        (["%1"] * 2, "send-keys"),
    ],
)
def test_actual_tmux_delivery_failures_map_to_one_stable_reason(
    tmp_path: Path, panes: list[str], failing_command: str | None
) -> None:
    # 2119: REQ-044.5.3
    from panopticon.sessionservice.session_io import deliver_pane_input

    raw = tmp_path / "ready"
    raw.write_bytes(BRACKETED_PASTE_ON if len(panes) < 4 else b"")

    def run(args: list[str], *, check: bool = True) -> str:
        del check
        if failing_command is not None and failing_command in args:
            raise OSError("tmux failed")
        if "display-message" in args:
            return (panes.pop(0) if panes else "") + "\n"
        return ""

    assert deliver_pane_input(
        "sess",
        "text",
        submit=True,
        run=run,
        raw_log=str(raw),
        timeout=2,
        sleep=lambda _: None,
    ) == (False, "tmux-delivery-failed")


@pytest.mark.parametrize("submit", [False, True])
def test_settled_idempotent_request_is_not_delivered_twice(submit: bool) -> None:
    # 2119: REQ-044.5.5
    from panopticon.sessionservice.session_io import SessionIOWorker

    class SettlingClient(_Client):
        def settle_session_input(
            self,
            task_id: str,
            delivery_id: str,
            status: str,
            failure_reason: str | None,
            runner_id: str,
        ) -> None:
            super().settle_session_input(task_id, delivery_id, status, failure_reason, runner_id)
            self.delivery = None

    client, runner = SettlingClient(_Delivery(submit=submit)), _Runner()
    worker = SessionIOWorker(client, runner, runner_id="host-1", dispatch=lambda call: call())
    task = {"id": "t1", "claimed_by": "host-1", "container_status": "live", "turn": "user"}
    worker.process(task)
    worker.process(task)
    assert runner.deliveries == [("t1", "hello\nworld", submit)]


def test_worker_publishes_transcript_only_for_its_live_task() -> None:
    # 2119: REQ-044.6.2
    from panopticon.sessionservice.session_io import SessionIOWorker

    client, runner = _Client(), _Runner()
    worker = SessionIOWorker(client, runner, runner_id="host-1", dispatch=lambda call: call())
    worker.process({"id": "t1", "claimed_by": "host-1", "container_status": "live", "turn": "user"})
    assert client.transcripts == [runner.capture]

    worker.process(
        {"id": "t1", "claimed_by": "host-1", "container_status": "live", "turn": "agent"}
    )
    assert client.transcripts == [runner.capture, runner.capture]
    client.task = client.task | {"claimed_by": "host-2"}
    worker.process(
        {"id": "t1", "claimed_by": "host-1", "container_status": "live", "turn": "agent"}
    )
    assert client.transcripts == [runner.capture, runner.capture]
    worker.process({"id": "t2", "claimed_by": "host-2", "container_status": "live", "turn": "user"})
    assert client.transcripts == [runner.capture, runner.capture]


@pytest.mark.parametrize(
    "lines",
    [
        [f"line-{index}" for index in range(260)],
        [f"byte-{index}-{'λ' * 400}" for index in range(100)],
        ["λ" * 40000],
    ],
)
def test_pane_capture_keeps_newest_200_lines_and_64_kib_without_ansi(lines: list[str]) -> None:
    # 2119: REQ-044.7.1
    # 2119: REQ-044.7.5
    from panopticon.sessionservice.session_io import capture_pane_snapshot

    controls = (
        "\x1b[31m\x1b[0m\x1b]0;title\x07\x1bPdata\x1b\\\x1bXx\x1b\\\x1b^x\x1b\\\x1b_x\x1b\\\x1b7"
    )
    lines = [f"{line}{controls}λ" for line in lines]

    def run(args: list[str], *, check: bool = True) -> str:
        del check
        if "capture-pane" in args:
            return "\n".join(lines)
        if any("pane_width" in arg for arg in args):
            return "100\t40\n"
        raise AssertionError(args)

    snapshot = capture_pane_snapshot("panopticon-t1", run=run, prefix=("tmux", "-L", "panopticon"))
    assert snapshot is not None
    expected = [line.split("\x1b", 1)[0] + "λ" for line in lines[-200:]]
    expected_bytes = "\n".join(expected).encode("utf-8")
    expected_text = expected_bytes[-65536:].decode("utf-8", errors="ignore")
    assert snapshot["text"] == expected_text
    assert "\x1b[" not in snapshot["text"] and "λ" in snapshot["text"]
    assert "\x1b]" not in snapshot["text"]
    assert "\x1b" not in snapshot["text"]
    assert snapshot | {"columns": 100, "rows": 40, "truncated": True} == snapshot
