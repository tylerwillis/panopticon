"""Golden rendering tests for the Outfitter 0.10.0 adapter.

The install requirements, ``run --profile --agent pi --`` pass-through surface, profile-source
settings shape, and pi state fallback come from Outfitter's published docs/source. Live npm/tmux
smoke reached pi but exposed Outfitter 0.10.0's width-unsafe injected header; registry tests keep
this staged adapter unavailable until upstream fixes it.
"""

from __future__ import annotations

from pathlib import Path

from panopticon.core.models import Skill
from panopticon.harnesses import INTERRUPT_PROMPT, BootstrapContext, LaunchContext
from panopticon.harnesses.outfitter import (
    EXTENSION_FILE,
    NODE_VERSION,
    OUTFITTER_VERSION,
    PI_NATIVE_CONFIG_DIR,
    PI_VERSION,
    PROFILE_SOURCES_DIR,
    SETTINGS,
    SETTINGS_FILE,
    TURN_EXTENSION,
    WORKFLOW_OVERVIEW_FILE,
    OutfitterHarness,
)

HARNESS = OutfitterHarness()


def _ctx(home: Path, **kwargs: str | None) -> LaunchContext:
    return LaunchContext(home=home, cwd=Path("/workspace"), **kwargs)  # type: ignore[arg-type]


def _bootstrap_ctx(home: Path, **kwargs: object) -> BootstrapContext:
    defaults: dict[str, object] = {
        "home": home,
        "cwd": Path("/workspace"),
        "service_url": "http://host.docker.internal:8000",
        "task_id": "t1",
        "skills": [Skill(name="open-pr", description="Open the PR.", instructions="gh pr create")],
        "operations": {"advance": "COMPLETE"},
        "overview": "# the workflow map",
        "environ": {},
    }
    defaults.update(kwargs)
    return BootstrapContext(**defaults)  # type: ignore[arg-type]


def test_bootstrap_pins_every_outfitter_artifact(tmp_path: Path) -> None:
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path))
    config = tmp_path / ".outfitter"
    assert (config / SETTINGS_FILE).read_text() == SETTINGS
    assert SETTINGS == "profile_sources:\n  - path: ./profile_sources\n"
    assert (config / PROFILE_SOURCES_DIR).is_dir()
    assert (config / WORKFLOW_OVERVIEW_FILE).read_text() == "# the workflow map"
    assert (config / EXTENSION_FILE).read_text() == TURN_EXTENSION

    skill = (tmp_path / ".agents" / "skills" / "open-pr" / "SKILL.md").read_text()
    assert skill == (
        "---\nname: open-pr\ndescription: Open the PR.\n---\n"
        'gh pr create\n\nThis is task `t1` — pass `task_id="t1"` to every panopticon MCP '
        "tool you call here.\n"
    )
    operation = (tmp_path / ".agents" / "skills" / "advance" / "SKILL.md").read_text()
    assert operation == (
        "---\nname: advance\ndescription: Apply the workflow's 'advance' operation.\n---\n"
        "Apply this workflow's `advance` operation — it moves the task to **COMPLETE**. "
        "pi has no MCP client, so call the task service's REST API directly (no request body "
        "needed): `curl --fail --silent --show-error --request POST "
        '"http://host.docker.internal:8000/tasks/t1/operations/advance"`. Don\'t edit the state '
        "directly. It's gated on the current state's responsibilities and starts a new turn.\n\n"
        'This is task `t1` — pass `task_id="t1"` to every panopticon MCP tool you call here.\n'
    )


def test_argv_passes_profile_and_panopticon_controls_through_to_pi(tmp_path: Path) -> None:
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path))
    assert HARNESS.argv(
        _ctx(tmp_path, starting_model="engineering-default", initial_prompt="start now")
    ) == [
        "outfitter",
        "run",
        "--profile",
        "engineering-default",
        "--agent",
        "pi",
        "--",
        "--extension",
        str(tmp_path / ".outfitter" / EXTENSION_FILE),
        "--append-system-prompt",
        "# the workflow map",
        "--skill",
        str(tmp_path / ".agents" / "skills" / "advance"),
        "--skill",
        str(tmp_path / ".agents" / "skills" / "open-pr"),
        "start now",
    ]


def test_starting_model_is_a_profile_id_not_a_pi_model(tmp_path: Path) -> None:
    argv = HARNESS.argv(_ctx(tmp_path, starting_model="local-qwen-high"))
    assert argv == [
        "outfitter",
        "run",
        "--profile",
        "local-qwen-high",
        "--agent",
        "pi",
        "--",
    ]
    assert "--model" not in argv
    assert "-p" not in argv and "--print" not in argv  # interactive tmux launch, not smoke mode


def test_blank_overview_and_absent_skills_still_render_required_turn_extension(
    tmp_path: Path,
) -> None:
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, overview=" ", skills=[], operations={}))
    assert HARNESS.argv(_ctx(tmp_path)) == [
        "outfitter",
        "run",
        "--agent",
        "pi",
        "--",
        "--extension",
        str(tmp_path / ".outfitter" / EXTENSION_FILE),
    ]


def test_resume_uses_pi_native_state_fallback_and_interrupt_prompt(tmp_path: Path) -> None:
    sessions = tmp_path / ".pi" / "agent" / "sessions" / "--workspace--"
    sessions.mkdir(parents=True)
    (sessions / "session-1.jsonl").write_text("{}")
    assert HARNESS.argv(
        _ctx(
            tmp_path,
            starting_model="ignored-on-resume",
            initial_prompt="ignored on resume",
            turn="agent",
        )
    ) == [
        "outfitter",
        "run",
        "--profile",
        "ignored-on-resume",
        "--agent",
        "pi",
        "--",
        "--continue",
        INTERRUPT_PROMPT,
    ]


def test_auth_delegates_to_pi_presence_rules_and_links_credential_file(tmp_path: Path) -> None:
    assert HARNESS.missing_auth({"GROQ_API_KEY": "k"}, home=tmp_path) is None
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "auth.json").write_text("{}")
    env = {"PANOPTICON_CREDENTIALS": str(credentials)}
    assert HARNESS.missing_auth(env, home=tmp_path) is None
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, environ=env))
    auth = tmp_path / PI_NATIVE_CONFIG_DIR / "auth.json"
    assert auth.is_symlink() and auth.resolve() == (credentials / "auth.json").resolve()


def test_missing_auth_accepts_outfitters_native_pi_state_fallback(tmp_path: Path) -> None:
    auth = tmp_path / PI_NATIVE_CONFIG_DIR / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text("{}")
    assert HARNESS.missing_auth({}, home=tmp_path) is None


def test_missing_auth_does_not_mistake_direct_pi_harness_state_for_outfitter_state(
    tmp_path: Path,
) -> None:
    direct_pi_auth = tmp_path / ".pi" / "auth.json"
    direct_pi_auth.parent.mkdir(parents=True)
    direct_pi_auth.write_text("{}")
    assert HARNESS.missing_auth({}, home=tmp_path) is not None


def test_missing_auth_honestly_names_pi_credentials(tmp_path: Path) -> None:
    detail = HARNESS.missing_auth({}, home=tmp_path)
    assert detail is not None and "pi credentials" in detail and "credential_dir" in detail


def test_image_layer_installs_all_runtime_components_at_pinned_versions() -> None:
    layer = HARNESS.image_layer()
    assert NODE_VERSION == "22.19.0"
    assert PI_VERSION == "0.80.3"
    assert OUTFITTER_VERSION == "0.10.0"
    assert layer == (
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


def test_config_dir_is_the_outfitter_home_and_env_needs_no_override(tmp_path: Path) -> None:
    assert HARNESS.config_dir(tmp_path) == tmp_path / ".outfitter"
    assert HARNESS.env(_ctx(tmp_path)) == {}
