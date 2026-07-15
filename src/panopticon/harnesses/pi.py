"""The pi harness — earendil-works' ``pi`` coding-agent CLI
(https://github.com/earendil-works/pi, npm ``@earendil-works/pi-coding-agent``) as a third
harness adapter, alongside claude and codex.

Facts below are pinned against **pi-coding-agent 0.80.7** — its README, ``docs/`` tree (cloned
from the upstream repo), and the published npm manifest (version/``engines``/``bin``) as of
2026-07-15. Unlike codex (verified against a real running binary), nothing here has been
exercised against a live ``pi`` process — there is no Node/pi runtime available while writing
this module, only its documented and published surface. Treat anything below as "per the
docs", not "observed".

- **Install.** No static-binary release (unlike codex): pi ships only as the npm package
  ``@earendil-works/pi-coding-agent``, requiring Node.js ``>=22.19.0`` (its ``package.json``
  ``engines``). :meth:`PiHarness.image_layer` installs a pinned Node.js release (the official
  linux-x64/linux-arm64 tarballs from nodejs.org) and then the pinned npm package globally —
  two steps where codex needed one, since codex's binary carries its own runtime.

- **Config dir.** pi's default root is ``~/.pi/agent``, relocatable wholesale via the
  ``PI_CODING_AGENT_DIR`` env var. The harness registry requires ``config_dirname`` to be a
  flat dotdir with no ``/`` (:func:`test_config_dirnames_are_distinct_dotdirs`,
  one per-task Docker volume mountpoint) — so this harness points ``PI_CODING_AGENT_DIR`` at
  ``<home>/.pi`` directly (skipping the nested ``.../agent``) rather than encoding a nested path
  in ``config_dirname``. Functionally identical: the env var is a full override of pi's config
  root, not a suffix on top of one, so nothing under it needs to be named ``agent``.

- **Session / resume.** Sessions are JSONL files under ``<config_dir>/sessions/**``. Per the
  pi-mono source (``SessionManager.continueRecent``), ``pi --continue`` resumes the most recent
  one for the cwd and **silently starts a fresh session** when none exists — no error, no
  prompt — so (mirroring codex's ``resume --last`` gate) this harness always passes
  ``--continue`` once *any* ``*.jsonl`` is recorded anywhere under ``sessions/`` and never
  otherwise, rather than needing pi's own cwd-hashing scheme.

- **MCP: none.** pi's stated philosophy (its README) is "No MCP. Build CLI tools with READMEs
  (see Skills), or build an extension that adds MCP support." There is no MCP client config to
  render, unlike claude/codex. The two core **operations** (advance/drop) this harness itself
  renders are therefore instructions to call the task service's plain REST API directly
  (``POST /tasks/{id}/operations/{name}``) rather than an MCP tool — pi's own documented
  pattern for exactly this situation. **This does not extend to workflow-authored skills**:
  several already name an MCP tool directly in their instructions text (e.g. core's
  ``provision`` skill says "set it with the ``set_slug`` tool"; ``github_forge``'s open-pr skill
  names ``set_url``; ``planned_workflow`` names ``put_artifact``/``set_token_estimate``;
  ``orchestrator`` names ``create_task``/``set_slug``/``resolve_responsibility``). Those assume
  an MCP-capable harness and will not work unmodified under pi — making every workflow skill
  MCP-agnostic is a cross-cutting change to ``core``/``workflows``, out of scope for a harness
  adapter. Documented here as a known gap rather than silently patched over.

- **Skills.** pi implements the Agent Skills standard (agentskills.io) and reads
  ``~/.agents/skills/`` at the user scope — unaffected by the ``PI_CODING_AGENT_DIR`` redirect
  (it's a separate, fixed path), the same directory codex writes to, in the same shape
  (frontmatter + instructions). Written there rather than under the config volume so a pi and a
  codex task in the same container home would see the same rendered skills.

- **Hooks / turn signals: none wired.** pi ships no Stop/UserPromptSubmit equivalent — no
  declarative, zero-code hook config a harness can render into a file (unlike codex's
  Claude-Code-compatible ``hooks.toml``). Its only lifecycle mechanism is a TypeScript
  *extension* API running inside pi's own Node process (``agent_settled``,
  ``before_agent_start``, per its ``docs/extensions.md``) — real, documented events, but a
  fundamentally different, code-executing integration surface this module does not attempt to
  wire, since it can't be exercised or verified without a live pi+Node runtime. Per the turn
  contract's degradation rule: state **transitions** (advance/drop, applied via the REST
  operation instructions above) still flip ``Task.turn`` correctly regardless of harness — that
  happens in the workflow's ``turn_on_enter`` when the operation is applied, not in a hook. What
  is lost is *mid-state* ball-tracking (the back-and-forth within one state, e.g. this task's own
  ITERATING phase): a pi task's turn will not auto-flip to ``user`` when the agent falls silent,
  nor back to ``agent`` when the operator replies. Left undone rather than faked; an operator
  driving a pi task should expect to check in on it directly rather than trust the dashboard's
  turn indicator.

- **Auth.** Subscription OAuth (``/login``) and API keys share one file,
  ``<config_dir>/auth.json`` (pi's ``~/.pi/agent/auth.json`` analog, resolved relative to
  ``PI_CODING_AGENT_DIR``). Per pi's own documented resolution order, plain **process env
  vars rank above ``auth.json``'s absence** — so unlike codex, this harness never renders an
  api-key ``auth.json`` itself; it only checks for one of the common provider keys
  (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, ``GEMINI_API_KEY`` — the ones panopticon already
  names elsewhere) being present, a mounted credential dir (a subscription ``auth.json``,
  symlinked in exactly like codex's), or one already materialized on the config volume from a
  prior ``/login``. pi supports many more providers via their own env vars (see its
  ``docs/providers.md`` upstream); those work too, just aren't named in ``missing_auth``'s
  message.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from panopticon.harnesses.base import INTERRUPT_PROMPT, BootstrapContext, Harness, LaunchContext
from panopticon.harnesses.claude import _task_id_note
from panopticon.harnesses.config import update_json_config

#: The pi-coding-agent release the harness image layer installs (published npm manifest:
#: ``engines.node >= 22.19.0``, ``bin.pi = dist/cli.js``).
PI_VERSION = "0.80.7"

#: The Node.js release installed alongside it — the minimum pi's own ``engines`` requires;
#: pi ships no static binary, so a Node runtime is a real prerequisite in the image (unlike codex).
NODE_VERSION = "22.19.0"

#: pi's shared credentials file (subscriptions *and* API keys), under ``PI_CODING_AGENT_DIR``.
AUTH_FILE = "auth.json"

#: pi's JSON settings file, global scope once ``PI_CODING_AGENT_DIR`` points here.
SETTINGS_FILE = "settings.json"

#: Rendered so `argv()` (given only a `LaunchContext`, no workflow overview) can read it back —
#: same seam as claude's `WORKFLOW_OVERVIEW_FILE`.
WORKFLOW_OVERVIEW_FILE = "workflow-overview.md"

#: The common provider API-key env vars this harness checks for in `missing_auth` — the ones
#: panopticon already names elsewhere (claude/codex). pi reads these (and many more third-party
#: provider vars — see its own docs/providers.md) directly at runtime; no file rendering needed.
API_KEY_ENV_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY")


def render_skill(name: str, description: str, instructions: str, task_id: str) -> str:
    """The ``SKILL.md`` body for one skill/operation: Agent-Skills frontmatter + the procedure."""
    return f"---\nname: {name}\ndescription: {description}\n---\n{instructions}\n{_task_id_note(task_id)}"


def write_skills(skills: Mapping[str, tuple[str, str]], root: Path, task_id: str) -> list[Path]:
    """Write ``name → (description, instructions)`` to ``<root>/.agents/skills/<name>/SKILL.md``.

    User-scope, not the config volume: pi reads ``~/.agents/skills/`` unconditionally (it isn't
    redirected by ``PI_CODING_AGENT_DIR``), the same location codex renders to."""
    written = []
    for name, (description, instructions) in skills.items():
        skill_dir = root / ".agents" / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        path = skill_dir / "SKILL.md"
        path.write_text(render_skill(name, description, instructions, task_id))
        written.append(path)
    return written


def operation_instructions(name: str, target_state: str, task_id: str, service_url: str) -> str:
    """The procedure body for a core operation (advance/drop/…) — a direct REST call, since pi
    has no MCP client to invoke ``apply_operation`` through (claude/codex's approach)."""
    url = f"{service_url.rstrip('/')}/tasks/{task_id}/operations/{name}"
    return (
        f"Apply this workflow's `{name}` operation — it moves the task to **{target_state}**. "
        "pi has no MCP client, so call the task service's REST API directly (no request body "
        f'needed): `curl --fail --silent --show-error --request POST "{url}"`. '
        "Don't edit the state directly. It's gated on the current state's responsibilities and "
        "starts a new turn."
    )


def settings() -> dict[str, Any]:
    """The ``settings.json`` we seed: pre-accept project trust.

    pi asks an interactive "trust this project folder?" question on startup whenever the
    workspace holds project-local settings/resources — there's no operator in the container to
    answer it. ``defaultProjectTrust: "always"`` (pi's own documented escape hatch, its analog of
    claude's trust-dialog seeding) makes non-interactive and interactive startup alike skip it.
    """
    return {"defaultProjectTrust": "always"}


def write_settings(config_dir: Path) -> Path:
    """Merge :func:`settings` into ``<config_dir>/settings.json``; return the path."""
    path = config_dir / SETTINGS_FILE
    with update_json_config(path) as data:
        data.update(settings())
    return path


def write_workflow_overview(config_dir: Path, overview: str) -> Path | None:
    """Write the whole-workflow map so `argv()` can pass it via ``--append-system-prompt``.
    Returns ``None`` (writes nothing) when there's no overview."""
    if not overview.strip():
        return None
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / WORKFLOW_OVERVIEW_FILE
    path.write_text(overview)
    return path


class PiHarness(Harness):
    """earendil-works' ``pi`` coding-agent CLI behind the harness interface."""

    name: ClassVar[str] = "pi"
    config_dirname: ClassVar[str] = ".pi"

    def image_layer(self) -> str:
        """Install a pinned Node.js release, then the pinned pi npm package globally. pi has no
        static binary (unlike codex), so the Node runtime is a real, versioned dependency here."""
        return (
            "RUN set -eux; \\\n"
            '    arch="$(uname -m)"; \\\n'
            '    case "$arch" in \\\n'
            '      x86_64) node_arch="x64" ;; \\\n'
            '      aarch64) node_arch="arm64" ;; \\\n'
            '      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\\n'
            "    esac; \\\n"
            "    curl --fail --silent --show-error --location \\\n"
            f'      "https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-$node_arch.tar.xz" \\\n'
            "      | tar --extract --xz --directory /usr/local --strip-components=1; \\\n"
            "    npm install --global --ignore-scripts "
            f"@earendil-works/pi-coding-agent@{PI_VERSION}"
        )

    def missing_auth(self, environ: Mapping[str, str], *, home: Path) -> str | None:
        if any(environ.get(var) for var in API_KEY_ENV_VARS):
            return None
        if (self.config_dir(home) / AUTH_FILE).exists():  # e.g. persisted on the config volume
            return None
        credentials = environ.get("PANOPTICON_CREDENTIALS")
        if credentials and (Path(credentials) / AUTH_FILE).exists():
            return None
        return (
            "No pi credentials — set ANTHROPIC_API_KEY (or OPENAI_API_KEY/GEMINI_API_KEY) in "
            "the repo's env_file, or give the repo a credential_dir holding a pi auth.json from "
            "`/login` (see docs/auth.md); pi supports other providers via env vars too "
            "(see its own docs/providers.md)"
        )

    def bootstrap(self, ctx: BootstrapContext) -> None:
        config_dir = self.config_dir(ctx.home)
        config_dir.mkdir(parents=True, exist_ok=True)
        write_settings(config_dir)
        write_workflow_overview(config_dir, ctx.overview)
        entries: dict[str, tuple[str, str]] = {
            s.name: (s.description, s.instructions) for s in ctx.skills
        }
        for name, target_state in ctx.operations.items():
            entries[name] = (
                f"Apply the workflow's '{name}' operation.",
                operation_instructions(name, target_state, ctx.task_id, ctx.service_url),
            )
        write_skills(entries, ctx.home, ctx.task_id)
        self._ensure_auth(config_dir, ctx.environ)

    def _ensure_auth(self, config_dir: Path, environ: Mapping[str, str]) -> None:
        """Symlink a mounted subscription ``auth.json`` in when present. Idempotent; never
        clobbers one already there. Unlike codex, no api-key file is ever rendered here — pi
        resolves an env-var API key itself at runtime."""
        auth = config_dir / AUTH_FILE
        if auth.exists() or auth.is_symlink():
            return
        credentials = environ.get("PANOPTICON_CREDENTIALS")
        if credentials and (Path(credentials) / AUTH_FILE).exists():
            auth.symlink_to(Path(credentials) / AUTH_FILE)

    def argv(self, ctx: LaunchContext) -> list[str]:
        """``pi`` argv. pi "runs with all permissions by default" (its own containerization
        docs) — no bypass/skip-permissions flag needed, unlike claude/codex. Resumes the config
        volume's most recent session when one is recorded (``--continue``, which silently starts
        fresh otherwise — see the module docstring); like claude/codex, a resume on the agent's
        turn gets :data:`INTERRUPT_PROMPT` appended so it picks back up."""
        argv = ["pi"]
        overview = self.config_dir(ctx.home) / WORKFLOW_OVERVIEW_FILE
        if overview.exists():
            argv += ["--append-system-prompt", overview.read_text()]
        sessions = self.config_dir(ctx.home) / "sessions"
        if sessions.exists() and any(sessions.rglob("*.jsonl")):
            argv.append("--continue")
            if ctx.turn == "agent":
                argv.append(INTERRUPT_PROMPT)
            return argv
        if ctx.starting_model:  # first run only — a resume keeps the session's model
            argv += ["--model", ctx.starting_model]
        if ctx.initial_prompt:
            argv.append(ctx.initial_prompt)  # positional: pi sends this as the first message
        return argv

    def env(self, ctx: LaunchContext) -> dict[str, str]:
        return {"PI_CODING_AGENT_DIR": str(self.config_dir(ctx.home))}
