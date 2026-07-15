"""The pi harness: settings.json, workflow-overview file, REST-curl operation instructions
(no MCP), SKILL.md rendering, auth, argv.

Facts pinned against pi-coding-agent 0.80.7 (its README/docs and published npm manifest) — not
observed behavior; there is no live pi process behind these assertions, see the module docstring.
"""

from __future__ import annotations

import json
from pathlib import Path

from panopticon.core.models import Skill
from panopticon.harnesses import INTERRUPT_PROMPT, BootstrapContext, LaunchContext
from panopticon.harnesses.pi import NODE_VERSION, PI_VERSION, PiHarness, settings

HARNESS = PiHarness()


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


# -- settings.json --------------------------------------------------------------------


def test_settings_pre_accepts_project_trust() -> None:
    # No operator in the container to answer pi's interactive trust prompt.
    assert settings() == {"defaultProjectTrust": "always"}


def test_bootstrap_writes_settings_json(tmp_path: Path) -> None:
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path))
    data = json.loads((tmp_path / ".pi" / "settings.json").read_text())
    assert data["defaultProjectTrust"] == "always"


def test_bootstrap_merges_settings_without_clobbering_existing_keys(tmp_path: Path) -> None:
    settings_path = tmp_path / ".pi" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"theme": "light"}))
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path))
    data = json.loads(settings_path.read_text())
    assert data["theme"] == "light"
    assert data["defaultProjectTrust"] == "always"


# -- workflow overview ------------------------------------------------------------------


def test_bootstrap_writes_the_workflow_overview_file(tmp_path: Path) -> None:
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, overview="# the map"))
    assert (tmp_path / ".pi" / "workflow-overview.md").read_text() == "# the map"


def test_bootstrap_omits_the_overview_file_when_blank(tmp_path: Path) -> None:
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, overview="   "))
    assert not (tmp_path / ".pi" / "workflow-overview.md").exists()


# -- bootstrap: skills + operations (no MCP) ---------------------------------------------


def test_bootstrap_writes_skills_and_operations(tmp_path: Path) -> None:
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path))
    skill = (tmp_path / ".agents" / "skills" / "open-pr" / "SKILL.md").read_text()
    assert skill.startswith("---\nname: open-pr\ndescription: Open the PR.\n---\ngh pr create")
    assert 'task_id="t1"' in skill  # the concrete task id, injected for REST calls

    operation = (tmp_path / ".agents" / "skills" / "advance" / "SKILL.md").read_text()
    assert "COMPLETE" in operation


def test_operation_instructions_curl_the_rest_api_not_an_mcp_tool(tmp_path: Path) -> None:
    # pi has no MCP client (its own stated design) — advance/drop must be plain REST calls.
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, task_id="t7", operations={"advance": "COMPLETE"}))
    operation = (tmp_path / ".agents" / "skills" / "advance" / "SKILL.md").read_text()
    assert "apply_operation" not in operation
    assert "MCP" in operation  # names the reason, for the agent's benefit
    assert (
        "curl --fail --silent --show-error --request POST "
        '"http://host.docker.internal:8000/tasks/t7/operations/advance"' in operation
    )


def test_bootstrap_renders_skills_user_scope_not_into_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, cwd=workspace))
    assert not (workspace / ".agents").exists()
    assert (tmp_path / ".agents" / "skills").is_dir()


def test_bootstrap_is_idempotent_across_respawns(tmp_path: Path) -> None:
    ctx = _bootstrap_ctx(tmp_path)
    HARNESS.bootstrap(ctx)
    HARNESS.bootstrap(ctx)  # a respawn re-runs the bootstrap on the same config volume
    assert (tmp_path / ".pi" / "settings.json").exists()


# -- auth ----------------------------------------------------------------------------


def test_bootstrap_never_renders_an_api_key_auth_file(tmp_path: Path) -> None:
    # Unlike codex: pi resolves an env-var API key itself at runtime, so bootstrap writes nothing.
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, environ={"ANTHROPIC_API_KEY": "sk-ant-x"}))
    assert not (tmp_path / ".pi" / "auth.json").exists()


def test_bootstrap_symlinks_auth_from_the_credential_mount(tmp_path: Path) -> None:
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "auth.json").write_text('{"anthropic": {"type": "oauth"}}')
    HARNESS.bootstrap(
        _bootstrap_ctx(tmp_path, environ={"PANOPTICON_CREDENTIALS": str(credentials)})
    )
    auth = tmp_path / ".pi" / "auth.json"
    assert auth.is_symlink() and auth.resolve() == (credentials / "auth.json").resolve()


def test_bootstrap_never_clobbers_an_existing_auth_file(tmp_path: Path) -> None:
    config_dir = tmp_path / ".pi"
    config_dir.mkdir(parents=True)
    (config_dir / "auth.json").write_text('{"anthropic": {"type": "oauth", "tokens": {}}}')
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "auth.json").write_text('{"anthropic": {"type": "oauth"}}')
    HARNESS.bootstrap(
        _bootstrap_ctx(tmp_path, environ={"PANOPTICON_CREDENTIALS": str(credentials)})
    )
    assert not (config_dir / "auth.json").is_symlink()


def test_missing_auth_accepts_each_api_key_env_var(tmp_path: Path) -> None:
    assert HARNESS.missing_auth({"ANTHROPIC_API_KEY": "k"}, home=tmp_path) is None
    assert HARNESS.missing_auth({"OPENAI_API_KEY": "k"}, home=tmp_path) is None
    assert HARNESS.missing_auth({"GEMINI_API_KEY": "k"}, home=tmp_path) is None


def test_missing_auth_accepts_a_mounted_credential_dir(tmp_path: Path) -> None:
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "auth.json").write_text("{}")
    env = {"PANOPTICON_CREDENTIALS": str(credentials)}
    assert HARNESS.missing_auth(env, home=tmp_path) is None


def test_missing_auth_accepts_an_auth_file_on_the_config_volume(tmp_path: Path) -> None:
    (tmp_path / ".pi").mkdir()
    (tmp_path / ".pi" / "auth.json").write_text("{}")
    assert HARNESS.missing_auth({}, home=tmp_path) is None


def test_missing_auth_names_the_fix_when_nothing_is_configured(tmp_path: Path) -> None:
    detail = HARNESS.missing_auth({}, home=tmp_path)
    assert detail is not None
    assert "ANTHROPIC_API_KEY" in detail and "credential_dir" in detail


# -- argv ----------------------------------------------------------------------------


def _seed_session(home: Path) -> None:
    sessions = home / ".pi" / "sessions" / "--workspace--"
    sessions.mkdir(parents=True)
    (sessions / "session-1.jsonl").write_text("{}")


def test_argv_first_run_is_bare() -> None:
    # pi "runs with all permissions by default" — no bypass/skip-permissions flag needed.
    assert HARNESS.argv(_ctx(Path("/home/x"))) == ["pi"]


def test_argv_first_run_passes_model_then_prompt(tmp_path: Path) -> None:
    argv = HARNESS.argv(_ctx(tmp_path, initial_prompt="start now", starting_model="sonnet"))
    assert argv == ["pi", "--model", "sonnet", "start now"]


def test_argv_resumes_with_continue_when_a_session_is_recorded(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    assert HARNESS.argv(_ctx(tmp_path)) == ["pi", "--continue"]


def test_argv_resume_appends_interrupt_prompt_on_agent_turn(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    argv = HARNESS.argv(_ctx(tmp_path, turn="agent"))
    assert argv == ["pi", "--continue", INTERRUPT_PROMPT]


def test_argv_resume_omits_model_and_initial_prompt(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    argv = HARNESS.argv(_ctx(tmp_path, initial_prompt="start now", starting_model="sonnet"))
    assert "--model" not in argv and "start now" not in argv


def test_argv_appends_system_prompt_from_the_rendered_overview_file(tmp_path: Path) -> None:
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, overview="# the map"))
    argv = HARNESS.argv(_ctx(tmp_path))
    assert argv[:3] == ["pi", "--append-system-prompt", "# the map"]


def test_argv_appends_system_prompt_on_resume_too(tmp_path: Path) -> None:
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, overview="# the map"))
    _seed_session(tmp_path)
    argv = HARNESS.argv(_ctx(tmp_path))
    assert argv == ["pi", "--append-system-prompt", "# the map", "--continue"]


# -- image layer + env ----------------------------------------------------------------


def test_image_layer_installs_pinned_node_and_pi_for_both_architectures() -> None:
    layer = HARNESS.image_layer()
    assert f"v{NODE_VERSION}/node-v{NODE_VERSION}-linux-$node_arch.tar.xz" in layer
    assert 'x86_64) node_arch="x64"' in layer and 'aarch64) node_arch="arm64"' in layer
    assert f"@earendil-works/pi-coding-agent@{PI_VERSION}" in layer  # pinned, not `latest`
    assert "--extract --xz --directory" in layer  # long options (repo convention)
    assert "npm install --global --ignore-scripts" in layer


def test_env_points_pi_at_the_per_task_config_dir(tmp_path: Path) -> None:
    assert HARNESS.env(_ctx(tmp_path)) == {"PI_CODING_AGENT_DIR": str(tmp_path / ".pi")}
