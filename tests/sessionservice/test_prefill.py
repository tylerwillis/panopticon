"""The tmux input-box primitive used by stage-entry wake. No real tmux, Docker, or LLM."""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from pathlib import Path

import pytest

from panopticon.sessionservice.local_runner import LocalRunner
from panopticon.sessionservice.prefill import BRACKETED_PASTE_ON, prefill_pane


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
        f"cat >> {shlex.quote(str(raw))}",
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

    assert runner.submit_prompt("t1", prompt) is True

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
