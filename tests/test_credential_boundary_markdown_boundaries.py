"""Markdown boundary counterexamples for rendered command discovery."""

from __future__ import annotations

from credential_guard_helpers import _authenticated_shell_commands


def test_rendered_command_discovery_excludes_non_single_backtick_code() -> None:
    # 2119: REQ-044.3.1
    content = "``curl http://double``\n```sh\ncurl http://fenced\n```"
    assert _authenticated_shell_commands(content) == []
