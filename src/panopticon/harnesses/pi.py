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

- **Config dir.** pi's native root is ``~/.pi/agent``. The registry's flat, ``/``-free
  ``config_dirname`` keeps the Docker volume mounted at ``<home>/.pi``; bootstrap, auth, sessions,
  and ``PI_CODING_AGENT_DIR`` consistently use its native ``agent`` child.

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

- **Auth.** Subscription OAuth and API keys share ``<config_dir>/auth.json``. Preflight accepts
  pi's native provider-generic OAuth/API-key shapes (including the additional ``accountId`` field
  required by ``openai-codex``), any provider env var from :data:`API_KEY_ENV_VARS`, or the selected
  custom model provider's resolvable ``apiKey``.
  Merely finding a file is not sufficient. The Claude setup-token variable is intentionally not a
  pi credential path; explicitly supplied Anthropic OAuth is passed through with a documentation
  warning, not blocked.

- **Personal config.** Entries under ``<credential_dir>/pi/agent/`` are imported without
  clobbering persistent task config or mutating the mounted source. ``models.json`` is copied so
  HTTP(S) loopback provider hosts can be adapted to ``host.docker.internal``; other regular config
  files are copied, while rotating auth and directories remain linked. This uses the existing
  per-repo credential mount.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import ClassVar, Protocol
from urllib.parse import urlsplit, urlunsplit

from panopticon.core.models import Skill
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

#: Pi-only personal config lives below the shared per-repo credential mount. Keeping this in a
#: subdirectory avoids exposing pi-specific layout as another Repo field or runner mount.
PERSONAL_CONFIG_DIR = "pi"
NATIVE_AGENT_DIR = "agent"

NO_USABLE_CREDENTIALS = (
    "No usable pi credentials: provide a valid ~/.pi/agent/auth.json, set "
    "ANTHROPIC_API_KEY (or another pi provider API key), or configure the selected "
    "provider's apiKey in models.json."
)

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
  const token = process.env.PANOPTICON_SERVICE_AUTH_TOKEN;
  const setTurn = async (turn) => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 2000);
    try {
      await fetch(url, {
        method: "PUT",
        headers: {
          "content-type": "application/json",
          ...(token ? { "authorization": `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ turn }),
        signal: controller.signal,
      });
    } catch {
    } finally {
      clearTimeout(timeout);
    }
  };

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

OAUTH_AUTH_PROVIDERS = frozenset({"openai-codex", "anthropic", "github-copilot", "xai", "radius"})
API_KEY_AUTH_PROVIDERS = frozenset(
    {
        "anthropic",
        "ant-ling",
        "azure-openai-responses",
        "openai",
        "deepseek",
        "nvidia",
        "google",
        "amazon-bedrock",
        "mistral",
        "groq",
        "cerebras",
        "cloudflare-ai-gateway",
        "cloudflare-workers-ai",
        "xai",
        "openrouter",
        "vercel-ai-gateway",
        "zai",
        "zai-coding-cn",
        "opencode",
        "opencode-go",
        "radius",
        "huggingface",
        "fireworks",
        "together",
        "baseten",
        "kimi-coding",
        "minimax",
        "minimax-cn",
        "qwen-token-plan",
        "qwen-token-plan-cn",
        "xiaomi",
        "xiaomi-token-plan-cn",
        "xiaomi-token-plan-ams",
        "xiaomi-token-plan-sgp",
    }
)


class CommandRunner(Protocol):
    """Runs an external command and returns its stdout; ``check`` raises on failure."""

    def __call__(self, args: Sequence[str], *, check: bool = True) -> str: ...


def _subprocess_run(args: Sequence[str], *, check: bool = True) -> str:
    return subprocess.run(list(args), check=check, capture_output=True, text=True).stdout


def operation_instructions(
    name: str,
    target_state: str,
    task_id: str,
    service_url: str,
    *,
    authenticated: bool = False,
) -> str:
    """The procedure body for a core operation (advance/drop/…) — a direct REST call, since pi
    has no MCP client to invoke ``apply_operation`` through (claude/codex's approach)."""
    url = f"{service_url.rstrip('/')}/tasks/{task_id}/operations/{name}"
    curl = (
        "_panopticon_had_xtrace=; case $- in *x*) set +x; "
        "_panopticon_had_xtrace=1 ;; esac; "
        "printf 'header = \"Authorization: Bearer %s\"\\n' "
        "\"$PANOPTICON_SERVICE_AUTH_TOKEN\" | curl --disable --noproxy '*' --config - "
        if authenticated
        else "curl --disable --noproxy '*' "
    )
    restore = (
        "; _panopticon_status=$?; "
        '[ -n "$_panopticon_had_xtrace" ] && set -x; (exit "$_panopticon_status")'
        if authenticated
        else ""
    )
    return (
        f"Apply this workflow's `{name}` operation — it moves the task to **{target_state}**. "
        "pi has no MCP client, so call the task service's REST API directly (no request body "
        "needed): `" + curl + "--fail --silent --show-error --request POST "
        f'"{url}"' + restore + "`. "
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
    host_binary: ClassVar[str] = "pi"
    install_hint: ClassVar[str] = (
        "Install pi (`npm install --global @earendil-works/pi-coding-agent`)."
    )

    def __init__(self, *, run: CommandRunner = _subprocess_run) -> None:
        self._run = run

    def suggested_models(self) -> Sequence[tuple[str, str]]:
        """Ask pi for its available models; a missing/broken CLI leaves free text available."""
        try:
            output = self._run(["pi", "--list-models"])
        except (OSError, subprocess.SubprocessError):
            return ()

        suggestions = []
        for line in output.splitlines()[1:]:  # provider/model table header
            columns = line.split()
            if len(columns) < 2:
                continue
            value = f"{columns[0]}/{columns[1]}"
            suggestions.append((value, value))
        return tuple(suggestions)

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
            f'      "https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-$node_arch.tar.gz" \\\n'
            "      | tar --extract --gzip --directory /usr/local --strip-components=1; \\\n"
            "    npm install --global --ignore-scripts "
            f"@earendil-works/pi-coding-agent@{PI_VERSION}"
        )

    def _agent_dir(self, home: Path) -> Path:
        return self.config_dir(home) / NATIVE_AGENT_DIR

    @staticmethod
    def _mounted_agent_dir(environ: Mapping[str, str]) -> Path | None:
        credentials = environ.get("PANOPTICON_CREDENTIALS")
        if not credentials:
            return None
        return Path(credentials) / PERSONAL_CONFIG_DIR / NATIVE_AGENT_DIR

    @staticmethod
    def _load_object(path: Path) -> dict[str, object] | None:
        try:
            value = json.loads(path.read_text())
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _resolved_key(value: object, environ: Mapping[str, str]) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        # Pi treats command-backed values as configured for model availability without executing
        # them. It resolves them only when making a request.
        if value.startswith("!"):
            return value

        resolved: list[str] = []
        index = 0
        while index < len(value):
            dollar = value.find("$", index)
            if dollar < 0:
                resolved.append(value[index:])
                break
            resolved.append(value[index:dollar])
            next_char = value[dollar + 1 : dollar + 2]
            if next_char in {"$", "!"}:
                resolved.append(next_char)
                index = dollar + 2
                continue
            if next_char == "{":
                end = value.find("}", dollar + 2)
                if end < 0:
                    resolved.append("$")
                    index = dollar + 1
                    continue
                name = value[dollar + 2 : end]
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                    replacement = environ.get(name)
                    if not replacement:
                        return None
                    resolved.append(replacement)
                else:
                    resolved.append(value[dollar : end + 1])
                index = end + 1
                continue
            match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", value[dollar + 1 :])
            if match is not None:
                replacement = environ.get(match.group())
                if not replacement:
                    return None
                resolved.append(replacement)
                index = dollar + 1 + len(match.group())
                continue
            resolved.append("$")
            index = dollar + 1
        return "".join(resolved) or None

    def _usable_auth(self, path: Path, environ: Mapping[str, str]) -> bool:
        data = self._load_object(path)
        if data is None:
            return False
        for provider, credential in data.items():
            if not isinstance(credential, dict):
                continue
            if provider in OAUTH_AUTH_PROVIDERS and credential.get("type") == "oauth":
                expires = credential.get("expires")
                common_fields = all(
                    isinstance(credential.get(field), str) and bool(credential[field])
                    for field in ("access", "refresh")
                )
                codex_fields = provider != "openai-codex" or (
                    isinstance(credential.get("accountId"), str) and bool(credential["accountId"])
                )
                if (
                    common_fields
                    and codex_fields
                    and isinstance(expires, (int, float))
                    and not isinstance(expires, bool)
                ):
                    return True
                continue
            if provider not in API_KEY_AUTH_PROVIDERS or credential.get("type") != "api_key":
                continue
            credential_environ = dict(environ)
            scoped = credential.get("env")
            if isinstance(scoped, dict):
                credential_environ.update(
                    (name, value)
                    for name, value in scoped.items()
                    if isinstance(name, str) and isinstance(value, str) and value
                )
            if self._resolved_key(credential.get("key"), credential_environ) is not None:
                return True
        return False

    def _usable_selected_model(self, path: Path, environ: Mapping[str, str]) -> bool:
        selected = environ.get("PANOPTICON_STARTING_MODEL")
        if not selected or "/" not in selected:
            return False
        provider_name, _ = selected.split("/", 1)
        data = self._load_object(path)
        providers = data.get("providers") if data else None
        if not isinstance(providers, dict):
            return False
        provider = providers.get(provider_name)
        return (
            isinstance(provider, dict)
            and self._resolved_key(provider.get("apiKey"), environ) is not None
        )

    def _effective_config_path(self, home: Path, environ: Mapping[str, str], name: str) -> Path:
        """Return the config entry bootstrap leaves for pi to consume.

        The persistent task volume wins when an entry already exists, including a broken
        symlink. Otherwise bootstrap imports the corresponding mounted personal-config entry.
        Preflight must apply that same precedence so it cannot approve a credential that will be
        shadowed at launch.
        """
        persisted = self._agent_dir(home) / name
        if persisted.exists() or persisted.is_symlink():
            return persisted
        mounted = self._mounted_agent_dir(environ)
        return mounted / name if mounted is not None else persisted

    def missing_auth(self, environ: Mapping[str, str], *, home: Path) -> str | None:
        if any(environ.get(var) for var in API_KEY_ENV_VARS):
            return None
        auth_path = self._effective_config_path(home, environ, AUTH_FILE)
        if self._usable_auth(auth_path, environ):
            return None
        models_path = self._effective_config_path(home, environ, "models.json")
        if self._usable_selected_model(models_path, environ):
            return None
        return NO_USABLE_CREDENTIALS

    def bootstrap(self, ctx: BootstrapContext) -> None:
        config_dir = self._agent_dir(ctx.home)
        config_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_personal_config(config_dir, ctx.environ)
        write_settings(config_dir)
        write_workflow_overview(config_dir, ctx.overview)
        (config_dir / EXTENSION_FILE).write_text(TURN_EXTENSION)
        # pi's operations keep their REST-flavored instructions (no MCP client to call), so the
        # shared operation_skill() — which speaks MCP — is deliberately not used here.
        entries = list(ctx.skills) + [
            Skill(
                name=name,
                description=f"Apply the workflow's '{name}' operation.",
                instructions=operation_instructions(
                    name,
                    target_state,
                    ctx.task_id,
                    ctx.service_url,
                    authenticated=bool(ctx.environ.get("PANOPTICON_SERVICE_AUTH_TOKEN")),
                ),
            )
            for name, target_state in ctx.operations.items()
        ]
        write_skills(entries, ctx.home, ctx.task_id)

    def _ensure_personal_config(self, config_dir: Path, environ: Mapping[str, str]) -> None:
        """Import entries from mounted ``pi/agent/`` into pi's native config directory.

        The config volume persists across respawns, so existing files and symlinks always win.
        Directories and rotating auth remain linked; regular files are copied, with
        ``models.json`` receiving safe URL adaptation.
        """
        personal_config = self._mounted_agent_dir(environ)
        if personal_config is None or not personal_config.is_dir():
            return
        for source in personal_config.iterdir():
            destination = config_dir / source.name
            if destination.exists() or destination.is_symlink():
                continue
            if source.name == "models.json" and source.is_file():
                destination.write_bytes(self._materialized_models(source))
            elif source.is_file() and source.name != AUTH_FILE:
                destination.write_bytes(source.read_bytes())
            else:
                destination.symlink_to(source)

    @staticmethod
    def _materialized_models(source: Path) -> bytes:
        try:
            data = json.loads(source.read_text())
        except (OSError, UnicodeError, json.JSONDecodeError):
            return source.read_bytes()
        if not isinstance(data, dict) or not isinstance(data.get("providers"), dict):
            return source.read_bytes()
        rendered = copy.deepcopy(data)
        for provider in rendered["providers"].values():
            if not isinstance(provider, dict) or not isinstance(provider.get("baseUrl"), str):
                continue
            provider["baseUrl"] = PiHarness._container_model_url(provider["baseUrl"])
        if rendered == data:
            return source.read_bytes()
        return json.dumps(rendered, ensure_ascii=False, separators=(",", ":")).encode()

    @staticmethod
    def _container_model_url(value: str) -> str:
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
                "localhost",
                "127.0.0.1",
                "::1",
            }:
                return value
            port = parsed.port
        except ValueError:
            return value
        userinfo = parsed.netloc.rsplit("@", 1)[0] + "@" if "@" in parsed.netloc else ""
        netloc = f"{userinfo}host.docker.internal"
        if port is not None:
            netloc += f":{port}"
        return urlunsplit(parsed._replace(netloc=netloc))

    def argv(self, ctx: LaunchContext) -> list[str]:
        """``pi`` argv. pi "runs with all permissions by default" (its own containerization
        docs) — no bypass/skip-permissions flag needed, unlike claude/codex. Resumes the config
        volume's most recent session when one is recorded (``--continue``, which silently starts
        fresh otherwise — see the module docstring); like claude/codex, a resume on the agent's
        turn gets :data:`INTERRUPT_PROMPT` appended so it picks back up."""
        config_dir = self._agent_dir(ctx.home)
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
        return {"PI_CODING_AGENT_DIR": str(self._agent_dir(ctx.home))}
