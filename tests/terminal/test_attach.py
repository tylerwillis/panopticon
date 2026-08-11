"""The tmux attach command builder."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from panopticon.terminal.attach import (
    attach_command,
    binding_query_command,
    return_hint_from_bindings,
    task_context_label,
)

_HAVE_TMUX = bool(shutil.which("tmux"))
_REMAPPED_HINT = "Control+A and then F12 to get back to the dashboard"
_FALLBACK_HINT = "Detach this tmux client to get back to the dashboard"


# 2119: REQ-054.3.2
@pytest.mark.parametrize("session", ["dashboard", "service", "runner"])
def test_attaches_the_terminal_to_an_unlabelled_session_without_decoration(session: str) -> None:
    assert attach_command(session, socket="panopticon") == [
        "tmux",
        "-L",
        "panopticon",
        "attach",
        "-t",
        session,
    ]


def test_remote_host_wraps_the_attach_in_ssh() -> None:
    # M5 shape: the same supervisor loop reaches a remote session by prefixing ssh -t <host>.
    assert attach_command("panopticon-t1", socket="panopticon", host="box") == [
        "ssh",
        "-t",
        "box",
        "tmux -L panopticon attach -t panopticon-t1",
    ]


# 2119: REQ-054.1.2
def test_return_hint_uses_current_prefix_and_direct_detach_binding() -> None:
    bindings = "\n".join(
        [
            "C-a",
            "bind-key -T prefix d display-message not-detach",
            "bind-key -T root F11 detach-client",
            "bind-key -T prefix F12 detach-client",
            "bind-key -T prefix x run-shell 'tmux detach-client'",
        ]
    )

    assert return_hint_from_bindings(bindings) == _REMAPPED_HINT


# 2119: REQ-054.1.4
@pytest.mark.parametrize(
    "bindings",
    [
        "None\nbind-key -T prefix d detach-client",
        "C-a\nbind-key -T prefix d display-message no-detach-binding",
        "C-a\nbind-key -T prefix x run-shell 'tmux detach-client'",
        "",
    ],
)
def test_return_hint_falls_back_without_a_usable_prefix_detach_sequence(bindings: str) -> None:
    assert return_hint_from_bindings(bindings) == _FALLBACK_HINT


# 2119: REQ-054.3.1
def test_binding_query_targets_the_selected_local_or_remote_tmux_server() -> None:
    local = binding_query_command("panopticon-t1", socket="panopticon")
    remote = binding_query_command("panopticon-t1", socket="panopticon", host="box.example.com")

    assert local == [
        "tmux",
        "-L",
        "panopticon",
        "show-options",
        "-g",
        "-v",
        "-t",
        "panopticon-t1",
        "prefix",
        ";",
        "list-keys",
        "-T",
        "prefix",
    ]
    assert remote[:2] == ["ssh", "box.example.com"]
    assert shlex.split(remote[2]) == local


# 2119: REQ-025.1.1
# 2119: REQ-025.1.2
# 2119: REQ-025.1.3
# 2119: REQ-025.1.4
def test_task_context_label_uses_available_human_context_then_session_fallback() -> None:
    assert task_context_label({"slug": "fix-login", "memo": "handle expiry"}, "session-id") == (
        "fix-login [handle expiry]"
    )
    assert task_context_label({"slug": "fix-login", "memo": None}, "session-id") == "fix-login"
    assert task_context_label({"slug": None, "memo": "handle expiry"}, "session-id") == (
        "[handle expiry]"
    )
    assert task_context_label({"slug": None, "memo": None}, "session-id") == "session-id"


# 2119: REQ-025.2.2
# 2119: REQ-025.2.3
def test_task_context_label_normalizes_unicode_whitespace() -> None:
    unicode_whitespace = "".join(
        chr(code_point) for code_point in range(sys.maxunicode + 1) if chr(code_point).isspace()
    )
    task = {
        "slug": (
            f"{unicode_whitespace}fix{unicode_whitespace}login"
            f"{unicode_whitespace}now{unicode_whitespace}"
        ),
        "memo": (
            f"{unicode_whitespace}handle{unicode_whitespace}token"
            f"{unicode_whitespace}expiry{unicode_whitespace}"
        ),
    }
    assert task_context_label(task, "session-id") == "fix login now [handle token expiry]"


# 2119: REQ-025.2.4
# 2119: REQ-025.2.5
def test_task_context_label_truncates_to_100_code_points_with_ellipsis() -> None:
    label = task_context_label({"slug": "🧭" * 101, "memo": None}, "session-id")
    assert len(label) == 100
    assert label == "🧭" * 99 + "…"

    combined = task_context_label({"slug": "s" * 60, "memo": "m" * 38}, "session-id")
    assert len(combined) == 100
    assert combined == "s" * 60 + " [" + "m" * 37 + "…"

    memo_only = task_context_label({"slug": None, "memo": "m" * 101}, "session-id")
    assert len(memo_only) == 100
    assert memo_only == "[" + "m" * 98 + "…"

    session_fallback = task_context_label({"slug": None, "memo": None}, "s" * 101)
    assert len(session_fallback) == 100
    assert session_fallback == "s" * 99 + "…"


# 2119: REQ-025.2.1
# 2119: REQ-025.3.1
# 2119: REQ-025.3.3
# 2119: REQ-054.1.1
# 2119: REQ-054.1.2
# 2119: REQ-054.1.3
# 2119: REQ-054.2.1
# 2119: REQ-054.2.2
# 2119: REQ-054.3.1
# 2119: REQ-054.3.3
def test_decorated_attach_builds_task_focused_status_line_without_renaming() -> None:
    label = (
        "task #[fg=red] #S #{session_name} #{?session_name,yes,no} #(printf injected) ## # %H %%"
    )
    assert attach_command(
        "panopticon-t1", socket="panopticon", label=label, return_hint=_REMAPPED_HINT
    ) == [
        "tmux",
        "-L",
        "panopticon",
        "set-option",
        "-t",
        "panopticon-t1",
        "status-left",
        "task ##[fg=red] ##S ##{session_name} ##{?session_name,yes,no} "
        "##(printf injected) #### ## %%H %%%%",
        ";",
        "set-option",
        "-t",
        "panopticon-t1",
        "status-left-length",
        "100",
        ";",
        "set-option",
        "-t",
        "panopticon-t1",
        "status-right",
        _REMAPPED_HINT,
        ";",
        "set-option",
        "-t",
        "panopticon-t1",
        "status-right-length",
        str(len(_REMAPPED_HINT)),
        ";",
        "set-option",
        "-t",
        "panopticon-t1",
        "status-format[0]",
        "#[align=left]#{T:status-left}#[align=right]#{T:status-right}",
        ";",
        "attach",
        "-t",
        "panopticon-t1",
    ]


# 2119: REQ-025.3.2
# 2119: REQ-054.3.1
def test_remote_decorated_attach_safely_sets_the_same_context_label() -> None:
    label = "fix login [quote's memo]"
    local = attach_command(
        "panopticon-t1", socket="panopticon", label=label, return_hint=_REMAPPED_HINT
    )
    remote = attach_command(
        "panopticon-t1",
        socket="panopticon",
        host="box",
        label=label,
        return_hint=_REMAPPED_HINT,
    )

    assert local[local.index("status-left") + 1] == label
    assert remote[:3] == ["ssh", "-t", "box"]
    assert shlex.split(remote[3]) == local


# 2119: REQ-025.2.1
# 2119: REQ-054.1.1
# 2119: REQ-054.1.2
# 2119: REQ-054.1.3
# 2119: REQ-054.1.4
# 2119: REQ-054.2.1
# 2119: REQ-054.2.2
# 2119: REQ-054.3.1
# 2119: REQ-054.3.3
@pytest.mark.skipif(not _HAVE_TMUX, reason="needs tmux")
@pytest.mark.parametrize(
    ("detach_key", "expected_hint"), [("F12", _REMAPPED_HINT), (None, _FALLBACK_HINT)]
)
def test_real_tmux_renders_only_literal_task_context_and_return_hint(
    tmp_path: Path, detach_key: str | None, expected_hint: str
) -> None:
    """Exercise tmux's own format parser, not merely Panopticon's emitted argv.

    The deliberately format-shaped label catches accidental interpretation, while the deliberately
    program-shaped window name proves the central window list is absent without renaming it.
    """
    socket = f"panopticon-status-{tmp_path.name}"
    session = "task-session"
    window_name = "python3.12"
    label = (
        "task #[fg=red] #S #{session_name} #{?session_name,yes,no} #(printf injected) ## # %H %%"
    )
    subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)
    try:
        created = subprocess.run(
            [
                "tmux",
                "-L",
                socket,
                "new-session",
                "-d",
                "-s",
                session,
                "-n",
                window_name,
                "sleep",
                "30",
            ],
            capture_output=True,
            text=True,
        )
        assert created.returncode == 0, created.stderr

        remap_command = [
            "tmux",
            "-L",
            socket,
            "set-option",
            "-g",
            "prefix",
            "C-a",
            ";",
            "unbind-key",
            "-T",
            "prefix",
            "d",
        ]
        if detach_key is not None:
            remap_command += [
                ";",
                "bind-key",
                "-T",
                "prefix",
                detach_key,
                "detach-client",
            ]
        remapped = subprocess.run(
            remap_command,
            capture_output=True,
            text=True,
        )
        assert remapped.returncode == 0, remapped.stderr

        binding_query = subprocess.run(
            binding_query_command(session, socket=socket),
            capture_output=True,
            text=True,
            check=True,
        )
        return_hint = return_hint_from_bindings(binding_query.stdout)
        assert return_hint == expected_hint

        decorated_attach = attach_command(
            session, socket=socket, label=label, return_hint=return_hint
        )
        assert decorated_attach[-4:] == [";", "attach", "-t", session]
        decorated = subprocess.run(decorated_attach[:-4], capture_output=True, text=True)
        assert decorated.returncode == 0, decorated.stderr

        def show(option: str) -> str:
            return subprocess.run(
                ["tmux", "-L", socket, "show-options", "-v", "-t", session, option],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.rstrip("\n")

        assert show("status-left-length") == "100"
        assert show("status-right") == expected_hint
        assert show("status-right-length") == str(len(expected_hint))
        assert show("status-format[0]") == (
            "#[align=left]#{T:status-left}#[align=right]#{T:status-right}"
        )

        rendered_left = subprocess.run(
            ["tmux", "-L", socket, "display-message", "-p", "-t", session, "#{T:status-left}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.rstrip("\n")
        # tmux versions differ on whether display-message's diagnostic expansion consumes the
        # extra hash which keeps a status-line style introducer literal. The drawn status line
        # consumes it either way; normalize that one documented escape before comparing text.
        assert rendered_left.replace("##[", "#[") == label

        rendered_status = (
            subprocess.run(
                [
                    "tmux",
                    "-L",
                    socket,
                    "display-message",
                    "-p",
                    "-t",
                    session,
                    "#{T:status-format[0]}",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            .stdout.rstrip("\n")
            .replace("##[", "#[")
        )
        assert rendered_status.startswith(label)
        assert rendered_status.endswith(expected_hint)
        assert window_name not in rendered_status

        assert (
            subprocess.run(
                ["tmux", "-L", socket, "display-message", "-p", "-t", session, "#S"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.rstrip("\n")
            == session
        )
        assert (
            subprocess.run(
                [
                    "tmux",
                    "-L",
                    socket,
                    "display-message",
                    "-p",
                    "-t",
                    session,
                    "#{window_name}",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.rstrip("\n")
            == window_name
        )
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)
