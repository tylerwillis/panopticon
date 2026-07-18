"""First-time setup helpers for ``panopticon quickstart``.

Detects and confirms the repo's default harness, registers the repo quickstart is run in with the
task service (idempotent — deduped on the remote URL), writes a secrets template when absent, and
creates a harness-aware ``setup-repo`` task the console attaches to for auth setup.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from panopticon.client import TaskServiceClient
from panopticon.harnesses import DEFAULT_HARNESS, HARNESSES
from panopticon.harnesses.base import Harness
from panopticon.terminal.setup_repo_task import SETUP_REPO_WORKFLOW, create_setup_repo_task

_FALLBACK_GIT_URL = "https://github.com/Unsupervisedcom/panopticon.git"

#: Task states past which a setup-repo task is done — used to decide whether to reuse one.
_TERMINAL_STATES = {"COMPLETE", "DROPPED"}

#: The opt-in coding workflows quickstart enables for a repo (kept in sync with the workflow
#: classes' ``name`` ClassVars): the forge lifecycle for hosted remotes, the forge-free one for
#: local-only repos.
_FORGE_WORKFLOW = "github-peer-reviewed"
_LOCAL_WORKFLOW = "local-git-self-reviewed"

#: URL schemes that mean a networked (hosted-forge) remote rather than a local path.
_FORGE_SCHEMES = ("https://", "http://", "ssh://", "git://", "ftp://", "ftps://")


@dataclass(frozen=True)
class HarnessDetection:
    """Host readiness for one registered harness, as shown by quickstart's tiny picker."""

    name: str
    installed: bool
    authenticated: bool
    install_hint: str

    @property
    def status(self) -> str:
        if self.authenticated:
            return "authed"
        if self.installed:
            return "installed"
        return f"not installed — {self.install_hint}"


def detect_harnesses(
    *,
    harnesses: Mapping[str, Harness] = HARNESSES,
    environ: Mapping[str, str] = os.environ,
    home: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> list[HarnessDetection]:
    """Probe every registered harness locally: CLI presence plus its own auth check.

    A harness is called ``authenticated`` only when both pieces are ready. Credentials for an
    uninstalled CLI are still probed (the adapter owns that logic), but do not make it runnable.
    """
    resolved_home = home or Path.home()
    detected = []
    for harness in harnesses.values():
        installed = which(harness.host_binary) is not None
        auth_ready = harness.missing_auth(environ, home=resolved_home) is None
        authenticated = installed and auth_ready
        detected.append(
            HarnessDetection(harness.name, installed, authenticated, harness.install_hint)
        )
    return detected


def recommended_harness(detected: list[HarnessDetection]) -> str:
    """Best runnable choice: authenticated, then installed, then the Claude fallback."""
    for harness in detected:
        if harness.authenticated:
            return harness.name
    for harness in detected:
        if harness.installed:
            return harness.name
    return DEFAULT_HARNESS


def choose_harness(
    detected: list[HarnessDetection], *, input_fn: Callable[[str], str] = input
) -> str:
    """Print detection evidence, then confirm one candidate or pick among several."""
    recommended = recommended_harness(detected)
    candidates = [harness for harness in detected if harness.installed]
    print("Detected agent harnesses:")
    for harness in detected:
        suffix = " (recommended)" if harness.name == recommended and candidates else ""
        print(f"  {harness.name}: {harness.status}{suffix}")

    if not candidates:
        claude = next((h for h in detected if h.name == DEFAULT_HARNESS), None)
        hint = claude.install_hint if claude is not None else "Install the Claude Code CLI."
        print(f"No agent harness CLI is installed. {hint}")
        return DEFAULT_HARNESS
    if len(candidates) == 1:
        choice = candidates[0].name
        input_fn(f"Use {choice} as this repo's default harness? Press Enter to continue. ")
        return choice

    numbered = {str(index): harness.name for index, harness in enumerate(candidates, start=1)}
    print("Choose the repo default:")
    for number, harness in zip(numbered, candidates, strict=True):
        suffix = " (recommended)" if harness.name == recommended else ""
        print(f"  {number}) {harness.name} — {harness.status}{suffix}")
    default_number = next(number for number, name in numbered.items() if name == recommended)
    while True:
        answer = input_fn(f"Harness [{default_number}]: ").strip()
        if not answer:
            return recommended
        if answer in numbered:
            return numbered[answer]
        print(f"Enter a number from 1 to {len(numbered)}.")


def harness_environment(env_file: str) -> dict[str, str]:
    """Environment visible to auth probes, including active values in quickstart's env-file."""
    from panopticon.core.dirs import secrets_file_path

    environ = dict(os.environ)
    path = secrets_file_path(env_file)
    if path is None:
        return environ
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return environ
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name:
            environ[name] = value
    return environ


def _secrets_template() -> str:
    """The secrets-file template, read from the packaged ``panopticon.env.template`` data file."""
    import importlib.resources

    ref = importlib.resources.files("panopticon.terminal") / "panopticon.env.template"
    return ref.read_text()


def detect_git_url() -> str:
    """Return the git remote URL for origin in CWD, or the panopticon fallback.

    Quickstart adopts whatever repo it's run in; the fallback covers running outside a git
    checkout (or one without an ``origin`` remote).
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        )
        url = result.stdout.strip()
        return url or _FALLBACK_GIT_URL
    except (subprocess.CalledProcessError, FileNotFoundError):
        return _FALLBACK_GIT_URL


def _normalize_url(git_url: str) -> str:
    """Canonical form for comparing remote URLs: trimmed, no trailing ``.git`` or ``/``."""
    url = git_url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[: -len(".git")]
    return url


def repo_id_from_url(git_url: str) -> str:
    """Derive a repo id/name from a git URL — its last path segment without the ``.git`` suffix.

    ``https://github.com/Unsupervisedcom/panopticon.git`` → ``panopticon``;
    ``git@github.com:acme/Widget.git`` → ``widget``. Falls back to ``repo`` if the URL yields
    nothing usable.
    """
    tail = _normalize_url(git_url).replace(":", "/").rstrip("/").rsplit("/", 1)[-1]
    return tail.lower() or "repo"


def _is_forge_url(git_url: str) -> bool:
    """True when ``git_url`` names a hosted-forge remote (network push/PR/CI), not a local path.

    Recognizes URL-scheme remotes (``https://…``, ``ssh://…``, …) and scp-like ``user@host:path``
    remotes; treats a bare filesystem path or a ``file://`` URL as local-only.
    """
    url = git_url.strip()
    if url.lower().startswith("file://"):
        return False
    if url.lower().startswith(_FORGE_SCHEMES):
        return True
    # scp-like syntax: user@host:path — an '@' and a ':' before any '/'. A Windows drive path
    # (``C:\…``) has the ':' but no '@', so it stays local.
    at, colon, slash = url.find("@"), url.find(":"), url.find("/")
    return at != -1 and colon > at and (slash == -1 or colon < slash)


def choose_enabled_workflow(git_url: str) -> str:
    """The opt-in workflow quickstart enables for a repo, chosen from its remote URL.

    A hosted-forge remote gets the forge lifecycle (``github-peer-reviewed``); a local-only repo
    gets the forge-free ``local-git-self-reviewed``.
    """
    return _FORGE_WORKFLOW if _is_forge_url(git_url) else _LOCAL_WORKFLOW


def _ensure_workflow_enabled(
    client: TaskServiceClient, repo: dict[str, object], workflow: str
) -> None:
    """Add ``workflow`` to an existing repo's ``enabled_workflows`` if it's missing.

    Merges rather than replaces, so a re-run (or a repo registered before quickstart enabled a
    workflow) gets the coding lifecycle without clobbering entries the operator set by hand. A
    no-op when it's already enabled.
    """
    raw = repo.get("enabled_workflows")
    enabled = [str(w) for w in raw] if isinstance(raw, list) else []
    if workflow in enabled:
        return
    repo_id = str(repo["id"])
    client.update_repo(repo_id, enabled_workflows=[*enabled, workflow])
    print(f"  → Enabled the {workflow!r} workflow for repo {repo_id!r}.")


def ensure_secrets_file() -> str:
    """Write the secrets template into the secrets dir (~/.config/panopticon/secrets/) if absent.

    Returns the file's **name** relative to the secrets dir (``panopticon.env``) — what a repo's
    ``env_file`` stores, so it resolves against whichever host runs the task (ADR 0007).
    """
    from panopticon.core.dirs import _secrets_dir

    secrets_dir = _secrets_dir()
    secrets_path = secrets_dir / "panopticon.env"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    if secrets_path.exists():
        print(f"Secrets file already exists: {secrets_path}")
    else:
        secrets_path.write_text(_secrets_template())
        print(f"Created secrets template: {secrets_path}")
        print("  → The setup-repo task will add harness auth; add GH_TOKEN there or by hand.")
    return secrets_path.name


def wait_for_service(service_url: str, *, timeout: int = 30) -> None:
    """Poll the task service until it responds or ``timeout`` seconds elapse."""
    import time

    import httpx as _httpx

    deadline = time.monotonic() + timeout
    while True:
        try:
            _httpx.get(f"{service_url}/tasks", timeout=1.0).raise_for_status()
            return
        except Exception as err:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Task service at {service_url} did not respond within {timeout}s"
                ) from err
            time.sleep(1.0)


def _find_existing_repo(client: TaskServiceClient, git_url: str) -> dict[str, object] | None:
    """The already-registered repo for ``git_url``, matched by normalized remote **or** derived id.

    Deduping on the remote URL alone misses a repo registered under a different spelling of the
    same remote (e.g. ``git@github.com:acme/x.git`` vs ``https://github.com/acme/x``), and since the
    id we'd create is derived from the URL, that mismatch would then collide on create. Matching the
    derived id too reuses the existing repo instead of colliding.
    """
    target = _normalize_url(git_url)
    want_id = repo_id_from_url(git_url)
    for repo in client.list_repos():
        if _normalize_url(str(repo.get("git_url", ""))) == target or repo.get("id") == want_id:
            return repo
    return None


def setup_repo(
    client: TaskServiceClient,
    git_url: str,
    env_file: str,
    *,
    default_harness: str | None = None,
) -> tuple[str, str]:
    """Register the repo quickstart is run in with the task service; return its ``(id, name)``.

    Enables the opt-in coding workflow appropriate to the repo's remote — the forge lifecycle
    (``github-peer-reviewed``) for a hosted remote, the forge-free ``local-git-self-reviewed`` for a
    local-only one (see :func:`choose_enabled_workflow`) — so a fresh quickstart repo can create a
    normal coding task without a hand-edit.

    Idempotent: an already-registered repo (matched by remote URL or derived id, see
    :func:`_find_existing_repo`) is reused rather than re-registered — and still has the workflow
    ensured (merged in if absent) — and a create that races into a conflict falls back to the same
    reuse. The name is used to seed the setup-repo task's memo.
    """
    workflow = choose_enabled_workflow(git_url)
    existing = _find_existing_repo(client, git_url)
    if existing is not None:
        print(f"Repo already configured for {git_url!r} — skipping registration.")
        _ensure_workflow_enabled(client, existing, workflow)
        repo_id = str(existing["id"])
        if default_harness is not None and existing.get("default_harness") != default_harness:
            client.update_repo(repo_id, default_harness=default_harness)
            print(f"  → Set the repo's default harness to {default_harness!r}.")
        return repo_id, str(existing.get("name") or repo_id)
    repo_id = repo_id_from_url(git_url)
    try:
        client.create_repo(
            repo_id,
            repo_id,
            git_url,
            env_file=env_file,
            enabled_workflows=[workflow],
            default_harness=default_harness,
        )
    except httpx.HTTPStatusError as err:
        if err.response.status_code != 409:
            raise
        # A repo with this id already exists (a race after our dedup check) — reuse it, and still
        # ensure the workflow is enabled on it.
        print(f"Repo {repo_id!r} already exists — reusing it.")
        raced = _find_existing_repo(client, git_url)
        if raced is not None:
            _ensure_workflow_enabled(client, raced, workflow)
        return repo_id, repo_id
    print(f"Registered repo {repo_id!r} (git_url={git_url!r}).")
    print(f"  → Secrets file: {env_file}")
    print(f"  → Enabled the {workflow!r} workflow.")
    return repo_id, repo_id


def ensure_setup_repo_task(client: TaskServiceClient, repo_id: str, name: str) -> str | None:
    """Return the id of a ``setup-repo`` task for ``repo_id`` to attach to, creating one if needed.

    Reuses an existing **non-terminal** setup-repo task for the repo when there is one, so
    re-running quickstart doesn't pile up orphaned ``RUNNING`` tasks; otherwise creates a fresh one
    (seeded with the shared memo, see :func:`create_setup_repo_task`). The console attaches to the
    returned task on open, dropping the operator into ``claude setup-token``. Best-effort: if the
    task can't be created (e.g. the workflow isn't available on an older task service), it warns and
    returns ``None`` so quickstart still opens the console.
    """
    try:
        for task in client.list_tasks():
            if (
                task.get("repo_id") == repo_id
                and task.get("workflow") == SETUP_REPO_WORKFLOW
                and task.get("state") not in _TERMINAL_STATES
            ):
                print("Attaching to the running setup-repo task to configure harness auth.")
                return str(task["id"])
        task = create_setup_repo_task(client, repo_id, name)
    except httpx.HTTPError as err:
        print(f"Could not start a setup-repo task ({err}); opening the dashboard instead.")
        print("  → Configure auth later by starting a setup-repo task from the repos screen.")
        return None
    print("Configuring the repo's harness auth — attach to complete setup-repo.")
    return str(task["id"])
