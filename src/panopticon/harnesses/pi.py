"""The pi harness — earendil-works' ``pi`` coding-agent CLI
(https://github.com/earendil-works/pi, npm ``@earendil-works/pi-coding-agent``) as a third
harness adapter, alongside claude and codex.

Verified against a real pi 0.80.3 install: its ``--help`` surface matches this module
(``--append-system-prompt``, ``--continue``, ``--skill``, sessions under the agent dir),
``PI_CODING_AGENT_DIR`` really relocates the whole config root (confirmed via its auth lookup),
and ``~/.pi/agent/mcp.json`` on that install is an empty ``{}`` — pi ships no MCP client.

- **Install.** No static binary (unlike codex): pi is only an npm package, requiring Node
  ``>=22.19.0``. :meth:`PiHarness.image_layer` installs a pinned Node.js release (the linux-x64/
  arm64 tarballs from nodejs.org) and then the pinned ``pi-coding-agent`` version globally.

- **Config dir.** pi's default root, ``~/.pi/agent``, is fully relocated by
  ``PI_CODING_AGENT_DIR``. The registry requires a flat, ``/``-free ``config_dirname`` (one
  Docker volume mountpoint per harness), so this points the env var at ``<home>/.pi`` directly
  rather than encoding a nested path — a full override needs no ``agent`` subdir underneath it.

- **Session / resume.** Sessions are JSONL under ``<config_dir>/sessions/**``. ``pi --continue``
  resumes the most recent one for the cwd and silently starts fresh when none exists (no error),
  so this harness passes it whenever any ``*.jsonl`` is recorded anywhere under ``sessions/``.

- **MCP: none** (confirmed above). The two core operations (advance/drop) this harness renders
  are REST calls against the task service's plain API instead of an MCP tool call — pi's own
  documented pattern ("build CLI tools with READMEs") for exactly this. This does not extend to
  workflow-authored skills that name an MCP tool directly (``provision``'s ``set_slug``,
  ``github_forge``'s ``set_url``, ``planned_workflow``'s ``put_artifact``/``set_token_estimate``,
  ``orchestrator``'s ``create_task``/``set_slug``/``resolve_responsibility``) — those assume an
  MCP-capable harness and won't work unmodified under pi; making every workflow skill
  MCP-agnostic is out of scope for a harness adapter.

- **Skills.** pi implements the Agent Skills standard and reads ``~/.agents/skills/`` at the
  user scope, unaffected by the ``PI_CODING_AGENT_DIR`` redirect — the same directory and shape
  codex renders to, reused directly (:func:`panopticon.harnesses.codex.write_skills`).

- **Turn signals.** pi has no Stop/UserPromptSubmit hook config, but its extension API has real
  equivalents, confirmed against the pi-mono TypeScript source (not just its docs): the
  ``AgentSettledEvent``/``InputEvent`` types and the ``ExtensionHandler``/``ExtensionFactory``
  signatures in ``core/extensions/types.ts``. :data:`TURN_EXTENSION` is a minimal extension
  rendered at bootstrap and loaded via ``--extension <path>`` on every launch; it mirrors
  :mod:`panopticon.container.hook`'s contract exactly — ``PUT .../tasks/{id}/turn`` with
  ``{"turn": "user"}`` on ``agent_end`` (pi "will not continue running automatically", the
  closest analog to Stop), ``{"turn": "agent"}`` on ``input`` (fired when user input arrives).
  It reads ``PANOPTICON_SERVICE_URL``/``PANOPTICON_TASK_ID`` from the environment the launcher
  already sets, so its content needs no per-task templating. Not run against a live pi process —
  no Node/pi runtime was available while writing this, so the source-level type-checking above
  is the strongest evidence short of that.

- **Auth.** Subscription OAuth and API keys share ``<config_dir>/auth.json``. Per pi's documented
  resolution order, a plain env var ranks above ``auth.json``'s absence — so unlike codex, this
  harness never renders an api-key file, only checks for one of :data:`API_KEY_ENV_VARS` (every
  single-var provider credential pi resolves, pulled from its ``env-api-keys.ts`` source — see
  that constant's docstring), a mounted credential dir (symlinked in, same shape as codex's), or
  one already materialized on the config volume from a prior ``/login``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar

from panopticon.harnesses.base import INTERRUPT_PROMPT, BootstrapContext, Harness, LaunchContext
from panopticon.harnesses.codex import write_skills
from panopticon.harnesses.config import update_json_config

#: The pi-coding-agent release the harness image layer installs (published npm manifest:
#: ``engines.node >= 22.19.0``, ``bin.pi = dist/cli.js``) — the version verified locally.
PI_VERSION = "0.80.3"

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

#: Rendered so `argv()` can load it via `--extension` — see the module docstring's turn-signals
#: section. Static: it reads the task id/service URL from the environment the launcher already
#: sets, not from any per-task templating.
EXTENSION_FILE = "turn.ts"
TURN_EXTENSION = """\
export default function (pi) {
  const url = `${process.env.PANOPTICON_SERVICE_URL}/tasks/${process.env.PANOPTICON_TASK_ID}/turn`;
  const setTurn = (turn) =>
    fetch(url, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ turn }),
    }).catch(() => {});

  pi.on("agent_end", () => setTurn("user"));
  pi.on("input", () => setTurn("agent"));
}
"""

#: Every single-var provider credential pi resolves directly against the process environment —
#: pulled from ``getApiKeyEnvVars`` in ``packages/ai/src/env-api-keys.ts`` (the pi-mono source
#: docs/providers.md itself points at), so `missing_auth` doesn't reject a provider it simply
#: didn't enumerate (e.g. Groq/Cerebras/xAI/OpenRouter, or ``ANTHROPIC_OAUTH_TOKEN`` — which
#: takes precedence over ``ANTHROPIC_API_KEY`` there). Excludes the AWS/Google ambient-credential
#: paths (``AWS_PROFILE``, ``GOOGLE_APPLICATION_CREDENTIALS``, …), which are multi-variable
#: conditions a flat presence check can't represent correctly; those operators have the
#: credential_dir/persisted-``auth.json`` fallback below instead.
API_KEY_ENV_VARS = (
    "ANTHROPIC_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "COPILOT_GITHUB_TOKEN",
    "ANT_LING_API_KEY",
    "OPENAI_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "NVIDIA_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_CLOUD_API_KEY",
    "GROQ_API_KEY",
    "CEREBRAS_API_KEY",
    "XAI_API_KEY",
    "RADIUS_API_KEY",
    "OPENROUTER_API_KEY",
    "AI_GATEWAY_API_KEY",
    "ZAI_API_KEY",
    "ZAI_CODING_CN_API_KEY",
    "MISTRAL_API_KEY",
    "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY",
    "MOONSHOT_API_KEY",
    "HF_TOKEN",
    "FIREWORKS_API_KEY",
    "TOGETHER_API_KEY",
    "OPENCODE_API_KEY",
    "KIMI_API_KEY",
    "CLOUDFLARE_API_KEY",
    "XIAOMI_API_KEY",
    "XIAOMI_TOKEN_PLAN_CN_API_KEY",
    "XIAOMI_TOKEN_PLAN_AMS_API_KEY",
    "XIAOMI_TOKEN_PLAN_SGP_API_KEY",
)


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


def write_settings(config_dir: Path) -> Path:
    """Merge ``defaultProjectTrust: "always"`` into ``<config_dir>/settings.json``.

    pi asks an interactive "trust this project folder?" question on startup whenever the
    workspace holds project-local settings/resources — there's no operator in the container to
    answer it. ``defaultProjectTrust`` is pi's own documented escape hatch for this, its analog
    of claude's trust-dialog seeding."""
    path = config_dir / SETTINGS_FILE
    with update_json_config(path) as data:
        data["defaultProjectTrust"] = "always"
    return path


def write_workflow_overview(config_dir: Path, overview: str) -> Path | None:
    """Write the whole-workflow map so `argv()` can pass it via ``--append-system-prompt``.
    Returns ``None`` when there's no overview — removing a stale file from an earlier bootstrap
    (the config volume persists across respawns), so `argv()` doesn't keep injecting it."""
    path = config_dir / WORKFLOW_OVERVIEW_FILE
    if not overview.strip():
        path.unlink(missing_ok=True)
        return None
    config_dir.mkdir(parents=True, exist_ok=True)
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
            "No pi credentials — set one of pi's provider API-key env vars (ANTHROPIC_API_KEY, "
            "OPENAI_API_KEY, GEMINI_API_KEY, GROQ_API_KEY, … — see its own docs/providers.md for "
            "the full list) in the repo's env_file, or give the repo a credential_dir holding a "
            "pi auth.json from `/login` (see docs/auth.md)"
        )

    def bootstrap(self, ctx: BootstrapContext) -> None:
        config_dir = self.config_dir(ctx.home)
        config_dir.mkdir(parents=True, exist_ok=True)
        write_settings(config_dir)
        write_workflow_overview(config_dir, ctx.overview)
        (config_dir / EXTENSION_FILE).write_text(TURN_EXTENSION)
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
        config_dir = self.config_dir(ctx.home)
        argv = ["pi"]
        overview = config_dir / WORKFLOW_OVERVIEW_FILE
        if overview.exists():
            argv += ["--append-system-prompt", overview.read_text()]
        extension = config_dir / EXTENSION_FILE
        if extension.exists():
            argv += ["--extension", str(extension)]
        sessions = config_dir / "sessions"
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
