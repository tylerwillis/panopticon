"""Nearest no-matching-container and no-capture counterexamples for REQ-050.1.2."""

from __future__ import annotations

import subprocess

from panopticon.sessionservice.image_paste import CapturedImage, main


# 2119: REQ-050.1.2
def test_nonmatching_container_output_forwards_without_attempting_capture() -> None:
    calls: list[list[str]] = []
    capture_calls = 0

    def run(argv: list[str], **_kwargs: object):
        calls.append(argv)
        if argv[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=b"panopticon-some-other-task\n",
                stderr=b"",
            )
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    def capture() -> CapturedImage:
        nonlocal capture_calls
        capture_calls += 1
        return CapturedImage(b"must-not-be-read", "png")

    assert main(["panopticon-target-task", "%55"], run=run, capture=capture) == 0
    assert calls == [
        [
            "docker",
            "ps",
            "--filter",
            "name=^panopticon-target-task$",
            "--format",
            "{{.Names}}",
        ],
        ["tmux", "send-keys", "-t", "%55", "C-v"],
    ]
    assert capture_calls == 0
