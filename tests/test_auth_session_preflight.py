"""Authentication preflight at integrated dashboard and task-spawn boundaries."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from panopticon.sessionservice.spawner import Spawner
from panopticon.terminal import console as terminal_console


def test_integrated_dashboard_pins_current_auth_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-035.31.1
    monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_FILE", "current-auth.json")
    monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_MODE", "enforced")
    monkeypatch.setenv("PANOPTICON_CONFIG", "/current/config")
    captured: list[list[str]] = []
    monkeypatch.setattr(terminal_console, "wait_for_service", lambda _url: True)
    monkeypatch.setattr(terminal_console, "switch_file_path", lambda _socket: tmp_path / "switch")
    monkeypatch.setattr(
        terminal_console,
        "ensure_dashboard_session",
        lambda dashboard, **_kwargs: captured.append(dashboard),
    )
    monkeypatch.setattr(
        terminal_console.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0),
    )
    monkeypatch.setattr(
        terminal_console,
        "run_console",
        lambda *, show_dashboard, **_kwargs: show_dashboard(),
    )

    terminal_console.run_console_local("http://service")

    assert len(captured) == 1
    dashboard = captured[0]
    assert dashboard[:7] == [
        "env",
        "-u",
        "PANOPTICON_SERVICE_AUTH_FILE",
        "-u",
        "PANOPTICON_SERVICE_AUTH_MODE",
        "-u",
        "PANOPTICON_CONFIG",
    ]
    assert "PANOPTICON_SERVICE_AUTH_FILE=current-auth.json" in dashboard
    assert "PANOPTICON_SERVICE_AUTH_MODE=enforced" in dashboard
    assert "PANOPTICON_CONFIG=/current/config" in dashboard

    for name in [
        "PANOPTICON_SERVICE_AUTH_FILE",
        "PANOPTICON_SERVICE_AUTH_MODE",
        "PANOPTICON_CONFIG",
    ]:
        monkeypatch.delenv(name)
    captured.clear()
    terminal_console.run_console_local("http://service")
    assert len(captured) == 1
    cleared_dashboard = captured[0]
    for name in [
        "PANOPTICON_SERVICE_AUTH_FILE",
        "PANOPTICON_SERVICE_AUTH_MODE",
        "PANOPTICON_CONFIG",
    ]:
        assert ["-u", name] == cleared_dashboard[
            cleared_dashboard.index(name) - 1 : cleared_dashboard.index(name) + 1
        ]
        assert not any(argument.startswith(f"{name}=") for argument in cleared_dashboard)


def test_spawner_validates_runner_credential_before_claiming() -> None:
    claims: list[tuple[str, str]] = []

    class Client:
        def claim(self, task_id: str, runner_id: str) -> None:
            claims.append((task_id, runner_id))

    class Executions:
        def is_shell(self, _workflow: object) -> bool:
            return False

    class InvalidRunner:
        def validate_configuration(self) -> None:
            raise ValueError("missing credential")

        def delete_workspace_contents(self, _path: str) -> None:
            pass

    spawner = Spawner(  # type: ignore[arg-type]
        Client(),
        InvalidRunner(),
        runner_id="runner",
        cache=object(),
        tasks_root="/tasks",
        executions=Executions(),
    )

    with pytest.raises(ValueError, match="missing credential"):
        spawner.spawn_one(
            {
                "id": "task",
                "state": "ITERATING",
                "claimed_by": None,
                "workflow": "spike",
            }
        )

    assert claims == []


def test_spawner_validates_runner_credential_before_healing_side_effects() -> None:
    effects: list[str] = []

    class Client:
        def report_lifecycle(self, *_args: object, **_kwargs: object) -> None:
            effects.append("lifecycle")

    class Executions:
        def is_shell(self, _workflow: object) -> bool:
            return False

    class InvalidRunner:
        def validate_configuration(self) -> None:
            raise ValueError("invalid credential")

        def has_session(self, _task_id: str) -> bool:
            return False

        def delete_workspace_contents(self, _path: str) -> None:
            effects.append("workspace")

    spawner = Spawner(  # type: ignore[arg-type]
        Client(),
        InvalidRunner(),
        runner_id="runner",
        cache=object(),
        tasks_root="/tasks",
        executions=Executions(),
        daemon_reachable=lambda: effects.append("daemon") is None,
    )

    with pytest.raises(ValueError, match="invalid credential"):
        spawner.heal(
            {
                "id": "task",
                "state": "ITERATING",
                "claimed_by": "runner",
                "workflow": "spike",
                "container_status": "down",
            }
        )

    assert effects == []
