"""Local Docker + tmux runner (ADR 0008): the real execution backend on one host.

Spawns a **detached** task container on the host Docker daemon and a **host tmux** session
whose pane execs an interactive shell into it; the container's own entrypoint connects back to
the task service for liveness. We shell out to the ``docker`` and ``tmux`` CLIs — the
interactive surface (the container's TTY living in a tmux pane, and the operator's
``tmux attach``) is inherently CLI, and the Python SDKs don't serve it (see the ADR 0008
review). The command executor is **injectable** so the runner is unit-testable without a
daemon. LLM-free — the agent runs inside the container.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from panopticon.core.dirs import secrets_file_path
from panopticon.core.models import LifecyclePhase
from panopticon.harnesses import CREDENTIALS_MOUNT
from panopticon.harnesses.base import HONESTY_REVIEWER_ENV, REVIEWER_ENV_VARS
from panopticon.sessionservice.prefill import (
    DEFAULT_WAKE_TIMEOUT,
    prefill_pane,
    readiness_log,
    watch_pane,
)
from panopticon.sessionservice.runner import Runner
from panopticon.sessionservice.tmux_defaults import defaults_argv, new_session_argv
from panopticon.taskservice.auth import (
    load_tokens as load_service_tokens,
)
from panopticon.taskservice.auth import snapshot_task_capability

#: Default composed image (base layer, ADR 0005); built in a later PR of this slice.
DEFAULT_IMAGE = "panopticon-base"

#: Lets the container reach the host task service (container→host addressing, ADR 0008).
#: ``host-gateway`` maps to the host's gateway IP; native Linux uses the compatibility broad bind.
HOST_GATEWAY = "host.docker.internal:host-gateway"

#: Dedicated tmux server socket for panopticon's task sessions — isolates them from the
#: operator's own tmux and gives the terminal controller a known place to `tmux attach`.
TMUX_SOCKET = "panopticon"

logger = logging.getLogger(__name__)


def session_name(task_id: str) -> str:
    """The tmux session name (and, for a container task, its container name) for ``task_id``.

    The one definition of the ``panopticon-<task_id>`` convention, shared by :class:`LocalRunner`
    and :class:`~panopticon.sessionservice.shell_runner.ShellRunner` so the terminal supervisor's
    ``t`` attach and the daemon's self-heal probes reach a task's session the same way on either
    backend — a drift here would silently break attach/heal for one of them."""
    return f"panopticon-{task_id}"


#: Where a task's per-task clone is mounted — the one stable, writable path the agent works in
#: for the whole task (ADR 0011): planning, then coding on its branch once provisioned.
WORKSPACE_MOUNT = "/workspace"

#: The unprivileged in-container account the task runs as (created in the base image). The
#: entrypoint remaps it to the invoking user's uid/gid at start; `docker exec` for the agent pane
#: names it so the pane runs as that same user (ADR 0008 / the unprivileged-user work).
CONTAINER_USER = "panopticon"

#: The container home the harness config dirs hang off (matches the image's HOME).
CONTAINER_HOME = "/home/panopticon"

#: The default agent CLI's config dir inside the container (the claude harness's
#: ``config_dirname`` under :data:`CONTAINER_HOME`). A **per-task** named volume is mounted at
#: the task's harness's config dir so the CLI's session state survives respawn/recreate — the
#: container layer is thrown away each spawn, but the volume persists. Per-task (not per-repo)
#: so concurrent tasks don't share state.
CONFIG_MOUNT = "/home/panopticon/.claude"
SERVICE_AUTH_MOUNT = "/run/secrets/panopticon-service-auth"


class CommandRunner(Protocol):
    """Runs an external command and returns its stdout; ``check`` raises on non-zero exit.

    ``interactive`` attaches the caller's terminal (stdin/stdout/stderr) instead of capturing — for
    an interactive ``docker run -it``, where capturing would leave its TTY with no real input and
    hang. ``verbose`` also inherits the caller's streams but is for non-interactive commands whose
    output should be visible in the runner's tmux session (e.g. ``docker build``)."""

    def __call__(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        interactive: bool = False,
        verbose: bool = False,
    ) -> str: ...


def _subprocess_run(
    args: Sequence[str], *, check: bool = True, interactive: bool = False, verbose: bool = False
) -> str:
    if (
        interactive or verbose
    ):  # inherit streams: TTY attachment (interactive) or visible build output (verbose)
        subprocess.run(list(args), check=check)
        return ""
    return subprocess.run(list(args), check=check, capture_output=True, text=True).stdout


def _invoking_user() -> str:
    """The ``uid:gid`` of the host process invoking the runner — passed to the container (as
    ``PANOPTICON_PUID``/``PGID``) for its entrypoint to adopt, so the task runs **unprivileged** as
    that user and the files it writes to the bind-mounted ``/workspace`` (the per-task clone) are
    owned by the operator, not root. Matching the workspace owner's uid also sidesteps git's
    "dubious ownership" guard on the mounted checkout."""
    return f"{os.getuid()}:{os.getgid()}"


def _env_file_values(path: str | None, names: set[str]) -> list[str]:
    """Read literal values used by Docker's env-file format for selected names."""

    if path is None or not Path(path).is_file():
        return []
    values: list[str] = []
    for raw_line in Path(path).read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name in names:
            values.append(value)
    return values


def _env_file_names(path: str, names: set[str]) -> set[str]:
    """Return selected names present in a Docker env file without retaining their values."""

    env_path = Path(path)
    if not env_path.is_file():
        return set()
    found: set[str] = set()
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split("=", 1)[0]
        if name in names:
            found.add(name)
    return found


def _service_no_proxy(service_url: str, env_path: str | None, env: Mapping[str, str]) -> str:
    """Preserve configured bypasses and force the control-plane host off ambient proxies."""

    host = urlsplit(service_url).hostname
    if host is None:
        raise ValueError(f"task-service URL has no host: {service_url!r}")
    configured = _env_file_values(env_path, {"NO_PROXY", "no_proxy"})
    configured.extend(value for name in ("NO_PROXY", "no_proxy") if (value := env.get(name)))
    entries: list[str] = []
    for value in (*configured, host):
        for entry in value.split(","):
            entry = entry.strip()
            if entry and entry not in entries:
                entries.append(entry)
    return ",".join(entries)


class LocalRunner(Runner):
    """Runs task containers + host tmux on the local Docker daemon (one host)."""

    def __init__(
        self,
        service_url: str,
        *,
        image: str = DEFAULT_IMAGE,
        runner_id: str = "local",
        agent_command: Sequence[str] = ("python", "-m", "panopticon.container.agent"),
        tmux_socket: str | None = TMUX_SOCKET,
        extra_env: Mapping[str, str] | None = None,
        user: str | None = None,
        secrets_dir: str | Path | None = None,
        auth_file: str | None = None,
        run: CommandRunner = _subprocess_run,
    ) -> None:
        self._service_url = service_url
        self._image = image
        self._runner_id = runner_id
        # Root the repo's `env_file` name resolves against — this host's local secrets dir, so a
        # remote runner uses its own secrets (the stored value is host-agnostic; ADR 0007). None =
        # resolve the host's secrets dir dynamically at spawn.
        self._secrets_dir = secrets_dir
        self._auth_file = (
            auth_file if auth_file is not None else os.environ.get("PANOPTICON_SERVICE_AUTH_FILE")
        )
        # Run the task container unprivileged as the invoking user (uid:gid), so it can't act as
        # root on the host and its writes to the mounted workspace are owned by the operator.
        self._user = user if user is not None else _invoking_user()
        # What the tmux pane execs into the container: the in-container agent launcher (it
        # bootstraps the CLI then runs `claude`). `tmux attach` therefore reaches the live agent.
        self._agent_command = list(agent_command)
        self._tmux_socket = tmux_socket  # isolate panopticon's tmux server when set (-L)
        self._extra_env = dict(extra_env or {})
        self._run = run
        self._snapshot_dir = Path(tempfile.gettempdir())
        self._session_locks: dict[str, threading.Lock] = {}
        self._session_locks_guard = threading.Lock()
        self._warned_reviewer_env_files: set[str] = set()

    def _warn_legacy_reviewer_env(self, env_path: str) -> None:
        reviewer_names = {HONESTY_REVIEWER_ENV, *REVIEWER_ENV_VARS}
        found = _env_file_names(env_path, reviewer_names)
        if not found:
            return
        with self._session_locks_guard:
            if env_path in self._warned_reviewer_env_files:
                return
            self._warned_reviewer_env_files.add(env_path)
        logger.warning(
            "Repo env file %s contains inert reviewer setting(s): %s. Move reviewer selection "
            "to the repo fields honesty_reviewer, reviewer_1, and reviewer_2; env-file values "
            "are ignored.",
            env_path,
            ", ".join(sorted(found)),
        )

    def _session_lock(self, task_id: str) -> threading.Lock:
        with self._session_locks_guard:
            return self._session_locks.setdefault(task_id, threading.Lock())

    def _tmux(self, *args: str) -> list[str]:
        prefix = ["tmux", *(["-L", self._tmux_socket] if self._tmux_socket else [])]
        return [*prefix, *args]

    def deliver_session_input(
        self, task_id: str, delivery_id: str, text: str, *, submit: bool
    ) -> tuple[bool, str | None]:
        """Deliver through the same pre-armed watcher used by startup prompt delivery."""
        from panopticon.sessionservice.session_io import deliver_pane_input

        with self._session_lock(task_id):
            return deliver_pane_input(
                session_name(task_id),
                text,
                submit=submit,
                run=self._run,
                raw_log=readiness_log(session_name(task_id)),
                sleep=time.sleep,
                prefix=tuple(self._tmux()),
                buffer=f"panopticon-session-input-{delivery_id}",
            )

    def capture_session_transcript(self, task_id: str) -> dict[str, object] | None:
        """Capture a bounded, terminal-control-free pane snapshot."""
        from panopticon.sessionservice.session_io import capture_pane_snapshot

        return capture_pane_snapshot(
            session_name(task_id), run=self._run, prefix=tuple(self._tmux())
        )

    def validate_configuration(self) -> None:
        """Reject an unusable control-plane credential before any spawn side effect."""
        if self._auth_file:
            load_service_tokens(self._auth_file, secrets_dir=self._secrets_dir)

    def _remove_auth_snapshots(self, task_id: str) -> None:
        """Remove private snapshots left by a stopped or replaced task container."""
        for path in self._snapshot_dir.glob(f"panopticon-service-auth-{task_id}-*.json"):
            path.unlink(missing_ok=True)

    def spawn(
        self,
        task_id: str,
        *,
        env_file: str | None = None,
        workspace: str | None = None,
        image: str | None = None,
        docker_in_docker: bool = False,
        initial_prompt: str | None = None,
        turn: str | None = None,
        starting_model: str | None = None,
        harness: str | None = None,
        config_mount: str = CONFIG_MOUNT,
        credential_dir: str | None = None,
        honesty_reviewer: str | None = None,
        reviewer_1: str | None = None,
        reviewer_2: str | None = None,
        progress: Callable[[LifecyclePhase], None] | None = None,
    ) -> str:
        """Spawn the task container. ``env_file`` is the task's repo's secret reference (ADR
        0007) — a name **relative to this runner's secrets dir** (:data:`SECRETS_DIR`), resolved
        host-locally and injected at launch (``--env-file``), never baked into the image and never
        crossing the wire (so a remote runner uses its own host's secrets). ``workspace`` is the
        task's per-task clone on the host (ADR 0011), bind-mounted read-write at ``/workspace`` as
        the agent's working dir. ``image`` overrides the default base with the task's composed image
        (base → workflow → repo, ADR 0005); ``None`` uses the configured base. ``docker_in_docker``
        (the repo's ``capabilities``) runs the container ``--privileged`` and tells the entrypoint to
        start a nested Docker daemon — a trust escalation, opt-in per repo. ``initial_prompt``
        is passed as a positional arg to ``claude`` on the first run (no prior session) via the
        ``PANOPTICON_INITIAL_PROMPT`` env var; the agent starts autonomously without waiting for
        user input. ``turn`` is the
        task's current turn (``"agent"`` or ``"user"``); passed as ``PANOPTICON_TASK_TURN`` so the
        agent launcher can send :data:`~panopticon.container.agent.INTERRUPT_PROMPT` on respawn when
        the agent holds the turn. ``starting_model`` is the model the agent should start with
        (e.g. ``"opus"``); passed as ``PANOPTICON_STARTING_MODEL`` so the agent launcher can pass
        ``--model`` to ``claude`` on first launch. ``harness`` is the task's recorded
        agent-CLI harness name, exported as ``PANOPTICON_HARNESS`` for the in-container launcher;
        ``config_mount`` is that harness's config dir inside the container — where the per-task
        config volume lands (default: the claude harness's). ``credential_dir`` is the repo's
        shared-credential reference (a **directory** name under this runner's secrets dir, the
        sibling of ``env_file``), bind-mounted read-write at
        :data:`~panopticon.harnesses.CREDENTIALS_MOUNT` and exported as
        ``PANOPTICON_CREDENTIALS`` — shared across the repo's containers on purpose (one rotating
        credential chain, every session converges on it). ``progress`` (optional) is called with
        each spawn phase the runner passes through (``STARTING`` before ``docker run``,
        ``AWAITING`` once the tmux session is up) so the caller can surface it — see
        :class:`~panopticon.core.models.LifecyclePhase`."""

        def _report(phase: LifecyclePhase) -> None:
            if progress is not None:
                progress(phase)

        # The container name doubles as the tmux session name, so stop() needs only the id.
        container = session_name(task_id)
        puid, _, pgid = self._user.partition(":")
        env = {
            "PANOPTICON_SERVICE_URL": self._service_url,
            "PANOPTICON_TASK_ID": task_id,
            "PANOPTICON_CONTAINER_ID": container,
            "PANOPTICON_RUNNER_ID": self._runner_id,
            # The entrypoint adopts these: it remaps the `panopticon` user to the invoking uid/gid
            # and drops to it (so the task runs unprivileged, owning what it writes to /workspace).
            "PANOPTICON_PUID": puid,
            "PANOPTICON_PGID": pgid,
            # Optional runtime controls are explicit too: Docker applies --env after --env-file,
            # so empty values prevent a repo credential file from injecting stale control-plane
            # state when the runner did not supply that option.
            "PANOPTICON_RECONNECT_BACKOFF": "",
            "PANOPTICON_PROPOSED_SLUG": "",
            "PANOPTICON_INITIAL_PROMPT": "",
            "PANOPTICON_TASK_TURN": "",
            "PANOPTICON_STARTING_MODEL": "",
            "PANOPTICON_HARNESS": "",
            "PANOPTICON_CREDENTIALS": "",
            "PANOPTICON_DOCKER_IN_DOCKER": "",
            HONESTY_REVIEWER_ENV: "",
            REVIEWER_ENV_VARS[0]: "",
            REVIEWER_ENV_VARS[1]: "",
            **self._extra_env,
        }
        # These are repo settings, not secrets. Render them after the env-file transport so an
        # explicit empty value also prevents a credentials file from becoming a config surface.
        env[HONESTY_REVIEWER_ENV] = honesty_reviewer or ""
        env[REVIEWER_ENV_VARS[0]] = reviewer_1 or ""
        env[REVIEWER_ENV_VARS[1]] = reviewer_2 or ""
        if initial_prompt:
            # The agent launcher reads this and passes it as a positional arg to `claude` on the
            # first run (no prior session), so the agent's first action is to process the prompt.
            env["PANOPTICON_INITIAL_PROMPT"] = initial_prompt
        if turn:
            env["PANOPTICON_TASK_TURN"] = turn
        if starting_model:
            env["PANOPTICON_STARTING_MODEL"] = starting_model
        if harness:
            env["PANOPTICON_HARNESS"] = harness
        docker_run = [
            "docker",
            "run",
            "--detach",
            "--name",
            container,
            "--label",
            f"panopticon.task={task_id}",
            "--add-host",
            HOST_GATEWAY,
        ]
        if (
            docker_in_docker
        ):  # privileged nested Docker daemon (repo capability); entrypoint starts it
            docker_run.append("--privileged")
            docker_run += ["--volume", f"panopticon-dind-{task_id}:/var/lib/docker"]
            env["PANOPTICON_DOCKER_IN_DOCKER"] = "1"
        if env_path := secrets_file_path(env_file, secrets_dir=self._secrets_dir):
            self._warn_legacy_reviewer_env(env_path)
            docker_run += ["--env-file", env_path]  # per-repo secrets, resolved host-locally
        auth_snapshot: Path | None = None
        if workspace:  # the per-task clone — the agent's writable working dir (ADR 0011)
            docker_run += [
                "--volume",
                f"{workspace}:{WORKSPACE_MOUNT}",
                "--workdir",
                WORKSPACE_MOUNT,
            ]
        if credential_path := secrets_file_path(credential_dir, secrets_dir=self._secrets_dir):
            # The repo's shared credential dir (read-write: the CLI refreshes tokens in place).
            docker_run += ["--volume", f"{credential_path}:{CREDENTIALS_MOUNT}"]
            env["PANOPTICON_CREDENTIALS"] = CREDENTIALS_MOUNT
        # Per-task config volume: persists the agent CLI's session history across respawn/recreate
        # (the transcripts live in the config dir, otherwise thrown away with the container).
        docker_run += ["--volume", f"panopticon-config-{task_id}:{config_mount}"]
        if self._auth_file:
            self._remove_auth_snapshots(task_id)
            auth_snapshot = snapshot_task_capability(
                self._auth_file,
                task_id,
                directory=self._snapshot_dir,
                secrets_dir=self._secrets_dir,
                prefix=f"panopticon-service-auth-{task_id}-",
            )
            docker_run += ["--volume", f"{auth_snapshot}:{SERVICE_AUTH_MOUNT}:ro"]
            env["PANOPTICON_SERVICE_AUTH_FILE"] = SERVICE_AUTH_MOUNT
        else:
            # An env-file belongs to the repo and must not opt a task into a stale or attacker-
            # controlled control-plane credential when this runner has authentication disabled.
            env["PANOPTICON_SERVICE_AUTH_FILE"] = ""
        env["PANOPTICON_SERVICE_AUTH_TOKEN"] = ""
        # Native MCP clients honor the standard proxy variables inherited from the repo env-file.
        # Pin both spellings after that file so neither Claude nor Codex can send the fleet bearer
        # through an ambient proxy, while retaining bypasses needed by the repo's other traffic.
        no_proxy = _service_no_proxy(self._service_url, env_path, env)
        env["NO_PROXY"] = no_proxy
        env["no_proxy"] = no_proxy
        for key, value in env.items():
            docker_run += ["--env", f"{key}={value}"]
        docker_run.append(
            image or self._image
        )  # composed image if given, else base; its entrypoint runs
        # Clear any stale tmux session + container first — handles both a prior exited run and a
        # live force-respawn (dashboard `R` kills and restarts). Both are no-ops when nothing
        # exists, so spawn is fully idempotent. (`stop()` does the same pair.)
        try:
            self._run(
                self._tmux(*defaults_argv(self._tmux_socket), "kill-session", "-t", container),
                check=False,
            )
            self._run(["docker", "rm", "--force", container], check=False)
            _report(LifecyclePhase.STARTING)  # docker run + the tmux session coming up
            self._run(docker_run)
            # Docker has opened the bind source by the time detached ``docker run`` returns. The
            # mount retains that inode, so remove the host pathname immediately; this prevents a
            # one-shot runner process from stranding a fleet credential after it exits.
            if auth_snapshot is not None:
                auth_snapshot.unlink(missing_ok=True)
        except BaseException:
            if auth_snapshot is not None:
                auth_snapshot.unlink(missing_ok=True)
            raise
        # `docker run --detach` returns once the container is running (the entrypoint has remapped +
        # dropped), so the pane execs in as the unprivileged `panopticon` user — `tmux attach` and
        # the agent's `whoami` see that named user, not root.
        # Create the pane before starting the agent so its persistent pipe catches the CLI's first
        # bracketed-paste-ready signal. A wake may arrive much later, after an idle CLI has stopped
        # producing output; attaching the watcher at delivery time would miss that earlier signal.
        # The placeholder command (`sleep`, not a shell) must stay inert — a shell here could echo
        # its own bracketed-paste-ready marker before the agent CLI ever starts. This is also the
        # session-creating call, so it's the one that must carry the shipped tmux defaults (REQ-030)
        # via `-f`: a fresh socket's server only picks them up from whichever call starts it.
        try:
            self._run(
                self._tmux(
                    *new_session_argv(self._tmux_socket),
                    "-d",
                    "-s",
                    container,
                    "sleep 86400",
                )
            )
            pane = watch_pane(
                container,
                run=self._run,
                prefix=self._tmux(),
                raw_log=readiness_log(container),
            )
            self._run(
                self._tmux(
                    "respawn-pane",
                    "-k",
                    "-t",
                    pane or container,
                    "docker",
                    "exec",
                    "--interactive",
                    "--tty",
                    "--user",
                    CONTAINER_USER,
                    container,
                    *self._agent_command,
                )
            )
            _report(
                LifecyclePhase.AWAITING
            )  # container + tmux up; waiting for its /live registration
        except BaseException:
            try:
                self._run(self._tmux("kill-session", "-t", container), check=False)
            finally:
                try:
                    self._run(["docker", "rm", "--force", container], check=False)
                finally:
                    if auth_snapshot is not None:
                        auth_snapshot.unlink(missing_ok=True)
            raise
        return container

    def is_running(self, task_id: str) -> bool:
        """Whether the task's container is currently running on this host's Docker daemon.

        A ``docker ps`` (running containers only) filtered to the task's container name: empty
        output means the container is gone or exited — i.e. the task is **down** and should be
        respawned. Used by the host daemon to reconcile a claimed task that never came up (or
        died) into the displayed ``down`` status."""
        container = session_name(task_id)
        names = self._run(
            ["docker", "ps", "--filter", f"name=^{container}$", "--format", "{{.Names}}"],
            check=False,
        )
        return bool(names.strip())

    def has_session(self, task_id: str) -> bool:
        """Whether the task's host tmux session exists on this runner's tmux server.

        Lists the panopticon tmux server's sessions and looks for ``panopticon-<id>``; an empty list
        (or no server at all) means the session is gone. We list-and-match rather than ``has-session``
        because the command runner reports stdout, not exit status, and ``has-session`` signals only
        through its exit code.

        Distinct from :meth:`is_running` (the *container*): a kill of the ``-L panopticon`` tmux server
        that *isn't* ``make stop`` — a crash, a manual ``tmux kill-server``, a single killed session —
        leaves the detached containers running, so a task can be ``is_running`` yet have **no session**:
        the orphan the host daemon self-heals by respawning. (``make stop`` itself now stops the task
        containers too, so it leaves nothing running — but the still-claimed task is likewise healed on
        the next start.)"""
        session = session_name(task_id)
        sessions = self._run(self._tmux("list-sessions", "-F", "#{session_name}"), check=False)
        return session in sessions.splitlines()

    def submit_prompt(self, task_id: str, prompt: str) -> bool:
        """Wait for the task's input box, bracketed-paste ``prompt``, and submit it once."""
        fd, prompt_file = tempfile.mkstemp(
            prefix=f"panopticon-stage-entry-{task_id}-", suffix=".txt"
        )
        with os.fdopen(fd, "w") as handle:
            handle.write(prompt)
        try:
            timeout = float(
                os.environ.get("PANOPTICON_STAGE_ENTRY_WAKE_TIMEOUT", DEFAULT_WAKE_TIMEOUT)
            )
        except ValueError:
            timeout = DEFAULT_WAKE_TIMEOUT
        prefix = ["tmux", *(["-L", self._tmux_socket] if self._tmux_socket else [])]
        session = session_name(task_id)
        with self._session_lock(task_id):
            return prefill_pane(
                session,
                prompt_file,
                run=self._run,
                prefix=prefix,
                timeout=timeout,
                raw_log=readiness_log(session),
                buffer=f"panopticon-stage-entry-{task_id}",
                submit=True,
                watch=False,
                settle_delay=0,
            )

    def delete_workspace_contents(self, path: str) -> None:
        """Delete all files inside ``path`` by running a throwaway root Docker container.

        A task container may write root-owned files (e.g. ``.mypy_cache`` before the
        entrypoint's uid remap, or via ``docker_in_docker``). This spawns a short-lived
        ``--rm`` container as root with ``path`` bind-mounted and deletes everything inside
        it, so the daemon can then ``rmtree`` the now-empty directory. Overrides the
        panopticon entrypoint (which would remap uid) so the container runs as root and can
        reach files it created. Raises on nonzero docker exit."""
        self._run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "/bin/sh",
                "--volume",
                f"{path}:/cleanup",
                self._image,
                "-c",
                "find /cleanup -mindepth 1 -delete",
            ]
        )

    def stop(self, container_id: str) -> None:
        # Idempotent: tolerate an already-gone session/container.
        try:
            self._run(self._tmux("kill-session", "-t", container_id), check=False)
        finally:
            try:
                self._run(["docker", "rm", "--force", container_id], check=False)
            finally:
                self._remove_auth_snapshots(container_id.removeprefix("panopticon-"))
