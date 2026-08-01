"""The terminal CLI (`panopticon`). The shared REST client it uses is covered in
test_client.py; the dashboard in test_dashboard.py. Quickstart helpers are in test_quickstart.py."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from panopticon.sessionservice.tmux_defaults import server_default_config_text
from panopticon.terminal import __main__ as cli


class _FakeClient:
    def list_tasks(self) -> list[dict[str, object]]:
        return [{"id": "t1", "state": "ITERATING", "turn": "agent", "slug": None}]


def test_cli_tasks_lists(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["tasks"], client=_FakeClient())  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert rc == 0
    assert "t1" in out and "ITERATING" in out and "agent" in out


class _FakeCompletedProcess:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


# 2119: REQ-030.3.1
def test_start_sessions_loads_shipped_tmux_defaults_via_dash_f_for_service_and_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `panopticon start`/`host` run this before the dashboard or any task spawn — in the normal
    # startup flow it's the actual first thing to touch a fresh `-L panopticon` socket, so it must
    # carry the same shipped defaults (REQ-030) as the other three new-session call sites.
    monkeypatch.setattr("shutil.which", lambda _tool: None)
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> _FakeCompletedProcess:
        calls.append(args)
        return _FakeCompletedProcess(returncode=1)  # no session exists yet -> create it

    cli._start_sessions(run=fake_run)

    new_session_calls = [c for c in calls if "new-session" in c]
    assert len(new_session_calls) == 2  # "service" and "runner"
    for tmux_new in new_session_calls:
        assert tmux_new[:3] == ["tmux", "-L", "panopticon"]
        assert tmux_new[3] == "-f"
        config_path = Path(tmux_new[4])
        assert tmux_new[5] == "new-session"
        assert config_path.read_text() == server_default_config_text(clipboard=None)


# 2119: REQ-030.3.2
def test_start_sessions_places_dash_f_before_new_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _tool: None)
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> _FakeCompletedProcess:
        calls.append(args)
        return _FakeCompletedProcess(returncode=1)

    cli._start_sessions(run=fake_run)

    for tmux_new in (c for c in calls if "new-session" in c):
        assert tmux_new.index("-f") < tmux_new.index("new-session")


def test_start_sessions_skips_an_already_running_session(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> _FakeCompletedProcess:
        calls.append(args)
        return _FakeCompletedProcess(returncode=0)  # both already running

    cli._start_sessions(run=fake_run)

    assert not any("new-session" in c for c in calls)  # neither bounced


def test_dashboard_under_supervisor_wires_the_switch_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    # With --switch-file (set by the supervisor, ADR 0009 §6) the dashboard is wired with the
    # `t` (on_switch), `s` (on_service), and `u` (on_runner) hooks; the dashboard stays running.
    from panopticon.terminal import dashboard

    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        dashboard,
        "run",
        lambda _c, *, on_switch=None, on_service=None, on_runner=None, artifacts_root=None, draft_file=None: (
            seen.update(
                on_switch=on_switch,
                on_service=on_service,
                on_runner=on_runner,
                draft_file=draft_file,
            )
        ),
    )
    cli.main(["dashboard", "--switch-file", "/tmp/x"], client=_FakeClient())  # type: ignore[arg-type]
    assert (
        seen["on_switch"] is not None
        and seen["on_service"] is not None
        and seen["on_runner"] is not None
    )
    assert seen["draft_file"] == Path("/tmp/new-task-drafts.json")


def test_standalone_dashboard_has_no_switch_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    from panopticon.terminal import dashboard

    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        dashboard,
        "run",
        lambda _c, *, on_switch=None, on_service=None, on_runner=None, artifacts_root=None, draft_file=None: (
            seen.update(
                on_switch=on_switch,
                on_service=on_service,
                on_runner=on_runner,
                draft_file=draft_file,
            )
        ),
    )
    cli.main(["dashboard"], client=_FakeClient())  # type: ignore[arg-type]
    assert seen["on_switch"] is None and seen["on_service"] is None and seen["on_runner"] is None
    assert seen["draft_file"] is None


def test_quickstart_invokes_all_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    from panopticon.terminal import console, doctor
    from panopticon.terminal import quickstart as qs

    calls: list[str] = []

    monkeypatch.setattr(doctor, "run_checks", list)
    monkeypatch.setattr(doctor, "report", lambda results: (calls.append("doctor"), 0)[1])
    monkeypatch.setattr(cli, "_run_migrate", lambda: calls.append("migrate"))
    monkeypatch.setattr(cli, "_start_sessions", lambda: calls.append("sessions"))
    monkeypatch.setattr(qs, "wait_for_service", lambda url, **kw: calls.append("wait"))
    monkeypatch.setattr(qs, "ensure_secrets_file", lambda: (calls.append("secrets"), "/tmp/env")[1])
    monkeypatch.setattr(qs, "harness_environment", lambda env: (calls.append("harness-env"), {})[1])
    monkeypatch.setattr(qs, "detect_harnesses", lambda **kw: (calls.append("detect"), [])[1])
    monkeypatch.setattr(qs, "choose_harness", lambda found: (calls.append("choose"), "codex")[1])
    monkeypatch.setattr(qs, "detect_git_url", lambda: (calls.append("git_url"), "https://x.git")[1])
    monkeypatch.setattr(
        qs,
        "setup_repo",
        lambda c, g, e, **kw: (calls.append("setup"), ("repo1", "acme/repo1"))[1],
    )
    monkeypatch.setattr(
        qs,
        "ensure_setup_repo_task",
        lambda c, repo_id, name: (calls.append("token-task"), "task1")[1],
    )
    joined: dict[str, object] = {}
    monkeypatch.setattr(
        console,
        "run_console_local",
        lambda url, **kw: (calls.append("console"), joined.update(kw))[0],
    )

    rc = cli.main(["quickstart"])
    assert rc == 0
    # Doctor runs first, before any side effects.
    assert calls == [
        "doctor",
        "migrate",
        "sessions",
        "wait",
        "secrets",
        "harness-env",
        "detect",
        "choose",
        "git_url",
        "setup",
        "token-task",
        "console",
    ]
    # The console opens attached to the setup-repo task.
    assert joined["join"] == "task1"


def test_quickstart_aborts_when_doctor_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from panopticon.terminal import console, doctor

    calls: list[str] = []

    monkeypatch.setattr(doctor, "run_checks", list)
    monkeypatch.setattr(doctor, "report", lambda results: 1)
    monkeypatch.setattr(cli, "_run_migrate", lambda: calls.append("migrate"))
    monkeypatch.setattr(cli, "_start_sessions", lambda: calls.append("sessions"))
    monkeypatch.setattr(console, "run_console_local", lambda url, **kw: calls.append("console"))

    rc = cli.main(["quickstart"])
    assert rc == 1
    # A failing doctor aborts before migrations, sessions, or the console.
    assert calls == []
