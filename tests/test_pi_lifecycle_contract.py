"""Cross-boundary pi lifecycle evidence without launching a real model."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from panopticon.client import TaskServiceClient
from panopticon.container import agent
from panopticon.core.models import Repo
from panopticon.sessionservice.host import HostDaemon
from panopticon.sessionservice.local_runner import LocalRunner
from panopticon.sessionservice.prefill import readiness_log, readiness_watch_command
from panopticon.sessionservice.spawner import Spawner
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike


# 2119: REQ-050.4.2
# 2119: REQ-050.4.4
@pytest.mark.parametrize("status", [0, 7])
def test_failed_pi_exit_is_never_healed_across_repeated_daemon_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    service = TaskService(SqlAlchemyStore(), {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    asyncio.run(service.init())
    asyncio.run(
        service.create_repo(Repo(id="repo", name="acme/widgets", git_url="https://forge/repo"))
    )
    asyncio.run(service.register_runner("host-1"))

    with TestClient(create_app(service)) as http:
        client = TaskServiceClient(http)
        task_id = client.create_task("repo", "spike", harness="pi")["id"]
        client.claim(task_id, "host-1")
        credentials = tmp_path / "credentials"
        native = credentials / "pi" / "agent"
        native.mkdir(parents=True)
        (native / "auth.json").write_text(
            '{"openai-codex":{"type":"oauth","access":"a","refresh":"r",'
            '"expires":2000000000000,"accountId":"i"}}'
        )
        monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://service")
        monkeypatch.setenv("PANOPTICON_TASK_ID", task_id)
        monkeypatch.setenv("PANOPTICON_RUNNER_ID", "host-1")
        monkeypatch.setenv("PANOPTICON_HARNESS", "pi")
        monkeypatch.setenv("PANOPTICON_CREDENTIALS", str(credentials))
        detail = f"pi exited unexpectedly with status {status}"
        agent.main(
            client_factory=lambda _url: client,  # type: ignore[arg-type,return-value]
            home=tmp_path / "home",
            launch=lambda _harness, _ctx: status,  # type: ignore[arg-type]
            on_exit=lambda: None,
        )
        failed = client.get_task(task_id)
        assert failed["harness"] == "pi"
        assert failed["container_status"] == "failed"

        spawner = object.__new__(Spawner)
        spawner._client = client  # type: ignore[attr-defined]
        spawner._runner_id = "host-1"  # type: ignore[attr-defined]
        spawner._runner = mock.Mock(name="runner")  # type: ignore[attr-defined]
        spawner._runner.has_session.return_value = False  # type: ignore[attr-defined]
        spawner._executions = mock.Mock(name="executions")  # type: ignore[attr-defined]
        spawner._executions.is_shell.return_value = False  # type: ignore[attr-defined]
        spawner._respawns = {}  # type: ignore[attr-defined]
        spawner._pre_session_failures = set()  # type: ignore[attr-defined]
        provisioner = mock.Mock(name="provisioner")
        daemon = HostDaemon(client, spawner, provisioner, runner_id="host-1")

        for _ in range(257):
            tasks, version_before = client.list_tasks_versioned()
            daemon.tick(tasks)
            _, version_after = client.list_tasks_versioned()
            assert version_after == version_before
            current = client.get_task(task_id)
            assert current["container_status"] == "failed"
            assert current["lifecycle_detail"] == detail

        retained = client.get_task(task_id)
        assert retained["claimed_by"] == "host-1"
        assert retained["container_status"] == "failed"
        assert retained["lifecycle_detail"] == detail
        assert spawner._runner.mock_calls == []  # type: ignore[attr-defined]
        assert spawner._executions.mock_calls == []  # type: ignore[attr-defined]

        released = client.release(task_id)
        assert released["claimed_by"] is None
        assert released["container_status"] == "queued"
        assert released["lifecycle_detail"] is None


# 2119: REQ-050.4.3
# 2119: REQ-050.4.4
def test_real_pi_preflight_failure_is_durable_before_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    service = TaskService(SqlAlchemyStore(), {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    asyncio.run(service.init())
    asyncio.run(
        service.create_repo(Repo(id="repo", name="acme/widgets", git_url="https://forge/repo"))
    )
    asyncio.run(service.register_runner("host-1"))

    with TestClient(create_app(service)) as http:
        client = TaskServiceClient(http)
        task_id = client.create_task("repo", "spike", harness="pi")["id"]
        client.claim(task_id, "host-1")
        credentials = tmp_path / "credentials"
        credentials.mkdir()
        (credentials / "auth.json").write_text(
            '{"tokens":{"access_token":"codex-only"},"last_refresh":"yesterday"}'
        )
        monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://service")
        monkeypatch.setenv("PANOPTICON_TASK_ID", task_id)
        monkeypatch.setenv("PANOPTICON_RUNNER_ID", "host-1")
        monkeypatch.setenv("PANOPTICON_HARNESS", "pi")
        monkeypatch.setenv("PANOPTICON_CREDENTIALS", str(credentials))
        for key in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_OAUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "OPENAI_API_KEY",
            "CODEX_API_KEY",
            "CODEX_ACCESS_TOKEN",
        ):
            monkeypatch.delenv(key, raising=False)
        home = tmp_path / "home"

        agent.main(
            client_factory=lambda _url: client,  # type: ignore[arg-type,return-value]
            home=home,
            launch=lambda _harness, _ctx: pytest.fail("must fail before launch"),
            on_exit=lambda: pytest.fail("must fail before container stop"),
        )

        failed = client.get_task(task_id)
        detail = failed["lifecycle_detail"]
        assert failed["claimed_by"] == "host-1"
        assert failed["container_status"] == "failed"
        assert detail is not None and "~/.pi/agent/auth.json" in detail
        assert capsys.readouterr().err == f"{detail}\n"
        assert not (home / ".pi" / "agent" / "settings.json").exists()

        spawner = object.__new__(Spawner)
        spawner._client = client  # type: ignore[attr-defined]
        spawner._runner_id = "host-1"  # type: ignore[attr-defined]
        spawner._runner = mock.Mock(name="runner")  # type: ignore[attr-defined]
        spawner._runner.has_session.return_value = False  # type: ignore[attr-defined]
        spawner._executions = mock.Mock(name="executions")  # type: ignore[attr-defined]
        spawner._executions.is_shell.return_value = False  # type: ignore[attr-defined]
        spawner._respawns = {}  # type: ignore[attr-defined]
        spawner._pre_session_failures = set()  # type: ignore[attr-defined]
        daemon = HostDaemon(client, spawner, mock.Mock(name="provisioner"), runner_id="host-1")
        for _ in range(257):
            tasks, version_before = client.list_tasks_versioned()
            daemon.tick(tasks)
            _, version_after = client.list_tasks_versioned()
            assert version_after == version_before
            retained = client.get_task(task_id)
            assert retained["claimed_by"] == "host-1"
            assert retained["container_status"] == "failed"
            assert retained["lifecycle_detail"] == detail
        released = client.release(task_id)
        assert released["claimed_by"] is None
        assert released["lifecycle_detail"] is None


# 2119: REQ-050.4.4
@pytest.mark.parametrize("failure_stage", ["workflow", "bootstrap", "launch"])
def test_arbitrary_pi_bootstrap_failure_reaches_real_lifecycle_before_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str
) -> None:
    service = TaskService(SqlAlchemyStore(), {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    asyncio.run(service.init())
    asyncio.run(
        service.create_repo(Repo(id="repo", name="acme/widgets", git_url="https://forge/repo"))
    )
    asyncio.run(service.register_runner("host-1"))

    with TestClient(create_app(service)) as http:
        client = TaskServiceClient(http)
        task_id = client.create_task("repo", "spike", harness="pi")["id"]
        client.claim(task_id, "host-1")
        monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://service")
        monkeypatch.setenv("PANOPTICON_TASK_ID", task_id)
        monkeypatch.setenv("PANOPTICON_RUNNER_ID", "host-1")
        monkeypatch.setenv("PANOPTICON_HARNESS", "pi")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-valid")
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
        marker = Path(readiness_log(f"panopticon-{task_id}"))
        assert not marker.exists()

        expected_failure = f"{failure_stage} exploded"
        launch_client: object = client
        if failure_stage == "bootstrap":

            def fail_bootstrap(*_args: object, **_kwargs: object) -> None:
                raise RuntimeError(expected_failure)

            monkeypatch.setattr("panopticon.harnesses.pi.PiHarness.bootstrap", fail_bootstrap)
        elif failure_stage == "workflow":

            class _FailingWorkflowClient:
                def __getattr__(self, name: str) -> object:
                    return getattr(client, name)

                def list_skills(self, _task_id: str) -> list[dict[str, str]]:
                    raise RuntimeError(expected_failure)

            launch_client = _FailingWorkflowClient()

        def launch(_harness: object, _ctx: object) -> None:
            if failure_stage == "launch":
                raise RuntimeError(expected_failure)
            pytest.fail("must fail before launch")

        agent.main(
            client_factory=lambda _url: launch_client,  # type: ignore[arg-type,return-value]
            home=tmp_path / "home",
            launch=launch,  # type: ignore[arg-type]
            on_exit=lambda: pytest.fail("must fail before container stop"),
        )

        failed = client.get_task(task_id)
        assert failed["claimed_by"] == "host-1"
        assert failed["container_status"] == "failed"
        assert "pi" in failed["lifecycle_detail"]
        assert expected_failure in failed["lifecycle_detail"]
        assert not marker.exists()

        spawner = object.__new__(Spawner)
        spawner._client = client  # type: ignore[attr-defined]
        spawner._runner_id = "host-1"  # type: ignore[attr-defined]
        spawner._runner = mock.Mock(name="runner")  # type: ignore[attr-defined]
        spawner._runner.has_session.return_value = False  # type: ignore[attr-defined]
        spawner._executions = mock.Mock(name="executions")  # type: ignore[attr-defined]
        spawner._executions.is_shell.return_value = False  # type: ignore[attr-defined]
        spawner._respawns = {}  # type: ignore[attr-defined]
        spawner._pre_session_failures = set()  # type: ignore[attr-defined]
        daemon = HostDaemon(client, spawner, mock.Mock(name="provisioner"), runner_id="host-1")
        detail = failed["lifecycle_detail"]
        for _ in range(257):
            tasks, version_before = client.list_tasks_versioned()
            daemon.tick(tasks)
            _, version_after = client.list_tasks_versioned()
            assert version_after == version_before
            retained = client.get_task(task_id)
            assert retained["claimed_by"] == "host-1"
            assert retained["container_status"] == "failed"
            assert retained["lifecycle_detail"] == detail
            assert not marker.exists()
        released = client.release(task_id)
        assert released["claimed_by"] is None
        assert released["lifecycle_detail"] is None


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        interactive: bool = False,
        verbose: bool = False,
    ) -> str:
        command = list(args)
        self.calls.append(command)
        if "display-message" in command:
            return "%1\n"
        return ""


def test_tmux_output_path_is_installed_before_pi_launcher_with_tty_inheritance() -> None:
    recorder = _Recorder()
    LocalRunner("http://service", run=recorder).spawn(
        "pi-task",
        harness="pi",
        config_mount="/home/panopticon/.pi",
    )

    pipe_index = next(i for i, command in enumerate(recorder.calls) if "pipe-pane" in command)
    launch_index = next(i for i, command in enumerate(recorder.calls) if "respawn-pane" in command)
    docker_run = next(
        command for command in recorder.calls if command[:3] == ["docker", "run", "--detach"]
    )
    assert "PANOPTICON_HARNESS=pi" in docker_run
    launch = recorder.calls[launch_index]
    assert pipe_index < launch_index
    pipe = recorder.calls[pipe_index]
    assert pipe[3:7] == ["pipe-pane", "-O", "-t", "%1"]
    assert pipe[-1] == readiness_watch_command(readiness_log("panopticon-pi-task"))
    exec_index = launch.index("exec")
    assert launch[exec_index + 1 : exec_index + 3] == ["--interactive", "--tty"]
    assert launch[-3:] == ["python", "-m", "panopticon.container.agent"]
    assert not any(">" in token for token in launch)
