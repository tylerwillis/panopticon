"""The Outfitter harness — ``@ai-outfitter/outfitter`` wrapping pi.

Verified from Outfitter 0.10.0's published docs and TypeScript source: it requires Node
``>=22.19.0``, agent CLIs are installed separately, and ``outfitter run --profile <id>
--agent pi -- <args>`` passes the remaining arguments to pi. Outfitter profiles own provider,
model, thinking, skills, extensions, and prompts, so Panopticon deliberately interprets a task's
``starting_model`` as the Outfitter **profile id**, not as a model name.

Panopticon's additions ride through Outfitter's documented pass-through: the workflow overview
via pi's ``--append-system-prompt``, :data:`panopticon.harnesses.pi.TURN_EXTENSION` via
``--extension``, and rendered workflow skills via repeated ``--skill``. Core operations retain
pi's REST instructions because neither pi nor Outfitter provides an MCP client.

Profiles are an explicit v1 provisioning gap. Bootstrap writes ``~/.outfitter/settings.yml``
with a single local source, ``~/.outfitter/profile_sources``. An operator or future provisioning
slice must populate that directory with flat profile YAML files or directory profiles before
launch; this adapter does not invent a host mount or catalog-sync policy.

Auth is pi auth, not Outfitter auth. Presence checking uses pi's provider environment variables,
while credential-dir linking targets Outfitter's native pi-state fallback; provider validity
remains pi's concern.

**Hard blocker (verified live).** Outfitter does pass the terminal through correctly: its real
launcher spawns the bundled pi with ``stdio: "inherit"``. The failure is instead Outfitter
0.10.0's always-injected interactive extension. Its custom startup header returns fixed lines
from ``render: () => lines`` and ignores the terminal width; several lines exceed a normal
detached-tmux pane. pi-tui 0.80.3's ``doRender()`` intentionally throws when a custom component
renders wider than the viewport. Because ``doRender()`` is called by the scheduled render timer,
the stack misleadingly ends at ``Timeout._onTimeout`` / ``dist/tui.js:540``. Bare pi works because
it does not load this header.

No Outfitter invocation or flag disables only this extension while retaining an interactive pi
TUI. ``startup.ascii_art: false`` removes the logo but leaves other unbounded header lines, while
``-p``/``--print`` and other non-interactive modes are not substitutes for Panopticon's tmux UI.
The upstream unblock is precise: Outfitter's ``createStartupHeaderLines`` component must implement
``render(width)`` and wrap or truncate every returned line to that width (using pi-tui's
``visibleWidth`` plus ``wrapTextWithAnsi``/``truncateToWidth``), with a narrow-terminal regression
test. Until an Outfitter release contains that fix and passes the live tmux smoke, this module is
kept for the verified rendering work but deliberately omitted from :data:`HARNESSES`.

Resume remains inferred from Outfitter's documented default state symlink to
``~/.pi/agent/sessions`` rather than exercised in a successful live session.
"""

from __future__ import annotations

import json
import re
import textwrap
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar, Final

from panopticon.core.models import Skill
from panopticon.harnesses.base import INTERRUPT_PROMPT, BootstrapContext, Harness, LaunchContext
from panopticon.harnesses.codex import write_skills
from panopticon.harnesses.pi import (
    API_KEY_ENV_VARS,
    AUTH_FILE,
    NODE_VERSION,
    PI_VERSION,
    TURN_EXTENSION,
    operation_instructions,
)

OUTFITTER_VERSION = "0.11.0"
SETTINGS_FILE = "settings.yml"
PROFILE_SOURCES_DIR = "profile_sources"
WORKFLOW_OVERVIEW_FILE = "workflow-overview.md"
EXTENSION_FILE = "turn.ts"

SETTINGS = "profile_sources:\n  - path: ./profile_sources\n"
PI_NATIVE_CONFIG_DIR = Path(".pi") / "agent"
PROFILE_LABEL_WIDTH: Final = 80


def _top_level_scalar(text: str, key: str) -> str | bool | None:
    """Read the small scalar metadata subset used by profile discovery."""
    match = re.search(rf"(?m)^{re.escape(key)}:[ \t]*(.*)$", text)
    if match is None:
        return None
    value = match.group(1).strip()
    if value.casefold() in {"true", "false"}:
        return value.casefold() == "true"
    if value.startswith('"'):
        try:
            loaded = json.loads(value)
            return loaded if isinstance(loaded, str) else None
        except json.JSONDecodeError:
            return None
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    if value in {"|", "|-", "|+", ">", ">-", ">+"}:
        lines: list[str] = []
        for line in text[match.end() :].splitlines():
            if line and not line.startswith((" ", "\t")):
                break
            lines.append(line.strip())
        return " ".join(lines)
    return value.split(" #", 1)[0].strip() or None


class OutfitterHarness(Harness):
    """Outfitter's profile manager/launcher, fixed to its primary pi adapter."""

    name: ClassVar[str] = "outfitter"
    config_dirname: ClassVar[str] = ".outfitter"
    field_label: ClassVar[str] = "profile"

    def __init__(self, profile_sources_root: Path | None = None) -> None:
        self.profile_sources_root = profile_sources_root

    def suggested_models(self) -> tuple[tuple[str, str], ...]:
        """Discover launchable profiles from the adapter's local profile source."""
        root = self.profile_sources_root or self.config_dir(Path.home()) / PROFILE_SOURCES_DIR
        try:
            paths = sorted(root.glob("*.yml")) + sorted(root.glob("*.yaml"))
            paths += sorted(path / "profile.yml" for path in root.iterdir() if path.is_dir())
        except OSError:
            return ()

        profiles: dict[str, str] = {}
        for path in paths:
            try:
                text = path.read_text()
            except (OSError, UnicodeError):
                continue
            profile_id = _top_level_scalar(text, "id")
            if profile_id is None and path.parent == root:
                profile_id = path.stem
            if not isinstance(profile_id, str) or _top_level_scalar(text, "template") is True:
                continue
            description = _top_level_scalar(text, "description")
            label = profile_id
            if isinstance(description, str):
                summary = " ".join(description.split())
                label = textwrap.shorten(
                    f"{profile_id} — {summary}", width=PROFILE_LABEL_WIDTH, placeholder="…"
                )
            profiles[profile_id] = label
        return tuple(sorted(profiles.items()))

    def image_layer(self) -> str:
        """Install pinned Node, pi, and Outfitter releases.

        The versions and separate pi install are verified from Outfitter 0.10.0's package
        manifest and installation docs; the resulting container launch is not yet smoke-tested.
        """
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
            f"@earendil-works/pi-coding-agent@{PI_VERSION} "
            f"@ai-outfitter/outfitter@{OUTFITTER_VERSION}"
        )

    def missing_auth(self, environ: Mapping[str, str], *, home: Path) -> str | None:
        """Check pi credentials at Outfitter's actual durable-state source.

        Verified from Outfitter's ``resolvePiStateSourcePath``: a selected profile's existing
        ``cli_specific/pi/auth.json`` takes precedence; otherwise the composite ``auth.json`` is
        symlinked from ``~/.pi/agent/auth.json``. The credential-dir path is accepted because
        :meth:`bootstrap` links it to that fallback before Outfitter builds the composite.
        """
        if any(environ.get(var) for var in API_KEY_ENV_VARS):
            return None
        if (home / PI_NATIVE_CONFIG_DIR / AUTH_FILE).exists():
            return None
        credentials = environ.get("PANOPTICON_CREDENTIALS")
        if credentials and (Path(credentials) / AUTH_FILE).exists():
            return None
        return (
            "No pi credentials for Outfitter — set one of pi's provider API-key env vars "
            "(ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, GROQ_API_KEY, … — see pi's "
            "docs/providers.md for the full list), or give the repo a credential_dir holding "
            "a pi auth.json from `/login` (see docs/auth.md)"
        )

    def bootstrap(self, ctx: BootstrapContext) -> None:
        config_dir = self.config_dir(ctx.home)
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / PROFILE_SOURCES_DIR).mkdir(exist_ok=True)
        (config_dir / SETTINGS_FILE).write_text(SETTINGS)
        (config_dir / WORKFLOW_OVERVIEW_FILE).write_text(ctx.overview)
        (config_dir / EXTENSION_FILE).write_text(TURN_EXTENSION)

        entries = list(ctx.skills) + [
            Skill(
                name=name,
                description=f"Apply the workflow's '{name}' operation.",
                instructions=operation_instructions(
                    name, target_state, ctx.task_id, ctx.service_url
                ),
            )
            for name, target_state in ctx.operations.items()
        ]
        write_skills(entries, ctx.home, ctx.task_id)
        self._ensure_auth(ctx.home, ctx.environ)

    def _ensure_auth(self, home: Path, environ: Mapping[str, str]) -> None:
        """Link credential-dir ``auth.json`` at pi's native Outfitter fallback location."""
        pi_config = home / PI_NATIVE_CONFIG_DIR
        pi_config.mkdir(parents=True, exist_ok=True)
        auth = pi_config / AUTH_FILE
        if auth.exists() or auth.is_symlink():
            return
        credentials = environ.get("PANOPTICON_CREDENTIALS")
        if credentials and (Path(credentials) / AUTH_FILE).exists():
            auth.symlink_to(Path(credentials) / AUTH_FILE)

    def argv(self, ctx: LaunchContext) -> list[str]:
        """Launch the selected Outfitter profile through pi with Panopticon pass-through args."""
        config_dir = self.config_dir(ctx.home)
        argv = ["outfitter", "run"]
        if ctx.starting_model:
            argv += ["--profile", ctx.starting_model]
        argv += ["--agent", "pi", "--"]

        extension = config_dir / EXTENSION_FILE
        if extension.exists():
            argv += ["--extension", str(extension)]
        overview = config_dir / WORKFLOW_OVERVIEW_FILE
        if overview.exists() and (content := overview.read_text()).strip():
            argv += ["--append-system-prompt", content]
        skills = ctx.home / ".agents" / "skills"
        if skills.exists():
            for skill in sorted(path for path in skills.iterdir() if path.is_dir()):
                argv += ["--skill", str(skill)]

        sessions = ctx.home / ".pi" / "agent" / "sessions"
        if sessions.exists() and any(sessions.rglob("*.jsonl")):
            argv.append("--continue")
            if ctx.turn == "agent":
                argv.append(INTERRUPT_PROMPT)
            return argv
        if ctx.initial_prompt:
            argv.append(ctx.initial_prompt)
        return argv
