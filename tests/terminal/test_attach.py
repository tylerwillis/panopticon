"""The tmux attach command builder."""

from __future__ import annotations

from panopticon.terminal.attach import attach_command, task_context_label


def test_attaches_the_terminal_to_the_session() -> None:
    assert attach_command("panopticon-t1", socket="panopticon") == [
        "tmux",
        "-L",
        "panopticon",
        "attach",
        "-t",
        "panopticon-t1",
    ]


def test_remote_host_wraps_the_attach_in_ssh() -> None:
    # M5 shape: the same supervisor loop reaches a remote session by prefixing ssh -t <host>.
    assert attach_command("panopticon-t1", socket="panopticon", host="box") == [
        "ssh",
        "-t",
        "box",
        "tmux -L panopticon attach -t panopticon-t1",
    ]


# 2119: REQ-009.1.1
# 2119: REQ-009.1.2
# 2119: REQ-009.1.3
# 2119: REQ-009.1.4
def test_task_context_label_uses_available_human_context_then_session_fallback() -> None:
    assert task_context_label({"slug": "fix-login", "memo": "handle expiry"}, "session-id") == (
        "fix-login [handle expiry]"
    )
    assert task_context_label({"slug": "fix-login", "memo": None}, "session-id") == "fix-login"
    assert task_context_label({"slug": None, "memo": "handle expiry"}, "session-id") == (
        "[handle expiry]"
    )
    assert task_context_label({"slug": None, "memo": None}, "session-id") == "session-id"


# 2119: REQ-009.2.2
# 2119: REQ-009.2.3
def test_task_context_label_normalizes_unicode_whitespace() -> None:
    task = {"slug": "\u2003  fix\tlogin\n", "memo": "\u2003handle\r\n expiry  \u2003"}
    assert task_context_label(task, "session-id") == "fix login [handle expiry]"


# 2119: REQ-009.2.4
# 2119: REQ-009.2.5
def test_task_context_label_truncates_to_100_code_points_with_ellipsis() -> None:
    label = task_context_label({"slug": "🧭" * 120, "memo": None}, "session-id")
    assert len(label) == 100
    assert label == "🧭" * 99 + "…"


# 2119: REQ-009.2.1
# 2119: REQ-009.3.1
# 2119: REQ-009.3.3
def test_decorated_attach_sets_only_target_session_status_left_without_renaming() -> None:
    assert attach_command(
        "panopticon-t1", socket="panopticon", label="fix #[fg=red] #S #{session_name} ## #"
    ) == [
        "tmux",
        "-L",
        "panopticon",
        "set-option",
        "-t",
        "panopticon-t1",
        "status-left",
        "fix ##[fg=red] ##S ##{session_name} #### ##",
        ";",
        "attach",
        "-t",
        "panopticon-t1",
    ]


# 2119: REQ-009.3.2
def test_remote_decorated_attach_safely_sets_the_same_context_label() -> None:
    label = "fix login [quote's memo]"
    local = attach_command("panopticon-t1", socket="panopticon", label=label)
    remote = attach_command("panopticon-t1", socket="panopticon", host="box", label=label)

    assert local[local.index("status-left") + 1] == label
    assert remote == [
        "ssh",
        "-t",
        "box",
        "tmux -L panopticon set-option -t panopticon-t1 status-left "
        "'fix login [quote'\"'\"'s memo]' ';' attach -t panopticon-t1",
    ]
