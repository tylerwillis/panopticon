"""Docker daemon reachability (REQ-031): the injectable-command-runner check both the operator
CLI's startup preflight and the spawn loop's daemon-down deferral are built on. No real Docker —
the command runner is a fake exit-status function."""

from __future__ import annotations

from panopticon.sessionservice.docker_daemon import daemon_reachable, preflight_message


def _run_ok(command: object) -> int:
    return 0


def _run_fail(command: object) -> int:
    return 1


def test_daemon_reachable_true_when_docker_info_succeeds() -> None:
    # 2119: REQ-031.1.5
    assert daemon_reachable(run=_run_ok) is True


def test_daemon_reachable_false_when_docker_info_fails() -> None:
    # 2119: REQ-031.1.1
    assert daemon_reachable(run=_run_fail) is False


def test_daemon_reachable_probes_docker_info() -> None:
    seen: list[object] = []

    def _run(command: object) -> int:
        seen.append(command)
        return 0

    daemon_reachable(run=_run)
    assert seen == [["docker", "info"]]


def test_preflight_message_is_none_when_reachable() -> None:
    assert preflight_message("start", run=_run_ok) is None


def test_preflight_message_names_the_fix_and_the_command_when_unreachable() -> None:
    # Full-string equality, not a substring check: a substring check is a keyword-theater
    # trap — it would pass a *negated* remediation ("Never start OrbStack or Docker Desktop
    # (macOS)") just as readily as the real, actionable one.
    # 2119: REQ-031.1.3
    assert preflight_message("start", run=_run_fail) == (
        "Docker daemon unreachable — start OrbStack or Docker Desktop (macOS), or "
        "`systemctl start docker` (Linux), then rerun `panopticon start`."
    )


def test_preflight_message_names_the_host_command_when_refusing_host() -> None:
    # 2119: REQ-031.1.3
    # 2119: REQ-031.2.2
    assert preflight_message("host", run=_run_fail) == (
        "Docker daemon unreachable — start OrbStack or Docker Desktop (macOS), or "
        "`systemctl start docker` (Linux), then rerun `panopticon host`."
    )
