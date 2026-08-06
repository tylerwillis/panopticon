"""The in-container agent launcher: fetch the workflow surface, dispatch to the task's harness
(the deterministic bootstrap), then launch. No LLM — the real CLI launch is a fake here."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from panopticon.container import agent
from panopticon.harnesses import Harness, LaunchContext
from panopticon.harnesses import claude as claude_harness
from panopticon.harnesses.claude import MCP_CONFIG_FILE, WORKFLOW_OVERVIEW_FILE
from panopticon.harnesses.pi import PiHarness

# Plausible-length stand-ins for real credentials — the harnesses' shape checks reject anything
# shorter (see tests/harnesses/test_claude.py, test_codex.py for the length-bound tests).
VALID_OAUTH_TOKEN = "sk-ant-oat01-" + "x" * 40
VALID_ANTHROPIC_API_KEY = "sk-ant-" + "x" * 40
VALID_CODEX_API_KEY = "sk-" + "x" * 30


class _FakeClient:
    def __init__(
        self,
        skills: list[dict[str, str]] | None = None,
        operations: dict[str, str] | None = None,
        overview: str = "# the workflow",
    ) -> None:
        self._skills = skills or []
        self._operations = operations or {}
        self._overview = overview
        self.lifecycle_calls: list[dict[str, str | None]] = []

    def list_skills(self, task_id: str) -> list[dict[str, str]]:
        return self._skills

    def list_operations(self, task_id: str) -> dict[str, str]:
        return self._operations

    def workflow_overview(self, task_id: str) -> str:
        return self._overview

    def report_lifecycle(
        self, task_id: str, runner_id: str, phase: str, detail: str | None = None
    ) -> dict[str, str | None]:
        self.lifecycle_calls.append(
            {"task_id": task_id, "runner_id": runner_id, "phase": phase, "detail": detail}
        )
        return {}


def _base_env(monkeypatch: pytest.MonkeyPatch, probe_status: int | None = 200) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    # The claude preflight's one network seam — canned in every launcher test (no network in CI).
    monkeypatch.setattr(claude_harness, "_probe_status", lambda headers: probe_status)
    for var in (
        "PANOPTICON_HARNESS",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "CODEX_API_KEY",
        "OPENAI_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "PANOPTICON_CREDENTIALS",
    ):
        monkeypatch.delenv(var, raising=False)


def test_main_bootstraps_the_default_claude_harness_then_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", VALID_OAUTH_TOKEN)
    events: list[str] = []
    agent.main(
        client_factory=lambda url: _FakeClient(  # type: ignore[arg-type,return-value]
            [{"name": "s", "description": "d", "instructions": "i"}], {"advance": "COMPLETE"}
        ),
        home=tmp_path,
        launch=lambda harness, ctx: events.append(f"launch:{harness.name}"),
        on_exit=lambda: events.append("on_exit"),
    )
    commands = tmp_path / ".claude" / "commands"
    assert (commands / "s.md").exists()  # skills rendered...
    assert (commands / "advance.md").exists()  # ...operations rendered...
    assert (tmp_path / ".claude" / "settings.json").exists()  # ...turn-flip hooks written...
    assert (tmp_path / ".claude" / MCP_CONFIG_FILE).exists()  # ...MCP server wired...
    assert (tmp_path / ".claude" / WORKFLOW_OVERVIEW_FILE).exists()  # ...workflow map written...
    # ...launched with the claude harness, then the container is stopped on agent exit
    assert events == ["launch:claude", "on_exit"]


def test_main_dispatches_to_the_recorded_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("PANOPTICON_HARNESS", "codex")
    monkeypatch.setenv("CODEX_API_KEY", VALID_CODEX_API_KEY)
    launched: list[str] = []
    agent.main(
        client_factory=lambda url: _FakeClient(),  # type: ignore[arg-type,return-value]
        home=tmp_path,
        launch=lambda harness, ctx: launched.append(harness.name),
        on_exit=lambda: None,
    )
    assert launched == ["codex"]
    assert (tmp_path / ".codex" / "config.toml").exists()  # the codex surface, not claude's
    assert not (tmp_path / ".claude").exists()


@pytest.mark.parametrize("harness", ["claude", "codex", "pi"])
def test_main_propagates_the_runtime_service_token_through_each_harness_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    _base_env(monkeypatch)
    credential = tmp_path / "service-auth.json"
    credential.write_text(
        json.dumps({"read": ["container-reader-token"], "write": ["container-writer-token"]})
    )
    credential.chmod(0o600)
    monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_FILE", str(credential))
    monkeypatch.setenv("PANOPTICON_HARNESS", harness)
    if harness == "claude":
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", VALID_OAUTH_TOKEN)
    elif harness == "codex":
        monkeypatch.setenv("CODEX_API_KEY", VALID_CODEX_API_KEY)
    else:
        monkeypatch.setenv("OPENAI_API_KEY", VALID_CODEX_API_KEY)

    agent.main(
        client_factory=lambda url: _FakeClient(operations={"advance": "COMPLETE"}),  # type: ignore[arg-type,return-value]
        home=tmp_path,
        launch=lambda _harness, _ctx: None,
        on_exit=lambda: None,
    )

    assert agent.os.environ["PANOPTICON_SERVICE_AUTH_TOKEN"] == "container-writer-token"
    if harness == "claude":
        assert (
            "${PANOPTICON_SERVICE_AUTH_TOKEN}"
            in (tmp_path / ".claude" / MCP_CONFIG_FILE).read_text()
        )
    elif harness == "codex":
        assert (
            'bearer_token_env_var = "PANOPTICON_SERVICE_AUTH_TOKEN"'
            in (tmp_path / ".codex" / "config.toml").read_text()
        )
    else:
        assert (
            "PANOPTICON_SERVICE_AUTH_TOKEN"
            in next((tmp_path / ".agents" / "skills").glob("advance*/SKILL.md")).read_text()
        )


def test_main_fail_fast_message_names_the_active_harnesss_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A codex task missing credentials must point at codex's variables, not claude's.
    _base_env(monkeypatch)
    monkeypatch.setenv("PANOPTICON_HARNESS", "codex")
    monkeypatch.setenv("PANOPTICON_RUNNER_ID", "runner-1")
    fake = _FakeClient()
    agent.main(
        client_factory=lambda url: fake,  # type: ignore[arg-type,return-value]
        home=tmp_path,
        launch=lambda harness, ctx: pytest.fail("must not launch"),
        on_exit=lambda: None,
    )
    detail = fake.lifecycle_calls[0]["detail"] or ""
    assert "CODEX_API_KEY" in detail and "CLAUDE_CODE_OAUTH_TOKEN" not in detail


# 2119: REQ-050.2.2
# 2119: REQ-050.4.3
# 2119: REQ-050.4.4
@pytest.mark.parametrize(
    "contents",
    [
        "not-json",
        '{"x":}',
        "",
        "   \n",
        "{}",
        "[]",
        '"string"',
        "42",
        "null",
        json.dumps(
            {
                "OPENAI_API_KEY": None,
                "tokens": {"access_token": "must-not-leak"},
                "last_refresh": "2026-08-05T00:00:00Z",
            }
        ),
        json.dumps({"OPENAI_API_KEY": "must-not-leak"}),
        json.dumps({"last_refresh": "2026-08-05T00:00:00Z"}),
        json.dumps({"access_token": "must-not-leak", "refresh_token": "refresh"}),
        json.dumps({"tokens": {"access_token": "must-not-leak", "refresh_token": "refresh"}}),
        json.dumps({"tokens": {"id_token": "must-not-leak"}, "auth_mode": "chatgpt"}),
    ],
)
def test_pi_preflight_failure_is_identical_in_lifecycle_and_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    contents: str,
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("PANOPTICON_HARNESS", "pi")
    monkeypatch.setenv("PANOPTICON_RUNNER_ID", "runner-1")
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "auth.json").write_text(contents)
    monkeypatch.setenv("PANOPTICON_CREDENTIALS", str(credentials))
    fake = _FakeClient()

    agent.main(
        client_factory=lambda url: fake,  # type: ignore[arg-type,return-value]
        home=tmp_path,
        launch=lambda harness, ctx: pytest.fail("must not launch"),
        on_exit=lambda: None,
    )

    detail = fake.lifecycle_calls[0]["detail"] or ""
    stderr = capsys.readouterr().err
    expected = (
        "No usable pi credentials: provide a valid ~/.pi/agent/auth.json, set "
        "ANTHROPIC_API_KEY (or another pi provider API key), or configure the selected "
        "provider's apiKey in models.json."
    )
    assert fake.lifecycle_calls[0]["phase"] == "failed"
    assert len(fake.lifecycle_calls) == 1
    assert detail == expected
    assert stderr == f"{expected}\n"
    assert not (tmp_path / ".pi" / "agent" / "settings.json").exists()
    assert "must-not-leak" not in detail and "must-not-leak" not in stderr


# 2119: REQ-050.4.1
@pytest.mark.parametrize("status", [0, 1, 2, 7, 255, -9])
def test_pi_cli_exit_latches_and_prints_an_actionable_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: int,
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("PANOPTICON_HARNESS", "pi")
    monkeypatch.setenv("PANOPTICON_RUNNER_ID", "runner-1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", VALID_ANTHROPIC_API_KEY)
    order: list[str] = []
    seen_argv: list[str] = []
    monkeypatch.setattr(
        agent.os,
        "kill",
        lambda pid, sig: order.append(f"stopped:{pid}:{sig}"),
    )
    monkeypatch.setattr(
        agent.subprocess,
        "run",
        lambda argv, **_kwargs: seen_argv.extend(argv) or subprocess.CompletedProcess(argv, status),
    )

    class _OrderedClient(_FakeClient):
        def report_lifecycle(
            self, task_id: str, runner_id: str, phase: str, detail: str | None = None
        ) -> dict[str, str | None]:
            order.append(f"report:{phase}")
            return super().report_lifecycle(task_id, runner_id, phase, detail)

    fake = _OrderedClient()

    agent.main(
        client_factory=lambda url: fake,  # type: ignore[arg-type,return-value]
        home=tmp_path,
        launch=agent._run_agent,
    )

    failure = fake.lifecycle_calls[-1]
    detail = failure["detail"] or ""
    stderr = capsys.readouterr().err
    assert failure["phase"] == "failed"
    assert detail == f"pi exited unexpectedly with status {status}"
    assert stderr.strip() == detail
    assert order == ["report:failed", f"stopped:1:{agent.signal.SIGTERM}"]
    assert seen_argv[0] == "pi"


# 2119: REQ-050.4.1
def test_default_exit_handler_stops_the_container_with_sigterm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(agent.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    agent._stop_container()

    assert signals == [(1, agent.signal.SIGTERM)]


# 2119: REQ-050.1.2
# 2119: REQ-050.2.6
# 2119: REQ-050.3.3
@pytest.mark.parametrize("status", [0, 7, -9])
def test_explicit_pi_anthropic_oauth_reaches_the_child_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_OAUTH_TOKEN", "operator-explicit-token")
    recorded: dict[str, str] = {}
    recorded_argv: list[str] = []

    def fake_run(
        argv: list[str], env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        assert env is not None
        recorded.update(env)
        recorded_argv.extend(argv)
        return subprocess.CompletedProcess(argv, status)

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    native = tmp_path / ".pi" / "agent"
    native.mkdir(parents=True)
    (native / "models.json").write_text('{"providers":{"sparky2-vllm":{}}}')
    returned = agent._run_agent(
        PiHarness(),
        LaunchContext(
            home=tmp_path,
            cwd=Path("/workspace"),
            starting_model="sparky2-vllm/laguna-s-2.1-nvfp4",
        ),
    )

    assert recorded["ANTHROPIC_OAUTH_TOKEN"] == "operator-explicit-token"
    assert recorded["PI_CODING_AGENT_DIR"] == str(tmp_path / ".pi" / "agent")
    assert Path(recorded["PI_CODING_AGENT_DIR"]) / "models.json" == native / "models.json"
    assert recorded_argv[-2:] == ["--model", "sparky2-vllm/laguna-s-2.1-nvfp4"]
    assert returned == status


# 2119: REQ-050.2.6
def test_main_accepts_and_preserves_explicit_pi_anthropic_oauth_through_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("PANOPTICON_HARNESS", "pi")
    monkeypatch.setenv("ANTHROPIC_OAUTH_TOKEN", "operator-explicit-token")
    seen: list[str | None] = []

    agent.main(
        client_factory=lambda url: _FakeClient(),  # type: ignore[arg-type,return-value]
        home=tmp_path,
        launch=lambda harness, ctx: seen.append(agent.os.environ.get("ANTHROPIC_OAUTH_TOKEN")),
        on_exit=lambda: None,
    )

    assert seen == ["operator-explicit-token"]


def test_main_passes_the_launch_context_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", VALID_OAUTH_TOKEN)
    monkeypatch.setenv("PANOPTICON_INITIAL_PROMPT", "review your plan")
    monkeypatch.setenv("PANOPTICON_TASK_TURN", "agent")
    monkeypatch.setenv("PANOPTICON_STARTING_MODEL", "opus")
    seen: list[LaunchContext] = []
    agent.main(
        client_factory=lambda url: _FakeClient(),  # type: ignore[arg-type,return-value]
        home=tmp_path,
        launch=lambda harness, ctx: seen.append(ctx),
        on_exit=lambda: None,
    )
    (ctx,) = seen
    assert ctx.initial_prompt == "review your plan"
    assert ctx.turn == "agent"
    assert ctx.starting_model == "opus"
    assert ctx.home == tmp_path


def test_main_fails_fast_when_no_auth_token_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("PANOPTICON_RUNNER_ID", "runner-1")
    launched: list[str] = []
    fake = _FakeClient()
    agent.main(
        client_factory=lambda url: fake,  # type: ignore[arg-type,return-value]
        home=tmp_path,
        launch=lambda harness, ctx: launched.append("launched"),
        on_exit=lambda: launched.append("on_exit"),
    )
    assert launched == []  # launch must not be called
    assert len(fake.lifecycle_calls) == 1
    call = fake.lifecycle_calls[0]
    assert call["phase"] == "failed"
    assert call["runner_id"] == "runner-1"
    assert "CLAUDE_CODE_OAUTH_TOKEN" in (call["detail"] or "")


def test_main_fails_fast_on_a_rejected_credential_naming_the_env_file_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A revoked/invalid token must fail the spawn with the env-file fix, not reach claude's
    # in-container /login dead end. The probe result stands in for the API's 401.
    _base_env(monkeypatch, probe_status=401)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-revoked")
    monkeypatch.setenv("PANOPTICON_RUNNER_ID", "runner-1")
    fake = _FakeClient()
    agent.main(
        client_factory=lambda url: fake,  # type: ignore[arg-type,return-value]
        home=tmp_path,
        launch=lambda harness, ctx: pytest.fail("must not launch"),
        on_exit=lambda: None,
    )
    detail = fake.lifecycle_calls[0]["detail"] or ""
    assert "CLAUDE_CODE_OAUTH_TOKEN" in detail and "rejected" in detail


def test_main_proceeds_when_anthropic_api_key_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", VALID_ANTHROPIC_API_KEY)
    launched: list[str] = []
    agent.main(
        client_factory=lambda url: _FakeClient(),  # type: ignore[arg-type,return-value]
        home=tmp_path,
        launch=lambda harness, ctx: launched.append("launched"),
        on_exit=lambda: launched.append("on_exit"),
    )
    assert "launched" in launched  # ANTHROPIC_API_KEY alone is sufficient


def test_main_returns_early_without_lifecycle_call_when_runner_id_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _base_env(monkeypatch)
    monkeypatch.delenv("PANOPTICON_RUNNER_ID", raising=False)
    launched: list[str] = []
    fake = _FakeClient()
    agent.main(
        client_factory=lambda url: fake,  # type: ignore[arg-type,return-value]
        home=tmp_path,
        launch=lambda harness, ctx: launched.append("launched"),
        on_exit=lambda: launched.append("on_exit"),
    )
    assert launched == []  # still returns early without launching
    assert fake.lifecycle_calls == []  # no lifecycle call when runner_id absent


def test_run_agent_merges_the_harness_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The launch runs the harness argv with the harness env layered over the container's.
    recorded: dict[str, object] = {}

    class _FakeHarness(Harness):
        name = "fake"
        config_dirname = ".fake"

        def missing_auth(self, environ: object, *, home: Path) -> str | None:
            return None

        def bootstrap(self, ctx: object) -> None:
            pass

        def argv(self, ctx: LaunchContext) -> list[str]:
            return ["fake-cli", "--go"]

        def env(self, ctx: LaunchContext) -> dict[str, str]:
            return {"FAKE_HOME": "/f"}

    def fake_run(
        argv: list[str], env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        recorded["argv"] = argv
        recorded["env"] = env
        return subprocess.CompletedProcess(argv, 9)

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    status = agent._run_agent(_FakeHarness(), LaunchContext(home=Path("/h"), cwd=Path("/w")))
    assert recorded["argv"] == ["fake-cli", "--go"]
    assert status == 9
    env = recorded["env"]
    assert isinstance(env, dict) and env["FAKE_HOME"] == "/f"
