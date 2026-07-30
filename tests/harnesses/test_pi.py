"""The pi harness: settings.json, workflow-overview file, the turn-flip extension, REST-curl
operation instructions (no MCP), SKILL.md rendering, auth, argv.

Facts pinned against pi-coding-agent 0.80.3 (a real local install) and the pi-mono TypeScript
source (event/handler types) — see the module docstring for exactly what's verified vs. not.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

from panopticon.core.models import Skill
from panopticon.harnesses import INTERRUPT_PROMPT, BootstrapContext, LaunchContext
from panopticon.harnesses.pi import (
    API_KEY_ENV_VARS,
    EXTENSION_FILE,
    NODE_VERSION,
    PI_VERSION,
    TURN_EXTENSION,
    PiHarness,
)


class _Recorder:
    def __init__(self, stdout: str = "", error: Exception | None = None) -> None:
        self.calls: list[list[str]] = []
        self._stdout = stdout
        self._error = error

    def __call__(self, args: Sequence[str], *, check: bool = True) -> str:
        self.calls.append(list(args))
        if self._error:
            raise self._error
        return self._stdout


HARNESS = PiHarness(run=_Recorder())


def test_picker_metadata_uses_pi_native_model_syntax() -> None:
    output = """provider  model                    context max-out thinking images
anthropic claude-sonnet-4-5       200K    64K     yes      yes
openai    gpt-5.2-codex           400K    128K    yes      yes
openrouter anthropic/claude-opus-4 200K   32K     yes      yes
"""
    run = _Recorder(output)
    harness = PiHarness(run=run)

    assert HARNESS.field_label == "model"
    assert harness.suggested_models() == (
        ("anthropic/claude-sonnet-4-5", "anthropic/claude-sonnet-4-5"),
        ("openai/gpt-5.2-codex", "openai/gpt-5.2-codex"),
        ("openrouter/anthropic/claude-opus-4", "openrouter/anthropic/claude-opus-4"),
    )
    assert run.calls == [["pi", "--list-models"]]
    assert HARNESS.suggested_efforts("provider/model") == ()


def test_picker_metadata_fails_soft_when_pi_is_absent() -> None:
    harness = PiHarness(run=_Recorder(error=FileNotFoundError("pi")))

    assert harness.suggested_models() == ()


def test_picker_metadata_fails_soft_when_pi_errors() -> None:
    error = subprocess.CalledProcessError(1, ["pi", "--list-models"])
    harness = PiHarness(run=_Recorder(error=error))

    assert harness.suggested_models() == ()


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


# -- settings.json + workflow overview --------------------------------------------------


def test_bootstrap_writes_settings_and_merges_and_is_idempotent(tmp_path: Path) -> None:
    settings_path = tmp_path / ".pi" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"theme": "light"}))
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path))
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path))  # a respawn re-runs bootstrap; must be idempotent
    data = json.loads(settings_path.read_text())
    assert data["theme"] == "light"  # preserved
    # No operator in the container to answer pi's interactive project-trust prompt.
    assert data["defaultProjectTrust"] == "always"


def test_bootstrap_writes_the_workflow_overview_file(tmp_path: Path) -> None:
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, overview="# the map"))
    assert (tmp_path / ".pi" / "workflow-overview.md").read_text() == "# the map"


def test_bootstrap_omits_the_overview_file_when_blank(tmp_path: Path) -> None:
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, overview="   "))
    assert not (tmp_path / ".pi" / "workflow-overview.md").exists()


def test_bootstrap_removes_a_stale_overview_left_by_an_earlier_bootstrap(tmp_path: Path) -> None:
    # The config volume persists across respawns — a later bootstrap with no overview must not
    # leave an earlier one's file behind for argv() to keep injecting via --append-system-prompt.
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, overview="# the map"))
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, overview=""))
    assert not (tmp_path / ".pi" / "workflow-overview.md").exists()
    assert HARNESS.argv(_ctx(tmp_path))[:2] != ["pi", "--append-system-prompt"]


# -- the turn-flip extension -------------------------------------------------------------


def test_extension_puts_the_turn_via_the_task_service_rest_api() -> None:
    # Mirrors container/hook.py's contract exactly: PUT .../turn with {"turn": ...}.
    assert "process.env.PANOPTICON_SERVICE_URL" in TURN_EXTENSION
    assert "process.env.PANOPTICON_TASK_ID" in TURN_EXTENSION
    assert "/tasks/${process.env.PANOPTICON_TASK_ID}/turn" in TURN_EXTENSION
    assert 'method: "PUT"' in TURN_EXTENSION


def test_extension_flips_to_user_on_settle_and_agent_on_input() -> None:
    assert 'pi.on("agent_end", () => setTurn("user"));' in TURN_EXTENSION
    assert 'pi.on("input", () => setTurn("agent"));' in TURN_EXTENSION


# 2119: REQ-008.6.1
def test_input_handler_waits_for_the_agent_turn_request() -> None:
    source = TURN_EXTENSION.replace("export default function", "const extension = function")
    probe = (
        source
        + """
let inputHandler;
const pi = { on(event, handler) { if (event === "input") inputHandler = handler; } };
let resolveFetch;
globalThis.fetch = () => new Promise((resolve) => { resolveFetch = resolve; });
extension(pi);
let settled = false;
const pending = Promise.resolve(inputHandler()).then(() => { settled = true; });
await new Promise(setImmediate);
if (settled) throw new Error("input handler returned before the turn request completed");
resolveFetch({ ok: true });
await pending;
"""
    )

    subprocess.run(["node", "--input-type=module", "--eval", probe], check=True)


# 2119: REQ-016.1.1
# 2119: REQ-016.2.1
def test_turn_signal_handlers_bound_requests_and_fail_open() -> None:
    source = TURN_EXTENSION.replace("export default function", "const extension = function")
    probe = (
        source
        + """
const handlers = {};
const pi = { on(event, handler) { handlers[event] = handler; } };
globalThis.fetch = (_url, options) => {
  if (!options.signal) return new Promise(() => {});
  return new Promise((_resolve, reject) => {
    options.signal.addEventListener(
      "abort",
      () => reject(new Error("control plane unavailable")),
      { once: true },
    );
  });
};
extension(pi);
const started = Date.now();
await Promise.all([handlers.agent_end(), handlers.input()]);
const elapsed = Date.now() - started;
if (elapsed >= 3000) throw new Error(`handlers blocked for ${elapsed}ms`);
"""
    )

    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", probe],
        check=True,
        timeout=3.5,
        capture_output=True,
        text=True,
    )
    assert completed.stdout == "" and completed.stderr == ""


# 2119: REQ-016.2.1
def test_turn_signal_handlers_do_not_surface_network_or_status_failures() -> None:
    source = TURN_EXTENSION.replace("export default function", "const extension = function")
    probe = (
        source
        + """
const handlers = {};
const pi = { on(event, handler) { handlers[event] = handler; } };
let failure = "network";
globalThis.fetch = () => {
  if (failure === "network") {
    return Promise.reject(new Error("CONTROL_PLANE_FAILURE_SENTINEL"));
  }
  return Promise.resolve({ ok: false, status: 503, statusText: "CONTROL_PLANE_FAILURE_SENTINEL" });
};
extension(pi);
await handlers.agent_end();
await handlers.input();
failure = "status";
await handlers.agent_end();
await handlers.input();
"""
    )

    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", probe],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout == "" and completed.stderr == ""


def test_bootstrap_writes_the_extension_file(tmp_path: Path) -> None:
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path))
    assert (tmp_path / ".pi" / EXTENSION_FILE).read_text() == TURN_EXTENSION


def test_argv_loads_the_extension_when_rendered(tmp_path: Path) -> None:
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, overview=""))
    argv = HARNESS.argv(_ctx(tmp_path))
    assert argv == ["pi", "--extension", str(tmp_path / ".pi" / EXTENSION_FILE)]


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


def test_bootstrap_symlinks_personal_config_from_pi_credential_subdirectory(
    tmp_path: Path,
) -> None:
    credentials = tmp_path / "credentials"
    personal_config = credentials / "pi"
    personal_config.mkdir(parents=True)
    (personal_config / "models.json").write_text('{"providers": {"local": {}}}')
    (personal_config / "prompts").mkdir()

    HARNESS.bootstrap(
        _bootstrap_ctx(tmp_path, environ={"PANOPTICON_CREDENTIALS": str(credentials)})
    )

    models = tmp_path / ".pi" / "models.json"
    prompts = tmp_path / ".pi" / "prompts"
    assert models.is_symlink() and models.resolve() == (personal_config / "models.json").resolve()
    assert prompts.is_symlink() and prompts.resolve() == (personal_config / "prompts").resolve()


def test_bootstrap_never_clobbers_existing_personal_config(tmp_path: Path) -> None:
    config_dir = tmp_path / ".pi"
    config_dir.mkdir()
    (config_dir / "models.json").write_text('{"providers": {"persisted": {}}}')
    credentials = tmp_path / "credentials"
    personal_config = credentials / "pi"
    personal_config.mkdir(parents=True)
    (personal_config / "models.json").write_text('{"providers": {"mounted": {}}}')

    HARNESS.bootstrap(
        _bootstrap_ctx(tmp_path, environ={"PANOPTICON_CREDENTIALS": str(credentials)})
    )

    models = config_dir / "models.json"
    assert not models.is_symlink()
    assert models.read_text() == '{"providers": {"persisted": {}}}'


def test_missing_auth_accepts_every_known_provider_env_var(tmp_path: Path) -> None:
    for var in API_KEY_ENV_VARS:
        assert HARNESS.missing_auth({var: "k"}, home=tmp_path) is None, var


def test_missing_auth_accepts_a_provider_this_harness_does_not_special_case(
    tmp_path: Path,
) -> None:
    # Regression: a fixed 3-var allowlist (ANTHROPIC/OPENAI/GEMINI only) rejected valid pi
    # credentials for every other supported provider and blocked the container from launching.
    assert HARNESS.missing_auth({"GROQ_API_KEY": "k"}, home=tmp_path) is None
    assert HARNESS.missing_auth({"ANTHROPIC_OAUTH_TOKEN": "t"}, home=tmp_path) is None


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
    assert argv[:3] == ["pi", "--append-system-prompt", "# the map"]
    assert argv[-1] == "--continue"


# -- image layer + env ----------------------------------------------------------------


def test_image_layer_installs_pinned_node_and_pi_for_both_architectures() -> None:
    layer = HARNESS.image_layer()
    assert f"v{NODE_VERSION}/node-v{NODE_VERSION}-linux-$node_arch.tar.xz" in layer
    assert 'x86_64) node_arch="x64"' in layer and 'aarch64) node_arch="arm64"' in layer
    assert f"@earendil-works/pi-coding-agent@{PI_VERSION}" in layer  # pinned, not `latest`
    assert PI_VERSION == "0.80.3"  # the version verified against a real local install
    assert "--extract --xz --directory" in layer  # long options (repo convention)
    assert "npm install --global --ignore-scripts" in layer


def test_env_points_pi_at_the_per_task_config_dir(tmp_path: Path) -> None:
    assert HARNESS.env(_ctx(tmp_path)) == {"PI_CODING_AGENT_DIR": str(tmp_path / ".pi")}
