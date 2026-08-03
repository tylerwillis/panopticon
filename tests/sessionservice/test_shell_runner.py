"""ShellRunner: unit tests pin the emitted tmux commands + the assembled shell command. No tmux —
the command runner is a fake that records calls. LLM-free (a shell task runs no agent)."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from panopticon.core.dirs import _secrets_dir
from panopticon.core.models import LifecyclePhase
from panopticon.sessionservice.runner import Runner
from panopticon.sessionservice.shell_runner import ShellRunner, _minify_shell
from panopticon.sessionservice.tmux_defaults import server_default_config_text


class _Recorder:
    """An injectable CommandRunner that records calls and replays a queued stdout per call."""

    def __init__(self, stdout: str = "") -> None:
        self.calls: list[list[str]] = []
        self._stdout = stdout

    def __call__(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        interactive: bool = False,
        verbose: bool = False,
    ) -> str:
        self.calls.append(list(args))
        return self._stdout


def test_shell_runner_is_a_runner() -> None:
    assert issubclass(ShellRunner, Runner)


def test_spawn_kills_stale_session_then_starts_the_script_in_the_task_dir() -> None:
    rec = _Recorder()
    runner = ShellRunner("http://svc:8000", runner_id="r1", run=rec)

    session = runner.spawn("t1", script="claude setup-token", workdir="/tasks/t1")

    assert session == "panopticon-t1"
    kill = rec.calls[0]
    new_session = rec.calls[-1]
    # a stale session of the same name is cleared first (idempotent restart)
    assert kill[:3] == ["tmux", "-L", "panopticon"]
    assert kill[3] == "-f"
    assert kill[-3:] == ["kill-session", "-t", "panopticon-t1"]
    assert new_session[:3] == ["tmux", "-L", "panopticon"]
    tail = new_session[new_session.index("new-session") :]
    assert tail[:4] == ["new-session", "-d", "-s", "panopticon-t1"]
    assert tail[4:6] == ["-c", "/tasks/t1"]  # the pane starts in the task's own directory
    assert tail[6:8] == ["sh", "-c"]  # the pane runs the assembled script under sh -c


def test_spawn_falls_back_to_the_operator_home_without_a_workdir() -> None:
    import os

    rec = _Recorder()
    ShellRunner("http://svc:8000", run=rec).spawn("t1", script="echo hi")  # no workdir → home
    new_session = rec.calls[-1]
    assert new_session[new_session.index("-c") + 1] == os.path.expanduser("~")


def test_spawn_exports_service_env_and_runs_the_script() -> None:
    rec = _Recorder()
    ShellRunner("http://svc:8000", runner_id="r1", run=rec).spawn("t1", script="claude setup-token")
    command = rec.calls[-1][-1]  # the sh -c argument
    assert "export PANOPTICON_SERVICE_URL=http://svc:8000" in command
    assert "export PANOPTICON_TASK_ID=t1" in command
    assert "export PANOPTICON_RUNNER_ID=r1" in command
    assert "export PANOPTICON_PYTHON=" in command
    assert "export PANOPTICON_SECRETS_DIR=" in command
    assert command.rstrip().endswith("claude setup-token")  # the workflow script runs last


def test_spawn_loads_the_panopticon_shell_lib_before_the_script() -> None:
    # The shell lib (task_lib.sh) is injected so the workflow script can drive its task over REST
    # (panopticon_advance/_drop/…) instead of hand-rolling curl.
    rec = _Recorder()
    ShellRunner("http://svc:8000", run=rec).spawn("t1", script="panopticon_advance")
    command = rec.calls[-1][-1]
    assert "panopticon_advance()" in command  # the function is defined...
    assert "_panopticon_api" in command  # ...along with the lib internals
    assert command.index("panopticon_advance()") < command.rindex(
        "panopticon_advance"
    )  # def before use


def test_spawn_holds_a_liveness_registration_open_in_the_background() -> None:
    # A shell task runs no agent, so the runner holds its /live stream open so the dashboard shows
    # it live (not `awaiting`) while the script runs; a trap drops it when the script exits.
    rec = _Recorder()
    ShellRunner("http://svc:8000", runner_id="r1", run=rec).spawn("t1", script="echo hi")
    command = rec.calls[-1][-1]
    assert (
        "/tasks/t1/live?container_id=panopticon-t1&runner_id=r1" in command
    )  # holds liveness open
    assert "--no-buffer" in command and command.count(" &\n") >= 1  # backgrounded, streaming GET
    assert "trap '_panopticon_cleanup' EXIT" in command
    assert "trap '_panopticon_cleanup; exit 129' HUP INT TERM" in command
    assert "nohup sh -c" in command
    assert "tmux -L panopticon has-session -t panopticon-t1" in command
    assert '"$_panopticon_live_pid"' in command
    # the registration is established before the workflow script runs
    assert command.index("/live?") < command.index("echo hi")


def test_spawn_resolves_and_loads_the_env_file_without_executing_it() -> None:
    # env_file is a name relative to this runner's secrets dir (ADR 0007), resolved host-locally.
    rec = _Recorder()
    ShellRunner("http://svc:8000", secrets_dir="/host/secrets", run=rec).spawn(
        "t1", script="echo hi", env_file="r1.env"
    )
    command = rec.calls[-1][-1]
    assert (
        "export PANOPTICON_ENV_FILE=/host/secrets/r1.env" in command
    )  # path exposed to the script
    # resolved + loaded as literal NAME=VALUE records (guarded on existence — a
    # not-yet-created secrets file is fine)
    assert "[ -f /host/secrets/r1.env ]" in command
    assert 'export "$_pan_env_line"' in command
    assert ". /host/secrets/r1.env" not in command


def test_spawn_rejects_an_env_file_name_escaping_the_secrets_dir() -> None:
    rec = _Recorder()
    with pytest.raises(ValueError, match="escapes the secrets dir"):
        ShellRunner("http://svc:8000", secrets_dir="/host/secrets", run=rec).spawn(
            "t1", script="echo hi", env_file="../evil.env"
        )


def test_spawn_omits_env_sourcing_without_a_file() -> None:
    rec = _Recorder()
    ShellRunner("http://svc:8000", run=rec).spawn("t1", script="echo hi")
    command = rec.calls[-1][-1]
    assert "set -a" not in command
    assert "unset PANOPTICON_ENV_FILE" in command  # no repo file may inject a stale path


def test_spawn_exports_the_git_url_when_given() -> None:
    # A script uses PANOPTICON_GIT_URL to tell what forge the repo lives on (e.g. offer a GH token).
    rec = _Recorder()
    ShellRunner("http://svc:8000", run=rec).spawn(
        "t1", script="echo hi", git_url="https://github.com/o/r.git"
    )
    command = rec.calls[-1][-1]
    assert "export PANOPTICON_GIT_URL=https://github.com/o/r.git" in command


def test_spawn_omits_the_git_url_export_without_one() -> None:
    rec = _Recorder()
    ShellRunner("http://svc:8000", run=rec).spawn("t1", script="echo hi")
    command = rec.calls[-1][-1]
    assert "unset PANOPTICON_GIT_URL" in command


def test_spawn_exports_the_repo_name_when_given() -> None:
    # A script uses PANOPTICON_REPO_NAME to label the repo in its summary.
    rec = _Recorder()
    ShellRunner("http://svc:8000", run=rec).spawn("t1", script="echo hi", repo_name="acme/widget")
    command = rec.calls[-1][-1]
    assert "export PANOPTICON_REPO_NAME=acme/widget" in command


def test_spawn_omits_the_repo_name_export_without_one() -> None:
    rec = _Recorder()
    ShellRunner("http://svc:8000", run=rec).spawn("t1", script="echo hi")
    command = rec.calls[-1][-1]
    assert "unset PANOPTICON_REPO_NAME" in command


def test_minify_shell_drops_full_line_comments_and_blanks_only() -> None:
    src = "\n".join(
        [
            "# a full-line comment",
            "  # indented comment",
            "",
            "   ",
            "export FOO=bar",
            "run --flag  # trailing comment stays (mid-line #)",
            'echo "# not a comment inside a string"',
        ]
    )
    out = _minify_shell(src)
    assert out.splitlines() == [
        "export FOO=bar",
        "run --flag  # trailing comment stays (mid-line #)",
        'echo "# not a comment inside a string"',
    ]


def test_spawn_spills_a_large_script_to_avoid_the_imsg_cap(tmp_path: Path) -> None:
    # tmux sends the whole new-session command to its server over imsg (16 KiB cap); a heavily
    # commented workflow script + the task lib can exceed it and fail the spawn, so the assembled
    # command drops whole-line comments/blanks. The real setup-repo script is the motivating case.
    from panopticon.workflows import SetupRepo

    rec = _Recorder()
    ShellRunner("http://svc:8000", script_dir=tmp_path, run=rec).spawn(
        "t1",
        script=SetupRepo().shell_script(),
        git_url="https://github.com/o/r.git",
        repo_name="o/r",
    )
    command = rec.calls[-1][-1]
    script_path = tmp_path / "panopticon-shell-t1.sh"
    spilled = script_path.read_text()
    # no whole-line comments survive, but the code (functions, exports) does
    assert not [ln for ln in spilled.splitlines() if ln.lstrip().startswith("#")]
    assert "store_env_token" in spilled and "panopticon_advance()" in spilled
    # tmux receives only a tiny wrapper, which removes the private spill file when the pane exits.
    assert str(script_path) in command and "trap 'rm -f" in command
    assert len(command.encode()) < 16384


def test_spawn_reports_starting_then_awaiting() -> None:
    phases: list[LifecyclePhase] = []
    ShellRunner("http://svc:8000", run=_Recorder()).spawn(
        "t1", script="echo hi", progress=phases.append
    )
    assert phases == [LifecyclePhase.STARTING, LifecyclePhase.AWAITING]  # no PREPARING/BUILDING


def test_has_session_and_is_running_match_the_session_list() -> None:
    present = _Recorder(stdout="panopticon-t1\npanopticon-t2\n")
    runner = ShellRunner("http://svc:8000", run=present)
    assert runner.has_session("t1") is True
    assert runner.is_running("t1") is True  # for a shell task, the session IS its liveness

    absent = _Recorder(stdout="panopticon-other\n")
    runner_absent = ShellRunner("http://svc:8000", run=absent)
    assert runner_absent.has_session("t1") is False
    assert runner_absent.is_running("t1") is False


def test_stop_kills_the_session() -> None:
    rec = _Recorder()
    ShellRunner("http://svc:8000", run=rec).stop("panopticon-t1")
    assert rec.calls == [["tmux", "-L", "panopticon", "kill-session", "-t", "panopticon-t1"]]


def test_spawn_and_stop_remove_stranded_auth_snapshots(tmp_path: Path) -> None:
    rec = _Recorder()
    runner = ShellRunner("http://svc:8000", run=rec, script_dir=tmp_path)
    stale = tmp_path / "panopticon-service-auth-t1-stranded.json"
    stale.write_text("secret")
    runner.spawn("t1", script="true")
    assert not stale.exists()

    stranded = tmp_path / "panopticon-service-auth-t1-after-spawn.json"
    stranded.write_text("secret")
    runner.stop("panopticon-t1")
    assert not stranded.exists()


# 2119: REQ-030.3.1
def test_spawn_loads_every_shipped_tmux_server_default_via_dash_f_on_its_own_new_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A shell task (e.g. setup-repo's `claude setup-token`) may be the very first thing to touch a
    # fresh `-L panopticon` socket, exactly like a container task — same obligation applies.
    monkeypatch.setattr("shutil.which", lambda _tool: None)
    rec = _Recorder()
    ShellRunner("http://svc:8000", run=rec).spawn("t1", script="echo hi")
    tmux_new = rec.calls[-1]
    assert tmux_new[:3] == ["tmux", "-L", "panopticon"]
    assert tmux_new[3] == "-f"
    config_path = Path(tmux_new[4])
    assert tmux_new[5] == "new-session"
    assert config_path.read_text() == server_default_config_text(clipboard=None)


# 2119: REQ-030.3.2
def test_spawn_places_dash_f_before_new_session_so_it_applies_at_server_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _tool: None)
    rec = _Recorder()
    ShellRunner("http://svc:8000", run=rec).spawn("t1", script="echo hi")
    first_tmux = next(c for c in rec.calls if c[0] == "tmux")
    tmux_new = rec.calls[-1]
    assert first_tmux[:4] == ["tmux", "-L", "panopticon", "-f"]
    assert Path(first_tmux[4]).read_text() == server_default_config_text(clipboard=None)
    assert tmux_new.index("-f") < tmux_new.index("new-session")


# 2119: REQ-030.5.1
def test_spawn_applies_no_shipped_defaults_without_a_dedicated_socket() -> None:
    rec = _Recorder()
    ShellRunner("http://svc:8000", tmux_socket=None, run=rec).spawn("t1", script="echo hi")
    tmux_calls = [c for c in rec.calls if c[0] == "tmux"]
    command = tmux_calls[-1][-1]
    normalized = command.replace(sys.executable, "<python>")
    normalized = normalized.replace(str(_secrets_dir()), "<secrets>")
    assert hashlib.sha256(normalized.encode()).hexdigest() == (
        "80dca71cec96f4cef13a10c919f00d992df48447d9215ed8484ba3726f527ae7"
    )
    assert tmux_calls == [
        ["tmux", "kill-session", "-t", "panopticon-t1"],
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            "panopticon-t1",
            "-c",
            str(Path.home()),
            "sh",
            "-c",
            command,
        ],
    ]


# -- integration: a real host tmux session (no container) ---------------------------


# 2119: REQ-030.1.1
# 2119: REQ-030.3.1
@pytest.mark.skipif(not shutil.which("tmux"), reason="needs tmux")
def test_spawn_runs_the_script_in_a_real_tmux_session(tmp_path: Path) -> None:
    # Proves a shell task runs in a live host tmux session (no container): the script executes in
    # the pane (drops a marker), the session is attachable while it runs, and stop() tears it down.
    socket = "panopticon-shelltest"
    subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)  # genuinely fresh
    runner = ShellRunner("http://unused", tmux_socket=socket)
    marker = tmp_path / "ran"
    session = "panopticon-itest1"
    try:
        # touch a marker (the script really ran in the pane), then sleep so the session stays up
        assert (
            runner.spawn("itest1", script=f"touch {marker}; sleep 30", workdir=str(tmp_path))
            == session
        )
        for _ in range(50):  # new-session returns once the pane is up; poll defensively
            if runner.has_session("itest1") and marker.exists():
                break
            time.sleep(0.1)
        assert runner.has_session("itest1")  # a live tmux session the operator could `t`-attach to
        assert runner.is_running("itest1")  # the session is the shell task's liveness
        assert marker.exists()  # the script executed inside the pane
        # the shipped tmux defaults (REQ-030) landed on this genuinely fresh socket's server
        mouse = subprocess.run(
            ["tmux", "-L", socket, "show-options", "-g", "mouse"], capture_output=True, text=True
        ).stdout.strip()
        assert mouse == "mouse on"
    finally:
        runner.stop(session)
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)
    assert not runner.has_session("itest1")  # stop() killed it
