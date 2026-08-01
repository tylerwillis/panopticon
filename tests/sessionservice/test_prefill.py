"""The tmux input-box primitive used by stage-entry wake. No real tmux, Docker, or LLM."""

from __future__ import annotations

import shlex
import shutil
import stat
import subprocess
import time
import uuid
from collections.abc import Sequence
from pathlib import Path

import pytest

from panopticon.sessionservice.local_runner import LocalRunner
from panopticon.sessionservice.prefill import (
    BRACKETED_PASTE_ON,
    prefill_pane,
    readiness_log,
    readiness_watch_command,
    watch_pane,
)


class _Tmux:
    """Record commands and return one configured pane id per display-message call."""

    def __init__(self, panes: Sequence[str] = ("%1",)) -> None:
        self.calls: list[list[str]] = []
        self._panes = list(panes)

    def __call__(self, args: Sequence[str], *, check: bool = True) -> str:
        self.calls.append(list(args))
        if "display-message" in args:
            return (self._panes.pop(0) if self._panes else "") + "\n"
        return ""


class _ReadyTmux(_Tmux):
    """Make LocalRunner's real prefill path observe a ready pane and capture its buffer text."""

    def __init__(self) -> None:
        super().__init__(panes=["%1"] * 4)
        self.loaded_text: str | None = None

    def __call__(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        interactive: bool = False,
        verbose: bool = False,
    ) -> str:
        result = super().__call__(args, check=check)
        if "pipe-pane" in args and "-O" in args:
            raw_log = shlex.split(args[-1])[-1]
            Path(raw_log).write_bytes(BRACKETED_PASTE_ON)
        if "load-buffer" in args:
            self.loaded_text = Path(args[-1]).read_text()
        return result


def _prompt(tmp_path: Path, text: str = "You have entered WORKING.\nBuild the feature.") -> Path:
    path = tmp_path / "prompt.txt"
    path.write_text(text)
    return path


def _watch_command(raw: Path) -> str:
    return readiness_watch_command(str(raw))


def test_ready_pane_gets_one_bracketed_paste_and_one_submit(tmp_path: Path) -> None:
    # 2119: REQ-029.3.1
    prompt, raw = _prompt(tmp_path), tmp_path / "raw.log"
    raw.write_bytes(b"")
    tmux = _Tmux(panes=["%1", "%1", "%1"])

    def sleep(_seconds: float) -> None:
        raw.write_bytes(BRACKETED_PASTE_ON)

    assert (
        prefill_pane(
            "sess",
            str(prompt),
            run=tmux,
            sleep=sleep,
            raw_log=str(raw),
            timeout=5,
            submit=True,
        )
        is True
    )

    assert [
        "tmux",
        "pipe-pane",
        "-O",
        "-t",
        "%1",
        _watch_command(raw),
    ] in tmux.calls
    assert ["tmux", "load-buffer", "-b", "panopticon-prefill-sess", str(prompt)] in tmux.calls
    paste = ["tmux", "paste-buffer", "-p", "-d", "-b", "panopticon-prefill-sess", "-t", "%1"]
    submit = ["tmux", "send-keys", "-t", "%1", "Enter"]
    assert tmux.calls.count(paste) == 1
    assert tmux.calls.count(submit) == 1
    assert tmux.calls.index(paste) < tmux.calls.index(submit)


def test_local_runner_submits_the_wake_through_its_real_tmux_path() -> None:
    # 2119: REQ-029.3.1
    tmux = _ReadyTmux()
    runner = LocalRunner("http://svc", tmux_socket="wake-test", run=tmux)
    prompt = "You have entered WORKING.\nBuild the feature."
    raw = Path(readiness_log("panopticon-t1"))
    raw.write_bytes(BRACKETED_PASTE_ON)

    try:
        assert runner.submit_prompt("t1", prompt) is True
    finally:
        raw.unlink(missing_ok=True)

    assert tmux.loaded_text == prompt
    prefix = ["tmux", "-L", "wake-test"]
    paste = [
        *prefix,
        "paste-buffer",
        "-p",
        "-d",
        "-b",
        "panopticon-prefill-panopticon-t1",
        "-t",
        "%1",
    ]
    submit = [*prefix, "send-keys", "-t", "%1", "Enter"]
    assert tmux.calls.count(paste) == 1
    assert tmux.calls.count(submit) == 1


def test_persistent_watch_is_attached_before_a_later_delivery(tmp_path: Path) -> None:
    # 2119: REQ-029.3.1
    raw = tmp_path / "ready.raw"
    tmux = _Tmux(panes=["%1"])

    assert watch_pane("sess", run=tmux, raw_log=str(raw)) == "%1"

    assert not raw.exists()
    assert tmux.calls == [
        ["tmux", "display-message", "-p", "-t", "sess", "#{pane_id}"],
        [
            "tmux",
            "pipe-pane",
            "-O",
            "-t",
            "%1",
            _watch_command(raw),
        ],
    ]


@pytest.mark.parametrize(
    "panes,ready",
    [
        ([""], True),
        (["%1", ""], False),
        (["%1"] * 10, False),
    ],
    ids=["missing", "vanished", "timeout"],
)
def test_unavailable_pane_never_pastes_or_submits(
    tmp_path: Path, panes: list[str], ready: bool
) -> None:
    # 2119: REQ-029.3.2
    prompt, raw = _prompt(tmp_path), tmp_path / "raw.log"
    raw.write_bytes(BRACKETED_PASTE_ON if ready else b"")
    tmux = _Tmux(panes=panes)

    ok = prefill_pane(
        "sess",
        str(prompt),
        run=tmux,
        sleep=lambda _seconds: None,
        raw_log=str(raw),
        timeout=2,
        submit=True,
    )

    assert ok is False
    assert not any("paste-buffer" in call or "send-keys" in call for call in tmux.calls)


_HAVE_TMUX = shutil.which("tmux") is not None


@pytest.mark.skipif(not _HAVE_TMUX, reason="needs tmux")
def test_already_idle_real_pane_uses_readiness_recorded_at_startup(tmp_path: Path) -> None:
    # 2119: REQ-029.3.1
    socket = f"prefill-itest-{uuid.uuid4().hex}"
    session = "idle-agent"
    prefix = ["tmux", "-L", socket]
    raw = tmp_path / "ready.raw"
    received = tmp_path / "received.txt"
    prompt = _prompt(tmp_path, "continue-review")

    def run(args: Sequence[str], *, check: bool = True) -> str:
        return subprocess.run(list(args), check=check, capture_output=True, text=True).stdout

    try:
        run([*prefix, "new-session", "-d", "-s", session])
        pane = watch_pane(session, run=run, prefix=prefix, raw_log=str(raw))
        assert pane
        script = (
            "printf '\\033[?2004h'; printf 'secret transcript after readiness'; "
            f"head --lines=1 > {shlex.quote(str(received))}; sleep 10"
        )
        run([*prefix, "respawn-pane", "-k", "-t", pane, "bash", "-c", script])
        for _ in range(50):
            if raw.is_file() and BRACKETED_PASTE_ON in raw.read_bytes():
                break
            time.sleep(0.02)
        else:
            pytest.fail("pane never recorded bracketed-paste readiness")
        assert raw.read_bytes() == BRACKETED_PASTE_ON
        assert stat.S_IMODE(raw.stat().st_mode) == 0o600

        # The process is now idle in read(1); no new pane output is needed at delivery time.
        assert prefill_pane(
            session,
            str(prompt),
            run=run,
            prefix=prefix,
            raw_log=str(raw),
            timeout=2,
            submit=True,
            watch=False,
            settle_delay=0,
        )
        for _ in range(50):
            if received.is_file():
                break
            time.sleep(0.02)
        assert received.read_bytes() == b"\x1b[200~continue-review\x1b[201~\n"
    finally:
        subprocess.run([*prefix, "kill-server"], capture_output=True)
