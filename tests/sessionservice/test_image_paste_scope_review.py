"""Exhaust the two mutually exclusive non-container scope classes in REQ-050.1.2."""

# ruff: noqa: B023

from __future__ import annotations

import subprocess

from panopticon.sessionservice.image_paste import main


# 2119: REQ-050.1.2
def test_every_noncontainer_scope_class_forwards_only_ctrl_v_to_the_originating_pane() -> None:
    scenarios = (
        ("editor", True),
        ("arbitrary-session-name", True),
        ("サービス", True),
        ("panopticon-stopped", False),
        ("panopticon-missing", False),
    )
    for session, misleading_probe_success in scenarios:
        calls: list[list[str]] = []

        def run(argv: list[str], **_kwargs: object):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv,
                0 if misleading_probe_success else 1,
                stdout=f"{session}\n".encode() if misleading_probe_success else b"",
                stderr=b"",
            )

        assert main([session, "%42"], run=run) == 0
        assert calls[-1] == ["tmux", "send-keys", "-t", "%42", "C-v"]
        assert not any(argv[:2] == ["docker", "exec"] for argv in calls)
        assert not any("load-buffer" in argv or "paste-buffer" in argv for argv in calls)
