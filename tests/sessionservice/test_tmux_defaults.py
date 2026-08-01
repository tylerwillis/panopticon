"""REQ-030: panopticon's shipped tmux server defaults (mouse, scrollback, clipboard wiring) for
its dedicated ``-L panopticon`` socket. Pure-function tests need no tmux; the tests at the bottom
that source the generated config into a real tmux server (skipped without one) are the ones that
actually prove the config text is valid tmux syntax and lands correctly on a fresh socket."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from panopticon.sessionservice.tmux_defaults import (
    HISTORY_LIMIT,
    clipboard_tool,
    default_config_path,
    server_default_config_text,
    write_default_config,
)


def _which(present: set[str]):
    return lambda tool: f"/usr/bin/{tool}" if tool in present else None


# 2119: REQ-030.4.1
def test_clipboard_tool_selects_pbcopy_on_darwin_when_present() -> None:
    assert clipboard_tool(platform="darwin", which=_which({"pbcopy"})) == "pbcopy"


# 2119: REQ-030.4.1
def test_clipboard_tool_default_which_reflects_a_monkeypatched_shutil_which(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `which` must be resolved *inside* the call, not bound as an eager default argument — an
    # eager default would capture the real shutil.which once at import and never see this patch,
    # so every call site that omits `which=` (all of them) would silently ignore it.
    monkeypatch.setattr(
        "shutil.which", lambda tool: "/usr/bin/pbcopy" if tool == "pbcopy" else None
    )
    assert clipboard_tool(platform="darwin") == "pbcopy"
    monkeypatch.setattr("shutil.which", lambda _tool: None)
    assert clipboard_tool(platform="darwin") is None


# 2119: REQ-030.4.2
def test_clipboard_tool_selects_nothing_on_darwin_without_pbcopy() -> None:
    assert clipboard_tool(platform="darwin", which=_which(set())) is None
    # a linux-only tool present doesn't leak into the darwin resolution
    assert clipboard_tool(platform="darwin", which=_which({"wl-copy", "xclip", "xsel"})) is None


# 2119: REQ-030.4.3
def test_clipboard_tool_selects_wl_copy_on_linux_when_present() -> None:
    assert clipboard_tool(platform="linux", which=_which({"wl-copy"})) == "wl-copy"
    # wl-copy wins even when the other candidates are also present
    assert clipboard_tool(platform="linux", which=_which({"wl-copy", "xclip", "xsel"})) == "wl-copy"


# 2119: REQ-030.4.4
def test_clipboard_tool_falls_back_to_xclip_without_wl_copy() -> None:
    assert clipboard_tool(platform="linux", which=_which({"xclip"})) == "xclip -selection clipboard"
    assert (
        clipboard_tool(platform="linux", which=_which({"xclip", "xsel"}))
        == "xclip -selection clipboard"
    )


# 2119: REQ-030.4.5
def test_clipboard_tool_falls_back_to_xsel_without_wl_copy_or_xclip() -> None:
    assert clipboard_tool(platform="linux", which=_which({"xsel"})) == "xsel --clipboard --input"


# 2119: REQ-030.4.6
def test_clipboard_tool_selects_nothing_on_linux_with_no_candidate_present() -> None:
    assert clipboard_tool(platform="linux", which=_which(set())) is None
    # a darwin-only tool present doesn't leak into the non-darwin resolution
    assert clipboard_tool(platform="linux", which=_which({"pbcopy"})) is None


# 2119: REQ-030.1.1
# 2119: REQ-030.1.2
# 2119: REQ-030.1.3
def test_server_default_config_text_sets_mouse_history_and_clipboard_options() -> None:
    lines = server_default_config_text(clipboard="pbcopy").splitlines()
    assert "set-option -g mouse on" in lines
    assert f"set-option -g history-limit {HISTORY_LIMIT}" in lines
    assert HISTORY_LIMIT == "50000"
    assert "set-option -g set-clipboard on" in lines


# 2119: REQ-030.2.1
def test_server_default_config_text_binds_copy_mode_drag_release_to_the_clipboard_tool() -> None:
    lines = server_default_config_text(clipboard="pbcopy").splitlines()
    assert (
        'bind-key -T copy-mode MouseDragEnd1Pane send-keys -X copy-pipe-and-cancel "pbcopy"'
        in lines
    )


# 2119: REQ-030.2.2
def test_server_default_config_text_binds_copy_mode_vi_drag_release_to_the_clipboard_tool() -> None:
    lines = server_default_config_text(clipboard="xclip -selection clipboard").splitlines()
    assert (
        "bind-key -T copy-mode-vi MouseDragEnd1Pane send-keys -X copy-pipe-and-cancel "
        '"xclip -selection clipboard"'
    ) in lines


# 2119: REQ-030.2.3
# 2119: REQ-030.2.4
def test_server_default_config_text_binds_double_click_to_select_word_then_copy() -> None:
    lines = server_default_config_text(clipboard="pbcopy").splitlines()
    double_click = next(line for line in lines if "DoubleClick1Pane" in line)
    # copy-mode -M ; select the double-clicked word ; copy it — three chained sub-commands bound
    # to one key (config-file syntax, \\; between them), so the word selection (2.3) and its copy
    # (2.4) are both this one binding's action.
    assert double_click == (
        "bind-key -T root DoubleClick1Pane copy-mode -M \\; send-keys -X select-word \\; "
        'send-keys -X copy-pipe-and-cancel "pbcopy"'
    )


# 2119: REQ-030.2.5
def test_server_default_config_text_falls_back_to_plain_copy_without_a_clipboard_tool() -> None:
    lines = server_default_config_text(clipboard=None).splitlines()
    # every copy action becomes a plain in-tmux copy — no external command, no clipboard argument
    assert "bind-key -T copy-mode MouseDragEnd1Pane send-keys -X copy-selection-and-cancel" in lines
    assert (
        "bind-key -T copy-mode-vi MouseDragEnd1Pane send-keys -X copy-selection-and-cancel"
    ) in lines
    double_click = next(line for line in lines if "DoubleClick1Pane" in line)
    assert double_click.endswith("send-keys -X copy-selection-and-cancel")
    assert not any("copy-pipe-and-cancel" in line for line in lines)


def test_default_config_path_is_deterministic_per_socket(tmp_path: Path) -> None:
    a = default_config_path("panopticon", directory=tmp_path)
    b = default_config_path("panopticon", directory=tmp_path)
    assert a == b  # same socket -> same path, so repeated writes overwrite rather than litter
    other = default_config_path("other-socket", directory=tmp_path)
    assert other != a


def test_write_default_config_writes_the_resolved_text(tmp_path: Path) -> None:
    path = write_default_config("panopticon", directory=tmp_path, clipboard="pbcopy")
    assert path == default_config_path("panopticon", directory=tmp_path)
    assert path.read_text() == server_default_config_text(clipboard="pbcopy")


def test_write_default_config_resolves_clipboard_when_not_given(tmp_path: Path) -> None:
    path = write_default_config(
        "panopticon", directory=tmp_path, platform="darwin", which=_which({"pbcopy"})
    )
    assert 'copy-pipe-and-cancel "pbcopy"' in path.read_text()


def test_write_default_config_overwrites_via_atomic_rename_leaving_no_temp_files(
    tmp_path: Path,
) -> None:
    # A repeated write (every spawn/attach calls this) must fully replace the previous content —
    # and never leave the mkstemp scratch file it used to get there behind in the directory.
    write_default_config("panopticon", directory=tmp_path, clipboard="pbcopy")
    path = write_default_config(
        "panopticon", directory=tmp_path, clipboard="xclip -selection clipboard"
    )
    assert path.read_text() == server_default_config_text(clipboard="xclip -selection clipboard")
    assert list(tmp_path.iterdir()) == [path]  # only the final file — no leftover .tmp scratch


# -- integration: a real tmux server, from a genuinely fresh socket -----------------

_HAVE_TMUX = bool(shutil.which("tmux"))


# 2119: REQ-030.1.1
# 2119: REQ-030.1.2
# 2119: REQ-030.2.3
# 2119: REQ-030.2.4
# 2119: REQ-030.3.1
@pytest.mark.skipif(not _HAVE_TMUX, reason="needs tmux")
def test_config_loads_correctly_via_dash_f_on_a_genuinely_fresh_socket(tmp_path: Path) -> None:
    # This is the property unit tests (which never touch a real tmux binary) can't prove: that the
    # generated config text is valid tmux syntax, that `-f` on a `new-session` call is what
    # actually makes a brand-new socket's server durably pick up these defaults (separate `tmux -L
    # sock set-option ...` calls do NOT — tmux's exit-empty tears a sessionless server back down
    # between client invocations), and that the double-click binding's chained sub-commands land as
    # ONE bound action rather than three immediate top-level commands (which bare `;` tokens on a
    # plain CLI argv would do).
    socket = "panopticon-tmux-defaults-itest"
    subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)
    try:
        path = write_default_config(socket, directory=tmp_path, clipboard="cat")
        result = subprocess.run(
            ["tmux", "-L", socket, "-f", str(path), "new-session", "-d", "-s", "t1", "sleep", "30"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

        def show(option: str) -> str:
            return subprocess.run(
                ["tmux", "-L", socket, "show-options", "-g", option],
                capture_output=True,
                text=True,
            ).stdout.strip()

        assert show("mouse") == "mouse on"
        assert show("history-limit") == f"history-limit {HISTORY_LIMIT}"
        assert show("set-clipboard") == "set-clipboard on"

        # not just declared globally — actually effective on the session `-f` just created and its
        # first pane specifically (REQ-030.1: "a session created..."/"a pane created...")
        def effective(option: str) -> str:
            return subprocess.run(
                ["tmux", "-L", socket, "show-options", "-A", "-t", "t1", option],
                capture_output=True,
                text=True,
            ).stdout.strip()

        assert effective("mouse") == "mouse* on"
        assert effective("set-clipboard") == "set-clipboard on"
        pane_history_limit = subprocess.run(
            ["tmux", "-L", socket, "display-message", "-t", "t1", "-p", "#{history_limit}"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert pane_history_limit == HISTORY_LIMIT

        keys = subprocess.run(
            ["tmux", "-L", socket, "list-keys", "-T", "root"], capture_output=True, text=True
        ).stdout
        double_click = next(line for line in keys.splitlines() if "DoubleClick1Pane" in line)
        # all three chained sub-commands present on the SAME binding line — proves they're bound
        # together as one action, not split into separate immediate top-level commands
        assert "copy-mode" in double_click
        assert "select-word" in double_click
        assert "copy-pipe-and-cancel" in double_click
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)


# 2119: REQ-030.5.2
@pytest.mark.skipif(not _HAVE_TMUX, reason="needs tmux")
def test_dash_f_skips_the_operators_own_tmux_conf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".tmux.conf").write_text('set-option -g status-left "OPERATORS-OWN-CONFIG"\n')
    monkeypatch.setenv("HOME", str(fake_home))
    socket = "panopticon-tmux-isolation-itest"
    control_socket = "panopticon-tmux-isolation-itest-control"
    subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)
    subprocess.run(["tmux", "-L", control_socket, "kill-server"], capture_output=True)
    try:
        # negative control: without `-f`, the fake HOME/.tmux.conf IS eligible to load — proves the
        # marker would actually appear if `-f` weren't suppressing it, not that it was never reachable
        subprocess.run(
            ["tmux", "-L", control_socket, "new-session", "-d", "-s", "t1", "sleep", "30"],
            check=True,
        )
        control_status_left = subprocess.run(
            ["tmux", "-L", control_socket, "show-options", "-g", "status-left"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert "OPERATORS-OWN-CONFIG" in control_status_left

        path = write_default_config(socket, directory=tmp_path, clipboard=None)
        subprocess.run(
            ["tmux", "-L", socket, "-f", str(path), "new-session", "-d", "-s", "t1", "sleep", "30"],
            check=True,
        )
        status_left = subprocess.run(
            ["tmux", "-L", socket, "show-options", "-g", "status-left"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert "OPERATORS-OWN-CONFIG" not in status_left
    finally:
        subprocess.run(["tmux", "-L", control_socket, "kill-server"], capture_output=True)
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)
