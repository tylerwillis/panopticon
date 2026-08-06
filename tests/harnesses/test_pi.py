"""The pi harness: settings.json, workflow-overview file, the turn-flip extension, REST-curl
operation instructions (no MCP), SKILL.md rendering, auth, argv.

Facts pinned against pi-coding-agent 0.80.3 (a real local install) and the pi-mono TypeScript
source (event/handler types) — see the module docstring for exactly what's verified vs. not.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

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
    settings_path = tmp_path / ".pi" / "agent" / "settings.json"
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
    assert (tmp_path / ".pi" / "agent" / "workflow-overview.md").read_text() == "# the map"


def test_bootstrap_omits_the_overview_file_when_blank(tmp_path: Path) -> None:
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, overview="   "))
    assert not (tmp_path / ".pi" / "agent" / "workflow-overview.md").exists()


def test_bootstrap_removes_a_stale_overview_left_by_an_earlier_bootstrap(tmp_path: Path) -> None:
    # The config volume persists across respawns — a later bootstrap with no overview must not
    # leave an earlier one's file behind for argv() to keep injecting via --append-system-prompt.
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, overview="# the map"))
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, overview=""))
    assert not (tmp_path / ".pi" / "agent" / "workflow-overview.md").exists()
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
let requestSettled = false;
globalThis.fetch = (_url, options) => new Promise((_resolve, reject) => {
  options.signal.addEventListener("abort", () => {
    requestSettled = true;
    reject(new Error("bounded request abort"));
  }, { once: true });
});
extension(pi);
const started = Date.now();
await inputHandler();
if (!requestSettled) throw new Error("input handler returned before request settlement");
if (Date.now() - started >= 3000) throw new Error("input handler exceeded fail-open bound");
"""
    )

    subprocess.run(["node", "--input-type=module", "--eval", probe], check=True)


# 2119: REQ-008.6.1
def test_extension_sends_the_behavioral_turn_payload_for_each_event() -> None:
    source = TURN_EXTENSION.replace("export default function", "const extension = function")
    probe = (
        source
        + """
const handlers = {};
const turns = [];
const pi = { on(event, handler) { handlers[event] = handler; } };
globalThis.fetch = (_url, options) => {
  if (_url !== "http://service/tasks/task-1/turn") throw new Error(`wrong URL: ${_url}`);
  if (options.method !== "PUT") throw new Error(`wrong method: ${options.method}`);
  turns.push(JSON.parse(options.body).turn);
  return Promise.resolve({ ok: true });
};
process.env.PANOPTICON_SERVICE_URL = "http://service";
process.env.PANOPTICON_TASK_ID = "task-1";
extension(pi);
if (JSON.stringify(Object.keys(handlers).sort()) !== JSON.stringify(["agent_end", "input"])) {
  throw new Error(`unexpected injected event inventory: ${JSON.stringify(Object.keys(handlers))}`);
}
await handlers.agent_end();
await handlers.input();
if (JSON.stringify(turns) !== JSON.stringify(["user", "agent"])) {
  throw new Error(`wrong turn payloads: ${JSON.stringify(turns)}`);
}
"""
    )

    subprocess.run(["node", "--input-type=module", "--eval", probe], check=True)


# 2119: REQ-008.6.1
def test_input_handler_completes_a_real_task_service_turn_write() -> None:
    received: list[str] = []

    class _TurnService(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_PUT(self) -> None:
            body = self.rfile.read(int(self.headers["content-length"]))
            received.append(json.loads(body)["turn"])
            self.send_response(200)
            self.send_header("content-length", "0")
            self.end_headers()

    service = ThreadingHTTPServer(("127.0.0.1", 0), _TurnService)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    source = TURN_EXTENSION.replace("export default function", "const extension = function")
    probe = (
        source
        + f"""
let inputHandler;
const pi = {{ on(event, handler) {{ if (event === "input") inputHandler = handler; }} }};
process.env.PANOPTICON_SERVICE_URL = "http://127.0.0.1:{service.server_port}";
process.env.PANOPTICON_TASK_ID = "task-1";
extension(pi);
await inputHandler();
"""
    )
    try:
        subprocess.run(["node", "--input-type=module", "--eval", probe], check=True, timeout=3)
    finally:
        service.shutdown()
        service.server_close()
        thread.join(timeout=1)

    assert received == ["agent"]


# 2119: REQ-016.1.1
def test_turn_handlers_bound_a_delayed_http_error_response() -> None:
    source = TURN_EXTENSION.replace("export default function", "const extension = function")
    probe = (
        source
        + """
const handlers = {};
const pi = { on(event, handler) { handlers[event] = handler; } };
globalThis.fetch = (_url, options) => new Promise((resolve, reject) => {
  const delayed = setTimeout(() => resolve({ ok: false, status: 503 }), 10_000);
  options.signal.addEventListener("abort", () => {
    clearTimeout(delayed);
    reject(new Error("delayed 503 aborted"));
  }, { once: true });
});
extension(pi);
const started = Date.now();
await Promise.all([handlers.agent_end(), handlers.input()]);
if (Date.now() - started >= 3000) throw new Error("delayed HTTP failure exceeded hook bound");
"""
    )

    subprocess.run(
        ["node", "--input-type=module", "--eval", probe],
        check=True,
        timeout=3.5,
        capture_output=True,
        text=True,
    )


# 2119: REQ-016.1.1
def test_turn_handlers_return_within_bound_after_successful_writes() -> None:
    source = TURN_EXTENSION.replace("export default function", "const extension = function")
    probe = (
        source
        + """
const handlers = {};
const pi = { on(event, handler) { handlers[event] = handler; } };
globalThis.fetch = () => Promise.resolve({ ok: true });
extension(pi);
const started = Date.now();
await Promise.all([handlers.agent_end(), handlers.input()]);
if (Date.now() - started >= 3000) throw new Error("successful hooks exceeded callback bound");
"""
    )

    subprocess.run(
        ["node", "--input-type=module", "--eval", probe],
        check=True,
        timeout=3.5,
        capture_output=True,
        text=True,
    )


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
const networkResults = [await handlers.agent_end(), await handlers.input()];
failure = "status";
const statusResults = [await handlers.agent_end(), await handlers.input()];
if ([...networkResults, ...statusResults].some((value) => value !== undefined)) {
  throw new Error("hook surfaced a control-plane failure as its resolved value");
}
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
    assert (tmp_path / ".pi" / "agent" / EXTENSION_FILE).read_text() == TURN_EXTENSION


def test_argv_loads_the_extension_when_rendered(tmp_path: Path) -> None:
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, overview=""))
    argv = HARNESS.argv(_ctx(tmp_path))
    assert argv == ["pi", "--extension", str(tmp_path / ".pi" / "agent" / EXTENSION_FILE)]


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
        "curl --disable --noproxy '*' --fail --silent --show-error --request POST "
        '"http://host.docker.internal:8000/tasks/t7/operations/advance"' in operation
    )


def test_bootstrap_renders_skills_user_scope_not_into_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, cwd=workspace))
    assert not (workspace / ".agents").exists()
    assert (tmp_path / ".agents" / "skills").is_dir()


# -- auth ----------------------------------------------------------------------------


def _native_credential_agent_dir(root: Path) -> Path:
    agent_dir = root / "pi" / "agent"
    agent_dir.mkdir(parents=True)
    return agent_dir


# 2119: REQ-051.1.1
# 2119: REQ-051.1.2
def test_pi_uses_the_native_agent_directory_inside_its_persistent_volume(tmp_path: Path) -> None:
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, environ={"ANTHROPIC_API_KEY": "sk-ant-test"}))

    assert HARNESS.config_dir(tmp_path) == tmp_path / ".pi"
    assert (tmp_path / ".pi" / "agent" / "settings.json").is_file()
    assert not (tmp_path / ".pi" / "settings.json").exists()
    assert HARNESS.env(_ctx(tmp_path)) == {"PI_CODING_AGENT_DIR": str(tmp_path / ".pi" / "agent")}


# 2119: REQ-051.1.3
def test_bootstrap_imports_native_agent_layout_without_mutating_its_source(tmp_path: Path) -> None:
    credentials = tmp_path / "credentials"
    source = _native_credential_agent_dir(credentials)
    auth_text = json.dumps(
        {
            "openai-codex": {
                "type": "oauth",
                "access": "access-token",
                "refresh": "refresh-token",
                "expires": 2_000_000_000_000,
                "accountId": "account-π",
            }
        }
    )
    (source / "auth.json").write_text(auth_text)
    models_text = json.dumps(
        {
            "metadata": {"label": "λ"},
            "providers": {
                "local": {
                    "baseUrl": "http://127.0.0.1:18000/v1",
                    "api": "openai-completions",
                    "apiKey": "local",
                    "models": [{"id": "model"}],
                }
            },
        },
        ensure_ascii=False,
    )
    (source / "models.json").write_text(models_text)
    (source / "trust.json").write_text('{"/workspace": true}')
    custom_bytes = b"\x00\xffcustom\n"
    (source / "custom.json").write_bytes(custom_bytes)
    source_bytes = {path.name: path.read_bytes() for path in source.iterdir()}

    HARNESS.bootstrap(
        _bootstrap_ctx(tmp_path, environ={"PANOPTICON_CREDENTIALS": str(credentials)})
    )

    native = tmp_path / ".pi" / "agent"
    assert (
        json.loads((native / "auth.json").read_text())["openai-codex"]["accountId"] == "account-π"
    )
    assert json.loads((native / "auth.json").read_text()) == json.loads(auth_text)
    expected_models = json.loads(models_text)
    expected_models["providers"]["local"]["baseUrl"] = "http://host.docker.internal:18000/v1"
    assert json.loads((native / "models.json").read_text()) == expected_models
    assert json.loads((native / "trust.json").read_text()) == {"/workspace": True}
    assert (native / "custom.json").read_bytes() == custom_bytes
    assert {path.name: path.read_bytes() for path in source.iterdir()} == source_bytes


# 2119: REQ-051.1.3
@pytest.mark.parametrize(
    ("name", "contents"),
    [
        ("auth.json", b'{"openai-codex":{"type":"oauth"}}'),
        ("models.json", b'{"providers":{}}'),
        ("trust.json", b'{"/workspace":true}'),
        ("custom.json", b"\x00\xffcustom-only"),
    ],
)
def test_bootstrap_imports_each_native_agent_entry_when_it_is_the_only_file(
    tmp_path: Path, name: str, contents: bytes
) -> None:
    credentials = tmp_path / "credentials"
    source = _native_credential_agent_dir(credentials)
    (source / name).write_bytes(contents)

    HARNESS.bootstrap(
        _bootstrap_ctx(tmp_path, environ={"PANOPTICON_CREDENTIALS": str(credentials)})
    )

    assert (tmp_path / ".pi" / "agent" / name).read_bytes() == contents
    assert (source / name).read_bytes() == contents


# 2119: REQ-051.2.1
def test_missing_auth_accepts_native_openai_codex_oauth_shape(tmp_path: Path) -> None:
    credentials = tmp_path / "credentials"
    agent_dir = _native_credential_agent_dir(credentials)
    (agent_dir / "auth.json").write_text(
        json.dumps(
            {
                "openai-codex": {
                    "type": "oauth",
                    "access": "access-token",
                    "refresh": "refresh-token",
                    "expires": 2_000_000_000_000,
                    "accountId": "account-id",
                }
            }
        )
    )

    assert HARNESS.missing_auth({"PANOPTICON_CREDENTIALS": str(credentials)}, home=tmp_path) is None


# 2119: REQ-051.2.1
@pytest.mark.parametrize("expires", [0, -1, 1.5, 9_999_999_999_999])
def test_missing_auth_accepts_openai_codex_oauth_string_and_numeric_boundaries(
    tmp_path: Path, expires: int | float
) -> None:
    native = tmp_path / ".pi" / "agent"
    native.mkdir(parents=True)
    (native / "auth.json").write_text(
        json.dumps(
            {
                "openai-codex": {
                    "type": "oauth",
                    "access": "a",
                    "refresh": "r",
                    "expires": expires,
                    "accountId": "i",
                }
            }
        )
    )

    assert HARNESS.missing_auth({}, home=tmp_path) is None


# 2119: REQ-051.2.1
@pytest.mark.parametrize("provider", ["openai_codex", "openai-codex ", "wrong-provider"])
def test_missing_auth_rejects_valid_oauth_shape_under_the_wrong_provider_key(
    tmp_path: Path, provider: str
) -> None:
    native = tmp_path / ".pi" / "agent"
    native.mkdir(parents=True)
    entry = {
        "type": "oauth",
        "access": "access-token",
        "refresh": "refresh-token",
        "expires": 2_000_000_000_000,
        "accountId": "account-id",
    }
    (native / "auth.json").write_text(json.dumps({provider: entry}))

    assert HARNESS.missing_auth({}, home=tmp_path) is not None


# 2119: REQ-051.1.1
# 2119: REQ-051.1.2
def test_preflight_and_session_detection_use_only_the_native_agent_directory(
    tmp_path: Path,
) -> None:
    native = tmp_path / ".pi" / "agent"
    native.mkdir(parents=True)
    (native / "auth.json").write_text(
        json.dumps({"anthropic": {"type": "api_key", "key": "native-key"}})
    )
    sessions = native / "sessions" / "--workspace--"
    sessions.mkdir(parents=True)
    (sessions / "session.jsonl").write_text("{}")
    legacy = tmp_path / ".pi" / "auth.json"
    legacy.write_text(
        json.dumps(
            {
                "openai-codex": {
                    "type": "oauth",
                    "access": "legacy-access",
                    "refresh": "legacy-refresh",
                    "expires": 2_000_000_000_000,
                    "accountId": "legacy-account",
                }
            }
        )
    )

    assert HARNESS.missing_auth({}, home=tmp_path) is None
    assert "--continue" in HARNESS.argv(_ctx(tmp_path))
    (sessions / "session.jsonl").unlink()
    legacy_sessions = tmp_path / ".pi" / "sessions" / "--workspace--"
    legacy_sessions.mkdir(parents=True)
    (legacy_sessions / "legacy.jsonl").write_text("{}")
    assert "--continue" not in HARNESS.argv(_ctx(tmp_path))
    (native / "auth.json").unlink()
    assert HARNESS.missing_auth({}, home=tmp_path) is not None


# 2119: REQ-051.2.2
@pytest.mark.parametrize(
    "contents",
    [
        "not-json",
        "{}",
        json.dumps({"OPENAI_API_KEY": "codex-only"}),
        json.dumps({"tokens": {"access_token": "codex-only"}}),
        json.dumps({"last_refresh": "2026-08-05T00:00:00Z"}),
        json.dumps({"tokens": {}, "openai-codex": {}}),
    ],
)
def test_missing_auth_rejects_files_pi_cannot_use_with_an_actionable_reason(
    tmp_path: Path, contents: str
) -> None:
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "auth.json").write_text(contents)  # codex's shared-dir location/shape

    detail = HARNESS.missing_auth({"PANOPTICON_CREDENTIALS": str(credentials)}, home=tmp_path)

    assert detail is not None
    assert "~/.pi/agent/auth.json" in detail
    assert "ANTHROPIC_API_KEY" in detail
    assert "models.json" in detail
    assert "codex-only" not in detail


# 2119: REQ-051.2.2
@pytest.mark.parametrize(
    "contents",
    [
        "not-json",
        "{}",
        json.dumps({"OPENAI_API_KEY": "codex-only"}),
        json.dumps({"tokens": {"access_token": "codex-only"}}),
        json.dumps({"last_refresh": "2026-08-05T00:00:00Z"}),
    ],
)
def test_missing_auth_rejects_malformed_or_empty_native_auth_file(
    tmp_path: Path, contents: str
) -> None:
    native = tmp_path / ".pi" / "agent"
    native.mkdir(parents=True)
    (native / "auth.json").write_text(contents)

    detail = HARNESS.missing_auth({}, home=tmp_path)

    assert detail is not None
    assert "~/.pi/agent/auth.json" in detail
    assert "ANTHROPIC_API_KEY" in detail
    assert "models.json" in detail


# 2119: REQ-051.2.2
@pytest.mark.parametrize(
    "marker",
    [
        {"OPENAI_API_KEY": "codex-marker"},
        {"tokens": {"access_token": "codex-marker"}},
        {"last_refresh": "2026-08-05T00:00:00Z"},
    ],
)
def test_codex_marker_does_not_hide_a_usable_pi_provider_entry(
    tmp_path: Path, marker: dict[str, object]
) -> None:
    native = tmp_path / ".pi" / "agent"
    native.mkdir(parents=True)
    (native / "auth.json").write_text(
        json.dumps({**marker, "anthropic": {"type": "api_key", "key": "sk-ant-usable"}})
    )

    assert HARNESS.missing_auth({}, home=tmp_path) is None


# 2119: REQ-051.2.3
@pytest.mark.parametrize(
    ("api_key", "extra_env"),
    [
        ("local", {}),
        ("ordinary-literal-provider-secret", {}),
        ("$LOCAL_MODEL_KEY", {"LOCAL_MODEL_KEY": "resolved"}),
    ],
)
def test_missing_auth_accepts_selected_custom_provider_models_json_key(
    tmp_path: Path, api_key: str, extra_env: dict[str, str]
) -> None:
    credentials = tmp_path / "credentials"
    agent_dir = _native_credential_agent_dir(credentials)
    (agent_dir / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "sparky2-vllm": {
                        "baseUrl": "http://127.0.0.1:18000/v1",
                        "api": "openai-completions",
                        "apiKey": api_key,
                        "models": [{"id": "laguna-s-2.1-nvfp4"}],
                    }
                }
            }
        )
    )
    env = {
        "PANOPTICON_CREDENTIALS": str(credentials),
        "PANOPTICON_STARTING_MODEL": "sparky2-vllm/laguna-s-2.1-nvfp4",
        **extra_env,
    }

    assert HARNESS.missing_auth(env, home=tmp_path) is None


# 2119: REQ-051.2.3
@pytest.mark.parametrize(
    ("selected", "api_key", "extra_env"),
    [
        ("sparky/model", "", {}),
        ("sparky/model", "$MISSING_LOCAL_KEY", {}),
        ("absent/model", "local", {}),
    ],
)
def test_missing_auth_rejects_unusable_or_unselected_custom_provider_keys(
    tmp_path: Path, selected: str, api_key: str, extra_env: dict[str, str]
) -> None:
    credentials = tmp_path / "credentials"
    agent_dir = _native_credential_agent_dir(credentials)
    (agent_dir / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "sparky": {
                        "baseUrl": "http://127.0.0.1:18000/v1",
                        "api": "openai-completions",
                        "apiKey": api_key,
                        "models": [{"id": "model"}],
                    },
                    "other": {
                        "baseUrl": "http://127.0.0.1:18080/v1",
                        "api": "openai-completions",
                        "apiKey": "valid-for-other-only",
                        "models": [{"id": "model"}],
                    },
                }
            }
        )
    )
    env = {
        "PANOPTICON_CREDENTIALS": str(credentials),
        "PANOPTICON_STARTING_MODEL": selected,
        **extra_env,
    }

    assert HARNESS.missing_auth(env, home=tmp_path) is not None


# 2119: REQ-051.2.3
@pytest.mark.parametrize(
    ("provider_patch", "extra_env"),
    [({}, {}), ({"apiKey": "$EMPTY_LOCAL_MODEL_KEY"}, {"EMPTY_LOCAL_MODEL_KEY": ""})],
)
def test_missing_auth_rejects_missing_or_empty_resolved_custom_provider_keys(
    tmp_path: Path, provider_patch: dict[str, str], extra_env: dict[str, str]
) -> None:
    credentials = tmp_path / "credentials"
    agent_dir = _native_credential_agent_dir(credentials)
    provider = {
        "baseUrl": "http://127.0.0.1:18000/v1",
        "api": "openai-completions",
        "models": [{"id": "model"}],
        **provider_patch,
    }
    (agent_dir / "models.json").write_text(json.dumps({"providers": {"sparky": provider}}))
    env = {
        "PANOPTICON_CREDENTIALS": str(credentials),
        "PANOPTICON_STARTING_MODEL": "sparky/model",
        **extra_env,
    }

    assert HARNESS.missing_auth(env, home=tmp_path) is not None


# 2119: REQ-051.2.1
@pytest.mark.parametrize(
    "patch",
    [
        {"__delete__": "access"},
        {"__delete__": "refresh"},
        {"__delete__": "accountId"},
        {"__delete__": "expires"},
        {"__delete__": "type"},
        {"access": ""},
        {"refresh": ""},
        {"accountId": ""},
        {"expires": "2000000000000"},
        {"expires": None},
        {"expires": True},
        {"access": 1},
        {"refresh": ["not", "a", "string"]},
        {"accountId": {"not": "a string"}},
        {"type": "api_key"},
        {"type": "OAuth"},
        {"type": "oauth "},
        {"type": ""},
    ],
)
def test_missing_auth_rejects_near_miss_openai_codex_oauth_shapes(
    tmp_path: Path, patch: dict[str, object]
) -> None:
    entry: dict[str, object] = {
        "type": "oauth",
        "access": "access-token",
        "refresh": "refresh-token",
        "expires": 2_000_000_000_000,
        "accountId": "account-id",
    }
    if missing := patch.get("__delete__"):
        entry.pop(str(missing))
    else:
        entry.update(patch)
    native = tmp_path / ".pi" / "agent"
    native.mkdir(parents=True)
    (native / "auth.json").write_text(json.dumps({"openai-codex": entry}))

    assert HARNESS.missing_auth({}, home=tmp_path) is not None


# 2119: REQ-051.2.4
def test_missing_auth_accepts_anthropic_api_key_env_and_native_auth_entry(tmp_path: Path) -> None:
    assert HARNESS.missing_auth({"ANTHROPIC_API_KEY": "sk-ant-api"}, home=tmp_path) is None
    assert HARNESS.missing_auth({"ANTHROPIC_API_KEY": " "}, home=tmp_path) is None

    credentials = tmp_path / "credentials"
    agent_dir = _native_credential_agent_dir(credentials)
    (agent_dir / "auth.json").write_text(
        json.dumps({"anthropic": {"type": "api_key", "key": "sk-ant-api"}})
    )
    assert HARNESS.missing_auth({"PANOPTICON_CREDENTIALS": str(credentials)}, home=tmp_path) is None

    (agent_dir / "auth.json").write_text(json.dumps({"anthropic": {"type": "api_key", "key": " "}}))
    assert HARNESS.missing_auth({"PANOPTICON_CREDENTIALS": str(credentials)}, home=tmp_path) is None

    (agent_dir / "auth.json").write_text(
        json.dumps({"anthropic": {"type": "api_key", "key": "$STORED_ANTHROPIC_KEY"}})
    )
    assert (
        HARNESS.missing_auth(
            {
                "PANOPTICON_CREDENTIALS": str(credentials),
                "STORED_ANTHROPIC_KEY": "sk-ant-resolved",
            },
            home=tmp_path,
        )
        is None
    )


# 2119: REQ-051.2.4
@pytest.mark.parametrize(
    "entry",
    [
        None,
        [],
        {},
        True,
        False,
        {"type": "api_key", "key": ""},
        {"type": "api_key", "key": "$MISSING_ANTHROPIC_KEY"},
        {"type": "api_key", "key": "$EMPTY_ANTHROPIC_KEY"},
        {"type": "oauth", "key": "sk-ant-wrong-type"},
        {"type": "API_KEY", "key": "sk-ant-wrong-case"},
        {"type": "api_key ", "key": "sk-ant-trailing-space"},
        {"type": "api_key"},
        {"key": "sk-ant-missing-type"},
        {"type": "api_key", "key": None},
        {"type": "api_key", "key": True},
        {"type": "api_key", "key": False},
        {"type": "api_key", "key": 123},
        {"type": "api_key", "key": 1.5},
        {"type": "api_key", "key": []},
        {"type": "api_key", "key": {}},
        "not-an-object",
    ],
)
def test_missing_auth_rejects_empty_or_malformed_anthropic_api_key_entries(
    tmp_path: Path, entry: object
) -> None:
    native = tmp_path / ".pi" / "agent"
    native.mkdir(parents=True)
    (native / "auth.json").write_text(json.dumps({"anthropic": entry}))

    assert (
        HARNESS.missing_auth({"ANTHROPIC_API_KEY": "", "EMPTY_ANTHROPIC_KEY": ""}, home=tmp_path)
        is not None
    )


# 2119: REQ-051.2.4
def test_malformed_anthropic_entry_does_not_hide_another_valid_credential(tmp_path: Path) -> None:
    native = tmp_path / ".pi" / "agent"
    native.mkdir(parents=True)
    (native / "auth.json").write_text(json.dumps({"anthropic": {"type": "api_key", "key": ""}}))

    assert HARNESS.missing_auth({"OPENAI_API_KEY": "sk-openai-valid"}, home=tmp_path) is None


# 2119: REQ-051.2.4
def test_anthropic_api_key_shape_under_another_provider_is_not_anthropic_auth(
    tmp_path: Path,
) -> None:
    native = tmp_path / ".pi" / "agent"
    native.mkdir(parents=True)
    (native / "auth.json").write_text(
        json.dumps({"other": {"type": "api_key", "key": "sk-ant-wrong-provider"}})
    )

    assert HARNESS.missing_auth({}, home=tmp_path) is not None


# 2119: REQ-051.2.5
# 2119: REQ-051.2.6
def test_anthropic_oauth_is_warned_not_suggested_and_explicit_input_is_not_blocked(
    tmp_path: Path,
) -> None:
    documentation = [Path("README.md"), *Path("docs").rglob("*.md")]
    pi_named_documents = [document for document in documentation if "pi" in document.stem.lower()]
    pi_sections: list[str] = []
    for document in documentation:
        sections = document.read_text().split("\n## ")
        pi_sections.extend(
            section for section in sections if "pi" in section.splitlines()[0].lower()
        )
    pi_docs = "\n".join(pi_sections)
    pi_bearing_sentences = [
        sentence
        for document in documentation
        for sentence in re.split(r"(?<=[.!?])\s+|\n", document.read_text())
        if re.search(r"(?i)\bpi\b", sentence)
    ]
    pi_bearing_paragraphs = [
        paragraph
        for document in documentation
        for paragraph in re.split(r"\n\s*\n", document.read_text())
        if re.search(r"(?i)\bpi\b", paragraph) and "CLAUDE_CODE_OAUTH_TOKEN" in paragraph
    ]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in pi_docs
    assert all(
        "CLAUDE_CODE_OAUTH_TOKEN" not in document.read_text() for document in pi_named_documents
    )
    assert all("CLAUDE_CODE_OAUTH_TOKEN" not in sentence for sentence in pi_bearing_sentences)
    assert all(
        "Claude authenticates from" in paragraph or "- **Claude:**" in paragraph
        for paragraph in pi_bearing_paragraphs
    )
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in Path("src/panopticon/harnesses/pi.py").read_text()
    assert "ANTHROPIC_OAUTH_TOKEN=" not in pi_docs
    warning = (
        "Anthropic OAuth in pi is not recommended or supported by Panopticon and may risk your "
        "Anthropic account."
    )
    assert warning in pi_docs
    assert (
        HARNESS.missing_auth({"ANTHROPIC_OAUTH_TOKEN": "operator-chose-this"}, home=tmp_path)
        is None
    )
    detail = HARNESS.missing_auth({"CLAUDE_CODE_OAUTH_TOKEN": "setup-token"}, home=tmp_path)
    assert detail is not None and "ANTHROPIC_OAUTH_TOKEN" not in detail
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, environ={"CLAUDE_CODE_OAUTH_TOKEN": "setup-token"}))
    assert list((tmp_path / ".pi").rglob("auth.json")) == []
    assert all(
        b"setup-token" not in path.read_bytes()
        for path in (tmp_path / ".pi").rglob("*")
        if path.is_file()
    )


# 2119: REQ-051.3.1
# 2119: REQ-051.3.2
# 2119: REQ-051.3.3
def test_bootstrap_rewrites_only_http_loopback_model_hosts_without_mutating_source(
    tmp_path: Path,
) -> None:
    credentials = tmp_path / "credentials"
    agent_dir = _native_credential_agent_dir(credentials)
    models = {
        "metadata": {"kept": True},
        "providers": {
            "vllm": {
                "baseUrl": "http://127.0.0.1:18000/v1?mode=x#frag",
                "api": "openai-completions",
                "apiKey": "local",
                "headers": {"x-custom": "kept"},
                "mirror": "http://localhost:9999/must-not-change",
                "models": [
                    {
                        "id": "laguna-s-2.1-nvfp4",
                        "reasoning": True,
                        "documentationUrl": "http://127.0.0.1:7777/must-not-change",
                        "baseUrl": "http://localhost:7777/must-not-change",
                    }
                ],
            },
            "ollama": {
                "baseUrl": "https://localhost:11434/v1",
                "api": "openai-completions",
                "apiKey": "ollama",
                "models": [{"id": "qwen3.6"}],
            },
            "ollama-http": {
                "baseUrl": "http://localhost:11434/v1",
                "api": "openai-completions",
                "apiKey": "ollama",
                "models": [{"id": "qwen3.6"}],
            },
            "uppercase-localhost": {
                "baseUrl": "http://LOCALHOST:19000/v1",
                "apiKey": "local",
            },
            "portless-localhost": {
                "baseUrl": "http://localhost/v1",
                "apiKey": "local",
            },
            "portless-ipv4": {
                "baseUrl": "https://127.0.0.1/v1",
                "apiKey": "local",
            },
            "mixed-case-localhost": {
                "baseUrl": "https://LocalHost:19001/v1",
                "apiKey": "local",
            },
            "https-ipv4": {
                "baseUrl": "https://127.0.0.1:19002/v1",
                "apiKey": "local",
            },
            "ipv6": {
                "baseUrl": "http://[::1]:18080/v1",
                "api": "openai-completions",
                "apiKey": "local",
                "models": [{"id": "step"}],
            },
            "https-ipv6": {
                "baseUrl": "https://[::1]:19003/v1",
                "apiKey": "local",
            },
            "lan": {"baseUrl": "http://192.168.1.171:8080/v1"},
            "lan-10": {"baseUrl": "http://10.0.0.1:8080/v1"},
            "lan-172": {"baseUrl": "http://172.16.0.1:8080/v1"},
            "link-local": {"baseUrl": "http://169.254.1.2:8080/v1"},
            "unspecified-ipv4": {"baseUrl": "http://0.0.0.0:8080/v1"},
            "unspecified-ipv6": {"baseUrl": "http://[::]:8080/v1"},
            "public": {"baseUrl": "https://models.example/v1"},
            "socket": {"baseUrl": "unix:///tmp/model.sock"},
            "broken": {"baseUrl": "not a url"},
            "broken-ipv6": {"baseUrl": "http://[::1"},
            "missing-host": {"baseUrl": "http://:8080/v1"},
            "localhost-suffix": {"baseUrl": "http://localhost.example/v1"},
            "ipv4-suffix": {"baseUrl": "http://127.0.0.10/v1"},
            "ipv6-public": {"baseUrl": "http://[2001:db8::1]:9000/v1"},
            "ftp-loopback": {"baseUrl": "ftp://localhost:21/v1"},
        },
    }
    source_text = json.dumps(models, indent=2)
    source_path = agent_dir / "models.json"
    source_path.write_text(source_text)

    HARNESS.bootstrap(
        _bootstrap_ctx(tmp_path, environ={"PANOPTICON_CREDENTIALS": str(credentials)})
    )

    rendered = json.loads((tmp_path / ".pi" / "agent" / "models.json").read_text())
    expected = json.loads(source_text)
    expected["providers"]["vllm"]["baseUrl"] = "http://host.docker.internal:18000/v1?mode=x#frag"
    expected["providers"]["ollama"]["baseUrl"] = "https://host.docker.internal:11434/v1"
    expected["providers"]["ollama-http"]["baseUrl"] = "http://host.docker.internal:11434/v1"
    expected["providers"]["uppercase-localhost"]["baseUrl"] = "http://host.docker.internal:19000/v1"
    expected["providers"]["portless-localhost"]["baseUrl"] = "http://host.docker.internal/v1"
    expected["providers"]["portless-ipv4"]["baseUrl"] = "https://host.docker.internal/v1"
    expected["providers"]["mixed-case-localhost"]["baseUrl"] = (
        "https://host.docker.internal:19001/v1"
    )
    expected["providers"]["https-ipv4"]["baseUrl"] = "https://host.docker.internal:19002/v1"
    expected["providers"]["ipv6"]["baseUrl"] = "http://host.docker.internal:18080/v1"
    expected["providers"]["https-ipv6"]["baseUrl"] = "https://host.docker.internal:19003/v1"
    assert rendered == expected
    providers = rendered["providers"]
    assert providers["vllm"]["baseUrl"] == "http://host.docker.internal:18000/v1?mode=x#frag"
    assert providers["ollama"]["baseUrl"] == "https://host.docker.internal:11434/v1"
    assert providers["ollama-http"]["baseUrl"] == "http://host.docker.internal:11434/v1"
    assert providers["uppercase-localhost"]["baseUrl"] == "http://host.docker.internal:19000/v1"
    assert providers["portless-localhost"]["baseUrl"] == "http://host.docker.internal/v1"
    assert providers["portless-ipv4"]["baseUrl"] == "https://host.docker.internal/v1"
    assert providers["mixed-case-localhost"]["baseUrl"] == "https://host.docker.internal:19001/v1"
    assert providers["https-ipv4"]["baseUrl"] == "https://host.docker.internal:19002/v1"
    assert providers["ipv6"]["baseUrl"] == "http://host.docker.internal:18080/v1"
    assert providers["https-ipv6"]["baseUrl"] == "https://host.docker.internal:19003/v1"
    assert providers["lan"]["baseUrl"] == "http://192.168.1.171:8080/v1"
    assert providers["lan-10"]["baseUrl"] == "http://10.0.0.1:8080/v1"
    assert providers["lan-172"]["baseUrl"] == "http://172.16.0.1:8080/v1"
    assert providers["link-local"]["baseUrl"] == "http://169.254.1.2:8080/v1"
    assert providers["unspecified-ipv4"]["baseUrl"] == "http://0.0.0.0:8080/v1"
    assert providers["unspecified-ipv6"]["baseUrl"] == "http://[::]:8080/v1"
    assert providers["public"]["baseUrl"] == "https://models.example/v1"
    assert providers["socket"]["baseUrl"] == "unix:///tmp/model.sock"
    assert providers["broken"]["baseUrl"] == "not a url"
    assert providers["broken-ipv6"]["baseUrl"] == "http://[::1"
    assert providers["missing-host"]["baseUrl"] == "http://:8080/v1"
    assert providers["localhost-suffix"]["baseUrl"] == "http://localhost.example/v1"
    assert providers["ipv4-suffix"]["baseUrl"] == "http://127.0.0.10/v1"
    assert providers["ipv6-public"]["baseUrl"] == "http://[2001:db8::1]:9000/v1"
    assert providers["ftp-loopback"]["baseUrl"] == "ftp://localhost:21/v1"
    assert providers["vllm"]["api"] == "openai-completions"
    assert providers["vllm"]["apiKey"] == "local"
    assert providers["vllm"]["headers"] == {"x-custom": "kept"}
    assert providers["vllm"]["mirror"] == "http://localhost:9999/must-not-change"
    assert providers["vllm"]["models"] == [
        {
            "id": "laguna-s-2.1-nvfp4",
            "reasoning": True,
            "documentationUrl": "http://127.0.0.1:7777/must-not-change",
            "baseUrl": "http://localhost:7777/must-not-change",
        }
    ]
    assert rendered["metadata"] == {"kept": True}
    assert source_path.read_text() == source_text


# 2119: REQ-051.3.3
def test_first_launch_selects_native_local_provider_and_model(tmp_path: Path) -> None:
    credentials = tmp_path / "credentials"
    agent_dir = _native_credential_agent_dir(credentials)
    native_provider = {
        "baseUrl": "http://127.0.0.1:18000/v1",
        "api": "openai-completions",
        "apiKey": "local",
        "headers": {"x-native": "kept"},
        "compat": {"supportsDeveloperRole": False},
        "models": [
            {
                "id": "laguna-s-2.1-nvfp4",
                "reasoning": True,
                "contextWindow": 131072,
            }
        ],
    }
    (agent_dir / "models.json").write_text(
        json.dumps({"providers": {"sparky2-vllm": native_provider}})
    )
    HARNESS.bootstrap(
        _bootstrap_ctx(tmp_path, environ={"PANOPTICON_CREDENTIALS": str(credentials)})
    )

    argv = HARNESS.argv(
        _ctx(
            tmp_path,
            starting_model="sparky2-vllm/laguna-s-2.1-nvfp4",
            initial_prompt="say ready",
        )
    )

    assert argv[-3:] == [
        "--model",
        "sparky2-vllm/laguna-s-2.1-nvfp4",
        "say ready",
    ]
    assert "--continue" not in argv
    rendered = json.loads((tmp_path / ".pi" / "agent" / "models.json").read_text())
    expected_provider = json.loads(json.dumps(native_provider))
    expected_provider["baseUrl"] = "http://host.docker.internal:18000/v1"
    assert rendered["providers"]["sparky2-vllm"] == expected_provider
    assert set(rendered["providers"]) == {"sparky2-vllm"}


def test_bootstrap_never_renders_an_api_key_auth_file(tmp_path: Path) -> None:
    # Unlike codex: pi resolves an env-var API key itself at runtime, so bootstrap writes nothing.
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, environ={"ANTHROPIC_API_KEY": "sk-ant-x"}))
    assert not (tmp_path / ".pi" / "agent" / "auth.json").exists()


def test_bootstrap_symlinks_auth_from_the_credential_mount(tmp_path: Path) -> None:
    credentials = tmp_path / "credentials"
    source = _native_credential_agent_dir(credentials)
    (source / "auth.json").write_text('{"anthropic": {"type": "api_key", "key": "sk-ant-test"}}')
    HARNESS.bootstrap(
        _bootstrap_ctx(tmp_path, environ={"PANOPTICON_CREDENTIALS": str(credentials)})
    )
    auth = tmp_path / ".pi" / "agent" / "auth.json"
    assert auth.is_symlink() and auth.resolve() == (source / "auth.json").resolve()


def test_bootstrap_never_clobbers_an_existing_auth_file(tmp_path: Path) -> None:
    config_dir = tmp_path / ".pi" / "agent"
    config_dir.mkdir(parents=True)
    (config_dir / "auth.json").write_text('{"anthropic": {"type": "oauth", "tokens": {}}}')
    credentials = tmp_path / "credentials"
    source = _native_credential_agent_dir(credentials)
    (source / "auth.json").write_text('{"anthropic": {"type": "api_key", "key": "sk-ant-mounted"}}')
    HARNESS.bootstrap(
        _bootstrap_ctx(tmp_path, environ={"PANOPTICON_CREDENTIALS": str(credentials)})
    )
    assert not (config_dir / "auth.json").is_symlink()


def test_bootstrap_imports_personal_config_from_native_credential_subdirectory(
    tmp_path: Path,
) -> None:
    credentials = tmp_path / "credentials"
    personal_config = credentials / "pi" / "agent"
    personal_config.mkdir(parents=True)
    (personal_config / "models.json").write_text('{"providers": {"local": {}}}')
    (personal_config / "prompts").mkdir()

    HARNESS.bootstrap(
        _bootstrap_ctx(tmp_path, environ={"PANOPTICON_CREDENTIALS": str(credentials)})
    )

    models = tmp_path / ".pi" / "agent" / "models.json"
    prompts = tmp_path / ".pi" / "agent" / "prompts"
    assert not models.is_symlink()
    assert models.read_text() == '{"providers": {"local": {}}}'
    assert prompts.is_symlink() and prompts.resolve() == (personal_config / "prompts").resolve()


def test_bootstrap_never_clobbers_existing_personal_config(tmp_path: Path) -> None:
    config_dir = tmp_path / ".pi" / "agent"
    config_dir.mkdir(parents=True)
    (config_dir / "models.json").write_text('{"providers": {"persisted": {}}}')
    credentials = tmp_path / "credentials"
    personal_config = credentials / "pi" / "agent"
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
    source = _native_credential_agent_dir(credentials)
    (source / "auth.json").write_text('{"anthropic": {"type": "api_key", "key": "sk-ant-test"}}')
    env = {"PANOPTICON_CREDENTIALS": str(credentials)}
    assert HARNESS.missing_auth(env, home=tmp_path) is None


def test_missing_auth_accepts_an_auth_file_on_the_config_volume(tmp_path: Path) -> None:
    (tmp_path / ".pi" / "agent").mkdir(parents=True)
    (tmp_path / ".pi" / "agent" / "auth.json").write_text(
        '{"anthropic": {"type": "api_key", "key": "sk-ant-test"}}'
    )
    assert HARNESS.missing_auth({}, home=tmp_path) is None


def test_missing_auth_names_the_fix_when_nothing_is_configured(tmp_path: Path) -> None:
    detail = HARNESS.missing_auth({}, home=tmp_path)
    assert detail is not None
    assert "ANTHROPIC_API_KEY" in detail and "~/.pi/agent/auth.json" in detail


# -- argv ----------------------------------------------------------------------------


def _seed_session(home: Path) -> None:
    sessions = home / ".pi" / "agent" / "sessions" / "--workspace--"
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
    assert PI_VERSION == "0.80.3"  # the version verified against a real local install
    assert layer == (
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
    assert ".tar.xz" not in layer and "--xz" not in layer and "xz-utils" not in layer


def test_env_points_pi_at_the_per_task_config_dir(tmp_path: Path) -> None:
    assert HARNESS.env(_ctx(tmp_path)) == {"PI_CODING_AGENT_DIR": str(tmp_path / ".pi" / "agent")}
