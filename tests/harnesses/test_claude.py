"""The claude harness: argv (first-run vs resume), config rendering, trust seeds, bootstrap.

These are the golden expectations carried over verbatim from the pre-harness agent launcher
(Slice 6) — the M3 seam extraction must not change what claude is launched with or what lands
in its config dir.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from panopticon.core.models import Skill
from panopticon.harnesses import INTERRUPT_PROMPT, BootstrapContext, LaunchContext, claude
from panopticon.harnesses.claude import (
    MCP_CONFIG_FILE,
    WORKFLOW_OVERVIEW_FILE,
    ClaudeHarness,
    trust_workspace,
    write_mcp_config,
    write_workflow_overview,
)

HARNESS = ClaudeHarness()


def test_picker_metadata() -> None:
    assert HARNESS.field_label == "model"
    assert HARNESS.suggested_models() == (
        ("fable", "Fable 5"),
        ("opus", "Opus 4.8"),
        ("sonnet", "Sonnet 5"),
    )
    assert HARNESS.suggested_efforts("opus") == ()


def _ctx(home: Path, **kwargs: str | None) -> LaunchContext:
    return LaunchContext(home=home, cwd=Path("/work/repo"), **kwargs)  # type: ignore[arg-type]


def _seed_session(home: Path) -> None:
    project = (
        home / ".claude" / "projects" / "-work-repo"
    )  # claude's <config>/projects/<cwd, / → ->
    project.mkdir(parents=True)
    (project / "session.jsonl").write_text("{}")


# 2119: REQ-008.4.1
def test_user_prompt_submit_waits_for_the_agent_turn_hook() -> None:
    prompt = claude.settings()["hooks"]["UserPromptSubmit"][0]
    assert prompt == {
        "hooks": [
            {
                "type": "command",
                "command": "python -m panopticon.container.hook agent prompt",
                "timeout": 3,
            }
        ]
    }


def test_argv_starts_fresh_without_a_session(tmp_path: Path) -> None:
    # Unattended container, per-task clone → skip permission prompts (no operator to answer them).
    assert HARNESS.argv(_ctx(tmp_path)) == ["claude", "--dangerously-skip-permissions"]


def test_argv_continues_an_existing_session(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    assert HARNESS.argv(_ctx(tmp_path)) == [
        "claude",
        "--dangerously-skip-permissions",
        "--continue",
    ]


def test_argv_appends_initial_prompt_on_first_session(tmp_path: Path) -> None:
    argv = HARNESS.argv(_ctx(tmp_path, initial_prompt="review your plan"))
    assert argv == ["claude", "--dangerously-skip-permissions", "review your plan"]


def test_argv_omits_initial_prompt_when_continuing_a_session(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    argv = HARNESS.argv(_ctx(tmp_path, initial_prompt="review your plan"))
    assert "--continue" in argv
    assert "review your plan" not in argv


def test_argv_appends_interrupt_prompt_on_respawn_for_agent_turn(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    argv = HARNESS.argv(_ctx(tmp_path, turn="agent"))
    assert argv == [
        "claude",
        "--dangerously-skip-permissions",
        "--continue",
        INTERRUPT_PROMPT,
    ]


def test_argv_omits_interrupt_prompt_on_respawn_for_user_turn(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    argv = HARNESS.argv(_ctx(tmp_path, turn="user"))
    assert argv == ["claude", "--dangerously-skip-permissions", "--continue"]


def test_argv_passes_model_on_first_run(tmp_path: Path) -> None:
    argv = HARNESS.argv(_ctx(tmp_path, starting_model="opus"))
    assert argv == ["claude", "--dangerously-skip-permissions", "--model", "opus"]


def test_argv_splits_an_effort_suffix_into_an_effort_flag(tmp_path: Path) -> None:
    argv = HARNESS.argv(_ctx(tmp_path, starting_model="opus:high"))
    assert argv == [
        "claude",
        "--dangerously-skip-permissions",
        "--model",
        "opus",
        "--effort",
        "high",
    ]


def test_argv_omits_model_on_resume(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    argv = HARNESS.argv(_ctx(tmp_path, starting_model="opus:high"))
    assert "--model" not in argv and "--effort" not in argv
    assert "--continue" in argv


def test_argv_passes_model_before_initial_prompt_on_first_run(tmp_path: Path) -> None:
    argv = HARNESS.argv(_ctx(tmp_path, initial_prompt="start now", starting_model="opus"))
    assert argv == ["claude", "--dangerously-skip-permissions", "--model", "opus", "start now"]


def test_write_mcp_config_points_claude_at_the_task_service_mcp(tmp_path: Path) -> None:
    path = write_mcp_config(tmp_path, "http://host.docker.internal:8000")
    assert path == tmp_path / MCP_CONFIG_FILE
    cfg = json.loads(path.read_text())
    server = cfg["mcpServers"]["panopticon"]
    assert server == {"type": "http", "url": "http://host.docker.internal:8000/mcp"}


def test_argv_adds_strict_mcp_config_when_present(tmp_path: Path) -> None:
    write_mcp_config(tmp_path / ".claude", "http://svc:8000")
    argv = HARNESS.argv(_ctx(tmp_path))
    assert argv == [
        "claude",
        "--dangerously-skip-permissions",
        "--mcp-config",
        str(tmp_path / ".claude" / MCP_CONFIG_FILE),
        "--strict-mcp-config",
    ]


def test_write_workflow_overview_writes_the_map_else_skips(tmp_path: Path) -> None:
    path = write_workflow_overview(tmp_path, "# github-peer-reviewed\nphases…")
    assert (
        path == tmp_path / WORKFLOW_OVERVIEW_FILE
        and path.read_text() == "# github-peer-reviewed\nphases…"
    )
    assert write_workflow_overview(tmp_path / "empty", "  ") is None  # no overview → skipped


def test_argv_appends_the_workflow_overview_to_the_system_prompt(tmp_path: Path) -> None:
    write_workflow_overview(tmp_path / ".claude", "# the workflow map")
    argv = HARNESS.argv(_ctx(tmp_path))
    i = argv.index("--append-system-prompt")
    assert argv[i + 1] == "# the workflow map"  # the map's contents go inline


def test_trust_workspace_seeds_acceptance_for_a_fresh_config(tmp_path: Path) -> None:
    config_dir = tmp_path / ".claude"
    trust_workspace(config_dir, Path("/workspace"))
    data = json.loads((config_dir / ".claude.json").read_text())
    assert data["projects"]["/workspace"]["hasTrustDialogAccepted"] is True
    assert data["hasCompletedOnboarding"] is True
    assert data["hasAcknowledgedCostThreshold"] is True  # suppresses the API-key cost dialog


def test_trust_workspace_merges_and_is_idempotent(tmp_path: Path) -> None:
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    # claude already wrote config (incl. an existing project) — we must not clobber it.
    (config_dir / ".claude.json").write_text(
        json.dumps({"userID": "u", "projects": {"/other": {"history": []}}})
    )
    trust_workspace(config_dir, Path("/workspace"))
    trust_workspace(config_dir, Path("/workspace"))  # idempotent
    data = json.loads((config_dir / ".claude.json").read_text())
    assert data["userID"] == "u"  # preserved
    assert data["projects"]["/other"] == {"history": []}  # preserved
    assert data["projects"]["/workspace"]["hasTrustDialogAccepted"] is True


def _probing(monkeypatch, status):
    """Route the harness's one network seam to a canned probe result (no network in CI)."""
    monkeypatch.setattr(claude, "_probe_status", lambda headers: status)


def test_missing_auth_names_the_token_when_nothing_is_set(tmp_path: Path) -> None:
    detail = HARNESS.missing_auth({}, home=tmp_path)
    assert detail is not None and "CLAUDE_CODE_OAUTH_TOKEN" in detail


def test_missing_auth_accepts_a_credential_the_api_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _probing(monkeypatch, 200)
    assert HARNESS.missing_auth({"CLAUDE_CODE_OAUTH_TOKEN": "t"}, home=tmp_path) is None
    assert HARNESS.missing_auth({"ANTHROPIC_API_KEY": "k"}, home=tmp_path) is None


def test_missing_auth_rejects_a_credential_the_api_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One mechanism catches revoked, malformed, and truncated alike: the API's own 401.
    _probing(monkeypatch, 401)
    detail = HARNESS.missing_auth({"CLAUDE_CODE_OAUTH_TOKEN": "revoked"}, home=tmp_path)
    assert detail is not None and "CLAUDE_CODE_OAUTH_TOKEN" in detail and "rejected" in detail


def test_missing_auth_fails_open_when_the_probe_cannot_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Offline/timeout/5xx must never block a spawn — the agent makes the same call anyway.
    _probing(monkeypatch, None)
    assert HARNESS.missing_auth({"CLAUDE_CODE_OAUTH_TOKEN": "t"}, home=tmp_path) is None


def test_missing_auth_skips_the_probe_when_a_gateway_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ANTHROPIC_BASE_URL means a gateway with its own auth semantics — a gateway-issued
    # credential can 401 against Anthropic while being valid, so the preflight stands down.
    _probing(monkeypatch, 401)
    env = {"ANTHROPIC_API_KEY": "gw-key", "ANTHROPIC_BASE_URL": "https://llm-gw.example"}
    assert HARNESS.missing_auth(env, home=tmp_path) is None


def test_probe_seam_sends_the_right_request_and_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class _Resp:
        status_code = 403

    def fake_get(url: str, *, headers: dict[str, str], timeout: float) -> _Resp:
        seen.update(url=url, headers=headers, timeout=timeout)
        return _Resp()

    monkeypatch.setattr(claude.httpx, "get", fake_get)
    assert claude._probe_status({"x-api-key": "k"}) == 403  # non-401 statuses pass through...
    assert seen["url"] == "https://api.anthropic.com/v1/models"
    assert seen["headers"] == {"x-api-key": "k"}
    assert seen["timeout"] == 10

    def raising_get(url: str, *, headers: dict[str, str], timeout: float) -> _Resp:
        raise OSError("offline")

    monkeypatch.setattr(claude.httpx, "get", raising_get)
    assert claude._probe_status({"x-api-key": "k"}) is None  # ...and errors fail open


def test_missing_auth_builds_the_right_headers_per_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, str]] = []
    monkeypatch.setattr(claude, "_probe_status", lambda headers: captured.append(headers) or 200)
    HARNESS.missing_auth({"ANTHROPIC_API_KEY": "k"}, home=tmp_path)
    HARNESS.missing_auth({"CLAUDE_CODE_OAUTH_TOKEN": "t"}, home=tmp_path)
    assert captured[0] == {"x-api-key": "k", "anthropic-version": "2023-06-01"}
    assert captured[1] == {
        "authorization": "Bearer t",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
    }


def test_missing_auth_names_the_api_key_when_it_wins_and_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ANTHROPIC_API_KEY overrides the token (docs/auth.md) — the message must blame the
    # credential claude will actually use.
    _probing(monkeypatch, 401)
    detail = HARNESS.missing_auth(
        {"CLAUDE_CODE_OAUTH_TOKEN": "t", "ANTHROPIC_API_KEY": "k"}, home=tmp_path
    )
    assert detail is not None and "ANTHROPIC_API_KEY" in detail


def test_bootstrap_renders_the_full_claude_surface(tmp_path: Path) -> None:
    HARNESS.bootstrap(
        BootstrapContext(
            home=tmp_path,
            cwd=Path("/workspace"),
            service_url="http://svc:8000",
            task_id="t1",
            skills=[Skill(name="s", description="d", instructions="i")],
            operations={"advance": "COMPLETE"},
            overview="# map",
        )
    )
    commands = tmp_path / ".claude" / "commands"
    assert (commands / "s.md").exists()  # skills rendered...
    assert (commands / "advance.md").exists()  # ...operations rendered...
    assert (tmp_path / ".claude" / "settings.json").exists()  # ...turn-flip hooks written...
    assert (tmp_path / ".claude" / MCP_CONFIG_FILE).exists()  # ...MCP server wired...
    assert (tmp_path / ".claude" / WORKFLOW_OVERVIEW_FILE).exists()  # ...workflow map written...
    trust = json.loads((tmp_path / ".claude" / ".claude.json").read_text())
    assert trust["projects"]["/workspace"]["hasTrustDialogAccepted"] is True  # ...trust seeded


def test_env_points_claude_at_the_per_task_config_dir(tmp_path: Path) -> None:
    assert HARNESS.env(_ctx(tmp_path)) == {"CLAUDE_CONFIG_DIR": str(tmp_path / ".claude")}
