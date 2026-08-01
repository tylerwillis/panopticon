"""The codex harness: config.toml rendering (validated as real TOML), skills, auth, argv.

Facts pinned against codex-cli 0.144.4: the config keys come from its published config schema;
the api-key ``auth.json`` shape is what ``codex login --with-api-key`` writes; ``codex resume``
takes an explicit session id (scanned from the newest interactive rollout — see REQ-032, not
``--last``, which reviewer ``codex exec`` rollouts sharing the same ``CODEX_HOME`` can poison),
the bypass flags, and a positional prompt.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest

from panopticon.core.models import Skill
from panopticon.harnesses import INTERRUPT_PROMPT, BootstrapContext, LaunchContext
from panopticon.harnesses.codex import CODEX_VERSION, CodexHarness, render_config

HARNESS = CodexHarness()


def test_picker_metadata() -> None:
    assert HARNESS.field_label == "model"
    assert HARNESS.suggested_models() == (
        ("gpt-5.6-sol", "GPT-5.6 Sol"),
        ("terra", "Terra"),
        ("luna", "Luna"),
    )
    assert HARNESS.suggested_efforts("terra") == (
        ("low", "Low"),
        ("medium", "Medium"),
        ("high", "High"),
        ("xhigh", "X-high"),
    )


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


# -- config.toml ---------------------------------------------------------------------


def test_config_is_valid_toml_with_the_panopticon_mcp_server() -> None:
    cfg = tomllib.loads(render_config("http://svc:8000", "# map", Path("/workspace")))
    assert cfg["mcp_servers"]["panopticon"] == {"url": "http://svc:8000/mcp"}


def test_config_carries_the_overview_as_developer_instructions() -> None:
    cfg = tomllib.loads(
        render_config("http://svc:8000", "# map\nwith 'quotes' & \"lines\"", Path("/w"))
    )
    assert cfg["developer_instructions"] == "# map\nwith 'quotes' & \"lines\""


def test_config_omits_developer_instructions_when_no_overview() -> None:
    cfg = tomllib.loads(render_config("http://svc:8000", "  ", Path("/w")))
    assert "developer_instructions" not in cfg


def test_config_trusts_the_workspace() -> None:
    # codex's analog of claude's trust dialog — an unattended container can't answer it.
    cfg = tomllib.loads(render_config("http://svc:8000", "", Path("/workspace")))
    assert cfg["projects"]["/workspace"] == {"trust_level": "trusted"}


# 2119: REQ-008.5.1
def test_config_wires_the_turn_flip_hooks_to_the_shared_callback() -> None:
    # codex's hooks system is Claude-Code-compatible (same events, same JSON-on-stdin), so both
    # events invoke the exact command claude's settings.json uses — one callback, two harnesses.
    cfg = tomllib.loads(render_config("http://svc:8000", "", Path("/w")))
    stop = cfg["hooks"]["Stop"][0]["hooks"][0]
    prompt = cfg["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert stop == {
        "type": "command",
        "command": "python -m panopticon.container.hook user stop",
        "timeout": 3,
    }
    assert prompt == {
        "type": "command",
        "command": "python -m panopticon.container.hook agent prompt",
        "timeout": 3,
    }


def test_config_disables_the_builtin_apps_connector_via_the_feature_flag() -> None:
    # It can't start in the container and would stall every spawn on its 30s startup timeout.
    # Must be the feature flag: an mcp_servers entry without a transport is invalid codex
    # config and kills the CLI at startup (live incident, 2026-07-15).
    cfg = tomllib.loads(render_config("http://svc:8000", "", Path("/w")))
    assert cfg["features"]["apps"] is False
    assert "codex_apps" not in cfg["mcp_servers"]


def test_config_forces_file_backed_credentials() -> None:
    # Containers have no OS keyring, and the subscription flow shares auth.json via a mount.
    cfg = tomllib.loads(render_config("http://svc:8000", "", Path("/w")))
    assert cfg["cli_auth_credentials_store"] == "file"


# -- bootstrap: skills + operations + auth ------------------------------------------


def test_bootstrap_writes_config_and_skills(tmp_path: Path) -> None:
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path))
    assert (tmp_path / ".codex" / "config.toml").exists()
    skill = (tmp_path / ".agents" / "skills" / "open-pr" / "SKILL.md").read_text()
    assert skill.startswith("---\nname: open-pr\ndescription: Open the PR.\n---\ngh pr create")
    assert 'task_id="t1"' in skill  # the concrete task id, injected for MCP tool calls
    operation = (tmp_path / ".agents" / "skills" / "advance" / "SKILL.md").read_text()
    assert "apply_operation" in operation and "COMPLETE" in operation


def test_bootstrap_renders_skills_user_scope_not_into_the_workspace(tmp_path: Path) -> None:
    # The workspace is the task's git clone — rendered files there could end up in a commit, so
    # skills land under the *home* (codex's user scope), never under ctx.cwd.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, cwd=workspace))
    assert not (workspace / ".agents").exists()
    assert (tmp_path / ".agents" / "skills").is_dir()


def test_bootstrap_writes_an_api_key_auth_file_from_the_env(tmp_path: Path) -> None:
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, environ={"CODEX_API_KEY": "sk-x"}))
    auth = tmp_path / ".codex" / "auth.json"
    # The exact shape `codex login --with-api-key` writes (observed, codex-cli 0.144.4).
    assert json.loads(auth.read_text()) == {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-x"}
    assert (auth.stat().st_mode & 0o777) == 0o600


def test_bootstrap_symlinks_auth_from_the_credential_mount(tmp_path: Path) -> None:
    # The repo's shared credential dir (ChatGPT subscription): every container of the repo links
    # the same auth.json, so a token refresh by any session is visible to all (codex re-reads the
    # file before refreshing and writes through the symlink — verified against 0.144.4).
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "auth.json").write_text('{"auth_mode": "chatgpt"}')
    HARNESS.bootstrap(
        _bootstrap_ctx(tmp_path, environ={"PANOPTICON_CREDENTIALS": str(credentials)})
    )
    auth = tmp_path / ".codex" / "auth.json"
    assert auth.is_symlink() and auth.resolve() == (credentials / "auth.json").resolve()


def test_bootstrap_prefers_the_credential_mount_over_an_env_key(tmp_path: Path) -> None:
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "auth.json").write_text('{"auth_mode": "chatgpt"}')
    HARNESS.bootstrap(
        _bootstrap_ctx(
            tmp_path,
            environ={"PANOPTICON_CREDENTIALS": str(credentials), "CODEX_API_KEY": "sk-x"},
        )
    )
    assert (tmp_path / ".codex" / "auth.json").is_symlink()  # subscription wins


def test_bootstrap_never_clobbers_an_existing_auth_file(tmp_path: Path) -> None:
    config_dir = tmp_path / ".codex"
    config_dir.mkdir(parents=True)
    (config_dir / "auth.json").write_text('{"auth_mode": "chatgpt", "tokens": {}}')
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, environ={"CODEX_API_KEY": "sk-x"}))
    assert json.loads((config_dir / "auth.json").read_text())["auth_mode"] == "chatgpt"


def test_bootstrap_is_idempotent_across_respawns(tmp_path: Path) -> None:
    ctx = _bootstrap_ctx(tmp_path, environ={"CODEX_API_KEY": "sk-x"})
    HARNESS.bootstrap(ctx)
    HARNESS.bootstrap(ctx)  # a respawn re-runs the bootstrap on the same config volume
    assert (tmp_path / ".codex" / "config.toml").exists()


# -- auth check ----------------------------------------------------------------------


def test_missing_auth_accepts_each_credential_source(tmp_path: Path) -> None:
    assert HARNESS.missing_auth({"CODEX_API_KEY": "k"}, home=tmp_path) is None
    assert HARNESS.missing_auth({"OPENAI_API_KEY": "k"}, home=tmp_path) is None
    assert HARNESS.missing_auth({"CODEX_ACCESS_TOKEN": "t"}, home=tmp_path) is None


def test_missing_auth_accepts_a_mounted_credential_dir(tmp_path: Path) -> None:
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "auth.json").write_text("{}")
    env = {"PANOPTICON_CREDENTIALS": str(credentials)}
    assert HARNESS.missing_auth(env, home=tmp_path) is None


def test_missing_auth_accepts_an_auth_file_on_the_config_volume(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text("{}")
    assert HARNESS.missing_auth({}, home=tmp_path) is None


def test_missing_auth_names_the_fix_when_nothing_is_configured(tmp_path: Path) -> None:
    detail = HARNESS.missing_auth({}, home=tmp_path)
    assert detail is not None
    assert "CODEX_API_KEY" in detail and "credential_dir" in detail


# -- argv ----------------------------------------------------------------------------

_SESSION_FLAGS = [
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust",
    "--no-alt-screen",
]


def _seed_rollout(
    home: Path,
    session_id: str,
    originator: str,
    *,
    mtime: float,
    name: str = "rollout.jsonl",
    thread_source: str = "user",
) -> Path:
    """A rollout file with a codex-shaped ``session_meta`` first line — the interactive TUI and
    ``codex exec`` (what the dual-review/test-honesty skills dispatch reviewers with, INSIDE the
    task's own container/CODEX_HOME) write the same record shape, differing in
    ``payload.originator``; ``codex-tui``'s own internal subagent threads (e.g. compaction) share
    ``originator`` with the root session but differ in ``payload.thread_source``."""
    rollouts = home / ".codex" / "sessions" / "2026" / "07" / "31"
    rollouts.mkdir(parents=True, exist_ok=True)
    path = rollouts / name
    meta = {
        "timestamp": "2026-07-31T00:00:00Z",
        "type": "session_meta",
        "payload": {"id": session_id, "originator": originator, "thread_source": thread_source},
    }
    path.write_text(json.dumps(meta) + "\n")
    os.utime(path, (mtime, mtime))
    return path


def _seed_session(home: Path) -> None:
    _seed_rollout(home, "interactive-1", "codex-tui", mtime=100)


# 2119: REQ-015.1.1
def test_argv_preserves_scrollback_for_new_and_resumed_sessions(tmp_path: Path) -> None:
    assert "--no-alt-screen" in HARNESS.argv(_ctx(tmp_path))
    _seed_session(tmp_path)
    assert "--no-alt-screen" in HARNESS.argv(_ctx(tmp_path))


def test_argv_first_run_bypasses_approvals_and_hook_trust(tmp_path: Path) -> None:
    # The container is the sandbox — same posture as claude --dangerously-skip-permissions; the
    # hook-trust bypass is required or codex stops on an interactive per-hash trust prompt.
    assert HARNESS.argv(_ctx(tmp_path)) == ["codex", *_SESSION_FLAGS]


def test_argv_first_run_passes_model_then_prompt(tmp_path: Path) -> None:
    argv = HARNESS.argv(_ctx(tmp_path, initial_prompt="start now", starting_model="gpt-5.6-sol"))
    assert argv == ["codex", *_SESSION_FLAGS, "--model", "gpt-5.6-sol", "start now"]


def test_argv_splits_an_effort_suffix_into_a_config_override(tmp_path: Path) -> None:
    # "gpt-5.6-sol:high" = Sol at high reasoning effort — the pi-style suffix convention.
    argv = HARNESS.argv(_ctx(tmp_path, starting_model="gpt-5.6-sol:high"))
    assert argv == [
        "codex",
        *_SESSION_FLAGS,
        "--model",
        "gpt-5.6-sol",
        "--config",
        "model_reasoning_effort=high",
    ]


# 2119: REQ-032.1.1
def test_argv_resumes_the_recorded_interactive_session_by_id(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    assert HARNESS.argv(_ctx(tmp_path)) == ["codex", "resume", "interactive-1", *_SESSION_FLAGS]


# 2119: REQ-032.5.2
def test_argv_resume_appends_interrupt_prompt_on_agent_turn(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    argv = HARNESS.argv(_ctx(tmp_path, turn="agent"))
    assert argv == ["codex", "resume", "interactive-1", *_SESSION_FLAGS, INTERRUPT_PROMPT]


def test_argv_resume_omits_model_and_initial_prompt(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    argv = HARNESS.argv(_ctx(tmp_path, initial_prompt="start now", starting_model="gpt-5.6-sol"))
    assert "--model" not in argv and "start now" not in argv


# 2119: REQ-032.1.1
# 2119: REQ-032.1.3
def test_argv_resume_picks_interactive_over_a_newer_exec_rollout(tmp_path: Path) -> None:
    # The exact live failure mode: a reviewer's `codex exec` rollout, dispatched by the
    # dual-review/test-honesty skills INSIDE this same task container, lands newer than the
    # task's own interactive session but must never be resumed.
    _seed_rollout(tmp_path, "interactive-1", "codex-tui", mtime=100)
    _seed_rollout(tmp_path, "reviewer-1", "codex_exec", mtime=200, name="reviewer.jsonl")
    assert HARNESS.argv(_ctx(tmp_path)) == ["codex", "resume", "interactive-1", *_SESSION_FLAGS]


# 2119: REQ-032.1.2
def test_argv_resume_picks_the_newest_of_several_interactive_sessions(tmp_path: Path) -> None:
    # Filename order, on-disk creation order, AND mtime order are deliberately all different, so
    # selecting by name/glob order or creation order rather than genuine recency (the recorded
    # mtime, set explicitly via os.utime below, independent of write order) would pick wrong:
    # "newer" is created FIRST on disk and sorts FIRST alphabetically, yet its mtime is set later.
    _seed_rollout(tmp_path, "newer", "codex-tui", mtime=200, name="a-created-first.jsonl")
    _seed_rollout(tmp_path, "older", "codex-tui", mtime=100, name="z-created-second.jsonl")
    assert HARNESS.argv(_ctx(tmp_path)) == ["codex", "resume", "newer", *_SESSION_FLAGS]


# 2119: REQ-032.1.2
def test_argv_resume_recency_survives_float_mtime_precision_loss(tmp_path: Path) -> None:
    # At the current epoch, comparing Path.stat().st_mtime (a float) loses sub-microsecond
    # differences — two distinct nanosecond-precision mtimes can round to the identical float.
    # A selection based on that float would treat these as tied (or pick whichever rglob()
    # happens to see first) rather than the genuinely newest one, violating "most recently
    # written." Comparing st_mtime_ns (an integer) instead resolves the true ordering.
    older = _seed_rollout(tmp_path, "older", "codex-tui", mtime=0, name="a.jsonl")
    newer = _seed_rollout(tmp_path, "newer", "codex-tui", mtime=0, name="b.jsonl")
    base_ns = 1_800_000_000_000_000_000
    os.utime(older, ns=(base_ns, base_ns))
    os.utime(newer, ns=(base_ns + 1, base_ns + 1))
    assert older.stat().st_mtime == newer.stat().st_mtime  # the float collapses the difference
    assert older.stat().st_mtime_ns != newer.stat().st_mtime_ns  # ...but the integer doesn't
    assert HARNESS.argv(_ctx(tmp_path)) == ["codex", "resume", "newer", *_SESSION_FLAGS]


# 2119: REQ-032.1.4
def test_argv_resume_skips_a_newer_subagent_thread_of_the_interactive_session(
    tmp_path: Path,
) -> None:
    # codex-tui can write rollouts for its own internal subagent threads (e.g. compaction) that
    # still carry originator "codex-tui" but a non-root thread_source — these must not be
    # mistaken for the root session, even when they're the newest codex-tui-originated rollout.
    _seed_rollout(tmp_path, "root-session", "codex-tui", mtime=100, thread_source="user")
    _seed_rollout(
        tmp_path,
        "subagent-thread",
        "codex-tui",
        mtime=200,
        name="subagent.jsonl",
        thread_source="subagent",
    )
    assert HARNESS.argv(_ctx(tmp_path)) == ["codex", "resume", "root-session", *_SESSION_FLAGS]


# 2119: REQ-032.2.1
def test_argv_resume_stays_fresh_when_no_rollouts_recorded(tmp_path: Path) -> None:
    assert HARNESS.argv(_ctx(tmp_path)) == ["codex", *_SESSION_FLAGS]


# 2119: REQ-032.2.2
def test_argv_resume_falls_back_to_fresh_when_every_rollout_is_exec(tmp_path: Path) -> None:
    _seed_rollout(tmp_path, "reviewer-1", "codex_exec", mtime=100, name="a.jsonl")
    _seed_rollout(tmp_path, "reviewer-2", "exec", mtime=200, name="b.jsonl")
    assert HARNESS.argv(_ctx(tmp_path)) == ["codex", *_SESSION_FLAGS]


# 2119: REQ-032.3.1
def test_argv_resume_skips_malformed_rollouts_but_still_finds_the_interactive_one(
    tmp_path: Path,
) -> None:
    rollouts = tmp_path / ".codex" / "sessions"
    rollouts.mkdir(parents=True)
    (rollouts / "empty.jsonl").write_text("")
    (rollouts / "not-json.jsonl").write_text("not json at all\n")
    (rollouts / "no-payload.jsonl").write_text(json.dumps({"type": "session_meta"}) + "\n")
    no_originator = {
        "payload": {"id": "no-originator", "thread_source": "user"}
    }  # a payload present, but its originator field itself is missing
    (rollouts / "no-originator.jsonl").write_text(json.dumps(no_originator) + "\n")
    for name in ("empty.jsonl", "not-json.jsonl", "no-payload.jsonl", "no-originator.jsonl"):
        os.utime(rollouts / name, (300, 300))  # newer than the interactive rollout below
    _seed_rollout(tmp_path, "interactive-1", "codex-tui", mtime=100)
    assert HARNESS.argv(_ctx(tmp_path)) == ["codex", "resume", "interactive-1", *_SESSION_FLAGS]


# 2119: REQ-032.3.1
# 2119: REQ-032.2.2
def test_argv_resume_treats_all_malformed_rollouts_as_fresh(tmp_path: Path) -> None:
    rollouts = tmp_path / ".codex" / "sessions"
    rollouts.mkdir(parents=True)
    (rollouts / "empty.jsonl").write_text("")
    (rollouts / "not-json.jsonl").write_text("{{{not json\n")
    assert HARNESS.argv(_ctx(tmp_path)) == ["codex", *_SESSION_FLAGS]


# 2119: REQ-032.3.1
def test_argv_resume_skips_rollout_with_non_string_id(tmp_path: Path) -> None:
    rollouts = tmp_path / ".codex" / "sessions" / "2026" / "07" / "31"
    rollouts.mkdir(parents=True)
    int_id_meta = {
        "timestamp": "2026-07-31T00:00:00Z",
        "type": "session_meta",
        "payload": {"id": 123, "originator": "codex-tui", "thread_source": "user"},
    }
    path = rollouts / "int-id.jsonl"
    path.write_text(json.dumps(int_id_meta) + "\n")
    os.utime(path, (300, 300))  # newer than interactive rollout below
    _seed_rollout(tmp_path, "interactive-1", "codex-tui", mtime=100)
    assert HARNESS.argv(_ctx(tmp_path)) == ["codex", "resume", "interactive-1", *_SESSION_FLAGS]


class _FirstLineOnly:
    """Wraps an open rollout file so any read reaching past its first line raises — a naive
    ``path.read_text()``/``readlines()`` whole-file slurp fails immediately, so a passing test
    proves the scan never consumed more than the first line, not merely that it tolerated
    trailing garbage."""

    def __init__(self, fh: object, limit: int) -> None:
        self._fh = fh
        self._limit = limit
        self._consumed = 0

    def _account(self, chunk: str) -> str:
        self._consumed += len(chunk)
        if self._consumed > self._limit:
            raise AssertionError("read past a rollout's first line")
        return chunk

    def readline(self, *a: object, **kw: object) -> str:
        return self._account(self._fh.readline(*a, **kw))  # type: ignore[attr-defined]

    def read(self, *a: object, **kw: object) -> str:
        return self._account(self._fh.read(*a, **kw))  # type: ignore[attr-defined]

    def readlines(self, *a: object, **kw: object) -> list[str]:
        lines = self._fh.readlines(*a, **kw)  # type: ignore[attr-defined]
        self._account("".join(lines))
        return lines

    def __iter__(self) -> _FirstLineOnly:
        return self

    def __next__(self) -> str:
        return self._account(next(self._fh))  # type: ignore[arg-type]

    def __enter__(self) -> _FirstLineOnly:
        return self

    def __exit__(self, *exc: object) -> None:
        self._fh.close()  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        return getattr(self._fh, name)


# 2119: REQ-032.4.1
def test_argv_resume_reads_only_the_first_line_of_each_rollout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The scan must stay cheap: only the first line is read, never the file's full contents. A
    # rollout whose second line is large unparseable garbage — a naive full-file read/parse would
    # both blow the read budget below AND raise on the garbage — proves both properties in one
    # test: correctness despite trailing junk, and that the junk was never actually read.
    path = _seed_rollout(tmp_path, "interactive-1", "codex-tui", mtime=100)
    first_line = path.read_text().splitlines(keepends=True)[0]
    with path.open("a") as f:
        f.write("{{{ this is not valid json and must never be parsed\n" * 5000)

    real_open = Path.open

    def limited_open(self: Path, *args: object, **kwargs: object) -> object:
        fh = real_open(self, *args, **kwargs)  # type: ignore[arg-type]
        if self == path:
            return _FirstLineOnly(fh, limit=len(first_line))
        return fh

    monkeypatch.setattr(Path, "open", limited_open)  # type: ignore[attr-defined]
    assert HARNESS.argv(_ctx(tmp_path)) == ["codex", "resume", "interactive-1", *_SESSION_FLAGS]


# 2119: REQ-032.5.1
def test_argv_resume_session_flags_unchanged(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    argv = HARNESS.argv(_ctx(tmp_path))
    assert argv[3:] == _SESSION_FLAGS


# -- image layer + env ----------------------------------------------------------------


def test_image_layer_installs_the_pinned_release_for_both_architectures() -> None:
    layer = HARNESS.image_layer()
    assert f"rust-v{CODEX_VERSION}" in layer  # pinned, not `latest` — the verified version
    assert "x86_64-unknown-linux-musl" in layer and "aarch64-unknown-linux-musl" in layer
    assert "--extract --gzip --directory" in layer  # long options (repo convention)


def test_env_points_codex_at_the_per_task_config_dir(tmp_path: Path) -> None:
    assert HARNESS.env(_ctx(tmp_path)) == {"CODEX_HOME": str(tmp_path / ".codex")}
