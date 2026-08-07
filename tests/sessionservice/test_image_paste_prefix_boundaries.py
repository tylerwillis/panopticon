"""Exact `panopticon-` prefix boundaries and side-effect exclusions for REQ-053.1.2."""

# ruff: noqa: B023

from __future__ import annotations

import subprocess

from panopticon.sessionservice.image_paste import CapturedImage, main


# 2119: REQ-053.1.2
def test_non_task_prefix_boundaries_forward_as_their_only_action() -> None:
    for session in ("panopticon", "xpanopticon-task", "-panopticon-task", "task"):
        calls: list[list[str]] = []
        capture_calls = 0

        def run(argv: list[str], **_kwargs: object):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

        def capture() -> CapturedImage:
            nonlocal capture_calls
            capture_calls += 1
            return CapturedImage(b"must-not-be-read", "png")

        assert main([session, "%61"], run=run, capture=capture) == 0
        assert calls == [["tmux", "send-keys", "-t", "%61", "C-v"]]
        assert capture_calls == 0


# 2119: REQ-053.1.2
def test_task_prefix_without_an_exact_container_match_only_probes_then_forwards() -> None:
    calls: list[list[str]] = []
    capture_calls = 0

    def run(argv: list[str], **_kwargs: object):
        calls.append(argv)
        if argv[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=b"panopticon-target-extra\npanopticon-other\n",
                stderr=b"",
            )
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    def capture() -> CapturedImage:
        nonlocal capture_calls
        capture_calls += 1
        return CapturedImage(b"must-not-be-read", "png")

    assert main(["panopticon-target", "%62"], run=run, capture=capture) == 0
    assert calls == [
        [
            "docker",
            "ps",
            "--filter",
            "name=^panopticon-target$",
            "--format",
            "{{.Names}}",
        ],
        ["tmux", "send-keys", "-t", "%62", "C-v"],
    ]
    assert capture_calls == 0
