"""Executable contract for RFC 2119 reviewer configuration layers."""

from __future__ import annotations

import asyncio
import importlib.resources
from collections.abc import Iterator, Sequence
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, get_origin, get_type_hints

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from textual.app import App
from textual.widgets import Input

from panopticon.container.reviewers import (
    ReviewerConfig,
    ReviewerDispatchError,
    dispatch_reviews,
    resolve_reviewers,
)
from panopticon.core.models import Repo
from panopticon.sessionservice.local_runner import LocalRunner
from panopticon.sessionservice.spawner import Spawner
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.terminal.dashboard import RepoFormScreen
from panopticon.workflows import Spike
from panopticon.workflows.discovery import discover_workflows


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    service = TaskService(
        SqlAlchemyStore(),
        {"spike": Spike()},
        FilesystemArtifactStore(tmp_path),
    )
    asyncio.run(service.init())
    asyncio.run(service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1")))
    with TestClient(create_app(service)) as test_client:
        yield test_client


def test_workflows_own_configurable_honesty_reviewer_defaults() -> None:
    # 2119: REQ-044.1.1
    # 2119: REQ-044.1.2
    # 2119: REQ-044.3.2
    workflows = discover_workflows(_home_workflows=Path("/nonexistent"))
    builtins = [workflow for name, workflow in workflows.items() if name.startswith("2119-")]
    assert {workflow.name for workflow in builtins} == {
        "2119-human-spec",
        "2119-auto-spec",
        "2119-auto-sol",
    }
    for workflow in builtins:
        owners = {
            name: next(base for base in type(workflow).__mro__ if name in base.__dict__)
            for name in ("honesty_reviewer", "reviewers", "fable_reviews")
        }
        assert all(get_origin(get_type_hints(owners[name])[name]) is ClassVar for name in owners)
        assert workflow.honesty_reviewer == ReviewerConfig("codex", "gpt-5.6-sol")
        assert workflow._honesty_reviewer_cmd({}) == (
            "codex exec --dangerously-bypass-approvals-and-sandbox -m gpt-5.6-sol"
        )
        assert workflow._honesty_reviewer_cmd(
            {"PANOPTICON_2119_HONESTY_REVIEWER": "claude:claude-opus-5"}
        ) == (
            "claude --print --output-format json --safe-mode "
            "--dangerously-skip-permissions --model claude-opus-5"
        )
        variant = type(
            "ReviewerVariant",
            (type(workflow),),
            {
                "honesty_reviewer": ReviewerConfig("claude", "claude-opus-5"),
                "reviewers": (ReviewerConfig("codex", "one"),) * 2,
                "fable_reviews": False,
            },
        )
        assert variant.honesty_reviewer == ReviewerConfig("claude", "claude-opus-5")
        assert variant.reviewers == (ReviewerConfig("codex", "one"),) * 2
        assert variant.fable_reviews is False
        variant_workflow = object.__new__(variant)
        expected_variant = (
            "claude --print --output-format json --safe-mode "
            "--dangerously-skip-permissions --model claude-opus-5"
        )
        assert variant_workflow._honesty_reviewer_cmd({}) == expected_variant
        for blank in ("", "   "):
            assert (
                variant_workflow._honesty_reviewer_cmd({"PANOPTICON_2119_HONESTY_REVIEWER": blank})
                == expected_variant
            )
        assert (
            variant_workflow._honesty_reviewer_cmd(
                {"PANOPTICON_2119_HONESTY_REVIEWER": "codex:provider/model:high"}
            )
            == "codex exec --dangerously-bypass-approvals-and-sandbox -m provider/model:high"
        )
        with pytest.raises(ReviewerDispatchError):
            variant_workflow._honesty_reviewer_cmd({"PANOPTICON_2119_HONESTY_REVIEWER": "claude"})


def test_repo_reviewer_overrides_persist_through_api_and_store(client: TestClient) -> None:
    # 2119: REQ-044.2.1
    created = client.post(
        "/repos",
        json={
            "id": "r2",
            "name": "acme/other",
            "git_url": "https://x/r2.git",
            "honesty_reviewer": "claude:claude-opus-5",
            "reviewer_1": "codex:provider/model:high",
            "reviewer_2": "claude:claude-fable-5",
        },
    )
    assert created.status_code == 201, created.text
    names = ("honesty_reviewer", "reviewer_1", "reviewer_2")
    assert set(names) <= {field.name for field in fields(Repo)}
    assert {name: created.json()[name] for name in names} == {
        "honesty_reviewer": "claude:claude-opus-5",
        "reviewer_1": "codex:provider/model:high",
        "reviewer_2": "claude:claude-fable-5",
    }
    assert {name: client.get("/repos/r2").json()[name] for name in names} == {
        "honesty_reviewer": "claude:claude-opus-5",
        "reviewer_1": "codex:provider/model:high",
        "reviewer_2": "claude:claude-fable-5",
    }
    patched = client.patch("/repos/r2", json={"reviewer_2": "codex:gpt-5.6-sol:medium"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["honesty_reviewer"] == "claude:claude-opus-5"
    assert patched.json()["reviewer_1"] == "codex:provider/model:high"
    assert patched.json()["reviewer_2"] == "codex:gpt-5.6-sol:medium"
    for name in names:
        cleared = client.patch("/repos/r2", json={name: None})
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()[name] is None
        assert client.get("/repos/r2").json()[name] is None


@pytest.mark.parametrize(
    "value",
    ["claude", ":claude-opus-5", "claude:", "pi:anthropic/claude-opus-5"],
)
@pytest.mark.parametrize("field", ["honesty_reviewer", "reviewer_1", "reviewer_2"])
def test_repo_rejects_malformed_reviewer_overrides(
    client: TestClient, field: str, value: str
) -> None:
    # 2119: REQ-044.2.2
    created = client.post(
        "/repos",
        json={"id": "r2", "name": "other", "git_url": "https://x/r2", field: value},
    )
    patched = client.patch("/repos/r1", json={field: value})
    assert created.status_code == 400, created.text
    assert patched.status_code == 400, patched.text
    assert field in created.json()["detail"]
    assert field in patched.json()["detail"]


@pytest.mark.parametrize("field", ["honesty_reviewer", "reviewer_1", "reviewer_2"])
@pytest.mark.parametrize("blank", [None, "", "   "])
def test_repo_accepts_blank_reviewer_override_as_unset(
    client: TestClient, field: str, blank: str | None
) -> None:
    # 2119: REQ-044.2.2
    response = client.patch("/repos/r1", json={field: blank})
    assert response.status_code == 200, response.text
    assert response.json()[field] is None


async def test_repo_form_exposes_all_reviewer_override_fields() -> None:
    # 2119: REQ-044.2.1
    saved: list[dict[str, Any]] = []
    repo = {
        "id": "r1",
        "name": "one",
        "git_url": "https://x/r1",
        "default_base": "main",
        "honesty_reviewer": "claude:claude-opus-5",
        "reviewer_1": "codex:gpt-5.6-sol:high",
        "reviewer_2": None,
    }
    app = App()
    async with app.run_test() as pilot:
        await app.push_screen(
            RepoFormScreen("edit repo", repo=repo, on_submit=lambda values: saved.append(values))
        )
        await pilot.pause()
        assert app.screen.query_one("#field-honesty_reviewer", Input).value == (
            "claude:claude-opus-5"
        )
        app.screen.query_one("#field-honesty_reviewer", Input).value = "codex:gpt-5.6-sol"
        app.screen.query_one("#field-reviewer_1", Input).value = "claude:claude-fable-5"
        app.screen.query_one("#field-reviewer_2", Input).value = "codex:gpt-5.6-sol:medium"
        await pilot.press("ctrl+s")
        await pilot.pause()
    assert saved[0]["honesty_reviewer"] == "codex:gpt-5.6-sol"
    assert saved[0]["reviewer_1"] == "claude:claude-fable-5"
    assert saved[0]["reviewer_2"] == "codex:gpt-5.6-sol:medium"


def _alembic_config(db_url: str) -> Config:
    ini_ref = importlib.resources.files("panopticon") / "alembic.ini"
    with importlib.resources.as_file(ini_ref) as ini_path:
        config = Config(str(ini_path))
    config.cmd_opts = type("opts", (), {"x": [f"db={db_url}"]})()
    return config


def test_migration_adds_nullable_repo_reviewer_columns(tmp_path: Path) -> None:
    # 2119: REQ-044.2.1
    url = f"sqlite:///{tmp_path / 'reviewers.db'}"
    command.upgrade(_alembic_config(url), "head")
    engine = create_engine(url)
    try:
        columns = {column["name"]: column for column in inspect(engine).get_columns("repo")}
    finally:
        engine.dispose()
    names = ("honesty_reviewer", "reviewer_1", "reviewer_2")
    assert set(names) <= set(columns)
    assert all(columns[name]["nullable"] for name in names)


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        interactive: bool = False,
        verbose: bool = False,
    ) -> str:
        self.calls.append(list(args))
        return "%1\n" if "display-message" in args else ""


def test_spawn_renders_repo_reviewer_overrides_after_env_file() -> None:
    # 2119: REQ-044.3.1
    recorder = _Recorder()
    LocalRunner("http://svc", secrets_dir="/host/secrets", run=recorder).spawn(
        "t1",
        env_file="r1.env",
        honesty_reviewer="claude:claude-opus-5",
        reviewer_1="codex:provider/model:high",
        reviewer_2="claude:claude-fable-5",
    )
    docker_run = recorder.calls[2]
    env_file_value_index = docker_run.index("--env-file") + 1
    assert docker_run[env_file_value_index] == "/host/secrets/r1.env"
    rendered = (
        "PANOPTICON_2119_HONESTY_REVIEWER=claude:claude-opus-5",
        "PANOPTICON_2119_REVIEWER_1=codex:provider/model:high",
        "PANOPTICON_2119_REVIEWER_2=claude:claude-fable-5",
    )
    assert all(value in docker_run for value in rendered)
    for value in rendered:
        value_index = docker_run.index(value)
        assert value_index > env_file_value_index
        assert docker_run[value_index - 1] == "--env"

    empty_recorder = _Recorder()
    LocalRunner("http://svc", secrets_dir="/host/secrets", run=empty_recorder).spawn(
        "t2", env_file="r1.env"
    )
    empty_run = empty_recorder.calls[2]
    empty_env_value_index = empty_run.index("--env-file") + 1
    assert empty_run[empty_env_value_index] == "/host/secrets/r1.env"
    for name in (
        "PANOPTICON_2119_HONESTY_REVIEWER",
        "PANOPTICON_2119_REVIEWER_1",
        "PANOPTICON_2119_REVIEWER_2",
    ):
        value_index = empty_run.index(f"{name}=")
        assert value_index > empty_env_value_index
        assert empty_run[value_index - 1] == "--env"

    captured: dict[str, Any] = {}
    spawner = object.__new__(Spawner)
    spawner._prepare_task_dir = lambda task, repo, clone: "/workspace"  # type: ignore[method-assign]
    spawner._run_hook = lambda *args: None  # type: ignore[method-assign]
    spawner._report = lambda *args, **kwargs: None  # type: ignore[method-assign]
    spawner._images = SimpleNamespace(build_base_if_missing=lambda verbose: None)
    spawner._compose_image = lambda harness, workflow, repo: None  # type: ignore[method-assign]
    spawner._runner = SimpleNamespace(spawn=lambda task_id, **kwargs: captured.update(kwargs))
    spawner._spawn_container(
        {"id": "t1", "workflow": "spike", "harness": "claude"},
        {
            "id": "r1",
            "git_url": "https://x/r1",
            "honesty_reviewer": "claude:claude-opus-5",
            "reviewer_1": "codex:provider/model:high",
            "reviewer_2": "claude:claude-fable-5",
        },
    )
    assert captured["honesty_reviewer"] == "claude:claude-opus-5"
    assert captured["reviewer_1"] == "codex:provider/model:high"
    assert captured["reviewer_2"] == "claude:claude-fable-5"


def test_reviewer_resolution_uses_repo_override_then_workflow_default() -> None:
    # 2119: REQ-044.3.2
    defaults = (
        ReviewerConfig("claude", "claude-fable-5"),
        ReviewerConfig("codex", "gpt-5.6-sol"),
    )
    assert resolve_reviewers(defaults, {}) == defaults
    assert resolve_reviewers(defaults, {"PANOPTICON_2119_REVIEWER_1": ""}) == defaults
    assert resolve_reviewers(defaults, {"PANOPTICON_2119_REVIEWER_1": "   "}) == defaults
    assert resolve_reviewers(
        defaults,
        {"PANOPTICON_2119_REVIEWER_1": "codex:provider/model:high"},
    ) == (ReviewerConfig("codex", "provider/model:high"), defaults[1])
    assert resolve_reviewers(
        defaults,
        {"PANOPTICON_2119_REVIEWER_2": "claude:claude-opus-5"},
    ) == (defaults[0], ReviewerConfig("claude", "claude-opus-5"))
    with pytest.raises(ReviewerDispatchError):
        resolve_reviewers((ReviewerConfig("pi", "model"), defaults[1]), {})
    with pytest.raises(ReviewerDispatchError):
        resolve_reviewers(defaults, {"PANOPTICON_2119_REVIEWER_2": "claude"})
    called = False

    def forbidden_run(*args: Any, **kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("invalid configuration reached a reviewer command")

    invalid_cases = tuple(
        (invalid_defaults, {})
        for invalid_defaults in (
            (ReviewerConfig("pi", "model"), defaults[1]),
            (ReviewerConfig("", "model"), defaults[1]),
            (ReviewerConfig("claude", ""), defaults[1]),
        )
    ) + tuple(
        (defaults, {"PANOPTICON_2119_REVIEWER_2": value})
        for value in ("claude", ":model", "claude:", "pi:model")
    )
    for invalid_defaults, environment in invalid_cases:
        with pytest.raises(ReviewerDispatchError):
            dispatch_reviews(
                invalid_defaults,
                environment,
                prompt="Review without edits.",
                commit="head",
                round_number=1,
                run=forbidden_run,
                post_comment=lambda comment: None,
            )
    assert called is False
