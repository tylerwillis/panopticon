"""CLI subcommand dispatch tests for `panopticon` / `python -m panopticon.terminal`."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

from panopticon.terminal.__main__ import main


def test_stop_kills_containers_and_server() -> None:
    ps_result = MagicMock()
    ps_result.stdout = "abc123\ndef456\n"
    with patch("subprocess.run", side_effect=[ps_result, MagicMock(), MagicMock()]) as mock_run:
        assert main(["stop"]) == 0
    calls = [c.args[0] for c in mock_run.call_args_list]
    assert calls[0] == ["docker", "ps", "--all", "--quiet", "--filter", "label=panopticon.task"]
    assert calls[1] == ["docker", "rm", "--force", "abc123", "def456"]
    assert calls[2] == ["tmux", "-L", "panopticon", "kill-server"]


def test_stop_skips_docker_rm_when_no_containers() -> None:
    ps_result = MagicMock()
    ps_result.stdout = ""
    with patch("subprocess.run", side_effect=[ps_result, MagicMock()]) as mock_run:
        assert main(["stop"]) == 0
    calls = [c.args[0] for c in mock_run.call_args_list]
    assert len(calls) == 2
    assert calls[0][0] == "docker"
    assert calls[1] == ["tmux", "-L", "panopticon", "kill-server"]
    assert not any(c[0] == "docker" and "rm" in c for c in calls)


def test_stop_tolerates_missing_docker_or_tmux() -> None:
    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert main(["stop"]) == 0


def test_build_dispatches_to_image_builder() -> None:
    with patch("panopticon.sessionservice.images.ImageBuilder") as mock_cls:
        assert main(["build"]) == 0
    mock_cls.return_value.build_base.assert_called_once_with(verbose=True)


def test_doctor_dispatches_to_the_checker_and_returns_its_code() -> None:
    with (
        patch("panopticon.terminal.doctor.run_checks", return_value=["sentinel"]) as mock_checks,
        patch("panopticon.terminal.doctor.report", return_value=3) as mock_report,
    ):
        assert main(["doctor"]) == 3
    mock_checks.assert_called_once_with()
    mock_report.assert_called_once_with(["sentinel"])


def test_host_runs_migrate_then_sessions() -> None:
    with (
        patch("panopticon.sessionservice.docker_daemon.preflight_message", return_value=None),
        patch("panopticon.terminal.__main__._run_migrate") as mock_migrate,
        patch("panopticon.terminal.__main__._start_sessions") as mock_sessions,
    ):
        assert main(["host"]) == 0
    mock_migrate.assert_called_once_with()
    mock_sessions.assert_called_once_with()


def test_no_arg_aliases_start() -> None:
    with (
        patch("panopticon.sessionservice.docker_daemon.preflight_message", return_value=None),
        patch("panopticon.terminal.__main__._run_migrate") as mock_migrate,
        patch("panopticon.terminal.__main__._start_sessions") as mock_sessions,
        patch("panopticon.terminal.console.run_console_local") as mock_console,
    ):
        assert main([]) == 0
    mock_migrate.assert_called_once_with()
    mock_sessions.assert_called_once_with()
    mock_console.assert_called_once()


def test_start_runs_migrate_sessions_then_console() -> None:
    with (
        patch("panopticon.sessionservice.docker_daemon.preflight_message", return_value=None),
        patch("panopticon.terminal.__main__._run_migrate") as mock_migrate,
        patch("panopticon.terminal.__main__._start_sessions") as mock_sessions,
        patch("panopticon.terminal.console.run_console_local") as mock_console,
    ):
        assert main(["start"]) == 0
    mock_migrate.assert_called_once_with()
    mock_sessions.assert_called_once_with()
    mock_console.assert_called_once()
    assert mock_console.call_args.kwargs["join"] is None  # no task → nothing to join


def test_start_with_a_task_arg_joins_it() -> None:
    # `panopticon start <task>` threads the task ref through to the console as `join=`.
    with (
        patch("panopticon.sessionservice.docker_daemon.preflight_message", return_value=None),
        patch("panopticon.terminal.__main__._run_migrate"),
        patch("panopticon.terminal.__main__._start_sessions"),
        patch("panopticon.terminal.console.run_console_local") as mock_console,
    ):
        assert main(["start", "fix-login"]) == 0
    assert mock_console.call_args.kwargs["join"] == "fix-login"


def _assert_names_the_fix(message: str) -> None:
    assert "docker daemon unreachable" in message.lower()  # names *what* is unreachable
    # The literal, complete actionable phrase for both platforms — not a loose keyword check, so
    # a negated or unrelated mention of "OrbStack"/"systemctl" can't slip a test through.
    assert "start OrbStack or Docker Desktop (macOS)" in message
    assert "systemctl start docker` (Linux)" in message


def test_start_refuses_when_docker_daemon_is_unreachable() -> None:
    # 2119: REQ-031.1.1
    # 2119: REQ-031.1.4
    with (
        patch(
            "panopticon.sessionservice.docker_daemon.preflight_message",
            return_value="Docker daemon unreachable — start OrbStack, then rerun `panopticon start`.",
        ),
        patch("panopticon.terminal.__main__._run_migrate") as mock_migrate,
        patch("panopticon.terminal.__main__._start_sessions") as mock_sessions,
        patch("panopticon.terminal.console.run_console_local") as mock_console,
        patch("builtins.print") as mock_print,
    ):
        assert main(["start"]) == 1
    mock_migrate.assert_not_called()
    mock_sessions.assert_not_called()
    mock_console.assert_not_called()
    assert "Docker daemon unreachable" in mock_print.call_args.args[0]


def test_host_refuses_when_docker_daemon_is_unreachable() -> None:
    # 2119: REQ-031.1.4
    with (
        patch(
            "panopticon.sessionservice.docker_daemon.preflight_message",
            return_value="Docker daemon unreachable — start OrbStack, then rerun `panopticon host`.",
        ),
        patch("panopticon.terminal.__main__._run_migrate") as mock_migrate,
        patch("panopticon.terminal.__main__._start_sessions") as mock_sessions,
    ):
        assert main(["host"]) == 1
    mock_migrate.assert_not_called()
    mock_sessions.assert_not_called()


def test_no_arg_refuses_when_docker_daemon_is_unreachable() -> None:
    # 2119: REQ-031.1.1
    # 2119: REQ-031.1.4
    with (
        patch(
            "panopticon.sessionservice.docker_daemon.preflight_message",
            return_value="Docker daemon unreachable — start OrbStack, then rerun `panopticon start`.",
        ),
        patch("panopticon.terminal.__main__._run_migrate") as mock_migrate,
        patch("panopticon.terminal.__main__._start_sessions") as mock_sessions,
        patch("panopticon.terminal.console.run_console_local") as mock_console,
    ):
        assert main([]) == 1
    mock_migrate.assert_not_called()
    mock_sessions.assert_not_called()
    mock_console.assert_not_called()


#: The exact `tmux new-session` invocations `_start_sessions` runs for each real session —
#: reproduced here (not derived from the source) so an exact-list comparison catches a mutant
#: that keeps the right session name but starts the wrong module, drops the log redirection, or
#: drops `-d` (detached — a foregrounded session would hang `panopticon start`/`host`).
_EXPECTED_NEW_SESSION_COMMANDS = [
    [
        "tmux",
        "-L",
        "panopticon",
        "new-session",
        "-d",
        "-s",
        "service",
        f"{sys.executable} -m panopticon.taskservice 2>&1 | tee /tmp/panopticon-service.log",
    ],
    [
        "tmux",
        "-L",
        "panopticon",
        "new-session",
        "-d",
        "-s",
        "runner",
        f"{sys.executable} -m panopticon.sessionservice.host 2>&1 | tee /tmp/panopticon-runner.log",
    ],
]


def _fake_subprocess_run(cmd: list[str], **kwargs: object) -> MagicMock:
    """Stands in for every `subprocess.run` call `main(["start"|"host"])` makes end to end: a
    reachable `docker info`, no pre-existing tmux session (so both get created), and a successful
    `tmux new-session`. Distinguishing on the command lets one fake serve the whole real call
    chain — no `docker_daemon`/`_start_sessions` mocking, so this is a true integration test."""
    if cmd == ["docker", "info"]:
        return MagicMock(returncode=0)  # daemon reachable → preflight clears
    if "has-session" in cmd:
        return MagicMock(returncode=1)  # neither session exists yet → both get created
    return MagicMock(returncode=0)  # tmux new-session succeeds


def test_start_actually_starts_both_sessions_with_their_real_commands_when_reachable() -> None:
    # A true end-to-end run (only `subprocess.run` is faked): a genuinely reachable daemon,
    # through the real `_start_sessions`, must create both a "service" session running the exact
    # task-service command and a "runner" session running the exact session-service host command,
    # both detached — an exact argv match, not a substring, so a mutant that keeps the names but
    # swaps in an inert/wrong/foregrounded command is caught too.
    # 2119: REQ-031.1.5
    with (
        patch("subprocess.run", side_effect=_fake_subprocess_run) as mock_run,
        patch("panopticon.terminal.__main__._run_migrate"),
        patch("panopticon.terminal.console.run_console_local"),
    ):
        assert main(["start"]) == 0
    new_session_calls = [c.args[0] for c in mock_run.call_args_list if "new-session" in c.args[0]]
    assert sorted(new_session_calls) == sorted(_EXPECTED_NEW_SESSION_COMMANDS)


def test_host_actually_starts_both_sessions_with_their_real_commands_when_reachable() -> None:
    # Same as above for `panopticon host` — a wiring bug that starts only one session, drops
    # detachment, or uses the wrong command for this entry point specifically must be caught too.
    # 2119: REQ-031.1.5
    with (
        patch("subprocess.run", side_effect=_fake_subprocess_run) as mock_run,
        patch("panopticon.terminal.__main__._run_migrate"),
    ):
        assert main(["host"]) == 0
    new_session_calls = [c.args[0] for c in mock_run.call_args_list if "new-session" in c.args[0]]
    assert sorted(new_session_calls) == sorted(_EXPECTED_NEW_SESSION_COMMANDS)


def test_start_refuses_via_the_real_docker_probe_when_docker_info_fails() -> None:
    # Unlike the tests above (which mock `preflight_message` itself), this exercises the real
    # `daemon_reachable` → `preflight_message` chain down to the `docker info` subprocess call —
    # proving a genuinely unreachable daemon, not just a stubbed refusal, blocks `start`, and that
    # the printed refusal actually carries the real cross-platform remediation text.
    # 2119: REQ-031.1.1
    # 2119: REQ-031.1.3
    docker_info_failed = MagicMock(returncode=1)
    with (
        patch("subprocess.run", return_value=docker_info_failed) as mock_run,
        patch("panopticon.terminal.__main__._run_migrate") as mock_migrate,
        patch("panopticon.terminal.__main__._start_sessions") as mock_sessions,
        patch("panopticon.terminal.console.run_console_local") as mock_console,
        patch("builtins.print") as mock_print,
    ):
        assert main(["start"]) == 1
    mock_run.assert_called_once_with(["docker", "info"], capture_output=True)
    mock_migrate.assert_not_called()
    mock_sessions.assert_not_called()
    mock_console.assert_not_called()
    message = mock_print.call_args.args[0]
    _assert_names_the_fix(message)
    assert "panopticon start" in message


def test_host_refuses_via_the_real_docker_probe_when_docker_info_fails() -> None:
    # Same real-probe integration test as above, for `panopticon host`.
    # 2119: REQ-031.1.2
    # 2119: REQ-031.1.3
    docker_info_failed = MagicMock(returncode=1)
    with (
        patch("subprocess.run", return_value=docker_info_failed) as mock_run,
        patch("panopticon.terminal.__main__._run_migrate") as mock_migrate,
        patch("panopticon.terminal.__main__._start_sessions") as mock_sessions,
        patch("builtins.print") as mock_print,
    ):
        assert main(["host"]) == 1
    mock_run.assert_called_once_with(["docker", "info"], capture_output=True)
    mock_migrate.assert_not_called()
    mock_sessions.assert_not_called()
    message = mock_print.call_args.args[0]
    _assert_names_the_fix(message)
    assert "panopticon host" in message


def test_console_does_not_preflight_docker() -> None:
    # `console` assumes services are already running (REQ-031.1 only gates start/host).
    with (
        patch("panopticon.sessionservice.docker_daemon.preflight_message") as mock_preflight,
        patch("panopticon.terminal.console.run_console_local") as mock_console,
    ):
        assert main(["console"]) == 0
    mock_preflight.assert_not_called()
    mock_console.assert_called_once()
