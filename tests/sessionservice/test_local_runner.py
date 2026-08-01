"""LocalRunner: unit tests pin the emitted docker/tmux commands; one integration test
exercises a real container + tmux session (skipped when docker/tmux are unavailable)."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from panopticon.core.models import LifecyclePhase
from panopticon.sessionservice.local_runner import LocalRunner
from panopticon.sessionservice.prefill import readiness_log, readiness_watch_command
from panopticon.sessionservice.runner import Runner
from panopticon.sessionservice.tmux_defaults import server_default_config_text


class _Recorder:
    """An injectable CommandRunner that records calls instead of running them."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bool]] = []
        self.interactive: list[bool] = []

    def __call__(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        interactive: bool = False,
        verbose: bool = False,
    ) -> str:
        self.calls.append((list(args), check))
        self.interactive.append(interactive)
        if "display-message" in args:
            return "%1\n"
        return ""


def test_local_runner_is_a_runner() -> None:
    assert issubclass(LocalRunner, Runner)


def test_spawn_runs_detached_container_then_tmux_pane_execing_in() -> None:
    rec = _Recorder()
    runner = LocalRunner("http://svc:8000", image="img:1", runner_id="r1", run=rec)

    container_id = runner.spawn("t1")

    assert container_id == "panopticon-t1"
    (
        (kill_session, _),
        (rm, _),
        (docker_run, _),
        (tmux_new, _),
        (display, _),
        (pipe, _),
        (respawn, _),
    ) = rec.calls
    # clear any stale tmux session first (idempotent — no-op when nothing exists)
    assert kill_session == ["tmux", "-L", "panopticon", "kill-session", "-t", "panopticon-t1"]
    assert rm == ["docker", "rm", "--force", "panopticon-t1"]  # then clear a stale container
    assert docker_run[:3] == ["docker", "run", "--detach"]
    assert docker_run[-1] == "img:1"  # the image is the final positional arg (its entrypoint runs)
    assert docker_run[3:5] == ["--name", "panopticon-t1"]
    assert "PANOPTICON_SERVICE_URL=http://svc:8000" in docker_run
    assert "PANOPTICON_TASK_ID=t1" in docker_run
    assert "PANOPTICON_CONTAINER_ID=panopticon-t1" in docker_run
    assert "PANOPTICON_RUNNER_ID=r1" in docker_run
    # container -> host addressing so the container can reach the task service
    assert docker_run[docker_run.index("--add-host") + 1] == "host.docker.internal:host-gateway"
    # the tmux session (on the default `panopticon` socket) shares the container name; its
    # pane execs the in-container agent launcher (so `tmux attach` reaches the live agent).
    # This is also the session-creating call, so it carries the shipped tmux defaults (REQ-030)
    # via `-f` — the inert placeholder command (`sleep`, not a shell) is unaffected.
    assert tmux_new[:3] == ["tmux", "-L", "panopticon"]
    assert tmux_new[tmux_new.index("new-session") + 1 :][:2] == ["-d", "-s"]
    assert tmux_new[tmux_new.index("-s") + 1] == "panopticon-t1"
    assert tmux_new[3] == "-f"
    assert Path(
        tmux_new[4]
    ).is_file()  # exact defaults-content coverage lives in test_tmux_defaults.py
    assert tmux_new[5:] == [
        "new-session",
        "-d",
        "-s",
        "panopticon-t1",
        "sleep 86400",
    ]
    assert display == [
        "tmux",
        "-L",
        "panopticon",
        "display-message",
        "-p",
        "-t",
        "panopticon-t1",
        "#{pane_id}",
    ]
    assert pipe == [
        "tmux",
        "-L",
        "panopticon",
        "pipe-pane",
        "-O",
        "-t",
        "%1",
        readiness_watch_command(readiness_log("panopticon-t1")),
    ]
    # The watcher is installed before respawn starts the agent, so an idle pane retains the CLI's
    # first bracketed-paste-ready signal for a later stage-entry wake.
    assert respawn[:8] == [
        "tmux",
        "-L",
        "panopticon",
        "respawn-pane",
        "-k",
        "-t",
        "%1",
        "docker",
    ]
    # the pane execs in as the unprivileged `panopticon` user (so the agent's whoami isn't root)
    assert respawn[-10:] == [
        "docker",
        "exec",
        "--interactive",
        "--tty",
        "--user",
        "panopticon",
        "panopticon-t1",
        "python",
        "-m",
        "panopticon.container.agent",
    ]


def test_spawn_reports_starting_then_awaiting_via_the_progress_callback() -> None:
    runner = LocalRunner("http://svc:8000", run=_Recorder())
    phases: list[LifecyclePhase] = []
    runner.spawn("t1", progress=phases.append)
    # STARTING just before `docker run`, AWAITING once the container + tmux session are up
    assert phases == [LifecyclePhase.STARTING, LifecyclePhase.AWAITING]


class _ReturningRecorder(_Recorder):
    """A recorder whose calls return a canned stdout (for parsing ``docker ps`` output)."""

    def __init__(self, output: str) -> None:
        super().__init__()
        self._output = output

    def __call__(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        interactive: bool = False,
        verbose: bool = False,
    ) -> str:
        super().__call__(args, check=check, interactive=interactive)
        return self._output


def test_is_running_queries_docker_ps_by_container_name() -> None:
    rec = _ReturningRecorder("panopticon-t1\n")
    runner = LocalRunner("http://svc:8000", run=rec)
    assert runner.is_running("t1") is True
    ((ps, check),) = rec.calls
    assert ps == ["docker", "ps", "--filter", "name=^panopticon-t1$", "--format", "{{.Names}}"]
    assert check is False  # tolerate a daemon hiccup rather than raise


def test_is_running_is_false_when_no_container_is_listed() -> None:
    runner = LocalRunner("http://svc:8000", run=_Recorder())  # records, returns "" → not running
    assert runner.is_running("t1") is False


def test_has_session_lists_the_tmux_server_and_matches_the_session_name() -> None:
    rec = _ReturningRecorder("panopticon-t1\npanopticon-t2\n")  # two sessions on the server
    runner = LocalRunner("http://svc:8000", run=rec)
    assert runner.has_session("t1") is True
    ((ls, check),) = rec.calls
    assert ls == ["tmux", "-L", "panopticon", "list-sessions", "-F", "#{session_name}"]
    assert (
        check is False
    )  # an empty list (or no server at all) just means "no session", not an error


def test_has_session_is_false_when_the_session_is_absent() -> None:
    # No server running (e.g. after `make stop`) → list-sessions prints nothing → not a session.
    assert LocalRunner("http://svc:8000", run=_Recorder()).has_session("t1") is False
    # A server with *other* sessions but not this task's is still a miss (no substring false-match).
    runner = LocalRunner("http://svc:8000", run=_ReturningRecorder("panopticon-t10\n"))
    assert runner.has_session("t1") is False


def test_spawn_runs_container_unprivileged_as_the_invoking_user() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", run=rec).spawn("t1")
    docker_run = rec.calls[2][0]
    # the entrypoint adopts these and drops to the `panopticon` user (no root, no bare numeric uid)
    assert f"PANOPTICON_PUID={os.getuid()}" in docker_run
    assert f"PANOPTICON_PGID={os.getgid()}" in docker_run
    assert "--user" not in docker_run  # adoption happens in the entrypoint, not via docker --user


def test_spawn_user_can_be_overridden() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", user="1234:5678", run=rec).spawn("t1")
    docker_run = rec.calls[2][0]
    assert "PANOPTICON_PUID=1234" in docker_run and "PANOPTICON_PGID=5678" in docker_run


def test_spawn_without_docker_in_docker_is_not_privileged() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", run=rec).spawn("t1")
    docker_run = rec.calls[2][0]
    assert "--privileged" not in docker_run
    assert "PANOPTICON_DOCKER_IN_DOCKER=1" not in docker_run


def test_spawn_with_docker_in_docker_runs_privileged_and_flags_the_entrypoint() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", run=rec).spawn("t1", docker_in_docker=True)
    docker_run = rec.calls[2][0]
    assert "--privileged" in docker_run  # nested daemon needs it (repo capability, ADR 0005)
    assert "panopticon-dind-t1:/var/lib/docker" in docker_run  # per-task docker layer cache
    assert "PANOPTICON_DOCKER_IN_DOCKER=1" in docker_run  # entrypoint starts dockerd


def test_extra_env_is_forwarded() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", extra_env={"PANOPTICON_RECONNECT_BACKOFF": "0.5"}, run=rec).spawn(
        "t1"
    )
    assert "PANOPTICON_RECONNECT_BACKOFF=0.5" in rec.calls[2][0]


def test_spawn_resolves_env_file_against_the_runners_secrets_dir() -> None:
    # env_file is a name relative to this runner's secrets dir (ADR 0007 / remote runners), so the
    # runner resolves it host-locally rather than trusting an absolute path from another host.
    rec = _Recorder()
    LocalRunner("http://svc", secrets_dir="/host/secrets", run=rec).spawn("t1", env_file="r1.env")
    docker_run = rec.calls[2][0]
    assert docker_run[docker_run.index("--env-file") + 1] == "/host/secrets/r1.env"


def test_spawn_rejects_env_file_name_escaping_the_secrets_dir() -> None:
    rec = _Recorder()
    with pytest.raises(ValueError):
        LocalRunner("http://svc", secrets_dir="/host/secrets", run=rec).spawn(
            "t1", env_file="../evil.env"
        )


def test_spawn_omits_secret_flags_when_repo_has_none() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", run=rec).spawn("t1")
    docker_run = rec.calls[2][0]
    assert "--env-file" not in docker_run  # no API-key env-file
    # (the per-task config volume is always mounted — that's not a per-repo secret)


def test_spawn_mounts_the_per_task_clone_as_the_workspace() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", run=rec).spawn("t1", workspace="/tasks/t1")
    docker_run = rec.calls[2][0]
    assert "/tasks/t1:/workspace" in docker_run  # the per-task clone, read-write (ADR 0011)
    assert docker_run[docker_run.index("--workdir") + 1] == "/workspace"  # the agent's working dir


def test_spawn_mounts_a_per_task_config_volume_for_claude_history() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", run=rec).spawn("t1")
    docker_run = rec.calls[2][0]
    # a task-scoped named volume at the config dir → claude's transcripts survive respawn/recreate
    assert "panopticon-config-t1:/home/panopticon/.claude" in docker_run


def test_spawn_mounts_the_config_volume_at_the_harness_config_dir() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", run=rec).spawn(
        "t1", harness="codex", config_mount="/home/panopticon/.codex"
    )
    docker_run = rec.calls[2][0]
    # the same per-task volume lands wherever the task's harness keeps its session state
    assert "panopticon-config-t1:/home/panopticon/.codex" in docker_run
    assert "PANOPTICON_HARNESS=codex" in docker_run  # the launcher dispatches on this


def test_spawn_omits_the_harness_env_var_by_default() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", run=rec).spawn("t1")
    docker_run = rec.calls[2][0]
    assert not any(a.startswith("PANOPTICON_HARNESS=") for a in docker_run)  # None = default


def test_spawn_mounts_the_repo_credential_dir_read_write(tmp_path: Path) -> None:
    (tmp_path / "openai.d").mkdir()
    rec = _Recorder()
    LocalRunner("http://svc", secrets_dir=tmp_path, run=rec).spawn("t1", credential_dir="openai.d")
    docker_run = rec.calls[2][0]
    # the shared credential dir (ADR 0007's directory-shaped sibling of env_file): resolved
    # against this runner's own secrets dir, mounted rw, and announced to the harness bootstrap
    assert f"{tmp_path / 'openai.d'}:/panopticon/credentials" in docker_run
    assert "PANOPTICON_CREDENTIALS=/panopticon/credentials" in docker_run


def test_spawn_omits_the_credential_mount_when_the_repo_has_none() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", run=rec).spawn("t1")
    docker_run = rec.calls[2][0]
    assert not any("/panopticon/credentials" in a for a in docker_run)


def test_spawn_passes_initial_prompt_as_env_var() -> None:
    rec = _Recorder()
    runner = LocalRunner("http://svc", run=rec)
    runner.spawn("t1", initial_prompt="review your plan")
    docker_run = rec.calls[2][0]
    assert "PANOPTICON_INITIAL_PROMPT=review your plan" in docker_run


def test_spawn_passes_turn_as_env_var() -> None:
    rec = _Recorder()
    runner = LocalRunner("http://svc", run=rec)
    runner.spawn("t1", turn="agent")
    docker_run = rec.calls[2][0]
    assert "PANOPTICON_TASK_TURN=agent" in docker_run


def test_spawn_omits_turn_env_var_when_not_set() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", run=rec).spawn("t1")
    docker_run = rec.calls[2][0]
    assert not any("PANOPTICON_TASK_TURN" in arg for arg in docker_run)


def test_spawn_uses_the_composed_image_when_given_else_the_base() -> None:
    rec = _Recorder()
    runner = LocalRunner("http://svc", image="panopticon-base", run=rec)
    runner.spawn("t1")  # no override → base
    assert rec.calls[2][0][-1] == "panopticon-base"
    first_spawn_calls = len(rec.calls)
    runner.spawn("t2", image="panopticon-github-peer-reviewed-r1")  # composed image (ADR 0005)
    # t2's docker run is the 3rd call of its own spawn (kill-session, rm, run, ...)
    assert rec.calls[first_spawn_calls + 2][0][-1] == "panopticon-github-peer-reviewed-r1"


def test_stop_kills_session_and_force_removes_container_idempotently() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", run=rec).stop("panopticon-t1")
    assert (["tmux", "-L", "panopticon", "kill-session", "-t", "panopticon-t1"], False) in rec.calls
    assert (["docker", "rm", "--force", "panopticon-t1"], False) in rec.calls


def test_delete_workspace_contents_runs_root_container_to_empty_directory() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", image="panopticon-base", run=rec).delete_workspace_contents(
        "/tasks/t1"
    )
    assert rec.calls == [
        (
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "/bin/sh",
                "--volume",
                "/tasks/t1:/cleanup",
                "panopticon-base",
                "-c",
                "find /cleanup -mindepth 1 -delete",
            ],
            True,
        )
    ]


def test_tmux_socket_can_be_overridden() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", tmux_socket="panopt", run=rec).spawn("t1")
    tmux_new = next(c for c, _ in rec.calls if "new-session" in c)
    assert tmux_new[:3] == ["tmux", "-L", "panopt"]
    assert "new-session" in tmux_new


# 2119: REQ-030.3.1
def test_spawn_loads_every_shipped_tmux_server_default_via_dash_f_on_its_own_new_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A task container spawn may be the very first thing to touch a fresh `-L panopticon` socket
    # (e.g. the runner daemon spawning ahead of any dashboard) — it must not rely on some other
    # process having set these up first. Separate `tmux set-option ...` calls do not persist on a
    # fresh socket (tmux tears a sessionless server back down between client invocations), so `-f`
    # must be threaded onto THIS spawn's own new-session, loading every REQ-030.1/.2 default (not
    # just one) atomically with the session it creates.
    monkeypatch.setattr("shutil.which", lambda _tool: None)  # deterministic: no clipboard tool
    rec = _Recorder()
    LocalRunner("http://svc", run=rec).spawn("t1")
    tmux_new = next(c for c, _ in rec.calls if "new-session" in c)
    assert tmux_new[:3] == ["tmux", "-L", "panopticon"]
    assert tmux_new[3] == "-f"
    config_path = Path(tmux_new[4])
    assert tmux_new[5] == "new-session"
    assert config_path.read_text() == server_default_config_text(clipboard=None)


# 2119: REQ-030.3.2
def test_spawn_places_dash_f_before_new_session_so_it_applies_at_server_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `-f` only takes effect when it precedes the subcommand that starts a brand-new server;
    # placed after `new-session` it has no effect on that session's server at all, and
    # history-limit (bound per-pane at creation, never retroactively) would silently fall back to
    # tmux's stock 2000.
    monkeypatch.setattr("shutil.which", lambda _tool: None)
    rec = _Recorder()
    LocalRunner("http://svc", run=rec).spawn("t1")
    tmux_new = next(c for c, _ in rec.calls if "new-session" in c)
    assert tmux_new.index("-f") < tmux_new.index("new-session")


# 2119: REQ-030.5.1
def test_spawn_applies_no_shipped_defaults_without_a_dedicated_socket() -> None:
    # tmux_socket=None means "talk to the ambient default tmux server" (no -L) — that could be an
    # operator's own personal server, which panopticon's shipped defaults must never touch.
    rec = _Recorder()
    LocalRunner("http://svc", tmux_socket=None, run=rec).spawn("t1")
    assert not any("-f" in c for c, _ in rec.calls)


# -- integration: real docker + tmux ------------------------------------------------

_HAVE_DOCKER_TMUX = bool(shutil.which("docker") and shutil.which("tmux"))


def _docker_running() -> bool:
    return (
        _HAVE_DOCKER_TMUX
        and subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    )


# 2119: REQ-030.1.1
# 2119: REQ-030.3.1
@pytest.mark.skipif(not _docker_running(), reason="needs a working docker daemon + tmux")
def test_spawn_and_stop_real_container_and_session() -> None:
    image = "panopticon-itest:latest"
    socket = "panopticon-itest"
    subprocess.run(
        ["docker", "build", "--tag", image, "-"],
        # a `panopticon` user so the agent pane's `docker exec --user panopticon` resolves
        input='FROM alpine\nRUN adduser -D -u 1000 panopticon\nENTRYPOINT ["sleep", "3600"]\n',
        text=True,
        check=True,
        capture_output=True,
    )
    runner = LocalRunner(
        "http://unused", image=image, runner_id="itest", agent_command=["sh"], tmux_socket=socket
    )
    cid = "panopticon-itest1"
    try:
        assert runner.spawn("itest1") == cid
        running = ""
        for _ in range(50):  # `docker run -d` returns once running; poll defensively
            running = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}", cid],
                capture_output=True,
                text=True,
            ).stdout.strip()
            if running == "true":
                break
            time.sleep(0.1)
        assert running == "true"
        assert (
            subprocess.run(
                ["tmux", "-L", socket, "has-session", "-t", cid], capture_output=True
            ).returncode
            == 0
        )
        # the shipped tmux defaults (REQ-030) landed on this genuinely fresh socket's server
        mouse = subprocess.run(
            ["tmux", "-L", socket, "show-options", "-g", "mouse"], capture_output=True, text=True
        ).stdout.strip()
        assert mouse == "mouse on"

        runner.stop(cid)
        assert subprocess.run(["docker", "inspect", cid], capture_output=True).returncode != 0
        assert (
            subprocess.run(
                ["tmux", "-L", socket, "has-session", "-t", cid], capture_output=True
            ).returncode
            != 0
        )
    finally:
        subprocess.run(["docker", "rm", "--force", cid], capture_output=True)
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)
        subprocess.run(["docker", "rmi", "--force", image], capture_output=True)


class _FakeClient:
    """Stands in for TaskServiceClient: maps the task to its repo + that repo's secret refs."""

    def __init__(self, repo: dict[str, object]) -> None:
        self._repo = repo

    def get_task(self, task_id: str) -> dict[str, object]:
        return {"id": task_id, "repo_id": "r1"}

    def get_repo(self, repo_id: str) -> dict[str, object]:
        return self._repo


def test_cli_preps_the_workspace_then_spawns_with_secrets_and_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import panopticon.core.dirs as dirs_mod
    from panopticon.sessionservice import __main__ as cli
    from panopticon.sessionservice.__main__ import main as cli_main

    rec = _Recorder()
    fake = _FakeClient({"id": "r1", "git_url": "https://forge/r1.git", "env_file": "r1.env"})
    # The clone cache and per-task clones roots are the base-dir defaults (no per-path flags); the
    # defaults are import-time constants, so point them at tmp dirs by patching them in place.
    cache_root, tasks_root = tmp_path / "cache", tmp_path / "tasks"
    monkeypatch.setattr(cli, "CLONE_CACHE_DIR", str(cache_root))
    monkeypatch.setattr(cli, "TASKS_DIR", str(tasks_root))
    # The runner resolves the repo's env_file *name* against this host's secrets dir (ADR 0007).
    monkeypatch.setattr(dirs_mod, "user_config_dir", lambda: tmp_path)
    cid = cli_main(
        ["t1", "--service-url", "http://svc:9", "--image", "img:2"],
        run=rec,
        client=fake,  # type: ignore[arg-type]
    )
    assert cid == "panopticon-t1"
    cmds = [c for c, _ in rec.calls]
    # spawn-prep cloned the per-task checkout (ADR 0011) before launching the container
    assert ["git", "clone", "--local", str(cache_root / "r1"), str(tasks_root / "t1")] in cmds
    docker_run = next(c for c in cmds if c[:2] == ["docker", "run"])
    assert "PANOPTICON_SERVICE_URL=http://svc:9" in docker_run
    assert docker_run[-1] == "img:2"
    assert docker_run[docker_run.index("--env-file") + 1] == str(
        tmp_path / "secrets" / "r1.env"
    )  # repo's secrets
    assert f"{tasks_root}/t1:/workspace" in docker_run  # the per-task clone mounted as /workspace
