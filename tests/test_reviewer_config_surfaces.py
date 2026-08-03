"""Executable contract for RFC 2119 reviewer configuration layers."""

from __future__ import annotations

import asyncio
import importlib.resources
import logging
import shlex
import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from textual.app import App
from textual.widgets import Input

from panopticon.container.reviewers import (
    ReviewerConfig,
    ReviewerDispatchError,
    dispatch_reviews,
    main,
    resolve_reviewers,
)
from panopticon.core.models import Repo
from panopticon.harnesses.base import render_reviewer_command
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
    store = SqlAlchemyStore()
    service = TaskService(
        store,
        {"spike": Spike()},
        FilesystemArtifactStore(tmp_path),
    )
    asyncio.run(service.init())
    asyncio.run(service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1")))
    with TestClient(create_app(service)) as test_client:
        yield test_client
    asyncio.run(store.close())


def test_workflows_own_configurable_honesty_reviewer_defaults() -> None:
    # 2119: reviewer-config-surfaces.1.1
    # 2119: reviewer-config-surfaces.1.2
    # 2119: reviewer-config-surfaces.3.2
    workflows = discover_workflows(_home_workflows=Path("/nonexistent"))
    builtins = [workflow for name, workflow in workflows.items() if name.startswith("2119-")]
    assert {workflow.name for workflow in builtins} == {
        "2119-human-spec",
        "2119-auto-spec",
        "2119-auto-sol",
    }
    for workflow in builtins:
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
        reviewer_only = type(
            "ReviewerOnlyVariant",
            (type(workflow),),
            {"reviewers": (ReviewerConfig("codex", "two"),) * 2},
        )
        assert reviewer_only.honesty_reviewer == workflow.honesty_reviewer
        assert reviewer_only.fable_reviews == workflow.fable_reviews
        fable_only = type(
            "FableOnlyVariant",
            (type(workflow),),
            {"fable_reviews": not workflow.fable_reviews},
        )
        assert fable_only.fable_reviews is not workflow.fable_reviews
        assert fable_only.honesty_reviewer == workflow.honesty_reviewer
        assert fable_only.reviewers == workflow.reviewers
        variant_workflow = variant()
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
        invalid_defaults = (
            ReviewerConfig("pi", "model"),
            ReviewerConfig("", "model"),
            ReviewerConfig("   ", "model"),
            ReviewerConfig("claude", ""),
            ReviewerConfig("claude", "   "),
        )
        for invalid in invalid_defaults:
            invalid_default = type(
                "InvalidHonestyDefault",
                (type(workflow),),
                {"honesty_reviewer": invalid},
            )()
            for environment in (
                {},
                {"PANOPTICON_2119_HONESTY_REVIEWER": "claude:claude-opus-5"},
            ):
                with pytest.raises(ReviewerDispatchError):
                    invalid_default._honesty_reviewer_cmd(environment)
        for invalid_override in (
            "claude",
            ":model",
            "   :model",
            "claude:",
            "claude:   ",
            "pi:model",
            "claudee:model",
        ):
            with pytest.raises(ReviewerDispatchError):
                variant_workflow._honesty_reviewer_cmd(
                    {"PANOPTICON_2119_HONESTY_REVIEWER": invalid_override}
                )


def test_rendered_spec_skill_resolves_honesty_reviewer_inside_container(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 2119: reviewer-config-surfaces.1.2
    workflow = discover_workflows(_home_workflows=Path("/nonexistent"))["2119-auto-spec"]
    declared = next(skill for skill in workflow.skills() if skill.name == "spec-2119")
    delivered = next(skill for skill in workflow.container_skills() if skill.name == "spec-2119")
    instructions = delivered.instructions
    assert instructions != declared.instructions
    assert (
        "python -m panopticon.container.reviewers honesty-command --default codex:gpt-5.6-sol"
    ) in instructions
    variant = type(
        "HonestyVariant",
        (type(workflow),),
        {"honesty_reviewer": ReviewerConfig("claude", "claude-fable-5")},
    )()
    variant_instructions = next(
        skill for skill in variant.container_skills() if skill.name == "spec-2119"
    ).instructions
    assert "honesty-command --default claude:claude-fable-5" in variant_instructions
    monkeypatch.delenv("PANOPTICON_2119_HONESTY_REVIEWER", raising=False)
    assert main(["honesty-command", "--default", "claude:claude-fable-5"]) == 0
    assert capsys.readouterr().out.strip().endswith("--model claude-fable-5")
    monkeypatch.setenv("PANOPTICON_2119_HONESTY_REVIEWER", "   ")
    assert main(["honesty-command", "--default", "claude:claude-fable-5"]) == 0
    assert capsys.readouterr().out.strip().endswith("--model claude-fable-5")
    monkeypatch.setenv("PANOPTICON_2119_HONESTY_REVIEWER", "claude:claude-opus-5")
    assert main(["honesty-command", "--default", "codex:gpt-5.6-sol"]) == 0
    assert capsys.readouterr().out.strip() == (
        "claude --print --output-format json --safe-mode "
        "--dangerously-skip-permissions --model claude-opus-5"
    )
    malicious_model = "provider model; touch /tmp/not-a-command"
    monkeypatch.setenv("PANOPTICON_2119_HONESTY_REVIEWER", f"codex:{malicious_model}")
    assert main(["honesty-command", "--default", "claude:claude-fable-5"]) == 0
    assert shlex.split(capsys.readouterr().out.strip()) == [
        "codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "-m",
        malicious_model,
    ]


def test_custom_workflow_default_is_quoted_at_resolver_shell_boundary() -> None:
    # 2119: reviewer-config-surfaces.1.2
    workflow = discover_workflows(_home_workflows=Path("/nonexistent"))["2119-auto-spec"]
    model = "provider model' ; printf injected"
    variant = type(
        "QuotedDefaultVariant",
        (type(workflow),),
        {"honesty_reviewer": ReviewerConfig("codex", model)},
    )()
    instructions = next(
        skill for skill in variant.container_skills() if skill.name == "spec-2119"
    ).instructions
    resolver = next(
        fragment
        for fragment in instructions.split("`")
        if fragment.startswith("python -m panopticon.container.reviewers honesty-command")
    )
    completed = subprocess.run(
        ["sh", "-c", f'set -- {resolver}; printf "<%s>\\n" "$@"'],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.splitlines() == [
        "<python>",
        "<-m>",
        "<panopticon.container.reviewers>",
        "<honesty-command>",
        "<--default>",
        f"<codex:{model}>",
    ]


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (
            ReviewerConfig("claude", "model with 'quotes'; still-one"),
            [
                "claude",
                "--print",
                "--output-format",
                "json",
                "--safe-mode",
                "--dangerously-skip-permissions",
                "--model",
                "model with 'quotes'; still-one",
            ],
        ),
        (
            ReviewerConfig("codex", 'model with "quotes"; still-one'),
            [
                "codex",
                "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                "-m",
                'model with "quotes"; still-one',
            ],
        ),
    ],
)
def test_shared_reviewer_command_quotes_each_harness(
    config: ReviewerConfig, expected: list[str]
) -> None:
    # 2119: reviewer-config-surfaces.1.2
    assert shlex.split(render_reviewer_command(config)) == expected


def test_repo_reviewer_overrides_persist_through_api_and_store(client: TestClient) -> None:
    # 2119: reviewer-config-surfaces.2.1
    created = client.post(
        "/repos",
        json={
            "id": "r2",
            "name": "acme/other",
            "git_url": "https://x/r2.git",
            "honesty_reviewer": "claude:claude-opus-5",
            "reviewer_1": "  codex:provider/model:high  ",
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
    current = {
        "honesty_reviewer": "claude:claude-opus-5",
        "reviewer_1": "codex:provider/model:high",
        "reviewer_2": "claude:claude-fable-5",
    }
    updates = {
        "honesty_reviewer": "codex:gpt-5.6-sol",
        "reviewer_1": "claude:claude-opus-5",
        "reviewer_2": "codex:gpt-5.6-sol:medium",
    }
    for name, value in updates.items():
        patched = client.patch("/repos/r2", json={name: value})
        assert patched.status_code == 200, patched.text
        current[name] = value
        assert {field: patched.json()[field] for field in names} == current
    for name in names:
        cleared = client.patch("/repos/r2", json={name: None})
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()[name] is None
        assert client.get("/repos/r2").json()[name] is None


@pytest.mark.asyncio
async def test_repo_reviewer_overrides_survive_store_reopen(tmp_path: Path) -> None:
    # 2119: reviewer-config-surfaces.2.1
    url = f"sqlite:///{tmp_path / 'repos.db'}"
    values = {
        "honesty_reviewer": "claude:claude-opus-5",
        "reviewer_1": "codex:gpt-5.6-sol",
        "reviewer_2": "claude:claude-fable-5",
    }
    first_store = SqlAlchemyStore(url)
    first = TaskService(first_store, {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    await first.init()
    await first.create_repo(
        Repo(id="durable", name="durable", git_url="https://x/durable", **values)
    )
    await first_store.close()

    second_store = SqlAlchemyStore(url)
    second = TaskService(second_store, {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    await second.init()
    reopened = await second.get_repo("durable")
    assert {name: getattr(reopened, name) for name in values} == values
    await second.update_repo("durable", {"reviewer_1": None})
    await second_store.close()

    third_store = SqlAlchemyStore(url)
    third = TaskService(third_store, {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    await third.init()
    cleared = await third.get_repo("durable")
    assert cleared.honesty_reviewer == values["honesty_reviewer"]
    assert cleared.reviewer_1 is None
    assert cleared.reviewer_2 == values["reviewer_2"]
    await third_store.close()


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("claude", "malformed reviewer pair"),
        (":claude-opus-5", "missing harness"),
        ("   :claude-opus-5", "missing harness"),
        ("claude:", "missing model"),
        ("claude:   ", "missing model"),
        ("pi:anthropic/claude-opus-5", "unsupported harness"),
        ("claudee:claude-opus-5", "unsupported harness"),
    ],
)
@pytest.mark.parametrize("field", ["honesty_reviewer", "reviewer_1", "reviewer_2"])
def test_repo_rejects_malformed_reviewer_overrides(
    client: TestClient, field: str, value: str, reason: str
) -> None:
    # 2119: reviewer-config-surfaces.2.2
    created = client.post(
        "/repos",
        json={"id": "r2", "name": "other", "git_url": "https://x/r2", field: value},
    )
    patched = client.patch("/repos/r1", json={field: value})
    assert created.status_code == 400, created.text
    assert patched.status_code == 400, patched.text
    assert field in created.json()["detail"]
    assert field in patched.json()["detail"]
    assert reason in created.json()["detail"]
    assert reason in patched.json()["detail"]
    assert "Remediation:" in created.json()["detail"]
    assert "Remediation:" in patched.json()["detail"]


@pytest.mark.parametrize("field", ["honesty_reviewer", "reviewer_1", "reviewer_2"])
@pytest.mark.parametrize("blank", [None, "", "   "])
def test_repo_accepts_blank_reviewer_override_as_unset(
    client: TestClient, field: str, blank: str | None
) -> None:
    # 2119: reviewer-config-surfaces.2.1
    # 2119: reviewer-config-surfaces.2.2
    created = client.post(
        "/repos",
        json={"id": "r2", "name": "other", "git_url": "https://x/r2", field: blank},
    )
    assert created.status_code == 201, created.text
    assert created.json()[field] is None
    response = client.patch("/repos/r1", json={field: blank})
    assert response.status_code == 200, response.text
    assert response.json()[field] is None


async def test_repo_form_exposes_all_reviewer_override_fields() -> None:
    # 2119: reviewer-config-surfaces.2.1
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
        assert app.screen.query_one("#field-reviewer_1", Input).value == "codex:gpt-5.6-sol:high"
        assert app.screen.query_one("#field-reviewer_2", Input).value == ""
        app.screen.query_one("#field-honesty_reviewer", Input).value = ""
        app.screen.query_one("#field-reviewer_1", Input).value = "claude:claude-fable-5"
        app.screen.query_one("#field-reviewer_2", Input).value = "codex:gpt-5.6-sol:medium"
        await pilot.press("ctrl+s")
        await pilot.pause()
    assert saved[0]["honesty_reviewer"] is None
    assert saved[0]["reviewer_1"] == "claude:claude-fable-5"
    assert saved[0]["reviewer_2"] == "codex:gpt-5.6-sol:medium"


async def test_repo_create_form_exposes_and_submits_reviewer_overrides() -> None:
    # 2119: reviewer-config-surfaces.2.1
    saved: list[dict[str, Any]] = []
    values = {
        "honesty_reviewer": "claude:claude-opus-5",
        "reviewer_1": "codex:gpt-5.6-sol",
        "reviewer_2": "claude:claude-fable-5",
    }
    app = App()
    async with app.run_test() as pilot:
        await app.push_screen(
            RepoFormScreen("new repo", on_submit=lambda result: saved.append(result))
        )
        await pilot.pause()
        app.screen.query_one("#field-git_url", Input).value = "https://x/widgets.git"
        for name, value in values.items():
            app.screen.query_one(f"#field-{name}", Input).value = value
        await pilot.press("ctrl+s")
        await pilot.pause()
    assert {name: saved[0][name] for name in values} == values


async def test_repo_create_form_submits_blank_reviewer_overrides_as_null() -> None:
    # 2119: reviewer-config-surfaces.2.1
    saved: list[dict[str, Any]] = []
    app = App()
    async with app.run_test() as pilot:
        await app.push_screen(
            RepoFormScreen("new repo", on_submit=lambda result: saved.append(result))
        )
        await pilot.pause()
        app.screen.query_one("#field-git_url", Input).value = "https://x/widgets.git"
        await pilot.press("ctrl+s")
        await pilot.pause()
    assert {name: saved[0][name] for name in RepoFormScreen.REVIEWER_FIELDS} == dict.fromkeys(
        RepoFormScreen.REVIEWER_FIELDS
    )


def _alembic_config(db_url: str) -> Config:
    ini_ref = importlib.resources.files("panopticon") / "alembic.ini"
    with importlib.resources.as_file(ini_ref) as ini_path:
        config = Config(str(ini_path))
    config.cmd_opts = type("opts", (), {"x": [f"db={db_url}"]})()
    return config


def test_migration_adds_nullable_repo_reviewer_columns(tmp_path: Path) -> None:
    # 2119: reviewer-config-surfaces.2.1
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


def test_migration_preserves_existing_repo_from_prior_revision(tmp_path: Path) -> None:
    # 2119: reviewer-config-surfaces.2.1
    url = f"sqlite:///{tmp_path / 'upgrade.db'}"
    config = _alembic_config(url)
    command.upgrade(config, "baa229ad49e8")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO repo "
                    "(id, name, git_url, default_base, capabilities, enabled_workflows, "
                    "disabled_workflows) VALUES "
                    "('existing', 'existing', 'https://x/existing', 'main', '{}', '[]', '[]')"
                )
            )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT honesty_reviewer, reviewer_1, reviewer_2 "
                    "FROM repo WHERE id = 'existing'"
                )
            ).one()
        assert tuple(row) == (None, None, None)
    finally:
        engine.dispose()


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
    # 2119: reviewer-config-surfaces.3.1
    recorder = _Recorder()
    LocalRunner("http://svc", secrets_dir="/host/secrets", run=recorder).spawn(
        "t1",
        env_file="r1.env",
        honesty_reviewer="claude:claude-opus-5",
        reviewer_1="codex:provider/model:high",
        reviewer_2="claude:claude-fable-5",
    )
    docker_run = next(call for call in recorder.calls if call[:2] == ["docker", "run"])
    assert docker_run.count("--env-file") == 1
    env_file_value_index = docker_run.index("--env-file") + 1
    assert docker_run[env_file_value_index] == "/host/secrets/r1.env"
    rendered = (
        "PANOPTICON_2119_HONESTY_REVIEWER=claude:claude-opus-5",
        "PANOPTICON_2119_REVIEWER_1=codex:provider/model:high",
        "PANOPTICON_2119_REVIEWER_2=claude:claude-fable-5",
    )
    assert all(value in docker_run for value in rendered)
    for value in rendered:
        name = value.partition("=")[0]
        assert [item for item in docker_run if item.startswith(f"{name}=")] == [value]
        value_index = docker_run.index(value)
        assert value_index > env_file_value_index
        assert docker_run[value_index - 1] == "--env"

    empty_recorder = _Recorder()
    LocalRunner("http://svc", secrets_dir="/host/secrets", run=empty_recorder).spawn(
        "t2", env_file="r1.env"
    )
    empty_run = next(call for call in empty_recorder.calls if call[:2] == ["docker", "run"])
    assert empty_run.count("--env-file") == 1
    empty_env_value_index = empty_run.index("--env-file") + 1
    assert empty_run[empty_env_value_index] == "/host/secrets/r1.env"
    for name in (
        "PANOPTICON_2119_HONESTY_REVIEWER",
        "PANOPTICON_2119_REVIEWER_1",
        "PANOPTICON_2119_REVIEWER_2",
    ):
        value_index = empty_run.index(f"{name}=")
        assert [item for item in empty_run if item.startswith(f"{name}=")] == [f"{name}="]
        assert value_index > empty_env_value_index
        assert empty_run[value_index - 1] == "--env"

    mixed_cases = (
        (
            {"honesty_reviewer": "claude:one"},
            ("claude:one", "", ""),
        ),
        (
            {"reviewer_1": "codex:one"},
            ("", "codex:one", ""),
        ),
        (
            {"reviewer_2": "claude:two"},
            ("", "", "claude:two"),
        ),
    )
    names = (
        "PANOPTICON_2119_HONESTY_REVIEWER",
        "PANOPTICON_2119_REVIEWER_1",
        "PANOPTICON_2119_REVIEWER_2",
    )
    for index, (overrides, expected) in enumerate(mixed_cases, 3):
        mixed_recorder = _Recorder()
        LocalRunner("http://svc", secrets_dir="/host/secrets", run=mixed_recorder).spawn(
            f"t{index}",
            env_file="r1.env",
            **overrides,  # type: ignore[arg-type]
        )
        mixed_run = next(call for call in mixed_recorder.calls if call[:2] == ["docker", "run"])
        assert mixed_run.count("--env-file") == 1
        env_file_index = mixed_run.index("--env-file")
        assert mixed_run[env_file_index + 1] == "/host/secrets/r1.env"
        for name, value in zip(names, expected, strict=True):
            assignments = [item for item in mixed_run if item.startswith(f"{name}=")]
            assert assignments == [f"{name}={value}"]
            assert mixed_run.index(assignments[0]) > env_file_index

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


@pytest.mark.parametrize(
    "legacy_names",
    [
        ("PANOPTICON_2119_HONESTY_REVIEWER",),
        ("PANOPTICON_2119_REVIEWER_1",),
        ("PANOPTICON_2119_REVIEWER_2",),
        ("PANOPTICON_2119_HONESTY_REVIEWER", "PANOPTICON_2119_REVIEWER_1"),
        ("PANOPTICON_2119_HONESTY_REVIEWER", "PANOPTICON_2119_REVIEWER_2"),
        ("PANOPTICON_2119_REVIEWER_1", "PANOPTICON_2119_REVIEWER_2"),
        (
            "PANOPTICON_2119_HONESTY_REVIEWER",
            "PANOPTICON_2119_REVIEWER_1",
            "PANOPTICON_2119_REVIEWER_2",
        ),
    ],
)
def test_spawn_warns_on_first_use_then_suppresses_inert_env_file_reviewers(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    legacy_names: tuple[str, ...],
) -> None:
    # 2119: reviewer-config-surfaces.3.3
    env_file = tmp_path / "repo.env"
    secret_values = [f"secret-model-{index}" for index in range(len(legacy_names))]
    env_file.write_text(
        "GH_TOKEN=secret\n"
        + "".join(
            f"{name}={value}\n" for name, value in zip(legacy_names, secret_values, strict=True)
        )
        + f"{legacy_names[0]}=duplicate-secret-model\n"
    )
    recorder = _Recorder()
    runner = LocalRunner("http://svc", secrets_dir=tmp_path, run=recorder)
    logging.getLogger("panopticon.sessionservice.local_runner").disabled = False
    caplog.set_level(logging.WARNING)

    runner.spawn("t-warning-1", env_file="repo.env")
    expected_warning = (
        f"Repo env file {env_file} contains inert reviewer setting(s): "
        f"{', '.join(sorted(legacy_names))}. Move reviewer selection to the repo fields "
        "honesty_reviewer, reviewer_1, and reviewer_2; env-file values are ignored."
    )
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [expected_warning]
    warning = messages[0]
    assert "GH_TOKEN" not in warning
    for value in (*secret_values, "duplicate-secret-model"):
        assert value not in warning

    caplog.clear()
    runner.spawn("t-warning-2", env_file="repo.env")
    assert caplog.records == []

    other_env_file = tmp_path / "other.env"
    other_env_file.write_text(env_file.read_text())
    runner.spawn("t-warning-other-file", env_file="other.env")
    assert len(caplog.records) == 1
    assert str(other_env_file) in caplog.records[0].getMessage()

    caplog.clear()
    runner.spawn("t-warning-original-again", env_file="repo.env")
    assert caplog.records == []

    other_runner = LocalRunner("http://svc", secrets_dir=tmp_path, run=recorder)
    other_runner.spawn("t-warning-other-runner", env_file="repo.env")
    assert len(caplog.records) == 1
    assert str(env_file) in caplog.records[0].getMessage()


@pytest.mark.parametrize(
    "contents",
    [
        "GH_TOKEN=secret\n",
        "PANOPTICON_2119_REVIEWER_10=near-match\n",
        "PANOPTICON_2119_REVIEWER_1_EXTRA=near-match\n",
    ],
)
def test_spawn_does_not_warn_for_env_files_without_reviewer_transport_keys(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    contents: str,
) -> None:
    # 2119: reviewer-config-surfaces.3.3
    (tmp_path / "repo.env").write_text(contents)
    logging.getLogger("panopticon.sessionservice.local_runner").disabled = False
    caplog.set_level(logging.WARNING)

    LocalRunner("http://svc", secrets_dir=tmp_path, run=_Recorder()).spawn(
        "t-no-warning", env_file="repo.env"
    )

    assert caplog.records == []


@pytest.mark.parametrize("value", ["", "   "])
def test_spawn_warns_when_reviewer_transport_key_has_blank_value(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    value: str,
) -> None:
    # 2119: reviewer-config-surfaces.3.3
    env_file = tmp_path / "repo.env"
    env_file.write_text(f"PANOPTICON_2119_REVIEWER_1={value}\n")
    logging.getLogger("panopticon.sessionservice.local_runner").disabled = False
    caplog.set_level(logging.WARNING)

    LocalRunner("http://svc", secrets_dir=tmp_path, run=_Recorder()).spawn(
        "t-blank-reviewer", env_file="repo.env"
    )

    assert [record.getMessage() for record in caplog.records] == [
        f"Repo env file {env_file} contains inert reviewer setting(s): "
        "PANOPTICON_2119_REVIEWER_1. Move reviewer selection to the repo fields "
        "honesty_reviewer, reviewer_1, and reviewer_2; env-file values are ignored."
    ]


def test_reviewer_resolution_uses_repo_override_then_workflow_default() -> None:
    # 2119: reviewer-config-surfaces.3.2
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
    called = False

    def forbidden_run(*args: Any, **kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("invalid configuration reached a reviewer command")

    invalid_configs = (
        ReviewerConfig("pi", "model"),
        ReviewerConfig("", "model"),
        ReviewerConfig("   ", "model"),
        ReviewerConfig("claude", ""),
        ReviewerConfig("claude", "   "),
    )
    valid_overrides = (
        "claude:claude-opus-5",
        "codex:gpt-5.6-sol",
    )
    invalid_cases: list[tuple[tuple[ReviewerConfig, ReviewerConfig], dict[str, str]]] = []
    for slot in range(2):
        for invalid in invalid_configs:
            invalid_defaults = list(defaults)
            invalid_defaults[slot] = invalid
            invalid_pair = (invalid_defaults[0], invalid_defaults[1])
            invalid_cases.append((invalid_pair, {}))
            invalid_cases.append(
                (invalid_pair, {f"PANOPTICON_2119_REVIEWER_{slot + 1}": valid_overrides[slot]})
            )
        for value in (
            "claude",
            ":model",
            "   :model",
            "claude:",
            "claude:   ",
            "pi:model",
            "claudee:model",
        ):
            invalid_cases.append((defaults, {f"PANOPTICON_2119_REVIEWER_{slot + 1}": value}))
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
