"""The Textual dashboard: detail rendering (pure) + a pilot that mounts the app.

Uses a fake client (canned task dicts) so the TUI test is deterministic and offline — the
real HTTP client is covered in test_terminal.py."""

from __future__ import annotations

import contextlib
import threading
import time
from datetime import UTC, datetime, timedelta
from inspect import signature
from pathlib import Path
from typing import Any

import httpx
import pytest
from rich.text import Span, Text
from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Button, Checkbox, DataTable, Input, Label, OptionList, Select, Static

from panopticon.terminal import dashboard
from panopticon.terminal.dashboard import (
    _ENSEMBLE_KEY_PREFIX,
    Dashboard,
    SpaceCheckbox,
    _dim,
    _group_by_governor,
    _group_section,
    _make_sort_key,
    _matches,
    _repo_cell,
    _short_tokens,
    _slug_cell,
    _status_cell,
    _turn_cell,
    render_detail,
)

_TASK: dict[str, Any] = {
    "id": "task-abcdef0123",
    "repo_id": "default",
    "slug": "fix-widget",
    "state": "WORKING",
    "turn": "agent",
    "workflow": "spike",
    "provisioned": True,
    "created_at": "2026-06-22T10:00:00+00:00",
    "history": [
        {
            "at": "2026-06-22T10:00:00+00:00",
            "from_state": None,
            "to_state": "PLAN",
            "trigger": "start",
            "responsibilities": [],
        },
        {
            "at": "2026-06-22T11:00:00+00:00",
            "from_state": "PLAN",
            "to_state": "WORKING",
            "trigger": "advance",
            "responsibilities": [{"key": "tests-pass", "status": "pending"}],
        },
    ],
}


def _raise(*args: Any, **kwargs: Any) -> Any:
    """Stand in for a failing REST call (e.g. a down service)."""
    raise RuntimeError("service unavailable")


def _http_400(detail: str) -> httpx.HTTPStatusError:
    """A 400 HTTPStatusError carrying a ``{"detail": ...}`` body, as the task service returns for
    a rejected repo (e.g. a non-existent env_file). `_detail(exc)` reads that message back out."""
    request = httpx.Request("POST", "http://test/repos")
    response = httpx.Response(400, json={"detail": detail}, request=request)
    return httpx.HTTPStatusError(detail, request=request, response=response)


class _FakeClient:
    def __init__(
        self,
        tasks: list[dict[str, Any]],
        registrations: dict[str, list[dict[str, Any]]] | None = None,
        *,
        repos: list[str] | list[dict[str, Any]] | None = None,
        runners: list[dict[str, Any]] | None = None,
        workflows: list[dict[str, str]] | None = None,
        operations: dict[str, str] | None = None,
        artifacts: dict[str, list[str]] | None = None,
        artifact_content: bytes = b"",
    ) -> None:
        self._tasks = tasks
        self._registrations = registrations or {}
        self._runners: list[dict[str, Any]] = runners or []
        # repos may be bare ids (existing task-creation tests) or full dicts (repo-screen tests).
        # Unspecified (None) defaults to one repo present, so the start-up auto-open of the repo
        # screen (fired when there are *no* repos) doesn't pop over tests that don't care; pass an
        # explicit `repos=[]` to exercise the no-repos case.
        if repos is None:
            repos = [{"id": "default", "name": "default", "git_url": "", "default_base": "main"}]
        self._repos: list[dict[str, Any]] = [
            {"id": r, "name": r, "git_url": "", "default_base": "main"} if isinstance(r, str) else r
            for r in repos
        ]
        self._workflows = workflows or []
        self._operations = operations or {}
        self._artifacts = artifacts or {}
        self._artifact_content = artifact_content
        # Change-feed state for the long-poll worker: a version cursor + an event a test arms with
        # `signal_change()` to release a parked `list_tasks_versioned` (the producer "changed a task").
        self._version = 0
        self._change = threading.Event()
        self.list_tasks_calls = 0  # how many times the table was (re)built — counts feed refreshes
        self.created: list[tuple[str, str, str | None, str | None, str | None]] = []
        self.applied: list[tuple[str, str]] = []
        self.got_tasks: list[str] = []
        self.released: list[str] = []
        self.set_slugs: list[tuple[str, str]] = []
        self.snoozes: list[tuple[str, str | None]] = []
        self.created_repos: list[dict[str, Any]] = []
        self.updated_repos: list[tuple[str, dict[str, Any]]] = []
        # When set, create_repo/update_repo raise a 400 carrying this detail (mimics the task
        # service rejecting e.g. a non-existent env_file), to exercise the form's error path.
        self.repo_error: str | None = None
        self.fetched: list[tuple[str, str]] = []  # (task_id, name) passed to get_artifact

    def list_tasks(self) -> list[dict[str, Any]]:
        self.list_tasks_calls += 1
        return self._tasks

    def list_tasks_versioned(
        self, *, since: int = 0, wait: float | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        """Mimic the block-until-change feed: park until a test arms `signal_change()` (then bump
        the cursor and return) or the park elapses (return the current, unchanged cursor). `wait=None`
        is the immediate seed read the worker does before its first long-poll.

        The park is **capped** well below a real long-poll's `wait`: the worker thread blocks here,
        and the asyncio default executor is joined at loop teardown, so a multi-second park would
        stall every test's teardown. Capping keeps an idle worker cycling cheaply and teardown
        snappy while still releasing promptly on `signal_change`."""
        if wait is None:
            self.list_tasks_calls += 1
        timeout = 0.0 if wait is None else min(wait, 0.05)
        if self._change.wait(timeout=timeout):
            self._change.clear()
            self._version += 1
        return self._tasks, self._version

    def signal_change(self) -> None:
        """Release a parked long-poll once, as a task-state change would — the next
        `list_tasks_versioned` returns a bumped cursor and the worker refreshes."""
        self._change.set()

    def list_registrations(self, task_id: str) -> list[dict[str, Any]]:
        return self._registrations.get(task_id, [])

    def live_runners(self) -> list[dict[str, Any]]:
        return self._runners

    def list_artifacts(self, task_id: str) -> list[str]:
        return self._artifacts.get(task_id, [])

    def get_artifact(self, task_id: str, name: str) -> bytes:
        self.fetched.append((task_id, name))
        return self._artifact_content

    def list_repos(self) -> list[dict[str, Any]]:
        return self._repos

    def get_repo(self, repo_id: str) -> dict[str, Any]:
        return next(repo for repo in self._repos if repo["id"] == repo_id)

    def create_repo(
        self,
        repo_id: str,
        name: str,
        git_url: str,
        default_base: str = "main",
        *,
        env_file: str | None = None,
        image_layer_file: str | None = None,
        hook_file: str | None = None,
        capabilities: dict[str, Any] | None = None,
        enabled_workflows: list[str] | None = None,
        disabled_workflows: list[str] | None = None,
    ) -> dict[str, Any]:
        if self.repo_error is not None:
            raise _http_400(self.repo_error)
        repo: dict[str, Any] = {
            "id": repo_id,
            "name": name,
            "git_url": git_url,
            "default_base": default_base,
            "env_file": env_file,
            "image_layer_file": image_layer_file,
            "hook_file": hook_file,
            "enabled_workflows": enabled_workflows or [],
            "disabled_workflows": disabled_workflows or [],
        }
        if capabilities is not None:
            repo["capabilities"] = capabilities
        self.created_repos.append(repo)
        self._repos.append(repo)
        return repo

    def update_repo(self, repo_id: str, **changes: Any) -> dict[str, Any]:
        if self.repo_error is not None:
            raise _http_400(self.repo_error)
        self.updated_repos.append((repo_id, changes))
        for repo in self._repos:
            if repo["id"] == repo_id:
                repo.update(changes)
                return repo
        return {"id": repo_id, **changes}

    def list_workflows(self) -> list[dict[str, Any]]:
        return self._workflows

    def list_workflow_files(self) -> list[dict[str, Any]]:
        return self._workflows

    def list_workflows_for_repo(self, repo_id: str) -> list[dict[str, str]]:
        return self._workflows

    def list_operations(self, task_id: str) -> dict[str, str]:
        return self._operations

    def create_task(
        self,
        repo_id: str,
        workflow: str,
        memo: str | None = None,
        *,
        initial_prompt: str | None = None,
        harness: str | None = None,
        starting_model: str | None = None,
    ) -> dict[str, Any]:
        self.created.append((repo_id, workflow, memo, initial_prompt, harness, starting_model))
        return {"id": "new"}

    def apply_operation(self, task_id: str, operation: str) -> dict[str, Any]:
        self.applied.append((task_id, operation))
        return {"id": task_id}

    def get_task(self, task_id: str) -> dict[str, Any]:
        self.got_tasks.append(task_id)
        for t in self._tasks:
            if t["id"] == task_id:
                return t
        raise KeyError(task_id)

    def release(self, task_id: str) -> dict[str, Any]:
        self.released.append(task_id)
        self._registrations.pop(task_id, None)
        for t in self._tasks:  # reflect the unclaim in list_tasks (as the real service does)
            if t["id"] == task_id:
                t["claimed_by"] = None
        return {"id": task_id, "claimed_by": None}

    def set_slug(self, task_id: str, slug: str) -> dict[str, Any]:
        self.set_slugs.append((task_id, slug))
        for task in self._tasks:
            if task["id"] == task_id:
                task["slug"] = slug
                return task
        raise KeyError(task_id)

    def set_snooze(self, task_id: str, until: str | None) -> dict[str, Any]:
        self.snoozes.append((task_id, until))
        for task in self._tasks:
            if task["id"] == task_id:
                task["snoozed_until"] = until
                return task
        raise KeyError(task_id)


class _SuggestionHarness:
    """Controllable harness discovery fake for the memo modal's Pilot tests."""

    field_label = "model"

    def __init__(
        self,
        name: str,
        *,
        delay: float = 0.0,
        release: threading.Event | None = None,
        fail_models: bool = False,
        fail_efforts: bool = False,
        multiple: bool = False,
    ) -> None:
        self.name = name
        self.delay = delay
        self.release = release
        self.fail_models = fail_models
        self.fail_efforts = fail_efforts
        self.multiple = multiple
        self.field_label = f"{name} model"
        self.started = threading.Event()
        self.model_calls = 0
        self.effort_calls = 0
        self.effort_models: list[str | None] = []

    def suggested_models(self) -> tuple[tuple[str, str], ...]:
        self.model_calls += 1
        call = self.model_calls
        self.started.set()
        if self.release is not None:
            self.release.wait()
        if self.delay:
            time.sleep(self.delay)
        if self.fail_models:
            raise RuntimeError(f"{self.name} discovery failed")
        suggestions = [(f"{self.name}-model-{call}", f"{self.name} model {call}")]
        if self.multiple:
            suggestions.append((f"{self.name}-alternate-{call}", f"{self.name} alternate {call}"))
        return tuple(suggestions)

    def suggested_efforts(self, model: str | None = None) -> tuple[tuple[str, str], ...]:
        self.effort_calls += 1
        call = self.effort_calls
        self.effort_models.append(model)
        if self.delay:
            time.sleep(self.delay)
        if self.fail_efforts:
            raise RuntimeError(f"{self.name} effort discovery failed")
        model_tag = "none" if model is None else model or "empty"
        suggestions = [(f"{self.name}-{model_tag}-effort-{call}", f"{self.name} effort {call}")]
        if self.multiple:
            suggestions.append((f"{self.name}-alternate-effort-{call}", f"{self.name} alt effort"))
        return tuple(suggestions)


async def _open_memo(pilot: Any) -> None:
    await pilot.press("n", "enter", "enter")
    await pilot.pause()


async def _wait_for(pilot: Any, predicate: Any) -> None:
    for _ in range(100):
        if predicate():
            await pilot.pause()
            return
        await pilot.pause(0.01)
    raise AssertionError("condition did not become true")


async def _wait_for_suggestion(pilot: Any, input_widget: Input, expected: str) -> OptionList:
    field = "model" if input_widget.id == "launch-model" else "effort"
    options = input_widget.screen.query_one(f"#launch-{field}-options", OptionList)
    input_widget.focus()
    for _ in range(100):
        screen = input_widget.screen
        prompts = _option_prompts(options)
        if expected in screen._candidate_values[field] and any(
            prompt == expected or prompt.startswith(f"{expected} — ") for prompt in prompts
        ):
            return options
        await pilot.pause(0.01)
    raise AssertionError(f"suggestion {expected!r} did not become available")


def test_render_detail_shows_state_turn_and_history() -> None:
    text = render_detail(_TASK)
    assert "fix-widget" in text
    assert "state: WORKING" in text and "turn: agent" in text
    assert "∅ → PLAN (start)" in text
    assert "PLAN → WORKING (advance)" in text
    assert "tests-pass=pending" in text


def test_render_detail_shows_the_id() -> None:
    assert "id: task-abcdef0123" in render_detail(_TASK)


def test_render_detail_shows_the_memo() -> None:
    assert "make the widget green" not in render_detail(_TASK)
    text = render_detail({**_TASK, "memo": "make the widget green"})
    assert "make the widget green" in text


def test_render_detail_shows_the_url() -> None:
    assert "url:" not in render_detail(_TASK)
    text = render_detail({**_TASK, "url": "https://github.com/acme/widgets/pull/7"})
    assert "url: https://github.com/acme/widgets/pull/7" in text


def test_render_detail_is_plain_text_with_brackets_literal() -> None:
    # The detail must be plain (the caller wraps it in Text and renders literally) — never markup.
    task = {
        **_TASK,
        "memo": "do [the thing]",
        "lifecycle_detail": "docker run ['--add-host', '--privileged']",
    }
    out = render_detail({**task, "container_status": "failed"})
    assert (
        "['--add-host', '--privileged']" in out and "[the thing]" in out
    )  # brackets kept verbatim


async def test_dashboard_detail_survives_a_bracketed_lifecycle_detail() -> None:
    # Regression: a docker-command lifecycle_detail (a Python list repr with "[") rendered through
    # Textual's markup parser raised MarkupError and crashed the *whole dashboard* on startup, so
    # `make start` looked broken. The detail pane must render it literally (wrapped in Text).
    task = {
        **_TASK,
        "container_status": "failed",
        "lifecycle_detail": "Command ['docker','run','--add-host','--privileged'] returned 1",
    }
    app = Dashboard(_FakeClient([task]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:  # would raise here if the detail crashed the app
        await pilot.pause()
        await pilot.press("d")  # open the detail pane to trigger the fetch
        await pilot.pause()
        assert "--add-host" in str(
            app.query_one("#detail", Static).render()
        )  # rendered, didn't crash


def test_render_detail_shows_the_tokens_used() -> None:
    assert "tokens (wt):" not in render_detail(_TASK)  # both absent → no line
    assert "tokens (wt): 1.2K used / - est" in render_detail({**_TASK, "tokens_used": 1234})
    # the estimate alone (no usage yet) still renders the line
    assert "tokens (wt): - used / 500.0K est" in render_detail({**_TASK, "token_estimate": 500000})


def test_render_detail_marks_blocked() -> None:
    assert "(blocked)" not in render_detail(_TASK)
    assert "turn: agent (blocked)" in render_detail({**_TASK, "blocked": True})


def test_short_tokens_formats_human_short() -> None:
    assert _short_tokens(None) == "-"  # not yet reported
    assert _short_tokens(0) == "-"
    assert _short_tokens(300) == "300"  # under 1000 verbatim
    assert _short_tokens(1234) == "1.2K"
    assert _short_tokens(1_100_000) == "1.1M"
    assert _short_tokens(2_500_000_000) == "2.5B"


def test_turn_cell_color_codes_like_cloude_cade() -> None:
    # agent=green, user attention=orange, blocked=red (blocked wins).
    agent = _turn_cell(_TASK)
    assert agent.plain == "agent" and agent.style == "green"
    user = _turn_cell({**_TASK, "turn": "user"})
    assert user.plain == "user" and "orange" in str(user.style)
    blocked = _turn_cell({**_TASK, "blocked": True})
    assert blocked.plain == "agent ⚠" and blocked.style == "red"


# 2119: REQ-026.3.1
# 2119: REQ-026.3.2
# 2119: REQ-026.3.3
# 2119: REQ-026.3.4
# 2119: REQ-026.3.5
# 2119: REQ-026.6.1
# 2119: REQ-026.6.8
async def test_dependency_held_cells_details_and_pre_spawn_row_are_honest() -> None:
    live = {
        **_TASK,
        "id": "a-live-dependent",
        "slug": "synthesize-live",
        "turn": "user",
        "attention": False,
        "container_status": "live",
        "depends_on_task_ids": ["c-active", "d-complete", "e-dropped"],
    }
    gated = {
        **_TASK,
        "id": "b-gated-dependent",
        "slug": "synthesize-new",
        "turn": "user",
        "attention": False,
        "claimed_by": None,
        "container_status": "gated",
        "depends_on_task_ids": ["c-active", "d-complete", "e-dropped"],
    }
    dropped_only = {
        **_TASK,
        "id": "aa-dropped-only-dependent",
        "slug": "replace-missing-auditor",
        "turn": "user",
        "attention": False,
        "container_status": "gated",
        "depends_on_task_ids": ["e-dropped"],
    }
    escalated = {
        **_TASK,
        "id": "ab-escalated-dependent",
        "slug": "needs-user-while-waiting",
        "turn": "user",
        "attention": True,
        "container_status": "gated",
        "depends_on_task_ids": ["c-active"],
    }
    blocked_wait = {
        **_TASK,
        "id": "ac-blocked-dependent",
        "slug": "blocked-while-waiting",
        "turn": "user",
        "blocked": True,
        "attention": False,
        "container_status": "gated",
        "depends_on_task_ids": ["c-active"],
    }
    blocked_attention_wait = {
        **_TASK,
        "id": "ad-blocked-attention-dependent",
        "slug": "blocked-and-escalated",
        "turn": "user",
        "blocked": True,
        "attention": True,
        "container_status": "gated",
        "depends_on_task_ids": ["c-active"],
    }
    agent_wait = {
        **_TASK,
        "id": "ae-agent-dependent",
        "slug": "approved-but-gated",
        "turn": "agent",
        "attention": False,
        "container_status": "gated",
        "depends_on_task_ids": ["c-active"],
    }
    custom_terminal_dependency = {
        **_TASK,
        "id": "af-custom-terminal-dependent",
        "slug": "uses-archived-audit",
        "turn": "user",
        "attention": False,
        "depends_on_task_ids": ["z-archived"],
    }
    no_wait_tasks = [
        {
            **_TASK,
            "id": "f-no-wait-unset",
            "slug": "ordinary-user-turn",
            "turn": "user",
        },
        {
            **_TASK,
            "id": "g-no-wait-cleared",
            "slug": "ordinary-cleared",
            "turn": "user",
            "attention": False,
        },
        {
            **_TASK,
            "id": "h-no-wait-set",
            "slug": "ordinary-escalated",
            "turn": "user",
            "attention": True,
        },
    ]
    dependencies = [
        {
            **_TASK,
            "id": "c-active",
            "slug": "auditor-active",
            "state": "ITERATING",
            "governor_task_id": "a-live-dependent",
        },
        {**_TASK, "id": "d-complete", "slug": "auditor-done", "state": "COMPLETE"},
        {**_TASK, "id": "e-dropped", "slug": "auditor-missing", "state": "DROPPED"},
        {
            **_TASK,
            "id": "z-archived",
            "slug": "auditor-archived",
            "state": "ARCHIVED",
            "terminal": True,
        },
    ]
    fake = _FakeClient(
        [
            live,
            dropped_only,
            escalated,
            blocked_wait,
            blocked_attention_wait,
            agent_wait,
            custom_terminal_dependency,
            gated,
            *dependencies,
            *no_wait_tasks,
        ]
    )
    app = Dashboard(fake)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)

        assert "b-gated-dependent" in {str(key.value) for key in table.rows}
        expected_turns = {
            "a-live-dependent": "held 1",
            "ae-agent-dependent": "held",
            "b-gated-dependent": "held",
        }
        for task_id, expected in expected_turns.items():
            turn_cell = table.get_row(task_id)[1]
            assert turn_cell.plain == expected
            assert str(turn_cell.style) == "dim"
            assert "yellow" not in str(turn_cell.style) and "orange" not in str(turn_cell.style)

        attention_rows = [
            "aa-dropped-only-dependent",
            "ab-escalated-dependent",
            "af-custom-terminal-dependent",
            *(task["id"] for task in no_wait_tasks),
        ]
        for task_id in attention_rows:
            turn_cell = table.get_row(task_id)[1]
            assert turn_cell.plain == "user"
            assert str(turn_cell.style) == "dark_orange"

        blocked_turn = table.get_row("ac-blocked-dependent")[1]
        assert blocked_turn.plain == "user ⚠"
        assert str(blocked_turn.style) == "red"

        blocked_attention_turn = table.get_row("ad-blocked-attention-dependent")[1]
        assert blocked_attention_turn.plain == "user ⚠"
        assert str(blocked_attention_turn.style) == "red"

        await pilot.press("d")
        await pilot.pause()
        detail = str(app.query_one("#detail", Static).render())
        assert "waiting on 2/3" in detail
        assert "auditor-active ITERATING" in detail
        assert "auditor-done COMPLETE" in detail
        assert "auditor-missing DROPPED" in detail
        assert "edit dependencies or drop the dependent" in detail

    assert (
        live["turn"] == "user"
        and gated["turn"] == "user"
        and dropped_only["turn"] == "user"
        and agent_wait["turn"] == "agent"
    )


# 2119: REQ-026.5.1
# 2119: REQ-026.5.2
# 2119: REQ-026.5.3
# 2119: REQ-026.6.1
# 2119: REQ-026.6.8
async def test_governor_held_cell_tracks_active_children_and_reverts_when_they_finish() -> None:
    waiting_governor = {
        **_TASK,
        "id": "a-waiting-governor",
        "slug": "audit-governor",
        "turn": "user",
        "attention": False,
        "container_status": "live",
    }
    active_child = {
        **_TASK,
        "id": "b-active-child",
        "slug": "security-auditor",
        "state": "ITERATING",
        "governor_task_id": "a-waiting-governor",
    }
    second_active_child = {
        **_TASK,
        "id": "bb-second-active-child",
        "slug": "performance-auditor",
        "state": "WORKING",
        "governor_task_id": "a-waiting-governor",
    }
    finished_child = {
        **_TASK,
        "id": "c-finished-child",
        "slug": "docs-auditor",
        "state": "COMPLETE",
        "governor_task_id": "a-waiting-governor",
    }
    finished_governor = {
        **_TASK,
        "id": "d-finished-governor",
        "slug": "done-governor",
        "turn": "user",
        "attention": False,
        "container_status": "live",
    }
    only_finished_child = {
        **_TASK,
        "id": "e-only-finished-child",
        "slug": "finished-auditor",
        "state": "ARCHIVED",
        "terminal": True,
        "governor_task_id": "d-finished-governor",
    }
    agent_finished_governor = {
        **_TASK,
        "id": "da-agent-finished-governor",
        "slug": "agent-done-governor",
        "turn": "agent",
        "attention": False,
        "container_status": "live",
    }
    agent_finished_child = {
        **_TASK,
        "id": "db-agent-finished-child",
        "slug": "agent-finished-auditor",
        "state": "COMPLETE",
        "governor_task_id": "da-agent-finished-governor",
    }
    escalated_governor = {
        **_TASK,
        "id": "f-escalated-governor",
        "slug": "question-from-governor",
        "turn": "user",
        "attention": True,
        "container_status": "live",
    }
    escalated_child = {
        **_TASK,
        "id": "g-escalated-child",
        "slug": "still-running-auditor",
        "state": "ITERATING",
        "governor_task_id": "f-escalated-governor",
    }
    blocked_governor = {
        **_TASK,
        "id": "h-blocked-governor",
        "slug": "blocked-governor",
        "turn": "user",
        "blocked": True,
        "attention": False,
        "container_status": "live",
    }
    blocked_governor_child = {
        **_TASK,
        "id": "i-blocked-governor-child",
        "slug": "blocked-governor-auditor",
        "state": "ITERATING",
        "governor_task_id": "h-blocked-governor",
    }
    app = Dashboard(
        _FakeClient(
            [
                waiting_governor,
                active_child,
                second_active_child,
                finished_child,
                finished_governor,
                only_finished_child,
                agent_finished_governor,
                agent_finished_child,
                escalated_governor,
                escalated_child,
                blocked_governor,
                blocked_governor_child,
            ]
        )
    )  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)

        waiting_turn = table.get_row("a-waiting-governor")[1]
        assert waiting_turn.plain == "held 2"
        assert str(waiting_turn.style) == "dim"
        assert "yellow" not in str(waiting_turn.style) and "orange" not in str(waiting_turn.style)

        finished_turn = table.get_row("d-finished-governor")[1]
        assert finished_turn.plain == "user"
        assert str(finished_turn.style) == "dark_orange"

        agent_finished_turn = table.get_row("da-agent-finished-governor")[1]
        assert agent_finished_turn.plain == "agent"
        assert str(agent_finished_turn.style) == "green"

        escalated_turn = table.get_row("f-escalated-governor")[1]
        assert escalated_turn.plain == "user"
        assert str(escalated_turn.style) == "dark_orange"

        blocked_turn = table.get_row("h-blocked-governor")[1]
        assert blocked_turn.plain == "user ⚠"
        assert str(blocked_turn.style) == "red"

        await pilot.press("d")
        await pilot.pause()
        detail = str(app.query_one("#detail", Static).render())
        assert "security-auditor ITERATING" in detail
        assert "performance-auditor WORKING" in detail
        assert "docs-auditor COMPLETE" not in detail
        assert "still-running-auditor ITERATING" not in detail
        assert "blocked-governor-auditor ITERATING" not in detail


async def test_dashboard_mounts_lists_tasks_and_shows_detail() -> None:
    app = Dashboard(_FakeClient([_TASK]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        assert table.row_count == 1
        # detail pane is hidden by default — open it, then check content
        await pilot.press("d")
        await pilot.pause()
        detail = app.query_one("#detail", Static)
        assert "WORKING" in str(detail.render())
        assert "created:" in str(detail.render())
        assert "2026-06-22T10:00:00+00:00" not in str(detail.render())


# 2119: full-width-dashboard.1.1
async def test_vertically_overflowing_dashboard_uses_the_full_table_width() -> None:
    tasks = [
        {**_TASK, "id": f"overflow-{index:02}", "slug": f"overflow-{index:02}"}
        for index in range(30)
    ]
    app = Dashboard(_FakeClient(tasks), refresh_interval=None)  # type: ignore[arg-type]

    async with app.run_test(size=(80, 12)) as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)

        assert table.virtual_size.height > table.scrollable_content_region.height
        assert table.scrollbar_size_vertical == 0
        assert table.scrollable_content_region.width == table.content_region.width


# 2119: REQ-053.1.1
# 2119: REQ-053.2.1
# 2119: REQ-053.3.1
async def test_vertical_task_overflow_indicators_show_only_available_directions() -> None:
    tasks = [
        {**_TASK, "id": f"overflow-{index:02}", "slug": f"overflow-{index:02}"}
        for index in range(30)
    ]
    app = Dashboard(_FakeClient(tasks), refresh_interval=None)  # type: ignore[arg-type]

    async with app.run_test(size=(80, 12)) as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)

        def table_line(screen_y: int) -> str:
            strips = app.screen._compositor.render_strips()
            line = "".join(segment.text for segment in strips[screen_y])
            return line[table.scrollable_content_region.x : table.scrollable_content_region.right]

        def rendered_table() -> str:
            strips = app.screen._compositor.render_strips()
            return "\n".join(
                "".join(segment.text for segment in strip)[table.region.x : table.region.right]
                for strip in strips[table.region.y : table.region.bottom]
            )

        def assert_composited(line: str, row_id: str, message: str) -> None:
            marker_start = len(line) - len(message)
            assert line.endswith(message)
            assert line.index("WORKING") < marker_start
            assert line.index("agent") < marker_start
            assert line.index("default") < marker_start
            assert line.index(row_id) < marker_start

        first_task_line = table.scrollable_content_region.y + table.header_height
        last_task_line = table.scrollable_content_region.bottom - 1
        assert table.scroll_y == 0
        assert_composited(table_line(last_task_line), "overflow-08", "↓ more")
        assert "↑ more" not in rendered_table()

        for _ in range(9):
            await pilot.press("down")
        await pilot.pause()
        assert table.scroll_y == 1
        assert_composited(table_line(first_task_line), "overflow-01", "↑ more")
        assert_composited(table_line(last_task_line), "overflow-09", "↓ more")

        for _ in range(table.row_count):
            await pilot.press("down")
        await pilot.pause()
        assert table.scroll_y == table.max_scroll_y
        assert_composited(table_line(first_task_line), "overflow-21", "↑ more")
        assert "↓ more" not in rendered_table()


# 2119: REQ-053.3.1
async def test_vertical_task_overflow_indicators_disappear_when_everything_fits() -> None:
    app = Dashboard(_FakeClient([_TASK]), refresh_interval=None)  # type: ignore[arg-type]

    async with app.run_test(size=(80, 12)) as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        strips = app.screen._compositor.render_strips()
        rendered_table = "\n".join(
            "".join(segment.text for segment in strip)[table.region.x : table.region.right]
            for strip in strips[table.region.y : table.region.bottom]
        )

        assert table.virtual_size.height <= table.scrollable_content_region.height
        assert "↑ more" not in rendered_table
        assert "↓ more" not in rendered_table


# 2119: REQ-053.4.1
async def test_vertical_task_overflow_indicator_preserves_horizontal_scrollbar_line() -> None:
    tasks = [
        {**_TASK, "id": f"overflow-{index:02}", "slug": f"overflow-{index:02}"}
        for index in range(30)
    ]
    def scrollbar_cells(strip: object) -> tuple[tuple[str, bool, object], ...]:
        return tuple(
            (
                character,
                bool(segment.style and segment.style.reverse),
                (segment.style.meta or {}).get("@mouse.down")
                if segment.style is not None
                else None,
            )
            for segment in strip  # type: ignore[union-attr]
            for character in segment.text
        )

    baseline_app = Dashboard(
        _FakeClient(tasks[:1]), refresh_interval=None  # type: ignore[arg-type]
    )
    async with baseline_app.run_test(size=(35, 12)) as pilot:
        await pilot.pause()
        baseline_table = baseline_app.query_one("#tasks", DataTable)
        assert baseline_table.virtual_size.width > baseline_table.scrollable_content_region.width
        assert baseline_table.max_scroll_x > 0
        baseline_table.scroll_to(x=baseline_table.max_scroll_x, animate=False, force=True)
        await pilot.pause()
        baseline_strips = baseline_app.screen._compositor.render_strips()
        baseline_scrollbar = baseline_strips[baseline_table.scrollable_content_region.bottom]
        baseline_cells = scrollbar_cells(baseline_scrollbar)
        assert baseline_table.show_horizontal_scrollbar
        baseline_scrollbar_size = baseline_table.scrollbar_size_horizontal
        assert baseline_scrollbar_size == 1
        assert baseline_table.virtual_size.height <= baseline_table.scrollable_content_region.height

    app = Dashboard(_FakeClient(tasks), refresh_interval=None)  # type: ignore[arg-type]

    async with app.run_test(size=(35, 12)) as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        assert table.max_scroll_x == baseline_table.max_scroll_x
        table.scroll_to(x=table.max_scroll_x, animate=False, force=True)
        await pilot.pause()
        strips = app.screen._compositor.render_strips()
        scrollbar_y = table.scrollable_content_region.bottom
        scrollbar = strips[scrollbar_y]
        scrollbar_text = "".join(segment.text for segment in scrollbar)

        grab_segments = [
            segment
            for segment in scrollbar
            if segment.style is not None
            and segment.style.meta is not None
            and segment.style.meta.get("@mouse.down") == "grab"
        ]

        assert table.show_horizontal_scrollbar
        assert table.scrollbar_size_horizontal == baseline_scrollbar_size
        assert scrollbar_cells(scrollbar) == baseline_cells
        assert grab_segments
        assert any(segment.text.strip() or segment.style.reverse for segment in grab_segments)
        assert "↑ more" not in scrollbar_text
        assert "↓ more" not in scrollbar_text
        assert "↓ more" in "\n".join(
            "".join(segment.text for segment in strip)
            for strip in strips[table.region.y : table.region.bottom]
        )
        assert table.scrollable_content_region.width == table.content_region.width


# 2119: full-width-dashboard.1.2
async def test_zero_width_vertical_scrollbar_preserves_keyboard_task_navigation() -> None:
    tasks = [
        {**_TASK, "id": f"overflow-{index:02}", "slug": f"overflow-{index:02}"}
        for index in range(30)
    ]
    app = Dashboard(_FakeClient(tasks), refresh_interval=None)  # type: ignore[arg-type]

    async with app.run_test(size=(80, 12)) as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        for expected_row in range(table.row_count):
            if expected_row:
                await pilot.press("down")
            assert table.cursor_row == expected_row
            assert table.window_region.contains_region(table._get_row_region(expected_row))

        assert table.scrollbar_size_vertical == 0
        assert table.scroll_y > 0


# 2119: REQ-053.5.1
async def test_keyboard_navigation_reaches_every_row_with_overflow_indicators() -> None:
    tasks = [
        {**_TASK, "id": f"overflow-{index:02}", "slug": f"overflow-{index:02}"}
        for index in range(30)
    ]
    app = Dashboard(_FakeClient(tasks), refresh_interval=None)  # type: ignore[arg-type]

    async with app.run_test(size=(80, 12)) as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        assert table.row_count == len(tasks)
        assert {str(key.value) for key in table.rows} == {task["id"] for task in tasks}

        def rendered_table() -> str:
            strips = app.screen._compositor.render_strips()
            return "\n".join(
                "".join(segment.text for segment in strip)[table.region.x : table.region.right]
                for strip in strips[table.region.y : table.region.bottom]
            )

        def task_id_style(task_id: str) -> Any:
            return next(
                (
                    segment.style
                    for strip in app.screen._compositor.render_strips()[
                        table.scrollable_content_region.y : table.scrollable_content_region.bottom
                    ]
                    for segment in strip
                    if task_id in segment.text
                ),
                None,
            )

        initial_cursor_style = task_id_style(tasks[0]["id"])
        initial_plain_style = task_id_style(tasks[1]["id"])
        assert initial_cursor_style is not None
        assert initial_plain_style is not None
        assert initial_cursor_style.bgcolor != initial_plain_style.bgcolor
        cursor_bgcolor = initial_cursor_style.bgcolor

        for expected_row in range(table.row_count):
            if expected_row < table.row_count - 1:
                assert "↓ more" in rendered_table()
            if expected_row:
                await pilot.press("down")
            assert table.cursor_row == expected_row
            rendered = rendered_table()
            assert tasks[expected_row]["id"] in rendered
            selected_style = task_id_style(tasks[expected_row]["id"])
            assert selected_style is not None
            assert selected_style.bgcolor == cursor_bgcolor

        assert table.cursor_row == table.row_count - 1
        assert table.scroll_y == table.max_scroll_y

        for expected_row in reversed(range(table.row_count)):
            if expected_row < table.row_count - 1:
                await pilot.press("up")
            assert table.cursor_row == expected_row
            assert tasks[expected_row]["id"] in rendered_table()
            selected_style = task_id_style(tasks[expected_row]["id"])
            assert selected_style is not None
            assert selected_style.bgcolor == cursor_bgcolor
            if expected_row:
                assert "↑ more" in rendered_table()

        assert table.cursor_row == 0
        assert table.scroll_y == 0


# 2119: REQ-053.6.1
async def test_every_task_row_is_renderable_at_every_vertical_scroll_offset() -> None:
    governor = {
        **_TASK,
        "id": "overflow-governor",
        "slug": "overflow-governor",
        "governor_task_id": None,
    }
    children = [
        {
            **_TASK,
            "id": f"overflow-child-{index}",
            "slug": f"overflow-child-{index}",
            "governor_task_id": governor["id"],
        }
        for index in range(2)
    ]
    standalone = [
        {**_TASK, "id": f"overflow-{index:02}", "slug": f"overflow-{index:02}"}
        for index in range(28)
    ]
    tasks = [governor, *children, *standalone]
    app = Dashboard(_FakeClient(tasks), refresh_interval=None)  # type: ignore[arg-type]

    async with app.run_test(size=(80, 12)) as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        assert any(
            str(row.key.value).startswith(_ENSEMBLE_KEY_PREFIX) for row in table.ordered_rows
        )
        first_task_line = table.scrollable_content_region.y + table.header_height
        visible_task_lines = table.scrollable_content_region.height - table.header_height

        for scroll_y in range(round(table.max_scroll_y) + 1):
            table.scroll_to(y=scroll_y, animate=False, force=True)
            await pilot.pause()
            strips = app.screen._compositor.render_strips()
            for line_offset in range(visible_task_lines):
                row_index = scroll_y + line_offset
                if row_index >= table.row_count:
                    break
                row_key = str(table.ordered_rows[row_index].key.value)
                if row_key.startswith(_ENSEMBLE_KEY_PREFIX):
                    continue
                rendered_line = "".join(
                    segment.text for segment in strips[first_task_line + line_offset]
                )[table.region.x : table.region.right]
                assert row_key in rendered_line


async def test_detail_pane_shows_copy_key_hint_without_changing_copyable_detail() -> None:
    # 2119: REQ-007.2.1
    # 2119: REQ-007.3.1
    hint = "c: copy details  y: copy slug  Shift+Y: copy id"
    assert hint not in render_detail(_TASK)
    assert hint not in render_detail(_TASK).splitlines()
    app = Dashboard(_FakeClient([_TASK]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        detail = app.query_one("#detail", Static)
        assert detail.styles.display == "block"
        assert detail.region.width > 0
        rendered = detail.render()
        assert rendered.plain.endswith(f"\n{hint}")  # type: ignore[union-attr]
        hint_start = len(rendered.plain) - len(hint)  # type: ignore[union-attr]
        assert any(
            span.start == hint_start
            and span.end == len(rendered.plain)
            and getattr(span.style, "dim", False)
            for span in rendered.spans  # type: ignore[union-attr]
        )


async def test_detail_pane_is_hidden_by_default() -> None:
    # the detail pane starts hidden so the task table gets the full width; `d` reveals it.
    app = Dashboard(_FakeClient([_TASK]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        detail = app.query_one("#detail", Static)
        assert not app._detail_visible and detail.styles.display == "none"


async def test_pressing_d_toggles_the_detail_pane() -> None:
    # `d` reveals the (hidden-by-default) detail pane and hides it again.
    app = Dashboard(_FakeClient([_TASK]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        detail = app.query_one("#detail", Static)
        assert not app._detail_visible and detail.styles.display == "none"
        await pilot.press("d")  # show
        await pilot.pause()
        assert app._detail_visible and detail.styles.display == "block"
        await pilot.press("d")  # hide again
        await pilot.pause()
        assert not app._detail_visible and detail.styles.display == "none"


async def test_tasks_are_sorted_active_then_terminal_in_creation_order() -> None:
    # Active tasks: user turn first (operator action needed), then by created_at descending.
    # Terminal tasks: agent turn first (task just finished), then by updated_at descending.
    # In this fixture t-active-1 is user-turn, so it leads despite being the oldest.
    tasks = [
        {
            **_TASK,
            "id": "t-term-2",
            "slug": "done",
            "state": "COMPLETE",
            "turn": "user",
            "created_at": "2026-06-01T01:00:00",
            "updated_at": "2026-06-01T02:00:00",
        },
        {
            **_TASK,
            "id": "t-term-1",
            "slug": "drop",
            "state": "DROPPED",
            "turn": "agent",
            "created_at": "2026-06-01T02:00:00",
            "updated_at": "2026-06-01T03:00:00",
        },
        {
            **_TASK,
            "id": "t-active-3",
            "slug": "charlie",
            "turn": "agent",
            "created_at": "2026-06-01T03:00:00",
        },
        {
            **_TASK,
            "id": "t-active-1",
            "slug": "alpha",
            "turn": "user",
            "created_at": "2026-06-01T01:00:00",
        },
        {
            **_TASK,
            "id": "t-active-2",
            "slug": "bravo",
            "turn": "agent",
            "created_at": "2026-06-01T02:00:00",
        },
    ]
    app = Dashboard(_FakeClient(tasks))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        keys = [str(k.value) for k in table.rows]
        assert keys == [
            "t-active-1",
            "t-active-3",
            "t-active-2",  # active: user turn first, then newest created_at first
            "t-term-1",
            "t-term-2",  # terminal: newest updated_at first (t-term-1 updated 03:00 > 02:00)
        ]


async def test_sort_uses_creation_order_within_section() -> None:
    # Within the same turn-priority tier, created_at descending is the primary sort (newest first).
    # Falls back to updated_at when created_at is absent (pre-migration rows).
    tasks = [
        {
            **_TASK,
            "id": "t-old",
            "slug": "zebra",
            "turn": "user",
            "created_at": "2026-06-01T00:00:00",
        },
        {
            **_TASK,
            "id": "t-new",
            "slug": "alpha",
            "turn": "user",
            "created_at": "2026-06-25T00:00:00",
        },
    ]
    app = Dashboard(_FakeClient(tasks))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        order = [str(k.value) for k in app.query_one("#tasks", DataTable).rows]
        assert order == ["t-new", "t-old"]  # newer first, despite "alpha" < "zebra"


async def test_sort_breaks_ties_on_id() -> None:
    # Same terminal-ness and created_at → fall back to id for a stable order.
    tasks = [
        {**_TASK, "id": "t2", "slug": "zebra", "turn": "user"},
        {**_TASK, "id": "t1", "slug": "alpha", "turn": "user"},
    ]
    app = Dashboard(_FakeClient(tasks))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        order = [str(k.value) for k in app.query_one("#tasks", DataTable).rows]
        assert order == ["t1", "t2"]  # t1 < t2 by id


# 2119: REQ-038.1.1
@pytest.mark.parametrize("by_updated", [False, True])
async def test_snoozed_tasks_sort_after_active_and_before_terminal_in_both_modes(
    by_updated: bool,
) -> None:
    now = datetime(2026, 7, 31, 8, 0, tzinfo=UTC)
    tasks = [
        {
            **_TASK,
            "id": "complete",
            "state": "COMPLETE",
            "snoozed_until": "2026-07-31T12:00:00+00:00",
            "updated_at": "2026-07-31T08:00:00+00:00",
        },
        {
            **_TASK,
            "id": "dropped",
            "state": "DROPPED",
            "snoozed_until": "9999-12-31T23:59:59+00:00",
            "updated_at": "2026-07-31T07:00:00+00:00",
        },
        {
            **_TASK,
            "id": "snoozed-finite",
            "snoozed_until": "2026-07-31T12:00:00+00:00",
            "created_at": "2026-07-31T09:00:00+00:00",
            "updated_at": "2026-07-31T09:00:00+00:00",
        },
        {
            **_TASK,
            "id": "snoozed-iterating",
            "state": "ITERATING",
            "snoozed_until": "2026-07-31T12:00:00+00:00",
            "created_at": "2026-07-31T08:00:00+00:00",
            "updated_at": "2026-07-31T08:00:00+00:00",
        },
        {
            **_TASK,
            "id": "snoozed-plan",
            "state": "PLAN",
            "snoozed_until": "2026-07-31T12:00:00+00:00",
            "created_at": "2026-07-31T07:00:00+00:00",
            "updated_at": "2026-07-31T07:00:00+00:00",
        },
        {
            **_TASK,
            "id": "ordinary",
            "created_at": "2026-07-31T01:00:00+00:00",
            "updated_at": "2026-07-31T03:00:00+00:00",
        },
        {
            **_TASK,
            "id": "snoozed-indefinite",
            "snoozed_until": "9999-12-31T23:59:59+00:00",
            "created_at": "2026-07-31T10:00:00+00:00",
            "updated_at": "2026-07-31T10:00:00+00:00",
        },
        {
            **_TASK,
            "id": "expired-at-deadline",
            "snoozed_until": "2026-07-31T08:00:00+00:00",
            "created_at": "2026-07-31T03:00:00+00:00",
            "updated_at": "2026-07-31T01:00:00+00:00",
        },
        {
            **_TASK,
            "id": "strictly-expired",
            "snoozed_until": "2026-07-31T07:59:59+00:00",
            "created_at": "2026-07-31T00:30:00+00:00",
            "updated_at": "2026-07-31T03:30:00+00:00",
        },
        {
            **_TASK,
            "id": "pierced",
            "attention": True,
            "snoozed_until": "2026-07-31T12:00:00+00:00",
            "created_at": "2026-07-31T02:00:00+00:00",
            "updated_at": "2026-07-31T02:00:00+00:00",
        },
    ]
    app = Dashboard(_FakeClient(tasks), now=lambda: now)  # type: ignore[arg-type]
    app._sort_by_updated = by_updated
    async with app.run_test() as pilot:
        await pilot.pause()
        order = [str(key.value) for key in app.query_one("#tasks", DataTable).rows]
        ordinary_order = (
            ["strictly-expired", "ordinary", "pierced", "expired-at-deadline"]
            if by_updated
            else ["expired-at-deadline", "pierced", "ordinary", "strictly-expired"]
        )
        assert order == [
            *ordinary_order,
            "snoozed-indefinite",
            "snoozed-finite",
            "snoozed-iterating",
            "snoozed-plan",
            "complete",
            "dropped",
        ]


# -- active/terminal dim styling ---------------------------------------------------

_ACTIVE_A = {**_TASK, "id": "t-a", "slug": "alpha", "state": "WORKING", "turn": "user"}
_ACTIVE_B = {**_TASK, "id": "t-b", "slug": "bravo", "state": "ITERATING", "turn": "user"}
_TERM_A = {**_TASK, "id": "t-done", "slug": "done", "state": "COMPLETE", "turn": "user"}
_TERM_B = {**_TASK, "id": "t-drop", "slug": "dropped", "state": "DROPPED", "turn": "user"}


async def test_terminal_tasks_are_faded() -> None:
    # Terminal tasks come after active ones and all their cells carry dim styling.
    app = Dashboard(_FakeClient([_ACTIVE_A, _TERM_A, _ACTIVE_B, _TERM_B]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        keys = [str(k.value) for k in table.rows]
        assert keys == ["t-a", "t-b", "t-done", "t-drop"]  # active before terminal, no separator
        # Active rows: slug cell has no dim span.
        for task_id in ("t-a", "t-b"):
            slug_cell = table.get_row(task_id)[4]
            assert not any(s.style == "dim" for s in slug_cell._spans)
        # Terminal rows: every cell carries dim styling.
        for task_id in ("t-done", "t-drop"):
            row = table.get_row(task_id)
            for cell in row:
                assert cell._spans and all(s.style == "dim" for s in cell._spans), (
                    f"{task_id} cell {cell!r} should be fully dim"
                )


async def test_active_only_rows_not_faded() -> None:
    app = Dashboard(_FakeClient([_ACTIVE_A, _ACTIVE_B]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        keys = [str(k.value) for k in table.rows]
        assert keys == ["t-a", "t-b"]
        for task_id in ("t-a", "t-b"):
            slug_cell = table.get_row(task_id)[4]
            assert not any(s.style == "dim" for s in slug_cell._spans)


_SNOOZE_NOW = datetime(2026, 7, 31, 8, 0, tzinfo=UTC)
_INDEFINITE_SNOOZE = "9999-12-31T23:59:59+00:00"


def _assert_fully_dimmed(row: list[Any]) -> None:
    for cell in row:
        assert cell._spans and any(
            span.start == 0 and span.end == len(cell.plain) and span.style == "dim"
            for span in cell._spans
        ), f"{cell!r} should be dim across its full rendered text"


# 2119: REQ-027.2.1
# 2119: REQ-027.2.2
# 2119: REQ-027.3.1
# 2119: REQ-027.3.3
async def test_e_snoozes_for_twelve_hours_renders_countdown_and_toggles_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PANOPTICON_SNOOZE_HOURS", "1")
    assert set(signature(Dashboard).parameters) == {
        "client",
        "on_switch",
        "on_service",
        "on_runner",
        "artifacts_root",
        "draft_file",
        "refresh_interval",
        "now",
    }
    task = {
        **_TASK,
        "turn": "user",
        "attention": False,
        "snoozed_until": None,
    }
    fake = _FakeClient([task])
    app = Dashboard(fake, now=lambda: _SNOOZE_NOW, refresh_interval=0)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()

        class RejectSnoozeEnvironment(dict[str, str]):
            """Fail any environment lookup that could configure snooze duration."""

            @staticmethod
            def _check(key: object) -> None:
                if "SNOOZE" in str(key).upper():
                    raise AssertionError(f"unexpected snooze configuration lookup: {key}")

            def __getitem__(self, key: str) -> str:
                self._check(key)
                return super().__getitem__(key)

            def get(self, key: str, default: str | None = None) -> str | None:
                self._check(key)
                return super().get(key, default)

            def __contains__(self, key: object) -> bool:
                self._check(key)
                return super().__contains__(key)

        monkeypatch.setattr(
            dashboard.os,
            "environ",
            RejectSnoozeEnvironment(dashboard.os.environ),
        )
        await pilot.press("e")
        await pilot.pause()

        deadline = (_SNOOZE_NOW + timedelta(hours=12)).isoformat()
        assert fake.snoozes == [(_TASK["id"], deadline)]
        row = app.query_one("#tasks", DataTable).get_row(_TASK["id"])
        assert row[1].plain == "snoozed · 12h left"
        _assert_fully_dimmed(row)
        assert "orange" not in str(row[1].style)

        await pilot.press("e")
        await pilot.pause()
        assert fake.snoozes[-1] == (_TASK["id"], None)
        restored = app.query_one("#tasks", DataTable).get_row(_TASK["id"])
        assert restored[1].plain == "user"
        assert "orange" in str(restored[1].style)


# 2119: REQ-027.2.1
# 2119: REQ-027.3.3
async def test_e_replaces_an_expired_deadline_with_a_new_twelve_hour_snooze() -> None:
    task = {
        **_TASK,
        "turn": "user",
        "attention": False,
        "snoozed_until": (_SNOOZE_NOW - timedelta(seconds=1)).isoformat(),
    }
    fake = _FakeClient([task])
    app = Dashboard(fake, now=lambda: _SNOOZE_NOW, refresh_interval=0)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()

        expected = (_SNOOZE_NOW + timedelta(hours=12)).isoformat()
        assert fake.snoozes == [(_TASK["id"], expected)]


# 2119: REQ-027.2.3
# 2119: REQ-027.2.2
# 2119: REQ-027.3.1
async def test_shift_e_sets_an_indefinite_snooze_with_visible_dim_label() -> None:
    task = {
        **_TASK,
        "turn": "user",
        "attention": False,
        "snoozed_until": None,
    }
    other = {**task, "id": "task-other456789", "slug": "other-task"}
    fake = _FakeClient([task, other])
    app = Dashboard(fake, now=lambda: _SNOOZE_NOW, refresh_interval=0)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("E")
        await pilot.pause()

        assert fake.snoozes == [(_TASK["id"], _INDEFINITE_SNOOZE)]
        assert other["snoozed_until"] is None
        row = app.query_one("#tasks", DataTable).get_row(_TASK["id"])
        assert row[1].plain == "snoozed"
        _assert_fully_dimmed(row)

        await pilot.press("e")
        await pilot.pause()
        assert fake.snoozes[-1] == (_TASK["id"], None)


async def test_failed_snooze_write_keeps_dashboard_running(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient([{**_TASK, "snoozed_until": None}])
    request = httpx.Request("PUT", "http://service/tasks/task/snooze")
    response = httpx.Response(409, request=request, json={"detail": "task changed"})
    error = httpx.HTTPStatusError("conflict", request=request, response=response)

    def fail_snooze(_task_id: str, _until: str | None) -> dict[str, Any]:
        raise error

    monkeypatch.setattr(fake, "set_snooze", fail_snooze)
    notices: list[str] = []
    app = Dashboard(fake, now=lambda: _SNOOZE_NOW, refresh_interval=0)  # type: ignore[arg-type]
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notices.append(message))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert app.is_running
        assert notices == ["Can't snooze: task changed"]
        assert app.query_one("#tasks", DataTable).row_count == 1


# 2119: REQ-027.3.2
# 2119: REQ-027.3.3
async def test_clock_timer_expires_from_cached_snapshot_without_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [_SNOOZE_NOW]
    deadline = _SNOOZE_NOW + timedelta(milliseconds=50)
    task = {
        **_TASK,
        "turn": "user",
        "attention": False,
        "snoozed_until": deadline.isoformat(),
    }
    fake = _FakeClient([task])
    app = Dashboard(fake, now=lambda: clock[0], refresh_interval=0)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        assert table.get_row(_TASK["id"])[1].plain == "snoozed · <1m left"

        def fail_refresh(*, since: int = 0, wait: float | None = None) -> tuple[list[Any], int]:
            request = httpx.Request("GET", "http://service/tasks")
            raise httpx.RequestError("service restarting", request=request)

        monkeypatch.setattr(fake, "list_tasks_versioned", fail_refresh)
        clock[0] = deadline
        await pilot.pause(0.1)

        expired = table.get_row(_TASK["id"])
        assert expired[1].plain == "user"
        assert "orange" in str(expired[1].style)
        assert task["snoozed_until"] == deadline.isoformat()
        assert fake.snoozes == []


# 2119: REQ-027.3.1
async def test_active_snooze_mutes_a_blocked_task_instead_of_piercing() -> None:
    task = {
        **_TASK,
        "turn": "user",
        "blocked": True,
        "attention": False,
        "snoozed_until": (_SNOOZE_NOW + timedelta(hours=4)).isoformat(),
    }
    app = Dashboard(_FakeClient([task]), now=lambda: _SNOOZE_NOW, refresh_interval=0)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        row = app.query_one("#tasks", DataTable).get_row(_TASK["id"])
        assert row[1].plain == "snoozed · 4h left"
        assert "⚠" not in row[1].plain and "red" not in str(row[1].style)
        _assert_fully_dimmed(row)


# 2119: REQ-027.3.2
# 2119: REQ-027.3.3
@pytest.mark.parametrize(
    "expiry_offset",
    [timedelta(0), timedelta(seconds=1)],
    ids=["at-deadline", "after-deadline"],
)
async def test_finite_snooze_expiry_restores_attention_without_clearing_recorded_fact(
    expiry_offset: timedelta,
) -> None:
    clock = [_SNOOZE_NOW]
    deadline = _SNOOZE_NOW + timedelta(hours=4)
    task = {
        **_TASK,
        "turn": "user",
        "attention": False,
        "snoozed_until": deadline.isoformat(),
    }
    fake = _FakeClient([task])
    app = Dashboard(fake, now=lambda: clock[0], refresh_interval=0)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        assert table.get_row(_TASK["id"])[1].plain == "snoozed · 4h left"

        clock[0] = deadline + expiry_offset
        app.action_refresh()
        await pilot.pause()
        expired = table.get_row(_TASK["id"])
        assert expired[1].plain == "user"
        assert "orange" in str(expired[1].style)
        ordinary = _turn_cell({**task, "snoozed_until": None}, clock[0])
        assert (expired[1].plain, str(expired[1].style)) == (
            ordinary.plain,
            str(ordinary.style),
        )
        assert task["snoozed_until"] == deadline.isoformat()
        assert fake.snoozes == []

    ordinary_cases = [
        ({**_TASK, "turn": "agent", "blocked": False, "attention": False}, ("agent", "green")),
        ({**_TASK, "turn": "user", "blocked": True, "attention": False}, ("user ⚠", "red")),
        ({**_TASK, "turn": "agent", "blocked": False, "attention": True}, ("user", "orange")),
    ]
    for ordinary_task, (expected_text, expected_style) in ordinary_cases:
        expired_cell = _turn_cell(
            {**ordinary_task, "snoozed_until": deadline.isoformat()}, deadline
        )
        unsnoozed_cell = _turn_cell({**ordinary_task, "snoozed_until": None}, deadline)
        assert (expired_cell.plain, str(expired_cell.style)) == (
            unsnoozed_cell.plain,
            str(unsnoozed_cell.style),
        )
        assert expired_cell.plain == expected_text
        assert expected_style in str(expired_cell.style)


# 2119: REQ-027.3.4
async def test_attention_marker_pierces_snooze_and_snooze_precedes_held_and_gated() -> None:
    held = {
        **_TASK,
        "id": "held",
        "attention": False,
        "snoozed_until": (_SNOOZE_NOW + timedelta(hours=4)).isoformat(),
    }
    gated = {
        **_TASK,
        "id": "gated",
        "slug": "gated",
        "turn": "user",
        "attention": False,
        "depends_on_task_ids": [],
        "container_status": "gated",
        "snoozed_until": (_SNOOZE_NOW + timedelta(hours=4)).isoformat(),
    }
    pierced_gated = {
        **gated,
        "id": "pierced-gated",
        "slug": "pierced-gated",
        "turn": "agent",
        "blocked": True,
        "attention": True,
    }
    app = Dashboard(
        _FakeClient([gated, pierced_gated]),
        now=lambda: _SNOOZE_NOW,
        refresh_interval=0,
    )  # type: ignore[arg-type]
    held_candidate = dashboard._apply_snooze_precedence(
        held, _SNOOZE_NOW, Text("held", style="dim")
    )
    assert held_candidate.plain == "snoozed · 4h left"
    assert held_candidate.style == "dim"
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)

        gated_row = table.get_row("gated")
        assert gated_row[1].plain == "snoozed · 4h left"
        _assert_fully_dimmed(gated_row)

        pierced_row = table.get_row("pierced-gated")
        assert pierced_row[1].plain == "user"
        assert "orange" in str(pierced_row[1].style)
        assert not any(span.style == "dim" for span in pierced_row[-1]._spans)


def test_dim_helper_str_and_text() -> None:
    # _dim on a plain str or a Rich Text both produce a Text whose sole span is "dim".
    from rich.text import Text

    result = _dim("hello")
    assert result.plain == "hello"
    assert result._spans and result._spans[0].style == "dim"

    t = Text("world", style="green")
    dimmed = _dim(t)
    assert dimmed.plain == "world"
    assert dimmed._spans and all(s.style == "dim" for s in dimmed._spans)
    # original is unmodified
    assert t.plain == "world"
    assert str(t.style) == "green"


async def _settle(pilot: Any, predicate: Any, *, tries: int = 100, step: float = 0.02) -> None:
    """Pump the event loop until ``predicate()`` holds (or we run out of tries). The feed worker
    runs on a thread and marshals the rebuild back via ``call_from_thread``, so we poll rather than
    sleep a fixed span — robust against scheduling jitter in CI."""
    for _ in range(tries):
        if predicate():
            return
        await pilot.pause(step)


async def test_dashboard_refreshes_when_the_feed_signals_a_change() -> None:
    # No wall-clock timer: the long-poll worker redraws the table when the change feed reports a
    # task changed — exactly once per change, and the rebuild reflects the new snapshot.
    fake = _FakeClient([])
    app = Dashboard(
        fake, refresh_interval=0.05
    )  # short long-poll wait so idle polls cycle fast  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        assert table.row_count == 0
        builds = fake.list_tasks_calls  # the first paint
        fake._tasks = [_TASK]  # the producer grew a task...
        fake.signal_change()  # ...and the feed releases the worker's parked long-poll
        await _settle(pilot, lambda: table.row_count == 1)
        assert table.row_count == 1
        assert fake.list_tasks_calls == builds + 1  # exactly one feed-driven rebuild


# 2119: REQ-008.7.1
async def test_dashboard_initializes_snapshot_and_cursor_from_one_versioned_response() -> None:
    initial = {**_TASK, "turn": "user"}
    addressed = {**_TASK, "turn": "agent"}

    class _ChangeOnSeed(_FakeClient):
        feed_calls: list[tuple[int, float | None]] = []

        def list_tasks(self) -> list[dict[str, Any]]:
            raise AssertionError("initial rendering must not use an unversioned snapshot")

        def list_tasks_versioned(
            self, *, since: int = 0, wait: float | None = None
        ) -> tuple[list[dict[str, Any]], int]:
            self.feed_calls.append((since, wait))
            if wait is None and self._version == 0:
                self._tasks = [addressed]
                self._version = 1
            return super().list_tasks_versioned(since=since, wait=wait)

    fake = _ChangeOnSeed([initial])
    app = Dashboard(fake, refresh_interval=0.02)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await _settle(pilot, lambda: fake._version == 1)
        await pilot.pause(0.1)  # no later mutation is coming to rescue a discarded seed snapshot
        table = app.query_one("#tasks", DataTable)
        assert table.get_row(_TASK["id"])[1].plain == "agent"
        first_long_poll = next(call for call in fake.feed_calls if call[1] is not None)
        assert first_long_poll[0] == 1


async def test_dashboard_does_not_refresh_while_the_feed_is_idle() -> None:
    # A quiet feed (no change signalled) drives no rebuild, however many long-poll cycles elapse —
    # the old fixed-interval timer would have redrawn regardless.
    fake = _FakeClient([_TASK])
    app = Dashboard(
        fake, refresh_interval=0.02
    )  # fast idle polls, but nothing changes  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        builds = fake.list_tasks_calls  # first paint only
        await pilot.pause(0.2)  # several idle long-poll cycles elapse
        assert fake.list_tasks_calls == builds  # the quiet feed triggered no rebuild


async def test_auto_refresh_preserves_the_highlighted_task() -> None:
    # Two tasks; highlight the second, then a refresh must keep the cursor on it (not snap to first).
    other = {**_TASK, "id": "task-second9999", "slug": "other"}
    fake = _FakeClient([_TASK, other])
    app = Dashboard(fake, refresh_interval=0)  # feed worker disabled — drive the rebuild explicitly
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        table.move_cursor(row=1)
        await pilot.pause()
        assert app._current == "task-second9999"
        app.action_refresh()
        await pilot.pause()
        assert app._current == "task-second9999"  # highlight survived the rebuild
        assert table.cursor_row == 1


async def test_dashboard_with_no_tasks() -> None:
    app = Dashboard(_FakeClient([]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#tasks", DataTable).row_count == 0
        await pilot.press("d")  # open the detail pane
        await pilot.pause()
        assert str(app.query_one("#detail", Static).render()) == "no tasks"


# 2119: REQ-025.1.1
async def test_pressing_t_signals_the_pick_and_keeps_the_dashboard_running() -> None:
    # The dashboard records the pick via on_switch (the supervisor detaches + attaches the task)
    # and stays alive, so returning lands on this same live dashboard (ADR 0009 §6). A `live` task
    # is attachable; the session name is derived (`panopticon-<id>`), not read from a registration.
    picked: list[tuple[str, str | None, str]] = []
    task = {**_TASK, "memo": "make it green", "container_status": "live"}
    app = Dashboard(_FakeClient([task]), on_switch=lambda s, h, label: picked.append((s, h, label)))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        # (session, runner_host): runner_host is None when the task has no runner_host field
        assert picked == [("panopticon-task-abcdef0123", None, "fix-widget [make it green]")]
        assert app.is_running  # did NOT exit — the dashboard session persists


async def test_pressing_t_attaches_a_shell_task_with_no_registration() -> None:
    # A shell workflow task runs no agent, so it never registers; its host tmux session *is* its
    # liveness and it sits at `awaiting` for its whole run. `t` must still reach it — keyed off the
    # composed status, not a registration lookup — attaching to the same derived session name.
    picked: list[tuple[str, str | None, str]] = []
    task = {**_TASK, "container_status": "awaiting"}
    app = Dashboard(_FakeClient([task]), on_switch=lambda s, h, label: picked.append((s, h, label)))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        assert picked == [("panopticon-task-abcdef0123", None, "fix-widget")]
        assert app.is_running


async def test_pressing_t_with_no_running_session_does_not_signal() -> None:
    # No attachable session (here: no `container_status` at all → not in _ATTACHABLE_STATUSES):
    # report and stay on the dashboard rather than attach to nothing.
    picked: list[tuple[str, str | None, str]] = []
    app = Dashboard(
        _FakeClient([_TASK]), on_switch=lambda s, h, label: picked.append((s, h, label))
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        assert picked == []
        assert app.is_running


async def test_pressing_s_switches_to_the_service_session_when_one_exists() -> None:
    # `s` switches to the task-service tmux session via on_service (record + detach, like `t`),
    # and the dashboard stays alive; on_service returns True when a service session exists.
    calls: list[str] = []

    def on_service() -> bool:
        calls.append("service")
        return True

    app = Dashboard(_FakeClient([_TASK]), on_service=on_service)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        assert calls == ["service"]
        assert app.is_running


async def test_pressing_s_with_no_service_session_does_nothing() -> None:
    app = Dashboard(_FakeClient([_TASK]), on_service=lambda: False)  # no service session
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        assert app.is_running  # reported "none running"; stayed on the dashboard


async def test_pressing_u_switches_to_the_runner_session_when_one_exists() -> None:
    # `u` switches to the session-service (runner) tmux session via on_runner (record + detach,
    # like `s`), and the dashboard stays alive; on_runner returns True when a runner session exists.
    calls: list[str] = []

    def on_runner() -> bool:
        calls.append("runner")
        return True

    app = Dashboard(_FakeClient([_TASK]), on_runner=on_runner)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("u")
        await pilot.pause()
        assert calls == ["runner"]
        assert app.is_running


async def test_pressing_u_with_no_runner_session_does_nothing() -> None:
    app = Dashboard(_FakeClient([_TASK]), on_runner=lambda: False)  # no runner session
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("u")
        await pilot.pause()
        assert app.is_running  # reported "none running"; stayed on the dashboard


# 2119: REQ-018.13.1
async def test_pressing_n_creates_a_task_via_repo_workflow_then_memo() -> None:
    fake = _FakeClient(
        [],
        repos=["r1", "r2"],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")  # opens the repo picker
        await pilot.pause()
        await pilot.press("enter")  # first repo: r1
        await pilot.pause()
        await pilot.press("enter")  # first (only) workflow: spike
        await pilot.pause()
        await pilot.press("f", "i", "x")  # type a memo into the prompt
        await pilot.press("enter")  # submit
        await pilot.pause()
        # Enter always submits the memo as the agent's initial prompt
        assert fake.created == [("r1", "spike", "fix", "fix", None, None)]


# 2119: REQ-034.1.1
async def test_new_task_repo_picker_filters_case_insensitive_id_prefix_as_user_types() -> None:
    fake = _FakeClient(
        [],
        repos=["a-1@one", "ax-1@other", "xa-1@one", "beta"],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "A", "-", "1", "@")
        await pilot.pause()

        picker = app.screen
        assert isinstance(picker, dashboard.ChoiceScreen)
        assert str(picker.query_one(Label).render()) == "repo — search: A-1@"
        options = picker.query_one(OptionList)
        assert [
            str(options.get_option_at_index(i).prompt) for i in range(options.option_count)
        ] == [
            "a-1@one",
        ]


# 2119: REQ-034.1.2
async def test_new_task_repo_picker_backspace_restores_prefix_matches() -> None:
    fake = _FakeClient(
        [],
        repos=["alpha", "Alpine", "beta"],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "a", "l", "p", "h", "backspace")
        await pilot.pause()

        assert str(app.screen.query_one(Label).render()) == "repo — search: alp"
        options = app.screen.query_one(OptionList)
        assert [
            str(options.get_option_at_index(i).prompt) for i in range(options.option_count)
        ] == [
            "alpha",
            "Alpine",
        ]

        await pilot.press("backspace", "backspace", "backspace")
        await pilot.pause()
        assert str(app.screen.query_one(Label).render()) == "repo"
        assert [
            str(options.get_option_at_index(i).prompt) for i in range(options.option_count)
        ] == [
            "alpha",
            "Alpine",
            "beta",
        ]


# 2119: REQ-034.2.1
async def test_new_task_repo_picker_selects_exact_filtered_repository() -> None:
    fake = _FakeClient(
        [],
        repos=["alpha", "beta", "bravo"],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "b", "r", "enter", "enter", "f", "i", "x", "enter")
        await pilot.pause()

        assert fake.created == [("bravo", "spike", "fix", "fix", None, None)]


# 2119: REQ-018.8.1
async def test_new_task_memo_draft_survives_close_and_reopen(tmp_path: Path) -> None:
    fake = _FakeClient([], repos=["r1"], workflows=[{"name": "spike", "when_to_use": ""}])
    app = Dashboard(fake, draft_file=tmp_path / "new-task-drafts.json")  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter")
        await pilot.pause()
        await pilot.press("d", "r", "a", "f", "t", "escape")
        await pilot.pause()
        await pilot.press("n", "enter", "enter")
        await pilot.pause()
        assert app.screen.query_one(dashboard.MemoTextArea).text == "draft"


async def test_submitting_new_task_clears_its_saved_draft(tmp_path: Path) -> None:
    draft_file = tmp_path / "new-task-drafts.json"
    fake = _FakeClient([], repos=["r1"], workflows=[{"name": "spike", "when_to_use": ""}])
    app = Dashboard(fake, draft_file=draft_file)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter")
        await pilot.pause()
        await pilot.press("d", "r", "a", "f", "t", "escape")
        await pilot.pause()
        await pilot.press("n", "enter", "enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

    restarted = Dashboard(fake, draft_file=draft_file)  # type: ignore[arg-type]
    async with restarted.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter")
        await pilot.pause()
        assert restarted.screen.query_one(dashboard.MemoTextArea).text == ""


async def test_pressing_n_with_a_blank_memo_creates_with_none() -> None:
    fake = _FakeClient(
        [],
        repos=["r1"],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("enter")  # repo
        await pilot.pause()
        await pilot.press("enter")  # workflow
        await pilot.pause()
        await pilot.press("enter")  # submit an empty memo
        await pilot.pause()
        assert fake.created == [("r1", "spike", None, None, None, None)]


async def test_pressing_n_shows_the_repos_default_harness_and_creates_with_no_override() -> None:
    # The memo modal's bottom-area harness indicator reads the selected repo's default_harness;
    # leaving it untouched sends no override (harness=None), same as the ctrl+g hint's plain info.
    fake = _FakeClient(
        [],
        repos=[{"id": "r1", "name": "r1", "git_url": "", "default_base": "main"}],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("enter")  # repo r1
        await pilot.pause()
        await pilot.press("enter")  # workflow spike
        await pilot.pause()
        selector = app.screen.query_one(dashboard.HarnessSelector)
        assert selector.value == "claude"  # no default_harness on the repo → falls back to claude
        await pilot.press("enter")  # submit
        await pilot.pause()
        assert fake.created == [("r1", "spike", None, None, None, None)]


async def test_pressing_n_shows_the_repos_configured_default_harness() -> None:
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "r1",
                "git_url": "",
                "default_base": "main",
                "default_harness": "codex",
            }
        ],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("enter")  # repo r1
        await pilot.pause()
        await pilot.press("enter")  # workflow spike
        await pilot.pause()
        selector = app.screen.query_one(dashboard.HarnessSelector)
        assert selector.value == "codex"
        await pilot.press("enter")  # submit, unchanged → no override sent
        await pilot.pause()
        assert fake.created == [("r1", "spike", None, None, None, None)]


# 2119: layered-settings-hints.2.1
# 2119: layered-settings-hints.3.1
# 2119: layered-settings-hints.6.1
async def test_memo_launch_summary_re_resolves_after_repo_data_loads(tmp_path: Path) -> None:
    """Exercise the real modal path when repo detail arrives after the picker snapshot."""
    fake = _FakeClient(
        [],
        repos=[{"id": "r1", "name": "r1", "git_url": "", "default_base": "main"}],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    loaded_repo = {
        "id": "r1",
        "name": "r1",
        "git_url": "",
        "default_base": "main",
        "default_harness": "codex",
        "default_model": None,
    }

    def load_workflows(repo_id: str) -> list[dict[str, str]]:
        assert repo_id == "r1"
        fake._repos = [loaded_repo]
        return fake._workflows

    fake.list_workflows_for_repo = load_workflows  # type: ignore[method-assign]
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter")
        await pilot.pause()

        screenshot = app.save_screenshot("memo-repo-default.svg", str(tmp_path))
        summary = app.screen.query_one("#launch-summary", Static)
        assert Path(screenshot).exists()
        rendered = str(summary.render())
        assert rendered.startswith("codex · (codex default) — set by repo default")
        task_hint = (
            "Harness/model precedence: workflow config; repo config > app default; change here "
            "to override this task."
        )
        assert dashboard.layered_settings_hint("task-creation") == task_hint
        assert rendered == f"codex · (codex default) — set by repo default · {task_hint}"
        assert len(app.screen.query(".layered-settings-hint")) == 1
        label_texts = [
            "enter: submit",
            "ctrl+s: set without submitting",
            "ctrl+g: edit in $EDITOR",
            "harness",
            "model",
            "no matches",
            "effort",
            "no matches",
        ]
        assert [str(label.render()) for label in app.screen.query(Label)] == label_texts
        assert [str(static.render()) for static in app.screen.query(Static)] == [
            *label_texts[:4],
            "codex",
            *label_texts[4:],
            rendered,
        ]
        assert summary.styles.color.a == pytest.approx(0.6)


def _option_prompts(option_list: OptionList) -> list[str]:
    return [
        str(option_list.get_option_at_index(index).prompt)
        for index in range(option_list.option_count)
    ]


# 2119: REQ-018.1.1
# 2119: REQ-018.9.1
async def test_memo_launch_fields_are_labeled_visible_and_stay_within_64_columns() -> None:
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "r1",
                "git_url": "",
                "default_base": "main",
                "default_harness": "codex",
                "default_model": "terra:high",
            }
        ],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter")
        await pilot.pause()

        labels = {str(label.content) for label in app.screen.query(".launch-field-label")}
        harness = app.screen.query_one(dashboard.HarnessSelector)
        model = app.screen.query_one("#launch-model", Input)
        effort = app.screen.query_one("#launch-effort", Input)
        assert isinstance(app.screen.focused, dashboard.MemoTextArea)
        assert labels == {"harness", "model", "effort"}
        assert (harness.value, model.value, effort.value) == ("codex", "terra", "high")
        assert all(widget.region.width > 0 for widget in (harness, model, effort))
        assert app.screen.query_one("#memo-box").region.width == 64

        await pilot.press("tab", "tab")
        await pilot.pause()
        assert app.screen.query_one("#launch-model-options", OptionList).region.width > 0
        assert app.screen.query_one("#memo-box").region.width == 64
        await pilot.press("tab")
        await pilot.pause()
        assert app.screen.query_one("#launch-effort-options", OptionList).region.width > 0
        assert app.screen.query_one("#memo-box").region.width == 64


# 2119: REQ-018.9.1
@pytest.mark.parametrize(
    ("tabs", "options_id"),
    [
        (2, "#launch-model-options"),
        (3, "#launch-effort-options"),
    ],
)
async def test_each_candidate_list_preserves_the_64_column_modal_width(
    tabs: int, options_id: str
) -> None:
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "r1",
                "git_url": "",
                "default_base": "main",
                "default_harness": "codex",
                "default_model": "terra:high",
            }
        ],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter", *("tab" for _ in range(tabs)))
        await pilot.pause()

        assert app.screen.query_one(options_id, OptionList).region.width > 0
        assert app.screen.query_one("#memo-box").region.width == 64


# 2119: REQ-018.2.1
async def test_memo_launch_controls_follow_the_documented_tab_order() -> None:
    fake = _FakeClient([], repos=["r1"], workflows=[{"name": "spike", "when_to_use": ""}])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter")
        await pilot.pause()
        assert isinstance(app.screen.focused, dashboard.MemoTextArea)
        await pilot.press("tab")
        assert isinstance(app.screen.focused, dashboard.HarnessSelector)
        await pilot.press("tab")
        assert app.screen.focused is app.screen.query_one("#launch-model", Input)
        await pilot.press("tab")
        assert app.screen.focused is app.screen.query_one("#launch-effort", Input)


@pytest.mark.parametrize(
    ("tabs", "typed", "options_id", "expected"),
    [
        (2, "A", "#launch-model-options", ("terra — Terra", "luna — Luna")),
        (3, "H", "#launch-effort-options", ("high — High", "xhigh — X-high")),
    ],
)
# 2119: REQ-018.3.1
async def test_typing_filters_the_visible_model_and_effort_candidates(
    tabs: int,
    typed: str,
    options_id: str,
    expected: tuple[str, ...],
) -> None:
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "r1",
                "git_url": "",
                "default_base": "main",
                "default_harness": "codex",
            }
        ],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter", *("tab" for _ in range(tabs)))
        await pilot.press(*typed)
        await pilot.pause()

        candidates = app.screen.query_one(options_id, OptionList)
        prompts = _option_prompts(candidates)
        assert candidates.region.width > 0
        assert prompts == list(expected)


# 2119: REQ-018.4.1
# 2119: REQ-018.10.1
@pytest.mark.parametrize(
    ("tabs", "typed", "input_id", "options_id", "candidate_count", "target_index", "expected"),
    [
        (2, "a", "#launch-model", "#launch-model-options", 2, 0, "terra"),
        (3, "i", "#launch-effort", "#launch-effort-options", 3, 1, "high"),
    ],
)
async def test_arrow_then_enter_selects_a_filtered_candidate_without_submitting(
    tabs: int,
    typed: str,
    input_id: str,
    options_id: str,
    candidate_count: int,
    target_index: int,
    expected: str,
) -> None:
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "r1",
                "git_url": "",
                "default_base": "main",
                "default_harness": "codex",
            }
        ],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter", *("tab" for _ in range(tabs)))
        await pilot.press(typed)
        await pilot.pause()

        candidates = app.screen.query_one(options_id, OptionList)
        assert len(_option_prompts(candidates)) == candidate_count
        candidates.highlighted = None
        await pilot.press("down")
        assert candidates.highlighted == 0
        await pilot.press("down")
        assert candidates.highlighted == 1
        await pilot.press("up")
        assert candidates.highlighted == 0
        if target_index:
            await pilot.press(*("down" for _ in range(target_index)))
        assert candidates.highlighted == target_index
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, dashboard.MemoScreen)
        assert app.screen.query_one(input_id, Input).value == expected
        assert fake.created == []


# 2119: REQ-018.17.1
# 2119: REQ-018.20.1
@pytest.mark.parametrize(
    ("tabs", "options_id", "typed"),
    [
        (2, "#launch-model-options", "not-a-suggested-model"),
        (3, "#launch-effort-options", "not-a-suggested-effort"),
    ],
)
async def test_no_match_stays_visible_and_enter_does_not_submit(
    tabs: int, options_id: str, typed: str
) -> None:
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "r1",
                "git_url": "",
                "default_base": "main",
                "default_harness": "codex",
            }
        ],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter", *("tab" for _ in range(tabs)))
        await pilot.press(*typed)
        await pilot.pause()

        candidates = app.screen.query_one(options_id, OptionList)
        assert candidates.option_count == 0
        assert candidates.region.width > 0
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, dashboard.MemoScreen)
        assert fake.created == []


# 2119: REQ-018.17.1
@pytest.mark.parametrize(
    ("tabs", "options_id"),
    [
        (2, "#launch-model-options"),
        (3, "#launch-effort-options"),
    ],
)
async def test_enter_with_nonempty_candidates_but_no_highlight_does_not_submit(
    tabs: int, options_id: str
) -> None:
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "r1",
                "git_url": "",
                "default_base": "main",
                "default_harness": "codex",
            }
        ],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter", *(("tab",) * tabs))
        await pilot.pause()

        candidates = app.screen.query_one(options_id, OptionList)
        assert candidates.option_count > 0
        assert candidates.highlighted is None
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, dashboard.MemoScreen)
        assert fake.created == []


# 2119: REQ-018.6.1
async def test_harness_cycle_refreshes_dependent_candidate_vocabularies() -> None:
    fake = _FakeClient([], repos=["r1"], workflows=[{"name": "spike", "when_to_use": ""}])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter", "tab", "tab")
        await pilot.pause()

        assert _option_prompts(app.screen.query_one("#launch-model-options", OptionList)) == [
            "fable — Fable 5",
            "opus — Opus 4.8",
            "sonnet — Sonnet 5",
        ]
        await pilot.press("tab")
        assert _option_prompts(app.screen.query_one("#launch-effort-options", OptionList)) == []

        await pilot.press("shift+tab", "shift+tab", "enter", "tab")
        await pilot.pause()
        assert app.screen.query_one(dashboard.HarnessSelector).value == "codex"
        assert _option_prompts(app.screen.query_one("#launch-model-options", OptionList)) == [
            "gpt-5.6-sol — GPT-5.6 Sol",
            "terra — Terra",
            "luna — Luna",
        ]
        await pilot.press("tab")
        assert _option_prompts(app.screen.query_one("#launch-effort-options", OptionList)) == [
            "low — Low",
            "medium — Medium",
            "high — High",
            "xhigh — X-high",
        ]


async def test_candidate_vocabulary_is_cached_while_typing(monkeypatch: Any) -> None:
    model_calls = 0
    effort_calls = 0
    harness = dashboard.HARNESSES["codex"]

    def suggested_models() -> tuple[tuple[str, str], ...]:
        nonlocal model_calls
        model_calls += 1
        return (("gpt", "GPT"),)

    def suggested_efforts(model: str | None = None) -> tuple[tuple[str, str], ...]:
        nonlocal effort_calls
        assert model == "gpt"
        effort_calls += 1
        return (("high", "High"),)

    monkeypatch.setattr(harness, "suggested_models", suggested_models)
    monkeypatch.setattr(harness, "suggested_efforts", suggested_efforts)
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "r1",
                "git_url": "",
                "default_base": "main",
                "default_harness": "codex",
            }
        ],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter", "tab", "tab", *"gpt", "tab", *"hi")
        await pilot.pause()

        assert model_calls == 1
        assert effort_calls == 1


# 2119: REQ-018.7.1
# 2119: REQ-018.12.1
# 2119: layered-settings-hints.6.1
async def test_focus_only_keeps_launch_fields_tracking_changed_repo_defaults(
    tmp_path: Path,
) -> None:
    repo = {
        "id": "r1",
        "name": "r1",
        "git_url": "",
        "default_base": "main",
        "default_harness": "codex",
        "default_model": "terra:high",
    }
    fake = _FakeClient([], repos=[repo], workflows=[{"name": "spike", "when_to_use": ""}])
    app = Dashboard(fake, draft_file=tmp_path / "new-task-drafts.json")  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter", "tab", "tab", "tab")
        await pilot.press("escape")
        await pilot.pause()
        repo["default_harness"] = "outfitter"
        repo["default_model"] = "luna:low"
        await pilot.press("n", "enter", "enter")
        await pilot.pause()

        assert app.screen.query_one(dashboard.HarnessSelector).value == "outfitter"
        assert app.screen.query_one("#launch-model", Input).value == "luna"
        assert app.screen.query_one("#launch-effort", Input).value == "low"
        summary = app.screen.query_one("#launch-summary", Static)
        assert str(summary.render()) == (
            "outfitter · luna:low — set by repo default · "
            f"{dashboard.layered_settings_hint('task-creation')}"
        )


# 2119: REQ-018.7.1
# 2119: layered-settings-hints.6.1
async def test_focus_only_keeps_launch_fields_tracking_changed_workflow_defaults(
    tmp_path: Path,
) -> None:
    workflow = {
        "name": "spike",
        "when_to_use": "",
        "default_harness": "codex",
        "default_model": "terra:high",
    }
    fake = _FakeClient(
        [],
        repos=[{"id": "r1", "name": "r1", "git_url": "", "default_base": "main"}],
        workflows=[workflow],
    )
    app = Dashboard(fake, draft_file=tmp_path / "new-task-drafts.json")  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter", "tab", "tab", "tab", "escape")
        await pilot.pause()
        workflow["default_harness"] = "outfitter"
        workflow["default_model"] = "luna:low"
        await pilot.press("n", "enter", "enter")
        await pilot.pause()

        harness = app.screen.query_one(dashboard.HarnessSelector)
        model = app.screen.query_one("#launch-model", Input)
        effort = app.screen.query_one("#launch-effort", Input)
        assert (harness.value, model.value, effort.value) == ("outfitter", "luna", "low")
        summary = app.screen.query_one("#launch-summary", Static)
        assert str(summary.render()) == (
            "outfitter · luna:low — set by workflow default · "
            "Harness/model precedence: workflow config; repo config > app default; change here "
            "to override this task."
        )


# 2119: REQ-018.7.1
# 2119: layered-settings-hints.6.1
async def test_focus_only_keeps_launch_fields_tracking_changed_app_default(
    monkeypatch: Any, tmp_path: Path
) -> None:
    fake = _FakeClient(
        [],
        repos=[{"id": "r1", "name": "r1", "git_url": "", "default_base": "main"}],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake, draft_file=tmp_path / "new-task-drafts.json")  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter", "tab", "tab", "tab", "escape")
        await pilot.pause()
        monkeypatch.setattr(dashboard, "DEFAULT_HARNESS", "codex")
        await pilot.press("n", "enter", "enter")
        await pilot.pause()

        harness = app.screen.query_one(dashboard.HarnessSelector)
        model = app.screen.query_one("#launch-model", Input)
        effort = app.screen.query_one("#launch-effort", Input)
        assert (harness.value, model.value, effort.value) == ("codex", "", "")
        summary = app.screen.query_one("#launch-summary", Static)
        assert str(summary.render()) == (
            "codex · (codex default) — set by app default · "
            "Harness/model precedence: workflow config; repo config > app default; change here "
            "to override this task."
        )


# 2119: REQ-018.12.1
@pytest.mark.parametrize(
    ("tabs", "keys", "expected"),
    [
        (1, ("enter",), "codex · (codex default) — set by this task"),
        (2, ("x",), "claude · x — set by this task"),
        (3, ("x",), "claude · (claude default) — set by this task"),
    ],
)
async def test_each_launch_override_updates_the_rendered_summary_source(
    tabs: int, keys: tuple[str, ...], expected: str
) -> None:
    fake = _FakeClient([], repos=["r1"], workflows=[{"name": "spike", "when_to_use": ""}])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter", *("tab" for _ in range(tabs)), *keys)
        await pilot.pause()
        summary = app.screen.query_one("#launch-summary", Static)
        assert str(summary.render()) == (
            f"{expected} · {dashboard.layered_settings_hint('task-creation')}"
        )


# 2119: REQ-018.12.1
@pytest.mark.parametrize(
    ("repo", "workflow", "override"),
    [
        ({"default_harness": "codex", "default_model": "terra:high"}, {}, "terra"),
        (
            {"default_harness": "codex", "default_model": "terra:high"},
            {"default_harness": "claude", "default_model": "opus:high"},
            "opus",
        ),
    ],
)
def test_operator_override_replaces_repo_or_workflow_provenance_with_this_task(
    repo: dict[str, str], workflow: dict[str, str], override: str
) -> None:
    selection = dashboard.resolve_launch_selection(
        repo,
        workflow,
        overrides={"model": override},
        touched={"model"},
    )

    assert selection.model == override
    assert selection.source == "this task"
    assert selection.summary.endswith("set by this task")


# 2119: REQ-018.12.1
# 2119: REQ-018.7.1
# 2119: layered-settings-hints.6.1
@pytest.mark.parametrize(
    ("repo", "workflow", "expected_values", "expected_summary"),
    [
        (
            {"id": "r1", "name": "r1", "git_url": "", "default_base": "main"},
            {"name": "spike", "when_to_use": ""},
            ("claude", "", ""),
            "claude · (claude default) — set by app default",
        ),
        (
            {
                "id": "r1",
                "name": "r1",
                "git_url": "",
                "default_base": "main",
                "default_harness": "codex",
                "default_model": "terra:medium",
            },
            {
                "name": "spike",
                "when_to_use": "",
                "default_harness": "claude",
                "default_model": "opus:high",
            },
            ("claude", "opus", "high"),
            "claude · opus:high — set by workflow default",
        ),
    ],
)
async def test_rendered_launch_summary_names_app_and_workflow_default_sources(
    repo: dict[str, str],
    workflow: dict[str, str],
    expected_values: tuple[str, str, str],
    expected_summary: str,
) -> None:
    fake = _FakeClient([], repos=[repo], workflows=[workflow])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter")
        await pilot.pause()
        harness = app.screen.query_one(dashboard.HarnessSelector)
        model = app.screen.query_one("#launch-model", Input)
        effort = app.screen.query_one("#launch-effort", Input)
        assert (harness.value, model.value, effort.value) == expected_values
        summary = app.screen.query_one("#launch-summary", Static)
        assert str(summary.render()) == (
            f"{expected_summary} · {dashboard.layered_settings_hint('task-creation')}"
        )


# 2119: REQ-018.11.1
async def test_touched_model_and_effort_survive_a_harness_change() -> None:
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "r1",
                "git_url": "",
                "default_base": "main",
                "default_harness": "codex",
            }
        ],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter", "tab", "tab")
        await pilot.press(*"custom/model", "tab", *"maximum", "shift+tab", "shift+tab", "enter")
        await pilot.pause()
        assert app.screen.query_one("#launch-model", Input).value == "custom/model"
        assert app.screen.query_one("#launch-effort", Input).value == "maximum"


# 2119: REQ-018.11.1
async def test_candidate_accepted_model_and_effort_survive_a_harness_change() -> None:
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "r1",
                "git_url": "",
                "default_base": "main",
                "default_harness": "codex",
            }
        ],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter", "tab", "tab", "down", "enter")
        await pilot.press("tab", "down", "enter")
        model = app.screen.query_one("#launch-model", Input)
        effort = app.screen.query_one("#launch-effort", Input)
        accepted = (model.value, effort.value)
        assert all(accepted)

        await pilot.press("shift+tab", "shift+tab", "enter")
        await pilot.pause()

        assert app.screen.query_one(dashboard.HarnessSelector).value == "outfitter"
        assert (model.value, effort.value) == accepted


# 2119: REQ-018.11.1
# 2119: REQ-018.7.1
async def test_touched_effort_survives_when_untouched_model_clears_on_harness_change() -> None:
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "r1",
                "git_url": "",
                "default_base": "main",
                "default_harness": "codex",
                "default_model": "terra:high",
            }
        ],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter", "tab", "tab", "tab")
        await pilot.press("ctrl+shift+a", *"maximum", "shift+tab", "shift+tab", "enter")
        await pilot.pause()

        assert app.screen.query_one(dashboard.HarnessSelector).value == "outfitter"
        assert app.screen.query_one("#launch-model", Input).value == ""
        assert app.screen.query_one("#launch-effort", Input).value == "maximum"

        await pilot.press("tab", *"custom/model", "shift+tab", "shift+tab", "ctrl+s")
        await pilot.pause()
        assert fake.created == [("r1", "spike", None, None, "outfitter", "custom/model:maximum")]


# 2119: REQ-018.18.1
async def test_untouched_model_and_effort_clear_after_a_harness_change() -> None:
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "r1",
                "git_url": "",
                "default_base": "main",
                "default_harness": "codex",
                "default_model": "terra:high",
            }
        ],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter", "tab", "enter")
        await pilot.pause()
        assert app.screen.query_one("#launch-model", Input).value == ""
        assert app.screen.query_one("#launch-effort", Input).value == ""

        await pilot.press("ctrl+s")
        await pilot.pause()
        assert fake.created == [("r1", "spike", None, None, "outfitter", None)]


# 2119: REQ-018.18.1
# 2119: REQ-018.7.1
# 2119: REQ-018.11.1
async def test_untouched_effort_clears_when_touched_model_survives_harness_change() -> None:
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "r1",
                "git_url": "",
                "default_base": "main",
                "default_harness": "codex",
                "default_model": "terra:high",
            }
        ],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter", "tab", "tab")
        await pilot.press("ctrl+shift+a", *"custom/model", "shift+tab", "enter")
        await pilot.pause()

        assert app.screen.query_one(dashboard.HarnessSelector).value == "outfitter"
        assert app.screen.query_one("#launch-model", Input).value == "custom/model"
        assert app.screen.query_one("#launch-effort", Input).value == ""

        await pilot.press("shift+tab", "ctrl+s")
        await pilot.pause()
        assert fake.created == [("r1", "spike", None, None, "outfitter", "custom/model")]


# 2119: REQ-018.8.1
@pytest.mark.parametrize("tabs", [1, 2, 3])
async def test_escape_cancels_from_every_launch_control(tabs: int) -> None:
    fake = _FakeClient([], repos=["r1"], workflows=[{"name": "spike", "when_to_use": ""}])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter", *("tab" for _ in range(tabs)))
        await pilot.pause()
        if tabs == 2:
            assert app.screen.query_one("#launch-model-options", OptionList).region.width > 0
        elif tabs == 3:
            assert app.screen.query_one("#launch-effort-options", OptionList).region.width > 0
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, dashboard.MemoScreen)
        assert fake.created == []


async def test_enter_submits_while_harness_selector_is_unfocused(tmp_path: Path) -> None:
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "r1",
                "git_url": "",
                "default_base": "main",
                "default_harness": "codex",
            }
        ],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter")
        await pilot.pause()

        screenshot = app.save_screenshot("memo-enter-submit.svg", str(tmp_path))
        assert Path(screenshot).exists()
        assert isinstance(app.screen.focused, dashboard.MemoTextArea)
        assert app.screen.query_one("#enter-hint", Label).content == "enter: submit"

        await pilot.press("tab")
        await pilot.pause()
        focused_screenshot = app.save_screenshot("memo-selector-focused.svg", str(tmp_path))
        assert Path(focused_screenshot).exists()
        assert isinstance(app.screen.focused, dashboard.HarnessSelector)
        assert app.screen.query_one("#enter-hint", Label).content == "enter: cycle harness"

        await pilot.press("shift+tab")
        await pilot.pause()
        submit_screenshot = app.save_screenshot("memo-selector-unfocused.svg", str(tmp_path))
        assert Path(submit_screenshot).exists()
        assert isinstance(app.screen.focused, dashboard.MemoTextArea)
        assert app.screen.query_one("#enter-hint", Label).content == "enter: submit"

        await pilot.press("enter")
        await pilot.pause()
        assert fake.created == [("r1", "spike", None, None, None, None)]


async def test_pressing_n_with_an_unknown_repo_default_harness_sends_no_spurious_override() -> None:
    # Regression: an unknown default_harness makes the selector fall back to display the first
    # registered harness, but submitting untouched must still send no override — the selector's
    # displayed value differing from the raw (unknown) effective harness isn't a real change.
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "r1",
                "git_url": "",
                "default_base": "main",
                "default_harness": "nonexistent-harness",
            }
        ],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("enter")  # repo r1
        await pilot.pause()
        await pilot.press("enter")  # workflow spike
        await pilot.pause()
        await pilot.press("enter")  # submit untouched
        await pilot.pause()
        assert fake.created == [("r1", "spike", None, None, None, None)]


async def test_the_enter_hint_reflects_cycle_while_the_harness_selector_is_focused() -> None:
    # The bottom-area hint must stay truthful: "enter: submit" is wrong while the harness
    # selector is focused, since Enter there cycles the harness instead (it shadows submit).
    fake = _FakeClient(
        [],
        repos=["r1"],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("enter")  # repo
        await pilot.pause()
        await pilot.press("enter")  # workflow
        await pilot.pause()
        hint = app.screen.query_one("#enter-hint", Label)
        assert hint.content == "enter: submit"
        await pilot.press("tab")  # focus the harness selector
        await pilot.pause()
        assert isinstance(app.screen.focused, dashboard.HarnessSelector)
        assert hint.content == "enter: cycle harness"
        await pilot.press("tab")  # model
        await pilot.press("tab")  # effort
        await pilot.press("tab")  # focus wraps back to the memo text area
        await pilot.pause()
        assert hint.content == "enter: submit"


# 2119: REQ-018.16.1
async def test_tabbing_to_the_harness_selector_and_cycling_overrides_it_for_this_task() -> None:
    fake = _FakeClient(
        [],
        repos=["r1"],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("enter")  # repo
        await pilot.pause()
        await pilot.press("enter")  # workflow
        await pilot.pause()
        await pilot.press("tab")  # focus moves from the memo text area to the harness selector
        assert isinstance(app.screen.focused, dashboard.HarnessSelector)
        selector = app.screen.query_one(dashboard.HarnessSelector)
        registered = selector._names
        assert len(registered) == len(dashboard.HARNESSES)
        assert set(registered) == set(dashboard.HARNESSES)
        initial_index = registered.index(selector.value)
        expected = registered[initial_index + 1 :] + registered[: initial_index + 1]
        seen: list[str] = []
        for next_harness in expected:
            await pilot.press("enter")
            await pilot.pause()
            seen.append(selector.value)
            assert selector.value == next_harness
            assert fake.created == []
        assert seen == expected


# 2119: REQ-018.19.1
async def test_memo_harness_selector_cycles_exactly_the_registered_harnesses() -> None:
    fake = _FakeClient([], repos=["r1"], workflows=[{"name": "spike", "when_to_use": ""}])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter", "tab")
        selector = app.screen.query_one(dashboard.HarnessSelector)
        assert len(selector._names) == len(dashboard.HARNESSES)
        assert set(selector._names) == set(dashboard.HARNESSES)
        initial = selector.value
        seen: set[str] = set()
        for _ in dashboard.HARNESSES:
            seen.add(selector.value)
            await pilot.press("enter")
        assert seen == set(dashboard.HARNESSES)
        assert selector.value == initial


# 2119: REQ-019.1.1
async def test_memo_accepts_input_while_harness_suggestions_are_discovered(
    monkeypatch: Any,
) -> None:
    release = threading.Event()
    slow = _SuggestionHarness("claude", release=release)
    monkeypatch.setattr(dashboard, "HARNESSES", {"claude": slow})
    fake = _FakeClient([], repos=["r1"], workflows=[{"name": "spike", "when_to_use": ""}])
    app = Dashboard(fake)  # type: ignore[arg-type]
    timer: threading.Timer | None = None
    try:
        async with app.run_test() as pilot:
            await pilot.press("n", "enter")
            timer = threading.Timer(5, release.set)  # deadlock backstop
            timer.start()
            await pilot.press("enter")
            await _wait_for(pilot, slow.started.is_set)
            await pilot.press("x")
            assert not release.is_set()
            assert app.screen.query_one(dashboard.MemoTextArea).text == "x"
    finally:
        release.set()
        if timer is not None:
            timer.cancel()


# 2119: REQ-019.2.1
async def test_memo_discovers_each_harness_suggestions_once_per_open(
    monkeypatch: Any,
) -> None:
    harnesses = {name: _SuggestionHarness(name) for name in ("claude", "codex", "pi", "outfitter")}
    monkeypatch.setattr(dashboard, "HARNESSES", harnesses)
    fake = _FakeClient([], repos=["r1"], workflows=[{"name": "spike", "when_to_use": ""}])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await _open_memo(pilot)
        await _wait_for(
            pilot,
            lambda: all(h.model_calls == h.effort_calls == 1 for h in harnesses.values()),
        )
        app.screen.query_one(dashboard.HarnessSelector).focus()
        for _ in range(len(harnesses) * 3):
            await pilot.press("enter")
        assert all(h.model_calls == h.effort_calls == 1 for h in harnesses.values())


# 2119: REQ-019.2.1
async def test_memo_finishes_each_harness_discovery_after_early_close(
    monkeypatch: Any,
) -> None:
    release = threading.Event()
    harnesses = {
        "claude": _SuggestionHarness("claude"),
        "slow": _SuggestionHarness("slow", release=release),
        "zlater": _SuggestionHarness("zlater"),
    }
    monkeypatch.setattr(dashboard, "HARNESSES", harnesses)
    fake = _FakeClient([], repos=["r1"], workflows=[{"name": "spike", "when_to_use": ""}])
    app = Dashboard(fake)  # type: ignore[arg-type]
    try:
        async with app.run_test() as pilot:
            await _open_memo(pilot)
            await _wait_for(pilot, harnesses["slow"].started.is_set)
            await pilot.press("escape")
            release.set()
            await _wait_for(
                pilot,
                lambda: all(h.model_calls == h.effort_calls == 1 for h in harnesses.values()),
            )
            assert not isinstance(app.screen, dashboard.MemoScreen)
    finally:
        release.set()


# 2119: REQ-019.3.1
async def test_memo_suggestion_cache_is_fresh_for_each_open(monkeypatch: Any) -> None:
    claude = _SuggestionHarness("claude")
    monkeypatch.setattr(dashboard, "HARNESSES", {"claude": claude})
    fake = _FakeClient([], repos=["r1"], workflows=[{"name": "spike", "when_to_use": ""}])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await _open_memo(pilot)
        first_model = app.screen.query_one("#launch-model", Input)
        first_effort_input = app.screen.query_one("#launch-effort", Input)
        await _wait_for_suggestion(pilot, first_model, "claude-model-1")
        await _wait_for_suggestion(pilot, first_effort_input, "claude-claude-model-1-effort-1")
        await pilot.press("escape")
        await _open_memo(pilot)
        second_model = app.screen.query_one("#launch-model", Input)
        second_effort_input = app.screen.query_one("#launch-effort", Input)
        await _wait_for_suggestion(pilot, second_model, "claude-model-2")
        await _wait_for_suggestion(pilot, second_effort_input, "claude-claude-model-2-effort-2")


# 2119: REQ-019.4.1
# 2119: REQ-019.8.1
async def test_early_cycle_discovers_once_and_presents_the_selected_harness_suggestions(
    monkeypatch: Any,
) -> None:
    release = threading.Event()
    target = _SuggestionHarness("target", release=release, multiple=True)
    harnesses = {"claude": _SuggestionHarness("claude"), "target": target}
    monkeypatch.setattr(dashboard, "HARNESSES", harnesses)
    fake = _FakeClient([], repos=["r1"], workflows=[{"name": "spike", "when_to_use": ""}])
    app = Dashboard(fake)  # type: ignore[arg-type]
    timer: threading.Timer | None = None
    try:
        async with app.run_test() as pilot:
            await _open_memo(pilot)
            await _wait_for(pilot, target.started.is_set)
            app.screen.query_one(dashboard.HarnessSelector).focus()
            assert not release.is_set()
            assert "target" not in app.screen._suggestion_cache
            timer = threading.Timer(0.1, release.set)
            timer.start()
            await pilot.press("enter")
            assert "target" in app.screen._suggestion_cache
            suggestions = app.screen._suggestion_cache["target"]
            assert [value for value, _ in suggestions.models] == [
                "target-model-1",
                "target-alternate-1",
            ]
            assert [value for value, _ in suggestions.efforts] == [
                "target-target-model-1-effort-1",
                "target-alternate-effort-1",
            ]
            assert target.model_calls == target.effort_calls == 1
            model = app.screen.query_one("#launch-model", Input)
            model.focus()
            await pilot.pause()
            assert _option_prompts(app.screen.query_one("#launch-model-options", OptionList)) == [
                "target-model-1 — target model 1",
                "target-alternate-1 — target alternate 1",
            ]
            effort = app.screen.query_one("#launch-effort", Input)
            effort.focus()
            await pilot.pause()
            assert _option_prompts(app.screen.query_one("#launch-effort-options", OptionList)) == [
                "target-target-model-1-effort-1 — target effort 1",
                "target-alternate-effort-1 — target alt effort",
            ]
            assert target.model_calls == target.effort_calls == 1
            assert target.effort_models == ["target-model-1"]
    finally:
        release.set()
        if timer is not None:
            timer.cancel()


# 2119: REQ-019.4.1
async def test_early_cycle_discovers_an_unstarted_harness_before_returning(
    monkeypatch: Any,
) -> None:
    release = threading.Event()
    slow = _SuggestionHarness("claude", release=release)
    target = _SuggestionHarness("target")
    monkeypatch.setattr(dashboard, "HARNESSES", {"claude": slow, "target": target})
    fake = _FakeClient([], repos=["r1"], workflows=[{"name": "spike", "when_to_use": ""}])
    app = Dashboard(fake)  # type: ignore[arg-type]
    try:
        async with app.run_test() as pilot:
            await _open_memo(pilot)
            await _wait_for(pilot, slow.started.is_set)
            screen = app.screen
            screen.query_one(dashboard.HarnessSelector).focus()
            assert target.model_calls == target.effort_calls == 0
            assert "target" not in screen._suggestion_cache
            assert "target" not in screen._suggestion_pending
            await pilot.press("enter")
            assert "target" in screen._suggestion_cache
            suggestions = screen._suggestion_cache["target"]
            assert [value for value, _ in suggestions.models] == ["target-model-1"]
            assert [value for value, _ in suggestions.efforts] == ["target-target-model-1-effort-1"]
            assert target.model_calls == target.effort_calls == 1
            model = screen.query_one("#launch-model", Input)
            model.focus()
            await pilot.pause()
            assert _option_prompts(screen.query_one("#launch-model-options", OptionList)) == [
                "target-model-1 — target model 1"
            ]
            effort = screen.query_one("#launch-effort", Input)
            effort.focus()
            await pilot.pause()
            assert _option_prompts(screen.query_one("#launch-effort-options", OptionList)) == [
                "target-target-model-1-effort-1 — target effort 1"
            ]
            assert target.model_calls == target.effort_calls == 1
    finally:
        release.set()


# 2119: REQ-019.5.1
async def test_cached_harness_cycles_finish_under_ten_milliseconds(monkeypatch: Any) -> None:
    harnesses = {name: _SuggestionHarness(name, delay=0.02) for name in ("claude", "codex", "pi")}
    monkeypatch.setattr(dashboard, "HARNESSES", harnesses)
    fake = _FakeClient([], repos=["r1"], workflows=[{"name": "spike", "when_to_use": ""}])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await _open_memo(pilot)
        screen = app.screen
        await _wait_for(
            pilot,
            lambda: all(name in screen._suggestion_cache for name in harnesses),
        )
        selector = screen.query_one(dashboard.HarnessSelector)
        for name in ("codex", "pi", "claude"):
            started = time.perf_counter()
            selector.action_cycle()
            model = screen.query_one("#launch-model", Input)
            summary = screen.query_one("#launch-summary", Static)
            model_prompts = _option_prompts(screen.query_one("#launch-model-options", OptionList))
            effort_prompts = _option_prompts(screen.query_one("#launch-effort-options", OptionList))
            elapsed = time.perf_counter() - started
            assert elapsed < 0.01, f"{name} cycle took {elapsed * 1000:.3f}ms"
            assert selector.value == name
            assert screen._selection.harness == name
            assert name in str(summary.render())
            assert model.placeholder == f"{name} model"
            assert model_prompts == [f"{name}-model-1 — {name} model 1"]
            assert effort_prompts == [f"{name}-{name}-model-1-effort-1 — {name} effort 1"]


# 2119: REQ-019.2.1
# 2119: REQ-019.6.1
async def test_closing_memo_suppresses_an_in_flight_discovery_failure(monkeypatch: Any) -> None:
    release = threading.Event()
    slow = _SuggestionHarness("slow", release=release, fail_models=True)
    monkeypatch.setattr(
        dashboard,
        "HARNESSES",
        {"claude": _SuggestionHarness("claude"), "slow": slow},
    )
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "r1",
                "git_url": "",
                "default_base": "main",
                "default_harness": "slow",
            }
        ],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    notices: list[str] = []
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notices.append(str(message)))
    async with app.run_test() as pilot:
        await _open_memo(pilot)
        await _wait_for(pilot, slow.started.is_set)
        await pilot.press("escape")
        release.set()
        await _wait_for(pilot, lambda: slow.model_calls == slow.effort_calls == 1)
        assert app.is_running
        assert not isinstance(app.screen, dashboard.MemoScreen)
        assert notices == []


# 2119: REQ-019.2.1
async def test_discovery_failures_keep_the_successful_half_and_cycle_safely(
    monkeypatch: Any,
) -> None:
    harnesses = {
        "claude": _SuggestionHarness("claude"),
        "model-broken": _SuggestionHarness("model-broken", fail_models=True),
        "effort-broken": _SuggestionHarness("effort-broken", fail_efforts=True),
    }
    monkeypatch.setattr(dashboard, "HARNESSES", harnesses)
    fake = _FakeClient([], repos=["r1"], workflows=[{"name": "spike", "when_to_use": ""}])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await _open_memo(pilot)
        await _wait_for(
            pilot,
            lambda: all(h.model_calls == h.effort_calls == 1 for h in harnesses.values()),
        )
        selector = app.screen.query_one(dashboard.HarnessSelector)
        selector.focus()

        await pilot.press("enter")
        model = app.screen.query_one("#launch-model", Input)
        model.focus()
        await pilot.pause()
        assert _option_prompts(app.screen.query_one("#launch-model-options", OptionList)) == [
            "effort-broken-model-1 — effort-broken model 1"
        ]
        effort = app.screen.query_one("#launch-effort", Input)
        effort.focus()
        await pilot.pause()
        assert _option_prompts(app.screen.query_one("#launch-effort-options", OptionList)) == []

        selector.focus()
        await pilot.press("enter")
        model.focus()
        await pilot.pause()
        assert _option_prompts(app.screen.query_one("#launch-model-options", OptionList)) == []
        effort.focus()
        await pilot.pause()
        assert _option_prompts(app.screen.query_one("#launch-effort-options", OptionList)) == [
            "model-broken-empty-effort-1 — model-broken effort 1"
        ]
        assert app.is_running
        assert all(h.model_calls == h.effort_calls == 1 for h in harnesses.values())


# 2119: REQ-019.7.1
async def test_in_flight_discovery_does_not_update_widgets_after_memo_closes(
    monkeypatch: Any,
) -> None:
    release = threading.Event()
    slow = _SuggestionHarness("slow", release=release)
    monkeypatch.setattr(
        dashboard,
        "HARNESSES",
        {"claude": _SuggestionHarness("claude"), "slow": slow},
    )
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "r1",
                "git_url": "",
                "default_base": "main",
                "default_harness": "claude",
            }
        ],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await _open_memo(pilot)
        await _wait_for(pilot, slow.started.is_set)
        closed_screen = app.screen
        memo = closed_screen.query_one(dashboard.MemoTextArea)
        selector = closed_screen.query_one(dashboard.HarnessSelector)
        summary = closed_screen.query_one("#launch-summary", Static)
        model = closed_screen.query_one("#launch-model", Input)
        effort = closed_screen.query_one("#launch-effort", Input)
        model.focus()
        await pilot.pause()
        # Keep the launch input focused, but make the blocked discovery's harness the selected
        # one without cycling through `_suggestions_for` (which would synchronously wait on the
        # deliberately blocked background call and deadlock the Pilot).
        closed_screen._selection = dashboard.LaunchSelection("slow", "", "", "repo default")
        model_options = closed_screen.query_one("#launch-model-options", OptionList)
        effort_options = closed_screen.query_one("#launch-effort-options", OptionList)
        model_candidates = closed_screen.query_one("#launch-model-candidates")
        effort_candidates = closed_screen.query_one("#launch-effort-candidates")
        before = (
            memo.text,
            selector.value,
            str(summary.render()),
            model.value,
            model.placeholder,
            model.suggester,
            effort.value,
            effort.placeholder,
            effort.suggester,
            _option_prompts(model_options),
            _option_prompts(effort_options),
            frozenset(model_candidates.classes),
            frozenset(effort_candidates.classes),
        )
        await pilot.press("escape")
        assert not isinstance(app.screen, dashboard.MemoScreen)
        presentation_attempts: list[str] = []

        def reject_post_close_presentation(harness_name: str, _suggestions: Any) -> None:
            presentation_attempts.append(harness_name)
            raise AssertionError("discovery attempted to present after the modal closed")

        monkeypatch.setattr(
            closed_screen,
            "_present_suggestions_if_selected",
            reject_post_close_presentation,
        )
        release.set()
        await _wait_for(pilot, lambda: slow.model_calls == slow.effort_calls == 1)
        assert presentation_attempts == []
        assert (
            memo.text,
            selector.value,
            str(summary.render()),
            model.value,
            model.placeholder,
            model.suggester,
            effort.value,
            effort.placeholder,
            effort.suggester,
            _option_prompts(model_options),
            _option_prompts(effort_options),
            frozenset(model_candidates.classes),
            frozenset(effort_candidates.classes),
        ) == before


def test_launch_resolution_and_provenance_for_every_source() -> None:
    resolve = dashboard.resolve_launch_selection
    repo = {"default_harness": "codex", "default_model": "repo-model:medium"}
    workflow = {"default_harness": "claude", "default_model": "opus:high"}

    assert resolve({}, {}).source == "app default"
    assert resolve(repo, {}).source == "repo default"
    assert resolve(repo, workflow) == dashboard.LaunchSelection(
        "claude", "opus", "high", "workflow default"
    )
    assert resolve(
        repo,
        workflow,
        overrides={"model": "free/form", "effort": "maximum"},
        touched={"model", "effort"},
    ) == dashboard.LaunchSelection("claude", "free/form", "maximum", "this task")


def test_touched_launch_fields_survive_workflow_reselection() -> None:
    repo = {"default_harness": "claude", "default_model": "sonnet"}
    touched = {"model"}
    overrides = {"model": "operator/model"}
    first = dashboard.resolve_launch_selection(
        repo,
        {"default_harness": "codex", "default_model": "terra:low"},
        overrides=overrides,
        touched=touched,
    )
    second = dashboard.resolve_launch_selection(
        repo,
        {"default_harness": "claude", "default_model": "opus:high"},
        overrides=overrides,
        touched=touched,
    )
    assert (first.harness, first.model, first.effort) == ("codex", "operator/model", "low")
    assert (second.harness, second.model, second.effort) == (
        "claude",
        "operator/model",
        "high",
    )


# 2119: REQ-018.5.1
@pytest.mark.parametrize(
    ("model_value", "effort_value", "expected"),
    [
        ("custom/model", None, "custom/model:low"),
        (None, "maximum", "terra:maximum"),
        ("custom/model", "maximum", "custom/model:maximum"),
    ],
    ids=["model-only", "effort-only", "both"],
)
async def test_free_text_model_and_effort_are_composed_for_create(
    monkeypatch: pytest.MonkeyPatch,
    model_value: str | None,
    effort_value: str | None,
    expected: str,
) -> None:
    harness = _SuggestionHarness("codex")
    monkeypatch.setattr(dashboard, "HARNESSES", {"codex": harness})
    assert "custom/model" not in {value for value, _ in harness.suggested_models()}
    assert "maximum" not in {value for value, _ in harness.suggested_efforts()}
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "r1",
                "git_url": "",
                "default_base": "main",
                "default_harness": "codex",
                "default_model": "terra:low",
            }
        ],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter", "tab", "tab")
        if model_value is not None:
            await pilot.press("end", *("backspace" for _ in "terra"), *model_value)
        await pilot.press("tab")
        if effort_value is not None:
            await pilot.press("end", *("backspace" for _ in "low"), *effort_value)
        await pilot.press("tab", "enter")
        await pilot.pause()
    assert fake.created == [("r1", "spike", None, None, None, expected)]


# 2119: REQ-018.15.1
def test_external_editor_uses_configured_command_and_returns_saved_content(
    monkeypatch: Any,
) -> None:
    editor_path: Path | None = None

    def edit(argv: list[str]) -> None:
        nonlocal editor_path
        assert argv[:2] == ["custom-editor", "--wait"]
        editor_path = Path(argv[-1])
        assert editor_path.read_text(encoding="utf-8") == "draft"
        editor_path.write_text("edited", encoding="utf-8")

    monkeypatch.setenv("EDITOR", "custom-editor --wait")
    monkeypatch.setattr(dashboard.subprocess, "run", edit)
    assert dashboard._edit_with_editor("draft") == "edited"
    assert editor_path is not None
    assert not editor_path.exists()


# 2119: REQ-018.14.1
async def test_memo_ctrl_s_sets_the_memo_without_submitting() -> None:
    # ctrl+s records the memo but does NOT deliver it as an initial prompt (unsent paste).
    fake = _FakeClient(
        [],
        repos=["r1"],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("enter")  # repo
        await pilot.pause()
        await pilot.press("enter")  # workflow
        await pilot.pause()
        await pilot.press("f", "i", "x")  # type a memo
        await pilot.press("ctrl+s")  # set without submitting
        await pilot.pause()
        # memo stored, no initial_prompt
        assert fake.created == [("r1", "spike", "fix", None, None, None)]


# 2119: REQ-018.15.1
async def test_memo_ctrl_g_opens_editor_and_updates_textarea(monkeypatch: Any) -> None:
    # Ctrl-G should open $EDITOR and replace the TextArea's text with the returned content.
    monkeypatch.setattr(dashboard, "_edit_with_editor", lambda text: f"edited:{text}")
    # The headless test driver has can_suspend=False, which raises SuspendNotSupported.
    # Patch App.suspend to a no-op context manager so the action runs normally in tests.
    monkeypatch.setattr(App, "suspend", lambda self: contextlib.nullcontext())
    fake = _FakeClient(
        [],
        repos=["r1"],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("enter")  # repo
        await pilot.pause()
        await pilot.press("enter")  # workflow
        await pilot.pause()
        await pilot.press("h", "i")  # type initial text
        await pilot.press("ctrl+g")  # open editor
        await pilot.pause()
        await pilot.press("enter")  # submit
        await pilot.pause()
        assert fake.created == [("r1", "spike", "edited:hi", "edited:hi", None, None)]


async def test_memo_textarea_expands_for_multiline_content(monkeypatch: Any) -> None:
    # After ctrl+g loads multi-line content the full text is preserved and submitted.
    three_lines = "line one\nline two\nline three"
    monkeypatch.setattr(dashboard, "_edit_with_editor", lambda text: three_lines)
    monkeypatch.setattr(App, "suspend", lambda self: contextlib.nullcontext())
    fake = _FakeClient(
        [],
        repos=["r1"],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("enter")  # repo
        await pilot.pause()
        await pilot.press("enter")  # workflow
        await pilot.pause()
        await pilot.press("ctrl+g")
        await pilot.pause()
        await pilot.press("enter")  # submit
        await pilot.pause()
        assert fake.created == [("r1", "spike", three_lines, three_lines, None, None)]


async def test_dashboard_drives_drop() -> None:
    # Drop is the one transition the operator drives; advance and the rest are agent skills, so
    # they aren't dashboard actions (no `a`/`i` bindings).
    fake = _FakeClient([_TASK], operations={"advance": "MERGING", "drop": "DROPPED"})
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()
        assert fake.applied == [("task-abcdef0123", "drop")]


async def test_pressing_p_opens_the_task_url(monkeypatch: Any) -> None:
    # `p` opens the highlighted task's url in the browser (cloude-cade's `p` "open PR").
    opened: list[str] = []
    monkeypatch.setattr(dashboard.webbrowser, "open", opened.append)
    task = {**_TASK, "url": "https://github.com/acme/widgets/pull/7"}
    app = Dashboard(_FakeClient([task]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        assert opened == ["https://github.com/acme/widgets/pull/7"]


async def test_pressing_p_with_no_url_does_nothing(monkeypatch: Any) -> None:
    opened: list[str] = []
    monkeypatch.setattr(dashboard.webbrowser, "open", opened.append)
    app = Dashboard(_FakeClient([_TASK]))  # _TASK has no url  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        assert opened == []  # nothing to open; warned and stayed put
        assert app.is_running


def test_clipboard_command_is_platform_appropriate(monkeypatch: Any) -> None:
    # The result is cached (the installed tool can't change at runtime); clear it before each
    # probe so the monkeypatched platform/PATH takes effect, and once more at the end so the
    # cache doesn't leak the fakes into other tests.
    dashboard._clipboard_command.cache_clear()
    # macOS → pbcopy unconditionally (always present, no `which` probe).
    monkeypatch.setattr(dashboard.sys, "platform", "darwin")
    assert dashboard._clipboard_command() == ["pbcopy"]
    # Linux → the first installed of wl-copy / xclip / xsel.
    dashboard._clipboard_command.cache_clear()
    monkeypatch.setattr(dashboard.sys, "platform", "linux")
    monkeypatch.setattr(dashboard.shutil, "which", lambda name: name == "xclip")
    assert dashboard._clipboard_command() == ["xclip", "-selection", "clipboard"]
    # Wayland wins when both are present (preference order).
    dashboard._clipboard_command.cache_clear()
    monkeypatch.setattr(dashboard.shutil, "which", lambda name: name in {"wl-copy", "xclip"})
    assert dashboard._clipboard_command() == ["wl-copy"]
    # nothing installed → None (no host tool; the OSC 52 emit still applies separately).
    dashboard._clipboard_command.cache_clear()
    monkeypatch.setattr(dashboard.shutil, "which", lambda name: None)
    assert dashboard._clipboard_command() is None
    dashboard._clipboard_command.cache_clear()


async def test_pressing_y_copies_the_slug(monkeypatch: Any) -> None:
    # `y` copies the highlighted task's slug to the host clipboard tool.
    copied: list[str] = []
    monkeypatch.setattr(dashboard, "_clipboard_copy", lambda text: bool(copied.append(text)))
    app = Dashboard(_FakeClient([_TASK]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert copied == ["fix-widget"]


async def test_pressing_y_with_no_slug_warns(monkeypatch: Any) -> None:
    # An unprovisioned task (no slug) → nothing copied; warn and stay up.
    copied: list[str] = []
    monkeypatch.setattr(dashboard, "_clipboard_copy", lambda text: bool(copied.append(text)))
    app = Dashboard(_FakeClient([{**_TASK, "slug": None}]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert copied == []
        assert app.is_running


# 2119: REQ-017.1.1
async def test_pressing_v_edits_slug_only_while_detail_is_open() -> None:
    other = {**_TASK, "id": "task-other456789", "slug": "other-widget"}
    fake = _FakeClient([_TASK.copy(), other])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()
        assert not isinstance(app.screen, dashboard.SlugScreen)
        assert fake.set_slugs == []
        assert fake.get_task("task-abcdef0123")["slug"] == "fix-widget"

        await pilot.press("d")
        await pilot.press("j")
        await pilot.press("v")
        await pilot.pause()
        assert isinstance(app.screen, dashboard.SlugScreen)
        assert app.screen.query_one(Input).value == "other-widget"


# 2119: REQ-017.1.1
async def test_slug_editor_uses_current_service_value_over_stale_list_snapshot() -> None:
    class DivergentClient(_FakeClient):
        def get_task(self, task_id: str) -> dict[str, Any]:
            task = super().get_task(task_id)
            return {**task, "slug": "service-current"}

    app = Dashboard(DivergentClient([{**_TASK, "slug": "stale-summary"}]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.press("v")
        await pilot.pause()
        assert app.screen.query_one(Input).value == "service-current"


# 2119: REQ-017.2.1
async def test_submitting_slug_editor_renames_highlighted_task_and_refreshes() -> None:
    other = {**_TASK, "id": "task-other456789", "slug": "leave-alone"}
    fake = _FakeClient([_TASK.copy(), other])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.press("v")
        await pilot.pause()
        calls_before_submit = fake.list_tasks_calls
        editor = app.screen.query_one(Input)
        editor.value = "  better-widget-name  "
        await pilot.press("enter")
        await pilot.pause()

        assert fake.set_slugs == [("task-abcdef0123", "better-widget-name")]
        assert fake.list_tasks_calls > calls_before_submit
        assert "better-widget-name" in str(app.query_one("#detail", Static).render())
        assert "better-widget-name" in str(app.query_one("#tasks", DataTable).get_row_at(0))
        assert fake._tasks[1]["slug"] == "leave-alone"


# 2119: REQ-017.2.1
async def test_rejected_slug_keeps_dashboard_running_and_existing_slug_visible() -> None:
    class RejectingClient(_FakeClient):
        def set_slug(self, task_id: str, slug: str) -> dict[str, Any]:
            request = httpx.Request("PUT", f"http://service/tasks/{task_id}/slug")
            response = httpx.Response(
                400, request=request, json={"detail": "invalid artifact segment"}
            )
            raise httpx.HTTPStatusError("bad slug", request=request, response=response)

    app = Dashboard(RejectingClient([_TASK.copy()]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.press("v")
        await pilot.pause()
        app.screen.query_one(Input).value = "bad/name"
        await pilot.press("enter")
        await pilot.pause()

        assert app.is_running
        assert "fix-widget" in str(app.query_one("#detail", Static).render())


# 2119: REQ-017.3.1
async def test_cancelling_slug_editor_does_not_rename_task() -> None:
    fake = _FakeClient([_TASK.copy()])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.press("v")
        await pilot.pause()
        app.screen.query_one(Input).value = "discard-me"
        await pilot.press("escape")
        await pilot.pause()

        assert fake.set_slugs == []
        assert "fix-widget" in str(app.query_one("#detail", Static).render())


# 2119: REQ-017.4.1
async def test_detail_pane_shows_edit_slug_key_hint() -> None:
    app = Dashboard(_FakeClient([_TASK.copy()]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        rendered: Any = app.query_one("#detail", Static).render()
        assert str(rendered).splitlines()[-2] == "v: edit slug"
        assert rendered.spans[-2].style.dim is True


# 2119: REQ-024.7.1
async def test_copy_chord_hints_match_case_sensitive_dashboard_bindings() -> None:
    copy_hotkeys = {
        hotkey.action: hotkey
        for hotkey in dashboard.HOTKEYS
        if hotkey.action in {"copy_slug", "copy_id"}
    }
    assert copy_hotkeys["copy_slug"].key == "y"
    assert copy_hotkeys["copy_id"].key == "Y"
    assert copy_hotkeys["copy_slug"].display == "y"
    assert copy_hotkeys["copy_id"].display == "Shift+Y"

    app = Dashboard(_FakeClient([_TASK.copy()]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        detail = app.query_one("#detail", Static).render()
        assert str(detail).splitlines()[-1] == "c: copy details  y: copy slug  Shift+Y: copy id"

        await pilot.press("question_mark")
        await pilot.pause()
        help_text = str(app.screen.query_one("#help-keys", Static).render())
        assert "y     Copy the task's slug to the clipboard" in help_text
        assert "Shift+Y Copy the task's id to the clipboard" in help_text


async def test_pressing_shift_y_copies_the_id(monkeypatch: Any) -> None:
    # `Y` copies the highlighted task's internal id.
    copied: list[str] = []
    monkeypatch.setattr(dashboard, "_clipboard_copy", lambda text: bool(copied.append(text)))
    app = Dashboard(_FakeClient([_TASK]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("Y")
        await pilot.pause()
        assert copied == ["task-abcdef0123"]


async def test_pressing_ctrl_c_with_detail_open_shows_quit_notice_and_does_not_copy(
    monkeypatch: Any,
) -> None:
    copied: list[str] = []
    notices: list[tuple[str, str]] = []
    app = Dashboard(_FakeClient([_TASK]))  # type: ignore[arg-type]
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    monkeypatch.setattr(
        app,
        "notify",
        lambda message, *, title="", **kwargs: notices.append((message, title)),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert copied == []
        assert len(notices) == 1
        message, title = notices[0]
        assert "quit the app" in message
        assert title == "Do you want to quit?"


async def test_pressing_c_with_detail_open_copies_the_rendered_detail(monkeypatch: Any) -> None:
    # 2119: REQ-007.1.1
    copied: list[str] = []
    other = {**_TASK, "id": "task-second9999", "slug": "other"}
    app = Dashboard(_FakeClient([_TASK, other]))  # type: ignore[arg-type]
    monkeypatch.setattr(app, "_copy_to_clipboard", copied.append)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.press("j")
        await pilot.press("c")
        await pilot.pause()
        assert copied == [render_detail(other)]
        assert "task-second9999" in copied[0]


# 2119: REQ-007.1.1
async def test_pressing_c_attempts_both_clipboard_paths(monkeypatch: Any) -> None:
    terminal_copies: list[str] = []
    host_copies: list[str] = []
    app = Dashboard(_FakeClient([_TASK]))  # type: ignore[arg-type]
    monkeypatch.setattr(app, "copy_to_clipboard", terminal_copies.append)
    monkeypatch.setattr(dashboard, "_clipboard_copy", lambda text: host_copies.append(text))

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()

        expected = render_detail(_TASK)
        assert terminal_copies == [expected]
        assert host_copies == [expected]


# 2119: REQ-007.1.1
async def test_pressing_c_attempts_host_clipboard_after_terminal_failure(monkeypatch: Any) -> None:
    host_copies: list[str] = []
    app = Dashboard(_FakeClient([_TASK]))  # type: ignore[arg-type]
    monkeypatch.setattr(app, "copy_to_clipboard", _raise)
    monkeypatch.setattr(dashboard, "_clipboard_copy", lambda text: host_copies.append(text))

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()

        assert host_copies == [render_detail(_TASK)]


# 2119: REQ-007.1.1
async def test_pressing_c_attempts_terminal_clipboard_when_host_fails(monkeypatch: Any) -> None:
    terminal_copies: list[str] = []
    app = Dashboard(_FakeClient([_TASK]))  # type: ignore[arg-type]
    monkeypatch.setattr(app, "copy_to_clipboard", terminal_copies.append)
    monkeypatch.setattr(dashboard, "_clipboard_copy", _raise)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()

        assert terminal_copies == [render_detail(_TASK)]
        assert app.is_running


def test_render_detail_shows_the_claim() -> None:
    assert "claimed:" not in render_detail(_TASK)
    assert "claimed: host-1" in render_detail({**_TASK, "claimed_by": "host-1"})


def test_status_cell_displays_the_composed_status_color_coded() -> None:
    # The dashboard no longer computes status — the task service composes `container_status` and the
    # cell just renders it (color-coded). No per-task registration calls anymore.
    assert _status_cell({"container_status": "live"}).plain == "live"
    assert _status_cell({"container_status": "live"}).style == "green"
    assert _status_cell({"container_status": "building"}).style == "yellow"  # spawn in flight
    assert _status_cell({"container_status": "healing"}).plain == "healing"
    assert _status_cell({"container_status": "healing"}).style == "cyan"  # self-heal in progress
    assert _status_cell({"container_status": "down"}).style == "red"  # needs attention
    assert _status_cell({"container_status": "failed"}).style == "red"
    assert _status_cell({"container_status": "disconnected"}).style == "red"
    assert _status_cell({"container_status": "–"}).plain == "–"  # terminal task
    assert _status_cell({}).plain == "–"  # missing → em-dash, no crash


async def test_task_counter_shows_agent_versus_active_counts() -> None:
    # Counter shows agent-turn active / total active; terminal tasks are excluded.
    # pause() lets Footer's _bindings_ready recompose fire so #task-counter is mounted;
    # a second action_refresh() then populates it with the correct counts.
    tasks = [
        {**_TASK, "id": "t-agent", "slug": "a1", "state": "WORKING", "turn": "agent"},
        {**_TASK, "id": "t-user", "slug": "u1", "state": "PLANNING", "turn": "user"},
        {**_TASK, "id": "t-done", "slug": "d1", "state": "COMPLETE", "turn": "agent"},
    ]
    app = Dashboard(_FakeClient(tasks))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_refresh()  # Footer is now ready; refresh to populate the counter
        await pilot.pause()
        text = str(app.query_one("#task-counter", Static).render())
        assert "1/2" in text  # 1 agent-turn, 2 total active (COMPLETE excluded)
        assert "agent" in text


async def test_status_cell_is_used_without_per_task_registration_calls() -> None:
    # Building the table must not fan out a registrations request per row (the old N+1) — the status
    # rides on each task dict from the single list_tasks. A registrations call here would raise.
    class _NoRegClient(_FakeClient):
        def list_registrations(self, task_id: str) -> list[dict[str, Any]]:
            raise AssertionError("the table must not call list_registrations per task")

    task = {**_TASK, "container_status": "live"}
    app = Dashboard(_NoRegClient([task]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:  # mounts + paints the table via action_refresh
        await pilot.pause()


async def test_respawn_releases_a_down_tasks_claim() -> None:
    task = {**_TASK, "claimed_by": "host-1", "container_status": "down"}
    fake = _FakeClient([task], {})
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()
        assert fake.released == [task["id"]]  # released → the runner re-spawns it


# 2119: REQ-006.1.1
# 2119: REQ-006.2.1
# 2119: REQ-006.2.2
async def test_bulk_respawn_binding_lists_only_down_tasks_for_confirmation() -> None:
    down_a = {
        **_TASK,
        "id": "down-abcdef0123",
        "slug": "restart-api",
        "memo": "recover the API container after the host reboot and restore traffic without delay",
        "claimed_by": "host-1",
        "container_status": "down",
    }
    down_b = {
        **_TASK,
        "id": "down-fedcba9876",
        "slug": None,
        "memo": "recover   the\nbackground worker",
        "claimed_by": "host-1",
        "container_status": "down",
    }
    exact_memo = "x" * 60
    down_c = {
        **_TASK,
        "id": "down-123456789",
        "slug": "exact-boundary",
        "memo": exact_memo,
        "claimed_by": "host-1",
        "container_status": "down",
    }
    down_d = {
        **_TASK,
        "id": "down-space-boundary",
        "slug": "normalized-boundary",
        "memo": "a" * 58 + "   bc",
        "claimed_by": "host-1",
        "container_status": "down",
    }
    down_e = {
        **_TASK,
        "id": "down-empty-memo",
        "slug": "no-memo",
        "memo": None,
        "claimed_by": "host-1",
        "container_status": "down",
    }
    down_f = {
        **_TASK,
        "id": "down-padded-memo",
        "slug": "padded-memo",
        "memo": "  padded memo  ",
        "claimed_by": "host-1",
        "container_status": "down",
    }
    live = {
        **_TASK,
        "id": "live-0000000001",
        "slug": "healthy-web",
        "container_status": "live",
    }
    failed = {
        **_TASK,
        "id": "failed-00000001",
        "slug": "broken-spawn",
        "container_status": "failed",
    }
    app = Dashboard(_FakeClient([down_a, live, failed, down_b, down_c, down_d, down_e, down_f]))  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()

        assert isinstance(app.screen, dashboard.BulkRespawnScreen)
        text = str(app.screen.query_one("#bulk-respawn-tasks", Static).render())
        assert text.splitlines() == [
            f"down-123  exact-boundary  {exact_memo}",
            "down-abc  restart-api  recover the API container after the host reboot and restore …",
            "down-emp  no-memo",
            "down-fed  –  recover the background worker",
            "down-pad  padded-memo  padded memo",
            f"down-spa  normalized-boundary  {'a' * 58} b…",
        ]


# 2119: REQ-006.2.1
async def test_bulk_respawn_uses_latest_snapshot_at_exactly_one_down_boundary() -> None:
    fake = _FakeClient([{**_TASK, "container_status": "live"}])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        down = {**_TASK, "id": "newly-down", "container_status": "down"}
        fake._tasks = [down]
        await pilot.press("ctrl+r")
        await pilot.pause()

        assert isinstance(app.screen, dashboard.BulkRespawnScreen)
        assert app.screen._tasks == (down,)


# 2119: REQ-006.4.1
# 2119: REQ-006.4.2
# 2119: REQ-006.4.3
async def test_bulk_respawn_confirmation_releases_each_down_task_once_in_order(
    monkeypatch: Any,
) -> None:
    first = {
        **_TASK,
        "id": "down-first-0001",
        "slug": "first",
        "claimed_by": "host-1",
        "container_status": "down",
    }
    second = {
        **_TASK,
        "id": "down-second-002",
        "slug": "second",
        "claimed_by": "host-1",
        "container_status": "down",
    }

    class _SequencedClient(_FakeClient):
        events: list[tuple[str, str]] = []
        active_releases = 0
        max_active_releases = 0
        release_lock = threading.Lock()

        def get_task(self, task_id: str) -> dict[str, Any]:
            self.events.append(("check", task_id))
            return super().get_task(task_id)

        def release(self, task_id: str) -> dict[str, Any]:
            with self.release_lock:
                self.active_releases += 1
                self.max_active_releases = max(self.max_active_releases, self.active_releases)
            try:
                time.sleep(0.01)  # force concurrently started releases to overlap observably
                self.events.append(("release", task_id))
                return super().release(task_id)
            finally:
                with self.release_lock:
                    self.active_releases -= 1

    fake = _SequencedClient(
        [second, first]
    )  # reverse service order; display order sorts first → second
    notices: list[str] = []
    app = Dashboard(fake)  # type: ignore[arg-type]

    def record_notice(message: str, **kwargs: Any) -> None:
        notices.append(message)
        fake.events.append(("notify", message))

    monkeypatch.setattr(app, "notify", record_notice)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()
        text = str(app.screen.query_one("#bulk-respawn-tasks", Static).render())
        assert text.index("down-fir") < text.index("down-sec")
        await pilot.press("enter")
        await pilot.pause()

        assert fake.events == [
            ("check", first["id"]),
            ("release", first["id"]),
            ("check", second["id"]),
            ("release", second["id"]),
            ("notify", "respawned 2"),
        ]
        assert fake.got_tasks == fake.released == [first["id"], second["id"]]
        assert fake.max_active_releases == 1
        assert notices == ["respawned 2"]


# 2119: REQ-006.4.3
async def test_bulk_respawn_reports_zero_when_every_candidate_recovers(
    monkeypatch: Any,
) -> None:
    candidate = {**_TASK, "claimed_by": "host-1", "container_status": "down"}
    fake = _FakeClient([candidate])
    notices: list[str] = []
    app = Dashboard(fake)  # type: ignore[arg-type]
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notices.append(message))

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()
        fake._tasks[0] = {**candidate, "container_status": "live"}
        await pilot.press("enter")
        await pilot.pause()

        assert fake.released == []
        assert notices == ["respawned 0"]


# 2119: REQ-006.4.3
async def test_bulk_respawn_reports_through_the_dashboard_notification_surface() -> None:
    candidate = {**_TASK, "claimed_by": "host-1", "container_status": "down"}
    fake = _FakeClient([candidate])
    app = Dashboard(fake)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()
        fake._tasks[0] = {**candidate, "container_status": "live"}
        await pilot.press("enter")
        await pilot.pause()

        assert [notification.message for notification in app._notifications] == ["respawned 0"]


# 2119: REQ-006.4.1
# 2119: REQ-006.4.4
# 2119: REQ-006.4.5
@pytest.mark.parametrize(
    "recovered_status",
    [
        "–",
        "queued",
        "healing",
        "claiming",
        "preparing",
        "building",
        "starting",
        "awaiting",
        "live",
        "failed",
        "disconnected",
    ],
)
async def test_bulk_respawn_confirmation_silently_skips_a_task_no_longer_down(
    monkeypatch: Any, recovered_status: str
) -> None:
    still_down = {
        **_TASK,
        "id": "down-still-00001",
        "slug": "still-down",
        "claimed_by": "host-1",
        "container_status": "down",
    }
    recovered = {
        **_TASK,
        "id": "down-recovered-2",
        "slug": "recovered",
        "claimed_by": "host-1",
        "container_status": "down",
    }
    fake = _FakeClient([still_down, recovered])
    notices: list[str] = []
    app = Dashboard(fake)  # type: ignore[arg-type]
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notices.append(message))

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()
        # Replace the service-side object: the modal keeps its original down snapshot, so only a
        # confirmation-time get_task call can observe that this candidate recovered.
        fake._tasks[1] = {**recovered, "container_status": recovered_status}
        await pilot.press("enter")
        await pilot.pause()

        assert fake.got_tasks == [recovered["id"], still_down["id"]]
        assert fake.released == [still_down["id"]]
        assert notices == ["respawned 1"]


# 2119: REQ-006.3.1
async def test_bulk_respawn_escape_cancels_without_releasing_claims() -> None:
    task = {
        **_TASK,
        "claimed_by": "host-1",
        "container_status": "down",
    }
    fake = _FakeClient([task])
    app = Dashboard(fake)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert isinstance(app.screen, dashboard.BulkRespawnScreen)
        await pilot.press("escape")
        await pilot.pause()

        assert len(app.screen_stack) == 1
        assert fake.released == []


# 2119: REQ-006.5.1
# 2119: REQ-006.5.2
async def test_bulk_respawn_with_no_down_tasks_notifies_without_opening_modal(
    monkeypatch: Any,
) -> None:
    tasks = [
        {**_TASK, "id": "live", "container_status": "live"},
        {**_TASK, "id": "failed", "container_status": "failed"},
        {**_TASK, "id": "queued", "container_status": "queued"},
    ]
    notices: list[str] = []
    app = Dashboard(_FakeClient(tasks))  # type: ignore[arg-type]
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notices.append(message))

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()

        assert notices == ["no down tasks"]
        assert len(app.screen_stack) == 1


# 2119: REQ-006.5.1
# 2119: REQ-006.5.2
@pytest.mark.parametrize(
    "latest",
    [[]]
    + [
        [{**_TASK, "container_status": status}]
        for status in [
            "–",
            "queued",
            "healing",
            "claiming",
            "preparing",
            "building",
            "starting",
            "awaiting",
            "live",
            "failed",
            "disconnected",
            "Down",
            "down ",
        ]
    ],
)
async def test_bulk_respawn_empty_latest_snapshot_reports_no_down_tasks(
    monkeypatch: Any, latest: list[dict[str, Any]]
) -> None:
    fake = _FakeClient([{**_TASK, "container_status": "down"}])
    notices: list[str] = []
    app = Dashboard(fake)  # type: ignore[arg-type]
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notices.append(message))

    async with app.run_test() as pilot:
        await pilot.pause()
        opened: list[Any] = []
        monkeypatch.setattr(app, "push_screen", lambda screen, *args: opened.append(screen))
        fake._tasks = latest
        await pilot.press("ctrl+r")
        await pilot.pause()

        assert notices == ["no down tasks"]
        assert opened == []
        assert len(app.screen_stack) == 1


def test_matches_is_a_case_insensitive_substring_over_identifying_fields() -> None:
    task = {**_TASK, "slug": "fix-widget", "state": "WORKING", "workflow": "spike"}
    assert _matches(task, "")  # empty query → no filter
    assert _matches(task, "widget")  # slug substring
    assert _matches(task, "WIDGET")  # case-insensitive
    assert _matches(task, "working")  # state
    assert _matches(task, "spike")  # workflow
    assert not _matches(task, task["id"][:6])  # id is not a search field
    assert not _matches(task, "nope")
    # memo is searchable too
    assert _matches({**task, "memo": "make it green"}, "green")
    assert _matches({**_TASK, "memo": None}, "")  # None memo doesn't blow up


def test_repo_cell_returns_name_from_cache() -> None:
    names = {"r1": "acme/widgets", "r2": "acme/api"}
    assert _repo_cell({"repo_id": "r1"}, names) == "acme/widgets"
    assert _repo_cell({"repo_id": "r2"}, names) == "acme/api"
    assert _repo_cell({"repo_id": "unknown"}, names) == "unknown"  # fallback: bare id
    assert _repo_cell({}, names) == "?"  # no repo_id at all


def test_slug_cell_combines_slug_and_memo() -> None:
    # Returns a Rich Text (not a markup str) so the "[" survives — compare on .plain.
    # both present → slug[memo]
    assert _slug_cell({**_TASK, "slug": "fix-widget", "memo": "make it green"}).plain == (
        "fix-widget[make it green]"
    )
    # slug, no memo → bare slug
    assert _slug_cell({**_TASK, "slug": "fix-widget", "memo": None}).plain == "fix-widget"
    assert _slug_cell({"slug": "fix-widget"}).plain == "fix-widget"  # memo key absent
    # no slug, with memo → "[memo]" (no leading dash)
    assert _slug_cell({"slug": None, "memo": "make it green"}).plain == "[make it green]"
    assert _slug_cell({"memo": "make it green"}).plain == "[make it green]"  # slug key absent
    # neither → "-"
    assert _slug_cell({"slug": None}).plain == "-"
    assert _slug_cell({}).plain == "-"
    # multi-line memo → only the first line shown in the table cell
    assert _slug_cell({"slug": "s", "memo": "line one\nline two"}).plain == "s[line one]"
    assert _slug_cell({"memo": "line one\nline two"}).plain == "[line one]"


# 2119: REQ-042.1
async def test_dashboard_slug_prefix_identifies_a_task_with_artifacts() -> None:
    task = {**_TASK, "has_artifacts": True}
    fake = _FakeClient([task], artifacts={_TASK["id"]: ["specification.md"]})
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        slug_cell = app.query_one("#tasks", DataTable).get_row(_TASK["id"])[4]
        assert slug_cell.plain == "*a | fix-widget"


# 2119: REQ-042.2
async def test_dashboard_slug_prefix_identifies_github_pull_request_number() -> None:
    urls_and_labels = [
        ("https://github.com/acme/widgets/pull/123", "*PR123 | fix-widget"),
        ("http://github.com/acme/widgets/pull/456/files?diff=split#top", "*PR456 | fix-widget"),
        ("https://example.com/acme/widgets/pull/123", "fix-widget"),
        ("ssh://github.com/acme/widgets/pull/123", "fix-widget"),
        ("https://github.com.example/acme/widgets/pull/123", "fix-widget"),
        ("https://github.com/acme/widgets/pull/123abc", "fix-widget"),
        ("https://github.com/acme/widgets/pull/", "fix-widget"),
        ("http://[", "fix-widget"),
        ("https://github.com／evil/acme/widgets/pull/123", "fix-widget"),
    ]
    tasks = [
        {**_TASK, "id": f"task-url-{number}", "url": url}
        for number, (url, _) in enumerate(urls_and_labels)
    ]
    app = Dashboard(_FakeClient(tasks), refresh_interval=0)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        for number, (_, expected) in enumerate(urls_and_labels):
            assert table.get_row(f"task-url-{number}")[4].plain == expected


async def test_dashboard_task_refresh_does_not_list_artifacts_per_task() -> None:
    class NoArtifactListingClient(_FakeClient):
        def list_artifacts(self, task_id: str) -> list[str]:
            raise AssertionError(f"unexpected per-task artifact request for {task_id}")

    tasks = [
        {**_TASK, "id": f"task-{number}", "has_artifacts": number == 7} for number in range(20)
    ]
    app = Dashboard(
        NoArtifactListingClient(tasks, artifacts={"task-7": ["review.md"]}),
        refresh_interval=0,
    )  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        assert table.get_row("task-7")[4].plain == "*a | fix-widget"


# 2119: REQ-042.3
def test_slug_cell_stacks_artifact_then_pull_request_with_one_separator() -> None:
    task = {
        **_TASK,
        "url": "https://github.com/acme/widgets/pull/123/files?diff=split#discussion",
    }
    assert _slug_cell(task, has_artifacts=True).plain == "*a *PR123 | fix-widget"


# 2119: REQ-042.4
def test_slug_cell_indicators_preserve_structural_prefix_slug_and_memo() -> None:
    task = {
        **_TASK,
        "url": "https://github.com/acme/widgets/pull/7",
        "memo": "make it green\nextra detail",
    }
    assert _slug_cell(task, "├─ ", "▾ ", has_artifacts=True).plain == (
        "├─ ▾ *a *PR7 | fix-widget[make it green]"
    )
    assert _slug_cell({**task, "url": None}, "├─ ", "▾ ", has_artifacts=True).plain == (
        "├─ ▾ *a | fix-widget[make it green]"
    )
    assert _slug_cell(task, "├─ ", "▾ ").plain == ("├─ ▾ *PR7 | fix-widget[make it green]")
    assert _slug_cell({**task, "url": "https://example.com/pull/7"}, "├─ ", "▾ ").plain == (
        "├─ ▾ fix-widget[make it green]"
    )


def test_memo_textarea_height_logic() -> None:
    # on_text_area_changed sets styles.height = min(line_count, MAX_LINES); verify the formula.
    from panopticon.terminal.dashboard import MemoTextArea

    assert max(1, len("".splitlines())) == 1
    assert max(1, len("one line".splitlines())) == 1
    assert max(1, len("a\nb\nc".splitlines())) == 3
    assert (
        min(max(1, len(("\n" * 15).splitlines())), MemoTextArea.MAX_LINES) == MemoTextArea.MAX_LINES
    )


def test_harness_selector_starts_on_the_effective_harness() -> None:
    from panopticon.terminal.dashboard import HarnessSelector

    sel = HarnessSelector("codex", ["claude", "codex"])
    assert sel.value == "codex"


def test_harness_selector_falls_back_to_first_when_effective_is_unknown() -> None:
    from panopticon.terminal.dashboard import HarnessSelector

    sel = HarnessSelector("nonexistent-harness", ["claude", "codex"])
    assert sel.value == "claude"


def test_harness_selector_initial_tracks_the_resolved_fallback_not_the_raw_effective() -> None:
    # Regression: `initial` must reflect what `value` actually starts at (the resolved fallback),
    # not the raw `effective` argument — otherwise a caller comparing `value` against `effective`
    # for an unknown default would see a spurious "override" on an untouched selector.
    from panopticon.terminal.dashboard import HarnessSelector

    sel = HarnessSelector("nonexistent-harness", ["claude", "codex"])
    assert sel.initial == sel.value == "claude"


# 2119: REQ-018.19.1
def test_harness_selector_cycles_forward_and_wraps() -> None:
    from panopticon.terminal.dashboard import HarnessSelector

    sel = HarnessSelector("claude", ["claude", "codex"])
    sel.action_cycle()
    assert sel.value == "codex"
    sel.action_cycle()
    assert sel.value == "claude"  # wraps back around


def test_slug_cell_is_text_so_brackets_arent_eaten_as_markup() -> None:
    # The regression: a bare string cell is rendered through Textual markup, which swallows "[…]"
    # (e.g. "fix-widget[make it green]" → "fix-widget"). A Text renders literally.
    from rich.text import Text

    cell = _slug_cell({"slug": "fix-widget", "memo": "make it green"})
    assert isinstance(cell, Text)
    assert Text.from_markup(cell.plain).plain != cell.plain  # plain str WOULD be mangled by markup


_FIX = {**_TASK, "id": "t-fix", "slug": "fix-widget", "state": "WORKING", "workflow": "spike"}
_DEP = {
    **_TASK,
    "id": "t-dep",
    "slug": "deploy-api",
    "state": "PLANNING",
    "workflow": "github-self-reviewed",
}


async def test_pressing_slash_filters_the_task_list_as_you_type() -> None:
    app = Dashboard(_FakeClient([_FIX, _DEP]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        assert table.row_count == 2
        await pilot.press("slash")  # enter search mode → the box reveals + focuses
        await pilot.pause()
        assert app.query_one("#search", Input).styles.display == "block"
        await pilot.press("d", "e", "p")  # type a substring of the deploy-api slug
        await pilot.pause()
        assert [str(k.value) for k in table.rows] == ["t-dep"]  # only the match remains


async def test_search_matches_state_and_workflow_not_just_slug() -> None:
    app = Dashboard(_FakeClient([_FIX, _DEP]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        await pilot.press("slash")
        await pilot.press("p", "l", "a", "n")  # matches _DEP's PLANNING state
        await pilot.pause()
        assert [str(k.value) for k in table.rows] == ["t-dep"]


async def test_enter_locks_the_filter_and_restores_navigation() -> None:
    app = Dashboard(_FakeClient([_FIX, _DEP]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        await pilot.press("slash")
        await pilot.press("f", "i", "x")
        await pilot.press("enter")  # lock: box hides, filter stays, table regains focus
        await pilot.pause()
        assert app.query_one("#search", Input).styles.display == "none"
        assert app._query == "fix"  # filter preserved
        assert [str(k.value) for k in table.rows] == ["t-fix"]
        assert table.has_focus  # navigation keys work again


async def test_escape_clears_the_search() -> None:
    app = Dashboard(_FakeClient([_FIX, _DEP]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        await pilot.press("slash")
        await pilot.press("f", "i", "x")
        await pilot.pause()
        assert table.row_count == 1
        await pilot.press("escape")  # clear: query reset, all rows return, box hidden
        await pilot.pause()
        assert app._query == ""
        assert app.query_one("#search", Input).styles.display == "none"
        assert table.row_count == 2


async def test_search_filter_survives_auto_refresh() -> None:
    # The filter lives in action_refresh, so a change-feed rebuild keeps it applied.
    app = Dashboard(_FakeClient([_FIX, _DEP]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        await pilot.press("slash")
        await pilot.press("f", "i", "x")
        await pilot.pause()
        assert table.row_count == 1
        app.action_refresh()  # a rebuild (as the timer would do) keeps the filter
        await pilot.pause()
        assert [str(k.value) for k in table.rows] == ["t-fix"]


async def test_repo_column_shows_repo_name() -> None:
    # The "repo" column displays the repo's human-readable name, not its id.
    repos = [{"id": "r1", "name": "acme/widgets", "git_url": "", "default_base": "main"}]
    task = {**_TASK, "repo_id": "r1"}
    app = Dashboard(_FakeClient([task], repos=repos))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        row = table.get_row(task["id"])
        # row is (state, turn, container, repo, slug[memo]) — repo is index 3
        assert row[3] == "acme/widgets"


async def test_search_matches_repo_name() -> None:
    # Filtering by repo name surfaces only tasks belonging to that repo.
    repos = [
        {"id": "r1", "name": "acme/widgets", "git_url": "", "default_base": "main"},
        {"id": "r2", "name": "acme/api", "git_url": "", "default_base": "main"},
    ]
    task_w = {**_TASK, "id": "t-w", "repo_id": "r1", "slug": "fix-widget"}
    task_a = {**_TASK, "id": "t-a", "repo_id": "r2", "slug": "add-endpoint"}
    app = Dashboard(_FakeClient([task_w, task_a], repos=repos))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        assert table.row_count == 2
        await pilot.press("slash")
        await pilot.press("w", "i", "d", "g", "e", "t", "s")  # matches "acme/widgets" repo name
        await pilot.pause()
        assert [str(k.value) for k in table.rows] == ["t-w"]


async def test_repo_names_refresh_after_repo_edit() -> None:
    # After the repo-config screen closes, the column reflects the updated name.
    repos = [{"id": "r1", "name": "old-name", "git_url": "", "default_base": "main"}]
    task = {**_TASK, "repo_id": "r1"}
    fake = _FakeClient([task], repos=repos)
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        assert table.get_row(task["id"])[3] == "old-name"
        # Simulate a rename via the service (as RepoFormScreen would do) then the dismiss reload.
        fake.update_repo("r1", name="new-name")
        app._load_repo_names()
        app.action_refresh()
        await pilot.pause()
        assert table.get_row(task["id"])[3] == "new-name"


async def test_respawn_releases_a_live_tasks_claim() -> None:
    task = {**_TASK, "claimed_by": "host-1", "container_status": "live"}
    fake = _FakeClient([task], {task["id"]: [{"container_id": "c"}]})
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()
        assert fake.released == [task["id"]]  # live container → released so runner kills + respawns


# -- repo config screen (`g`) -------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/acme/widgets.git", "widgets"),
        ("git@github.com:acme/widgets.git", "widgets"),
        ("https://github.com/acme/widgets", "widgets"),  # no .git suffix
        ("https://github.com/acme/widgets/", "widgets"),  # trailing slash
        ("ssh://git@host:22/acme/widgets.git", "widgets"),
        ("", ""),  # empty
        ("widgets", ""),  # bare token, no path → unparseable
        ("   ", ""),  # whitespace only
    ],
)
def test_repo_name_from_git_url(url: str, expected: str) -> None:
    assert dashboard._repo_name_from_git_url(url) == expected


async def test_pressing_g_opens_the_repos_screen_listing_repos() -> None:
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "acme/widgets",
                "git_url": "https://x/r1.git",
                "default_base": "main",
            }
        ],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        assert isinstance(app.screen, dashboard.ReposScreen)
        table = app.screen.query_one("#repos", DataTable)
        assert table.row_count == 1


# 2119: layered-settings-hints.2.1
# 2119: layered-settings-hints.3.1
# 2119: layered-settings-hints.5.1
async def test_repos_screen_signposts_both_neighbouring_settings_layers_in_one_muted_line() -> None:
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "acme/widgets",
                "git_url": "https://x/r1.git",
                "default_base": "main",
            }
        ],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()

        hint = app.screen.query_one("#repos-layered-settings-hint", Label)
        rendered = str(hint.render())
        assert rendered == (
            "Reviewer defaults: workflow config; Workflow availability: workflow config; "
            "Harness/model defaults: override at per-task creation."
        )
        assert rendered == dashboard.layered_settings_hint("repos")
        assert len(app.screen.query(".layered-settings-hint")) == 1
        assert [str(label.render()) for label in app.screen.query(Label)] == [
            dashboard.ReposScreen.TITLE,
            rendered,
        ]
        assert hint.styles.color.a == pytest.approx(0.6)


async def test_pressing_w_lists_registered_workflows_and_marks_built_ins() -> None:
    fake = _FakeClient(
        [],
        workflows=[
            {
                "name": "spike",
                "when_to_use": "Open-ended work.",
                "path": "/site-packages/panopticon/workflows/spike.py",
                "built_in": True,
            },
            {
                "name": "release",
                "when_to_use": "Ship a release.",
                "path": "/config/workflows/release.py",
                "built_in": False,
            },
        ],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause()
        assert isinstance(app.screen, dashboard.WorkflowsScreen)
        table = app.screen.query_one("#workflows", DataTable)
        assert table.row_count == 2
        assert "built-in (edit with care)" in str(table.get_row_at(0))
        assert "Ship a release." in str(table.get_row_at(1))


# 2119: layered-settings-hints.2.1
# 2119: layered-settings-hints.3.1
# 2119: layered-settings-hints.4.1
async def test_workflows_screen_signposts_repo_overrides_and_filtering_in_one_muted_line() -> None:
    fake = _FakeClient(
        [],
        workflows=[
            {
                "name": "spike",
                "when_to_use": "Open-ended work.",
                "path": "/site-packages/panopticon/workflows/spike.py",
                "built_in": True,
            }
        ],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause()

        hint = app.screen.query_one("#workflows-layered-settings-hint", Label)
        rendered = str(hint.render())
        assert rendered == (
            "Reviewer defaults: repo config can override; Workflow availability: repo config "
            "filters; Harness/model defaults: override at per-task creation."
        )
        assert rendered == dashboard.layered_settings_hint("workflows")
        assert len(app.screen.query(".layered-settings-hint")) == 1
        assert [str(label.render()) for label in app.screen.query(Label)] == [
            dashboard.WorkflowsScreen.TITLE,
            rendered,
        ]
        assert hint.styles.color.a == pytest.approx(0.6)


# 2119: layered-settings-hints.1.1
# 2119: layered-settings-hints.2.1
def test_layered_setting_registry_declares_every_current_relationship_bidirectionally(
    monkeypatch: Any,
) -> None:
    declared = list(dashboard.LAYERED_SETTINGS)
    relationships = {relationship.key: relationship for relationship in declared}
    assert len(relationships) == len(declared)
    assert {
        key: (
            item.default_surface,
            item.default_layer_name,
            item.override_surface,
            item.override_layer_name,
            item.relationship,
            item.setting_names,
        )
        for key, item in relationships.items()
    } == {
        "reviewer-models": (
            "workflows",
            "workflow config",
            "repos",
            "repo config",
            "override",
            ("honesty_reviewer", "reviewer_1", "reviewer_2"),
        ),
        "workflow-task-launch": (
            "workflows",
            "workflow config",
            "task-creation",
            "per-task creation",
            "override",
            ("default_harness", "default_model"),
        ),
        "task-launch": (
            "repos",
            "repo config",
            "task-creation",
            "per-task creation",
            "override",
            ("default_harness", "default_model"),
        ),
        "workflow-availability": (
            "workflows",
            "workflow config",
            "repos",
            "repo config",
            "filter",
            ("opt_in", "enabled_workflows", "disabled_workflows"),
        ),
    }

    for relationship in declared:
        monkeypatch.setattr(dashboard, "LAYERED_SETTINGS", (relationship,))
        default_hint = dashboard.layered_settings_hint(relationship.default_surface)
        override_hint = dashboard.layered_settings_hint(relationship.override_surface)
        assert relationship.override_layer_name in default_hint
        assert relationship.default_layer_name in override_hint


# 2119: layered-settings-hints.3.1
async def test_each_layered_settings_surface_calls_the_shared_hint_renderer(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(dashboard, "layered_settings_hint", lambda surface: f"renderer:{surface}")
    fake = _FakeClient(
        [],
        repos=[{"id": "r1", "name": "r1", "git_url": "", "default_base": "main"}],
        workflows=[
            {
                "name": "spike",
                "when_to_use": "",
                "path": "/workflows/spike.py",
                "built_in": True,
            }
        ],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")
        assert str(app.screen.query_one("#workflows-layered-settings-hint", Label).render()) == (
            "renderer:workflows"
        )
        await pilot.press("escape", "g")
        assert str(app.screen.query_one("#repos-layered-settings-hint", Label).render()) == (
            "renderer:repos"
        )
        await pilot.press("escape", "n", "enter", "enter")
        assert str(app.screen.query_one("#launch-summary", Static).render()).endswith(
            "renderer:task-creation"
        )


def test_create_workflow_file_writes_discoverable_minimal_template(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))

    path = dashboard._create_workflow_file("release-check")

    assert path == tmp_path / "workflows" / "release-check.py"
    content = path.read_text()
    assert "class ReleaseCheck(Workflow):" in content
    assert 'name: ClassVar[str] = "release-check"' in content
    assert "class Working(InitialState):" in content
    assert "transitions = (Complete,)" in content
    assert "transitions = (Review,)" in content

    from panopticon.workflows.discovery import discover_workflows

    registry = discover_workflows(_home_workflows=tmp_path / "workflows")
    assert "release-check" in registry


async def test_workflows_screen_new_creates_template_and_opens_it(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))
    monkeypatch.setattr(App, "suspend", lambda self: contextlib.nullcontext())
    opened: list[Path] = []
    fake = _FakeClient([], workflows=[])

    def open_and_register(path: Path) -> None:
        opened.append(path)
        fake._workflows.append(
            {
                "name": "release-check",
                "when_to_use": "Check a release.",
                "path": str(path),
                "built_in": False,
            }
        )

    monkeypatch.setattr(dashboard, "_open_file_in_editor", open_and_register)
    app = Dashboard(fake)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#workflow-name", Input).value = "release-check"
        await pilot.press("enter")
        await pilot.pause()
        table = app.screen.query_one("#workflows", DataTable)
        assert table.row_count == 1
        assert "release-check" in str(table.get_row_at(0))

    path = tmp_path / "workflows" / "release-check.py"
    assert opened == [path]
    assert "class ReleaseCheck(Workflow):" in path.read_text()


async def test_workflows_screen_refreshes_after_editing_an_existing_file(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(App, "suspend", lambda self: contextlib.nullcontext())
    workflow = {
        "name": "release",
        "when_to_use": "Old description.",
        "path": "/config/workflows/release.py",
        "built_in": False,
    }
    fake = _FakeClient([], workflows=[workflow])

    def edit(_: Path) -> None:
        workflow["when_to_use"] = "New description."

    monkeypatch.setattr(dashboard, "_open_file_in_editor", edit)
    app = Dashboard(fake)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        table = app.screen.query_one("#workflows", DataTable)
        assert "New description." in str(table.get_row_at(0))


# 2119: REQ-001.1.1
# 2119: REQ-001.2.1
# 2119: REQ-001.6.1
# 2119: REQ-001.7.1
async def test_workflows_screen_x_opens_honest_confirmation_for_highlighted_operator_file(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("first")
    second.write_text("second")
    fake = _FakeClient(
        [],
        workflows=[
            {
                "name": "first",
                "when_to_use": "First workflow.",
                "path": str(first),
                "built_in": False,
            },
            {
                "name": "second",
                "when_to_use": "Second workflow.",
                "path": str(second),
                "built_in": False,
            },
        ],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await pilot.press("w")
        await pilot.press("n")
        await pilot.pause()
        new_workflow_box = app.screen.query_one("#new-workflow-box")
        reference_style = (
            new_workflow_box.region.width,
            str(new_workflow_box.styles.width),
            str(new_workflow_box.styles.height),
            new_workflow_box.styles.border,
            new_workflow_box.styles.background,
        )
        theme = app.get_css_variables()
        assert reference_style[:3] == (56, "56", "auto")
        assert new_workflow_box.styles.border.top[0] == "round"
        assert new_workflow_box.styles.border.top[1].hex == theme["accent"]
        assert new_workflow_box.styles.background.hex == theme["surface"]
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("x")
        await pilot.pause()

        assert not isinstance(app.screen, dashboard.WorkflowsScreen)
        message = "\n".join(label.content for label in app.screen.query(Label))
        assert message == (
            "delete workflow 'second'?\n"
            "This removes the file; the workflow remains loaded until the running service's "
            "next restart."
        )
        yes = app.screen.query_one("#delete-workflow-yes", Button)
        no = app.screen.query_one("#delete-workflow-no", Button)
        assert str(yes.label).lower() == "yes"
        assert str(no.label).lower() == "no"
        assert yes.display and no.display
        assert not yes.disabled and not no.disabled
        box = app.screen.query_one("#delete-workflow-box")
        assert (
            box.region.width,
            str(box.styles.width),
            str(box.styles.height),
            box.styles.border,
            box.styles.background,
        ) == reference_style
        assert box.region.x == (app.size.width - box.region.width) // 2
        assert box.region.y == (app.size.height - box.region.height) // 2


# 2119: REQ-001.1.1
async def test_workflows_screen_x_targets_first_highlighted_operator_file(tmp_path: Path) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("first")
    second.write_text("second")
    fake = _FakeClient(
        [],
        workflows=[
            {"name": "first", "when_to_use": "", "path": str(first), "built_in": False},
            {"name": "second", "when_to_use": "", "path": str(second), "built_in": False},
        ],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")
        await pilot.press("x")
        await pilot.pause()

        labels = "\n".join(label.content for label in app.screen.query(Label))
        assert "delete workflow 'first'?" in labels
        assert "second" not in labels


# 2119: REQ-001.2.1
# 2119: REQ-001.3.1
@pytest.mark.parametrize("cancel", ["no", "escape"])
async def test_workflow_delete_confirmation_cancels_without_removing_file(
    tmp_path: Path, cancel: str
) -> None:
    path = tmp_path / "release.py"
    path.write_text("workflow")
    fake = _FakeClient(
        [],
        workflows=[
            {
                "name": "release",
                "when_to_use": "Ship a release.",
                "path": str(path),
                "built_in": False,
            }
        ],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")
        await pilot.press("x")
        await pilot.pause()
        if cancel == "no":
            await pilot.click("#delete-workflow-no")
        else:
            await pilot.press("escape")
        await pilot.pause()

        assert isinstance(app.screen, dashboard.WorkflowsScreen)
        assert path.read_text() == "workflow"


# 2119: REQ-001.2.1
# 2119: REQ-001.4.1
async def test_workflow_delete_confirmation_yes_removes_file_and_refreshes_loaded_registry(
    tmp_path: Path, monkeypatch: Any
) -> None:
    first_path = tmp_path / "first.py"
    first_path.write_text("first workflow")
    path = tmp_path / "release.py"
    path.write_text("workflow")
    first_workflow = {
        "name": "first",
        "when_to_use": "First workflow.",
        "path": str(first_path),
        "built_in": False,
    }
    workflow = {
        "name": "release",
        "when_to_use": "Before deletion.",
        "path": str(path),
        "built_in": False,
    }
    fake = _FakeClient([], workflows=[first_workflow, workflow])
    list_calls = 0

    def list_workflow_files() -> list[dict[str, Any]]:
        nonlocal list_calls
        list_calls += 1
        description = "Before deletion." if list_calls == 1 else "Loaded until restart."
        return [first_workflow, {**workflow, "when_to_use": description}]

    monkeypatch.setattr(fake, "list_workflow_files", list_workflow_files)
    app = Dashboard(fake)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")
        await pilot.press("down")
        await pilot.press("x")
        await pilot.pause()
        await pilot.click("#delete-workflow-yes")
        await pilot.pause()

        assert not path.exists()
        assert first_path.read_text() == "first workflow"
        assert list_calls == 2
        table = app.screen.query_one("#workflows", DataTable)
        assert "Loaded until restart." in str(table.get_row_at(1))


# 2119: REQ-001.5.1
# 2119: REQ-001.8.1
async def test_workflows_screen_refuses_builtin_deletion_with_notification(
    tmp_path: Path, monkeypatch: Any
) -> None:
    path = tmp_path / "spike.py"
    path.write_text("built in")
    fake = _FakeClient(
        [],
        workflows=[
            {
                "name": "spike",
                "when_to_use": "Open-ended work.",
                "path": str(path),
                "built_in": True,
            }
        ],
    )
    notices: list[str] = []
    app = Dashboard(fake)  # type: ignore[arg-type]
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notices.append(message))

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")
        notices.clear()  # prove the deletion request, not screen mount, emits the warning
        await pilot.press("x")
        await pilot.pause()

        assert isinstance(app.screen, dashboard.WorkflowsScreen)
        assert path.read_text() == "built in"
        assert notices == ["Built-in workflows cannot be deleted."]


async def test_pressing_s_in_the_repos_screen_creates_a_setup_repo_task() -> None:
    # The setup-repo workflow is hidden from the pickers; the repos modal's `s` hotkey is how it's
    # launched — one setup-repo task for the highlighted repo, seeded with a memo.
    fake = _FakeClient(
        [_TASK],
        repos=[
            {
                "id": "r1",
                "name": "acme/widgets",
                "git_url": "https://x/r1.git",
                "default_base": "main",
            }
        ],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        # creating the task dismisses the repos modal, dropping back to the task view
        assert not isinstance(app.screen, dashboard.ReposScreen)
    assert len(fake.created) == 1
    repo_id, workflow, memo, _, _, _ = fake.created[0]
    assert (repo_id, workflow) == ("r1", "setup-repo")
    assert memo is not None and "acme/widgets" in memo


async def test_no_repos_auto_opens_the_repos_screen_on_start() -> None:
    # First-run nudge: with no repos configured, the dashboard drops straight into the repo
    # screen so the operator can add one (a task can't be created without a repo).
    app = Dashboard(_FakeClient([_TASK], repos=[]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, dashboard.ReposScreen)


async def test_repos_present_does_not_auto_open_the_repos_screen() -> None:
    # The common case: at least one repo → no auto-open, the operator lands on the task view.
    app = Dashboard(_FakeClient([_TASK], repos=["r1"]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not isinstance(app.screen, dashboard.ReposScreen)


async def test_repo_fetch_error_does_not_auto_open_the_repos_screen() -> None:
    # A down service can't list repos (and the repo screen couldn't either) — treat repos as
    # present and leave the operator on the task view rather than popping a screen that'd fail.
    fake = _FakeClient([_TASK], repos=[])
    fake.list_repos = _raise  # type: ignore[method-assign]
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not isinstance(app.screen, dashboard.ReposScreen)


async def test_repos_screen_creates_a_repo_autofilling_from_the_git_url() -> None:
    fake = _FakeClient([], repos=[])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("n")  # open the create form
        await pilot.pause()
        # Only the git URL is typed; id and name auto-fill from it, default_base defaults to main.
        app.screen.query_one("#field-git_url", Input).value = "git@github.com:acme/widgets.git"
        await pilot.press("enter")  # submit the form
        await pilot.pause()
        assert fake.created_repos == [
            {
                "id": "widgets",
                "name": "widgets",
                "git_url": "git@github.com:acme/widgets.git",
                "default_base": "main",
                "env_file": None,
                "image_layer_file": None,
                "hook_file": None,
                "enabled_workflows": [],
                "disabled_workflows": [],
                "capabilities": {"docker_in_docker": False},
            }
        ]


async def test_repo_form_autofill_only_fills_blank_fields() -> None:
    fake = _FakeClient([], repos=[])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#field-git_url", Input).value = "https://x/widgets.git"
        app.screen.query_one("#field-id", Input).value = "r9"  # pre-typed → kept
        app.screen.query_one("#field-name", Input).value = "acme/new"  # pre-typed → kept
        await pilot.press("enter")
        await pilot.pause()
        # id/name keep the user's values — pre-typed fields are never clobbered by autofill.
        assert fake.created_repos == [
            {
                "id": "r9",
                "name": "acme/new",
                "git_url": "https://x/widgets.git",
                "default_base": "main",
                "env_file": None,
                "image_layer_file": None,
                "hook_file": None,
                "enabled_workflows": [],
                "disabled_workflows": [],
                "capabilities": {"docker_in_docker": False},
            }
        ]


async def test_repo_form_git_url_leads_and_default_base_prefills_main() -> None:
    fake = _FakeClient([], repos=[])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        # git_url is the first Input on the form; default_base is pre-filled with main.
        inputs = [w.id for w in app.screen.query(Input)]
        assert inputs[0] == "field-git_url"
        assert inputs.index("field-git_url") < inputs.index("field-id") < inputs.index("field-name")
        assert app.screen.query_one("#field-default_base", Input).value == "main"


async def test_repo_form_autofills_on_git_url_blur() -> None:
    fake = _FakeClient([], repos=[])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#field-git_url", Input).value = "https://x/widgets.git"
        app.screen.query_one("#field-name", Input).focus()  # blur git_url
        await pilot.pause()
        assert app.screen.query_one("#field-name", Input).value == "widgets"
        assert app.screen.query_one("#field-id", Input).value == "widgets"


async def test_repo_form_edit_mode_does_not_autofill_blank_fields() -> None:
    fake = _FakeClient(
        [],
        repos=[
            {"id": "r1", "name": "", "git_url": "https://x/widgets.git", "default_base": "main"}
        ],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("e")  # edit
        await pilot.pause()
        # Editing an existing repo never derives values: blanks stay blank even on blur.
        app.screen.query_one("#field-default_base", Input).focus()  # blur git_url
        await pilot.pause()
        assert app.screen.query_one("#field-name", Input).value == ""
        assert app.screen.query_one("#field-env_file", dashboard.EnvFileField).env_file_value == ""


async def test_repos_screen_create_requires_id_name_and_git_url() -> None:
    fake = _FakeClient([], repos=[])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#field-id", Input).value = "r9"  # name + git_url left blank
        await pilot.press("enter")
        await pilot.pause()
        assert fake.created_repos == []  # refused; nothing created
        # The form stays open showing the error inline (invalid input isn't lost).
        assert isinstance(app.screen, dashboard.RepoFormScreen)
        error = app.screen.query_one("#form-error", Static)
        assert "required" in str(error.render())


async def test_repos_screen_create_keeps_form_open_on_invalid_env_file() -> None:
    """A server-rejected env_file (400) leaves the repo form open with the error shown inline,
    rather than closing the modal and toasting the error afterward."""
    fake = _FakeClient([], repos=[])
    fake.repo_error = "env_file 'nope' does not exist under the secrets dir"
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#field-id", Input).value = "r9"
        app.screen.query_one("#field-name", Input).value = "r9"
        app.screen.query_one("#field-git_url", Input).value = "https://x/r9.git"
        await pilot.press("enter")
        await pilot.pause()
        assert fake.created_repos == []  # the create raised → nothing recorded
        assert isinstance(app.screen, dashboard.RepoFormScreen)  # form still open
        error = app.screen.query_one("#form-error", Static)
        assert "does not exist under the secrets dir" in str(error.render())


async def test_repos_screen_edit_keeps_form_open_on_invalid_env_file() -> None:
    """Same for edit: a rejected PATCH keeps the edit form open with the error shown inline."""
    fake = _FakeClient(
        [],
        repos=[{"id": "r1", "name": "old", "git_url": "https://x/r1.git", "default_base": "main"}],
    )
    fake.repo_error = "env_file 'nope' does not exist under the secrets dir"
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        app.screen.query_one("#field-name", Input).value = "new"
        await pilot.press("enter")
        await pilot.pause()
        assert fake.updated_repos == []  # the update raised → nothing recorded
        assert isinstance(app.screen, dashboard.RepoFormScreen)  # form still open
        error = app.screen.query_one("#form-error", Static)
        assert "does not exist under the secrets dir" in str(error.render())


async def test_repos_screen_edits_a_repo_via_patch() -> None:
    fake = _FakeClient(
        [],
        repos=[{"id": "r1", "name": "old", "git_url": "https://x/r1.git", "default_base": "main"}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("e")  # edit the highlighted repo
        await pilot.pause()
        assert app.screen.query_one("#field-name", Input).value == "old"  # pre-populated
        app.screen.query_one("#field-name", Input).value = "new"
        await pilot.press("enter")
        await pilot.pause()
        # Core fields, capabilities, and workflow preferences are all PATCHed together.
        # The checkbox is unchecked → docker_in_docker=False; the layer field is empty →
        # image_layer_file=None. No workflows were passed to the form so enabled/disabled are empty.
        assert fake.updated_repos == [
            (
                "r1",
                {
                    "name": "new",
                    "git_url": "https://x/r1.git",
                    "default_base": "main",
                    "default_harness": None,
                    "default_model": None,
                    "env_file": None,
                    "image_layer_file": None,
                    "hook_file": None,
                    "capabilities": {"docker_in_docker": False},
                    "enabled_workflows": [],
                    "disabled_workflows": [],
                },
            )
        ]


async def test_repo_form_shows_effective_launch_defaults_for_unset_and_set_values() -> None:
    fake = _FakeClient(
        [],
        repos=[
            {"id": "r1", "name": "one", "git_url": "https://x/r1.git", "default_base": "main"},
            {
                "id": "r2",
                "name": "two",
                "git_url": "https://x/r2.git",
                "default_base": "main",
                "default_harness": "codex",
                "default_model": "gpt-5.6-sol:high",
            },
        ],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g", "e")
        await pilot.pause()
        harness = app.screen.query_one(dashboard.RepoHarnessSelector)
        model = app.screen.query_one("#default-model-effective", Static)
        assert "harness: claude (app default)" in str(harness.render())
        assert "model: harness default (app default)" in str(model.render())
        await pilot.press("escape")
        repos = app.screen.query_one("#repos", DataTable)
        repos.move_cursor(row=1)
        await pilot.press("e")
        await pilot.pause()
        harness = app.screen.query_one(dashboard.RepoHarnessSelector)
        model = app.screen.query_one("#default-model-effective", Static)
        assert "harness: codex (repo default)" in str(harness.render())
        assert "model: gpt-5.6-sol:high (repo default)" in str(model.render())


async def test_repo_form_edits_launch_defaults_via_patch() -> None:
    fake = _FakeClient(
        [],
        repos=[{"id": "r1", "name": "one", "git_url": "https://x/r1.git", "default_base": "main"}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g", "e")
        await pilot.pause()
        selector = app.screen.query_one(dashboard.RepoHarnessSelector)
        selector.focus()
        await pilot.press("enter")  # claude → codex
        app.screen.query_one("#field-default_model", Input).value = "gpt-5.6-sol:high"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert fake.updated_repos[0][1]["default_harness"] == "codex"
        assert fake.updated_repos[0][1]["default_model"] == "gpt-5.6-sol:high"


async def test_repo_form_workflows_tab_pre_populates_from_repo() -> None:
    """The workflows tab in the repo form pre-populates checkboxes from the repo's stored prefs."""
    existing = {
        "id": "r1",
        "name": "old",
        "git_url": "https://x/r1.git",
        "default_base": "main",
        "enabled_workflows": ["github-self-reviewed"],
        "disabled_workflows": ["orchestrator"],
    }
    workflows = [
        {"name": "spike", "when_to_use": "free-form", "opt_in": False},
        {"name": "github-self-reviewed", "when_to_use": "self-reviewed", "opt_in": True},
        {"name": "orchestrator", "when_to_use": "orchestrator workflow", "opt_in": False},
    ]
    fake = _FakeClient([], repos=[existing], workflows=workflows)
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("e")  # open edit form
        await pilot.pause()
        # opt-in workflow in enabled_workflows → checked
        assert app.screen.query_one("#wf-github-self-reviewed", SpaceCheckbox).value is True
        # opt-out workflow in disabled_workflows → unchecked
        assert app.screen.query_one("#wf-orchestrator", SpaceCheckbox).value is False
        # opt-out workflow not in disabled_workflows → checked (on by default)
        assert app.screen.query_one("#wf-spike", SpaceCheckbox).value is True
        # Save and confirm workflow prefs round-trip unchanged
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert fake.updated_repos[0][1]["enabled_workflows"] == ["github-self-reviewed"]
        assert fake.updated_repos[0][1]["disabled_workflows"] == ["orchestrator"]


async def test_repo_form_workflows_tab_toggles_save_with_form() -> None:
    """Toggling workflow checkboxes and saving the form captures them in the update call."""
    existing = {
        "id": "r1",
        "name": "old",
        "git_url": "https://x/r1.git",
        "default_base": "main",
        "enabled_workflows": [],
        "disabled_workflows": [],
    }
    workflows = [
        {"name": "spike", "when_to_use": "free-form", "opt_in": False},
        {"name": "github-peer-reviewed", "when_to_use": "review workflow", "opt_in": True},
    ]
    fake = _FakeClient([], repos=[existing], workflows=workflows)
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        # Toggle: enable the opt-in workflow and disable the opt-out one
        app.screen.query_one("#wf-github-peer-reviewed", SpaceCheckbox).value = True
        app.screen.query_one("#wf-spike", SpaceCheckbox).value = False
        # Save from the form (ctrl+s works from any focused widget)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert fake.updated_repos[0][1]["enabled_workflows"] == ["github-peer-reviewed"]
        assert fake.updated_repos[0][1]["disabled_workflows"] == ["spike"]


async def test_repos_screen_creates_a_repo_with_privileged_docker_enabled() -> None:
    fake = _FakeClient([], repos=[])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#field-git_url", Input).value = "https://x/widgets.git"
        app.screen.query_one(
            "#field-docker_in_docker", Checkbox
        ).value = True  # toggle privileged on
        await pilot.press("enter")
        await pilot.pause()
        # The toggle maps to capabilities.docker_in_docker, which drives the runner's --privileged.
        assert fake.created_repos[0]["capabilities"] == {"docker_in_docker": True}


async def test_repos_screen_edit_toggles_privileged_on_merging_existing_capabilities() -> None:
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "old",
                "git_url": "https://x/r1.git",
                "default_base": "main",
                "capabilities": {"some_other_cap": True},
            }
        ],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("e")  # edit the highlighted repo
        await pilot.pause()
        app.screen.query_one("#field-docker_in_docker", Checkbox).value = True
        await pilot.press("enter")
        await pilot.pause()
        # docker_in_docker is set; the pre-existing unrelated capability is preserved (merged).
        repo_id, changes = fake.updated_repos[0]
        assert repo_id == "r1"
        assert changes["capabilities"] == {"some_other_cap": True, "docker_in_docker": True}


async def test_repo_form_prechecks_the_toggle_for_a_privileged_repo() -> None:
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "old",
                "git_url": "https://x/r1.git",
                "default_base": "main",
                "capabilities": {"docker_in_docker": True},
            }
        ],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("e")  # edit the highlighted repo
        await pilot.pause()
        # A repo already opted into privileged docker opens the form with the box checked.
        assert app.screen.query_one("#field-docker_in_docker", Checkbox).value is True


async def test_repo_form_enter_saves_even_while_the_checkbox_is_focused() -> None:
    # Enter saves from any field, including the privileged-docker checkbox (which toggles on
    # Space only, so Enter bubbles up to the screen's submit binding).
    fake = _FakeClient([], repos=[])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#field-git_url", Input).value = "https://x/widgets.git"
        app.screen.query_one("#field-docker_in_docker", Checkbox).focus()
        await pilot.pause()
        await pilot.press("enter")  # saves rather than toggling the checkbox
        await pilot.pause()
        assert len(fake.created_repos) == 1
        assert fake.created_repos[0]["id"] == "widgets"
        # Enter didn't toggle the box on its way out.
        assert fake.created_repos[0]["capabilities"] == {"docker_in_docker": False}


async def test_repo_form_space_toggles_the_checkbox_without_saving() -> None:
    # Space toggles the focused checkbox and does not submit the form.
    fake = _FakeClient([], repos=[])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        checkbox = app.screen.query_one("#field-docker_in_docker", Checkbox)
        assert checkbox.value is False
        checkbox.focus()
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()
        assert checkbox.value is True  # toggled
        assert fake.created_repos == []  # but not saved
        assert isinstance(app.screen, dashboard.RepoFormScreen)  # form still open


@pytest.mark.asyncio
async def test_env_file_field_blank_when_no_known_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EnvFileField returns '' and shows nothing selected when secrets dir is absent."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    fake = _FakeClient(
        [], repos=[{"id": "r1", "name": "x", "git_url": "https://x/r.git", "default_base": "main"}]
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        ef = app.screen.query_one("#field-env_file", dashboard.EnvFileField)
        assert ef.env_file_value == ""


@pytest.mark.asyncio
async def test_env_file_field_pre_selects_known_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EnvFileField pre-selects an existing env_file by its name (relative to the secrets dir)."""
    cfg = tmp_path / "config" / "panopticon" / "secrets"
    cfg.mkdir(parents=True)
    (cfg / "r1.env").write_text("CLAUDE_CODE_OAUTH_TOKEN=tok")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "x",
                "git_url": "https://x/r.git",
                "default_base": "main",
                "env_file": "r1.env",
            }
        ],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        ef = app.screen.query_one("#field-env_file", dashboard.EnvFileField)
        assert ef.env_file_value == "r1.env"


@pytest.mark.asyncio
async def test_env_file_field_custom_path_pre_populates_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EnvFileField shows the custom input pre-populated when the stored name isn't a known file."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    custom = "other.env"  # a relative name with no matching file in the (absent) secrets dir
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "x",
                "git_url": "https://x/r.git",
                "default_base": "main",
                "env_file": custom,
            }
        ],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        ef = app.screen.query_one("#field-env_file", dashboard.EnvFileField)
        assert ef.env_file_value == custom
        # The custom input should be visible
        inp = ef.query_one("#env-file-input", Input)
        assert inp.display is True


@pytest.mark.asyncio
async def test_env_file_field_custom_absolute_path_normalized_to_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A custom absolute path is normalized to a bare name (resolved per-runner at launch)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    fake = _FakeClient(
        [], repos=[{"id": "r1", "name": "x", "git_url": "https://x/r.git", "default_base": "main"}]
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        ef = app.screen.query_one("#field-env_file", dashboard.EnvFileField)
        sel = ef.query_one("#env-file-select", Select)
        sel.value = ef._CUSTOM
        await pilot.pause()
        ef.query_one("#env-file-input", Input).value = "/some/other/path/r1.env"
        assert ef.env_file_value == "r1.env"


@pytest.mark.asyncio
async def test_env_file_field_custom_input_draws_a_bottom_border(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The revealed custom-path input renders a full box — including its bottom border row.

    Regression: ``EnvFileField`` (a bare ``Widget``) had no explicit height, so it expanded to
    ``1fr`` and Textual's compositor clipped the last child's ``tall`` bottom-border row. Sizing
    the field to its content (``height: auto``) fixes it."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    fake = _FakeClient(
        [], repos=[{"id": "r1", "name": "x", "git_url": "https://x/r.git", "default_base": "main"}]
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        ef = app.screen.query_one("#field-env_file", dashboard.EnvFileField)
        ef.query_one("#env-file-select", Select).value = ef._CUSTOM
        await pilot.pause()
        await pilot.pause()
        inp = ef.query_one("#env-file-input", Input)
        # Read the composited screen and check the input's bottom-border row (the last row of its
        # region) is actually painted with the ``tall`` bottom-border glyph.
        rows = [
            "".join(seg.text for seg in strip) for strip in app.screen._compositor.render_strips()
        ]
        region = inp.region
        bottom_row = rows[region.y + region.height - 1]
        assert "▁" in bottom_row[region.x : region.x + region.width]


@pytest.mark.asyncio
async def test_image_layer_field_blank_when_no_known_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ImageLayerField returns '' and shows nothing selected when the layers dir is absent."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    fake = _FakeClient(
        [], repos=[{"id": "r1", "name": "x", "git_url": "https://x/r.git", "default_base": "main"}]
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        lf = app.screen.query_one("#field-image_layer_file", dashboard.ImageLayerField)
        assert lf.image_layer_value == ""


@pytest.mark.asyncio
async def test_image_layer_field_pre_selects_known_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ImageLayerField pre-selects a stored image_layer_file by its name (relative to layers dir)."""
    layers = tmp_path / "config" / "panopticon" / "layers"
    layers.mkdir(parents=True)
    (layers / "r1.dockerfile").write_text("RUN echo hi")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "x",
                "git_url": "https://x/r.git",
                "default_base": "main",
                "image_layer_file": "r1.dockerfile",
            }
        ],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        lf = app.screen.query_one("#field-image_layer_file", dashboard.ImageLayerField)
        assert lf.image_layer_value == "r1.dockerfile"


@pytest.mark.asyncio
async def test_image_layer_field_custom_absolute_path_normalized_to_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A custom absolute path is normalized to a bare name (resolved per-runner at spawn)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    fake = _FakeClient(
        [], repos=[{"id": "r1", "name": "x", "git_url": "https://x/r.git", "default_base": "main"}]
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        lf = app.screen.query_one("#field-image_layer_file", dashboard.ImageLayerField)
        sel = lf.query_one("#image-layer-select", Select)
        sel.value = lf._CUSTOM
        await pilot.pause()
        lf.query_one("#image-layer-input", Input).value = "/some/other/path/r1.dockerfile"
        assert lf.image_layer_value == "r1.dockerfile"


@pytest.mark.asyncio
async def test_repo_form_saves_a_picked_image_layer_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Picking an image_layer_file in the form PATCHes it onto the repo (ADR 0005 repo tier)."""
    layers = tmp_path / "config" / "panopticon" / "layers"
    layers.mkdir(parents=True)
    (layers / "r1.dockerfile").write_text("RUN echo hi")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    fake = _FakeClient(
        [], repos=[{"id": "r1", "name": "x", "git_url": "https://x/r.git", "default_base": "main"}]
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        lf = app.screen.query_one("#field-image_layer_file", dashboard.ImageLayerField)
        lf.query_one("#image-layer-select", Select).value = "r1.dockerfile"
        await pilot.press("enter")
        await pilot.pause()
        assert fake.updated_repos[-1][1]["image_layer_file"] == "r1.dockerfile"


@pytest.mark.asyncio
async def test_hook_file_field_blank_when_no_known_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HookFileField returns '' and shows nothing selected when the hooks dir is absent."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    fake = _FakeClient(
        [], repos=[{"id": "r1", "name": "x", "git_url": "https://x/r.git", "default_base": "main"}]
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        hf = app.screen.query_one("#field-hook_file", dashboard.HookFileField)
        assert hf.hook_file_value == ""


@pytest.mark.asyncio
async def test_hook_file_field_pre_selects_known_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HookFileField pre-selects an existing hook_file by its name (relative to the hooks dir)."""
    cfg = tmp_path / "config" / "panopticon" / "hooks"
    cfg.mkdir(parents=True)
    (cfg / "prep.sh").write_text("#!/bin/sh\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "x",
                "git_url": "https://x/r.git",
                "default_base": "main",
                "hook_file": "prep.sh",
            }
        ],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        hf = app.screen.query_one("#field-hook_file", dashboard.HookFileField)
        assert hf.hook_file_value == "prep.sh"


@pytest.mark.asyncio
async def test_hook_file_field_custom_absolute_path_normalized_to_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A custom absolute hook path is normalized to a bare name (resolved per-runner at spawn)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    fake = _FakeClient(
        [], repos=[{"id": "r1", "name": "x", "git_url": "https://x/r.git", "default_base": "main"}]
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        hf = app.screen.query_one("#field-hook_file", dashboard.HookFileField)
        sel = hf.query_one("#hook-file-select", Select)
        sel.value = hf._CUSTOM
        await pilot.pause()
        hf.query_one("#hook-file-input", Input).value = "/some/other/path/prep.sh"
        assert hf.hook_file_value == "prep.sh"


@pytest.mark.asyncio
async def test_hook_file_field_value_is_saved_on_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Editing a repo PATCHes the picked hook_file name through to the client."""
    cfg = tmp_path / "config" / "panopticon" / "hooks"
    cfg.mkdir(parents=True)
    (cfg / "prep.sh").write_text("#!/bin/sh\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    fake = _FakeClient(
        [],
        repos=[{"id": "r1", "name": "old", "git_url": "https://x/r1.git", "default_base": "main"}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        hf = app.screen.query_one("#field-hook_file", dashboard.HookFileField)
        hf.query_one("#hook-file-select", Select).value = "prep.sh"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert fake.updated_repos[0][1]["hook_file"] == "prep.sh"


def _record_popen(monkeypatch: Any) -> list[list[str]]:
    """Capture `subprocess.Popen` argv (the host-open call) without launching anything."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        dashboard.subprocess, "Popen", lambda argv, *a, **k: calls.append(list(argv))
    )
    return calls


def test_open_command_is_xdg_open_on_linux_and_open_on_mac(monkeypatch: Any) -> None:
    monkeypatch.setattr(dashboard.sys, "platform", "linux")
    assert dashboard._open_command() == "xdg-open"
    monkeypatch.setattr(dashboard.sys, "platform", "darwin")
    assert dashboard._open_command() == "open"


def test_open_path_silences_child_streams(monkeypatch: Any) -> None:
    # The opener (and anything it spawns) must not inherit the TUI's TTY — its stdout/stderr
    # would garble Textual's frame. `_open_path` redirects all three streams to DEVNULL.
    kwargs: dict[str, Any] = {}
    monkeypatch.setattr(dashboard.subprocess, "Popen", lambda argv, *a, **k: kwargs.update(k))
    dashboard._open_path("/some/artifact.md")
    assert kwargs["stdin"] == dashboard.subprocess.DEVNULL
    assert kwargs["stdout"] == dashboard.subprocess.DEVNULL
    assert kwargs["stderr"] == dashboard.subprocess.DEVNULL


async def test_pressing_a_opens_the_selected_artifact_via_rest(monkeypatch: Any) -> None:
    # `a` lists the task's artifacts; Enter fetches the selection over REST to a temp file and
    # opens it with the host handler — the universal path (works even remote from the store).
    calls = _record_popen(monkeypatch)
    fake = _FakeClient(
        [_TASK], artifacts={_TASK["id"]: ["plan.md", "notes.md"]}, artifact_content=b"# Plan\n"
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")  # open the artifacts modal
        await pilot.pause()
        await pilot.press("enter")  # select the first artifact → REST open
        await pilot.pause()
        assert fake.fetched == [(_TASK["id"], "plan.md")]
        assert len(calls) == 1
        opener, path = calls[0]
        assert opener == dashboard._open_command()
        assert Path(path).name == "plan.md"  # basename (extension) preserved for the handler
        assert Path(path).read_bytes() == b"# Plan\n"


async def test_rest_open_reuses_one_scratch_dir_and_cleans_it_up(monkeypatch: Any) -> None:
    # Opening several artifacts reuses a single temp dir (no per-open leak), removed on exit.
    calls = _record_popen(monkeypatch)
    fake = _FakeClient(
        [_TASK], artifacts={_TASK["id"]: ["plan.md", "notes.md"]}, artifact_content=b"x"
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(2):  # open two different artifacts
            await pilot.press("a")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
        roots = {str(Path(path).parent.parent) for _, path in calls}
        assert len(roots) == 1  # both opens landed under the same scratch root
        scratch = next(iter(roots))
        assert app._artifact_tmp is not None and Path(scratch).is_dir()
    assert not Path(scratch).exists()  # cleaned up on unmount


async def test_pressing_e_opens_a_locally_present_artifact_in_place(
    monkeypatch: Any, tmp_path: Path
) -> None:
    # `e` opens the on-disk artifact directly (no temp copy, no REST) when the dashboard shares
    # the store's filesystem — resolved through FilesystemArtifactStore, which owns the layout.
    calls = _record_popen(monkeypatch)
    art = tmp_path / "tasks" / str(_TASK["id"]) / "plan.md"
    art.parent.mkdir(parents=True)
    art.write_text("# Local\n")
    fake = _FakeClient([_TASK], artifacts={_TASK["id"]: ["plan.md"]}, artifact_content=b"REST")
    app = Dashboard(fake, artifacts_root=tmp_path)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert calls == [[dashboard._open_command(), str(art)]]  # the real file, in place
        assert fake.fetched == []  # no REST fetch — opened the local file


async def test_e_warns_when_the_artifact_is_not_local(monkeypatch: Any, tmp_path: Path) -> None:
    # No co-located file → warn and do nothing (no silent REST fallback).
    calls = _record_popen(monkeypatch)
    fake = _FakeClient([_TASK], artifacts={_TASK["id"]: ["plan.md"]})
    app = Dashboard(fake, artifacts_root=tmp_path)  # empty root  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert calls == []  # nothing opened
        assert fake.fetched == []  # and no REST fallback


async def test_missing_opener_binary_is_handled_not_crashed(monkeypatch: Any) -> None:
    # On a headless host without `xdg-open`, Popen raises FileNotFoundError; the dashboard must
    # notify and stay up rather than let it escape the screen callback and kill the TUI.
    def _raise(argv: Any, *a: Any, **k: Any) -> None:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(dashboard.subprocess, "Popen", _raise)
    fake = _FakeClient([_TASK], artifacts={_TASK["id"]: ["plan.md"]}, artifact_content=b"x")
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("enter")  # REST open → Popen raises FileNotFoundError
        await pilot.pause()
        assert app.is_running  # handled, TUI survived


async def test_pressing_a_with_no_artifacts_warns_and_opens_no_modal(monkeypatch: Any) -> None:
    calls = _record_popen(monkeypatch)
    fake = _FakeClient([_TASK], artifacts={})  # task has no artifacts
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert calls == []
        assert len(app.screen_stack) == 1  # the modal was not pushed
        assert app.is_running


# -- help screen (`?`) --------------------------------------------------------------


def test_footer_shows_only_the_essential_keys() -> None:
    # The legend keeps the few most-used keys; the rest still dispatch but are hidden (show=False)
    # behind the `?` help screen. BINDINGS is derived from HOTKEYS, so every entry is a Binding.
    shown = {b.key for b in Dashboard.BINDINGS if b.show}
    hidden = {b.key for b in Dashboard.BINDINGS if not b.show}
    assert shown == {"t", "n", "x", "/", "d", "question_mark", "q"}
    assert hidden == {
        "o",
        "r",
        "R",
        "ctrl+r",
        "p",
        "f",
        "g",
        "w",
        "a",
        "s",
        "u",
        "e",
        "E",
        "v",
        "y",
        "Y",
        "c",
        "escape",
    }


def test_bindings_and_help_derive_from_the_single_hotkey_table() -> None:
    # The DRY invariant: the footer bindings and the help screen are *both* derived from HOTKEYS,
    # so the keymap can't drift between them. Every binding traces back to a HOTKEYS entry, and
    # every entry's action resolves to an action_* method on the dashboard (or Textual's built-in
    # quit) — so a stale action name can't slip in.
    assert [b.key for b in Dashboard.BINDINGS] == [h.key for h in dashboard.HOTKEYS]
    shown = {h.key for h in dashboard.HOTKEYS if h.show}
    assert {b.key for b in Dashboard.BINDINGS if b.show} == shown
    for hotkey in dashboard.HOTKEYS:
        assert hotkey.action == "quit" or hasattr(Dashboard, f"action_{hotkey.action}")


# 2119: REQ-027.2.3
def test_task_snooze_keybindings_are_unique() -> None:
    keys = [hotkey.key for hotkey in dashboard.HOTKEYS]
    assert len(keys) == len(set(keys))
    actions = {hotkey.key: hotkey.action for hotkey in dashboard.HOTKEYS}
    assert actions["e"] == "snooze"
    assert actions["E"] == "snooze_indefinitely"


# 2119: REQ-040.1.1
def test_open_checkout_is_a_hidden_global_unique_hotkey() -> None:
    keys = [hotkey.key for hotkey in dashboard.HOTKEYS]
    assert len(keys) == len(set(keys))
    binding = next(hotkey for hotkey in dashboard.HOTKEYS if hotkey.key == "f")
    assert binding.action == "open_checkout"
    assert binding.show is False


def _open_checkout_app(task: dict[str, Any] | None) -> Dashboard:
    app = Dashboard(_FakeClient([]))  # type: ignore[arg-type]
    app._current = task["id"] if task is not None else None
    app._tasks = {task["id"]: task} if task is not None else {}
    return app


# 2119: REQ-040.2.1
def test_open_checkout_warns_when_no_task_is_highlighted(monkeypatch: Any) -> None:
    notices: list[tuple[str, str]] = []
    opened: list[str] = []
    app = _open_checkout_app(None)
    monkeypatch.setattr(
        app, "notify", lambda message, severity="information": notices.append((message, severity))
    )
    monkeypatch.setattr(dashboard, "_open_path", opened.append)

    app.action_open_checkout()

    assert notices == [("No task highlighted.", "warning")]
    assert opened == []

    notices.clear()
    app._current = "stale-row-key"
    app.action_open_checkout()

    assert notices == [("No task highlighted.", "warning")]
    assert opened == []


# 2119: REQ-040.3.1
def test_open_checkout_warns_when_task_is_not_provisioned(monkeypatch: Any) -> None:
    notices: list[tuple[str, str]] = []
    opened: list[str] = []
    app = _open_checkout_app({**_TASK, "clone": None})
    monkeypatch.setattr(
        app, "notify", lambda message, severity="information": notices.append((message, severity))
    )
    monkeypatch.setattr(dashboard, "_open_path", opened.append)

    app.action_open_checkout()

    assert notices == [("This task has not been provisioned yet.", "warning")]
    assert opened == []


# 2119: REQ-040.5.1
@pytest.mark.parametrize(
    ("runner_host", "message"),
    [
        ("mac-mini", "This task runs on mac-mini; its checkout isn't on this machine."),
        (None, "This task's checkout isn't on this machine."),
    ],
)
def test_open_checkout_warns_when_clone_is_not_a_local_directory(
    monkeypatch: Any, runner_host: str | None, message: str
) -> None:
    checked: list[str] = []
    opened: list[str] = []
    notices: list[tuple[str, str]] = []
    clone = "/runner/checkouts/task-abcdef0123"
    app = _open_checkout_app({**_TASK, "clone": clone, "runner_host": runner_host})
    monkeypatch.setattr(dashboard.os.path, "isdir", lambda path: bool(checked.append(path)))
    monkeypatch.setattr(dashboard, "_open_path", opened.append)
    monkeypatch.setattr(
        app, "notify", lambda text, severity="information": notices.append((text, severity))
    )

    app.action_open_checkout()

    assert checked == [clone]
    assert opened == []
    assert notices == [(message, "warning")]


# 2119: REQ-040.4.1
# 2119: REQ-040.6.1
def test_open_checkout_uses_local_directory_evidence_despite_runner_host(monkeypatch: Any) -> None:
    clone = "/local/checkouts/task-abcdef0123"
    opened: list[str] = []
    app = _open_checkout_app({**_TASK, "clone": clone, "runner_host": "mac-mini"})
    monkeypatch.setattr(dashboard.os.path, "isdir", lambda path: path == clone)
    monkeypatch.setattr(dashboard, "_open_path", opened.append)

    app.action_open_checkout()

    assert opened == [clone]


# 2119: REQ-040.7.1
def test_open_checkout_handles_missing_file_manager_opener(monkeypatch: Any) -> None:
    notices: list[tuple[str, str]] = []
    app = _open_checkout_app({**_TASK, "clone": "/local/checkout"})
    monkeypatch.setattr(dashboard.os.path, "isdir", lambda path: True)
    monkeypatch.setattr(
        dashboard,
        "_open_path",
        lambda path: (_ for _ in ()).throw(FileNotFoundError(path)),
    )
    monkeypatch.setattr(
        app, "notify", lambda text, severity="information": notices.append((text, severity))
    )

    app.action_open_checkout()

    assert notices == [("No file manager opener is installed on this machine.", "warning")]


async def test_pressing_question_mark_opens_the_help_screen() -> None:
    app = Dashboard(_FakeClient([_TASK]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, dashboard.HelpScreen)


async def test_help_screen_lists_every_hotkey() -> None:
    # The help screen is the authoritative keymap: every entry in HOTKEYS (key + description)
    # must render, so a future binding change can't quietly drop a key from the listing.
    app = Dashboard(_FakeClient([_TASK]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        text = str(app.screen.query_one("#help-keys", Static).render())
        for hotkey in dashboard.HOTKEYS:
            assert hotkey.description in text
            assert (hotkey.display or hotkey.key) in text
        # the non-essential keys (hidden from the footer) are reachable here
        assert {"r", "R", "ctrl+r", "p", "g", "a", "s", "u"} <= {h.key for h in dashboard.HOTKEYS}


# 2119: REQ-023.5.1
async def test_help_screen_documents_ensemble_toggle() -> None:
    app = Dashboard(_FakeClient([_TASK]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        lines = str(app.screen.query_one("#help-keys", Static).render()).splitlines()
        ensemble_lines = [line.strip() for line in lines if "ensemble" in line.lower()]
        assert len(ensemble_lines) == 1
        line = ensemble_lines[0]
        assert "Enter" in line
        assert "collapse" in line.lower()
        assert "expand" in line.lower()


# 2119: REQ-006.1.2
async def test_help_screen_documents_bulk_respawn_binding() -> None:
    app = Dashboard(_FakeClient([_TASK]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        lines = str(app.screen.query_one("#help-keys", Static).render()).splitlines()
        assert [line.strip() for line in lines if "Ctrl+R" in line] == [
            "Ctrl+R Respawn all down tasks"
        ]


async def test_help_screen_closes_on_escape() -> None:
    app = Dashboard(_FakeClient([_TASK]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, dashboard.HelpScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1  # dismissed — back to the task view
        assert app.is_running


# -- governor grouping (_group_by_governor / _slug_cell prefix) ----------------------


def test_group_by_governor_ungoverned_tasks_unchanged() -> None:
    t1 = {**_TASK, "id": "t1", "slug": "alpha", "governor_task_id": None}
    t2 = {**_TASK, "id": "t2", "slug": "bravo", "governor_task_id": None}
    active, terminal = _group_by_governor([t1, t2])
    assert [(t["id"], p) for t, p in active] == [("t1", ""), ("t2", "")]
    assert terminal == []


def test_group_by_governor_governed_task_appears_after_governor() -> None:
    # Both active; governed is the only (last) child → gets "└─ " connector.
    governor = {**_TASK, "id": "gov", "slug": "orchestrator", "governor_task_id": None}
    governed = {**_TASK, "id": "wrk", "slug": "worker", "governor_task_id": "gov"}
    active, terminal = _group_by_governor([governor, governed])
    assert [(t["id"], p) for t, p in active] == [("gov", ""), ("wrk", "└─ ")]
    assert terminal == []


def test_group_by_governor_governed_before_governor_in_sort_still_groups() -> None:
    # When the governed task has a later created_at than its governor, it sorts first by
    # creation order (newest-first) — but _group_by_governor must still place it AFTER the governor.
    governor = {
        **_TASK,
        "id": "gov",
        "slug": "zoo",
        "governor_task_id": None,
        "created_at": "2026-06-01T01:00:00",
    }
    governed = {
        **_TASK,
        "id": "aaa",
        "slug": "alpha",
        "governor_task_id": "gov",
        "created_at": "2026-06-01T02:00:00",
    }
    sorted_tasks = sorted([governor, governed], key=_make_sort_key())
    assert sorted_tasks[0]["id"] == "aaa"  # governed created later → sorts first
    active, terminal = _group_by_governor(sorted_tasks)
    assert [(t["id"], p) for t, p in active] == [("gov", ""), ("aaa", "└─ ")]
    assert terminal == []


def test_group_by_governor_governor_not_in_list_behaves_as_root() -> None:
    governed = {**_TASK, "id": "wrk", "slug": "worker", "governor_task_id": "missing-id"}
    active, terminal = _group_by_governor([governed])
    assert [(t["id"], p) for t, p in active] == [("wrk", "")]
    assert terminal == []


def test_group_by_governor_terminal_governed_follows_active_governor() -> None:
    # A governed task in COMPLETE state is pulled into the active section when its governor
    # is still active, so it nests under the governor above the divider.
    governor = {
        **_TASK,
        "id": "gov",
        "slug": "orchestrator",
        "governor_task_id": None,
        "state": "WORKING",
    }
    governed = {
        **_TASK,
        "id": "wrk",
        "slug": "worker",
        "governor_task_id": "gov",
        "state": "COMPLETE",
    }
    sorted_tasks = sorted([governor, governed], key=_make_sort_key())
    active, terminal = _group_by_governor(sorted_tasks)
    assert [(t["id"], p) for t, p in active] == [("gov", ""), ("wrk", "└─ ")]
    assert terminal == []


def test_group_by_governor_active_child_keeps_terminal_governor_in_active_section() -> None:
    governor = {
        **_TASK,
        "id": "gov",
        "slug": "orchestrator",
        "governor_task_id": None,
        "state": "COMPLETE",
    }
    governed = {
        **_TASK,
        "id": "wrk",
        "slug": "worker",
        "governor_task_id": "gov",
        "state": "WORKING",
    }
    active, terminal = _group_by_governor([governor, governed])
    assert [(t["id"], p) for t, p in active] == [("gov", ""), ("wrk", "└─ ")]
    assert terminal == []


def test_group_by_governor_all_terminal_no_governor_stays_terminal() -> None:
    # Two unrelated terminal tasks: no governor chain → both stay in the terminal section.
    t1 = {**_TASK, "id": "t1", "slug": "alpha", "governor_task_id": None, "state": "COMPLETE"}
    t2 = {**_TASK, "id": "t2", "slug": "bravo", "governor_task_id": None, "state": "DROPPED"}
    active, terminal = _group_by_governor([t1, t2])
    assert active == []
    assert [(t["id"], p) for t, p in terminal] == [("t1", ""), ("t2", "")]


def test_group_by_governor_multiple_governed_tasks_in_sort_order() -> None:
    governor = {**_TASK, "id": "gov", "slug": "orch", "governor_task_id": None}
    w1 = {**_TASK, "id": "w1", "slug": "alpha", "governor_task_id": "gov"}
    w2 = {**_TASK, "id": "w2", "slug": "bravo", "governor_task_id": "gov"}
    sorted_tasks = sorted([governor, w1, w2], key=_make_sort_key())
    active, terminal = _group_by_governor(sorted_tasks)
    assert [(t["id"], p) for t, p in active] == [("gov", ""), ("w1", "├─ "), ("w2", "└─ ")]
    assert terminal == []


def test_group_by_governor_tree_connectors_nested() -> None:
    # Governor → child-1 (non-last) → grandchild; Governor → child-2 (last).
    gov = {**_TASK, "id": "gov", "slug": "orch", "governor_task_id": None}
    c1 = {**_TASK, "id": "c1", "slug": "child-1", "governor_task_id": "gov"}
    gc = {**_TASK, "id": "gc", "slug": "grand", "governor_task_id": "c1"}
    c2 = {**_TASK, "id": "c2", "slug": "child-2", "governor_task_id": "gov"}
    sorted_tasks = sorted([gov, c1, gc, c2], key=_make_sort_key())
    active, terminal = _group_by_governor(sorted_tasks)
    assert [(t["id"], p) for t, p in active] == [
        ("gov", ""),
        ("c1", "├─ "),
        ("gc", "│  └─ "),
        ("c2", "└─ "),
    ]
    assert terminal == []


def test_slug_cell_prefix_tree_connectors() -> None:
    task = {**_TASK, "slug": "worker", "memo": None}
    assert _slug_cell(task).plain == "worker"  # no prefix (root)
    assert _slug_cell(task, "├─ ").plain == "├─ worker"  # non-last child
    assert _slug_cell(task, "└─ ").plain == "└─ worker"  # last child
    assert _slug_cell(task, "│  └─ ").plain == "│  └─ worker"  # nested


def test_slug_cell_prefix_with_memo() -> None:
    task = {"slug": "worker", "memo": "fix it"}
    assert _slug_cell(task, "├─ ").plain == "├─ worker[fix it]"


# 2119: REQ-023.2.1
async def test_governed_task_appears_under_governor_in_dashboard() -> None:
    # Governor and governed both active; governed follows governor with a tree connector.
    # Governors start collapsed — expand before checking the child row.
    governor = {**_TASK, "id": "gov", "slug": "orchestrator", "governor_task_id": None}
    governed = {**_TASK, "id": "wrk", "slug": "worker", "governor_task_id": "gov"}
    app = Dashboard(_FakeClient([governor, governed]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        table.move_cursor(row=table.get_row_index("gov"))
        await pilot.press("enter")  # expand the ensemble
        await pilot.pause()
        order = [str(k.value) for k in table.rows]
        assert order == ["gov", "wrk"]
        gov_row = table.get_row("gov")
        wrk_row = table.get_row("wrk")
        assert gov_row[4].plain == "▾ orchestrator"
        assert wrk_row[4].plain == "└─ worker"  # last (only) child gets └─


async def test_active_governor_keeps_terminal_child_in_active_section() -> None:
    # Regression: a terminal governed task whose governor is still active must stay in the
    # active section (above the terminal section), not below it.
    # Governors start collapsed — expand before checking the child row's position and styling.
    governor = {
        **_TASK,
        "id": "gov",
        "slug": "orchestrator",
        "governor_task_id": None,
        "state": "WORKING",
    }
    governed = {
        **_TASK,
        "id": "wrk",
        "slug": "worker",
        "governor_task_id": "gov",
        "state": "COMPLETE",
        "turn": "agent",
    }
    other_done = {
        **_TASK,
        "id": "done",
        "slug": "other",
        "governor_task_id": None,
        "state": "COMPLETE",
        "turn": "agent",
    }
    tasks = sorted([governor, governed, other_done], key=_make_sort_key())
    app = Dashboard(_FakeClient(tasks))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        table.move_cursor(row=table.get_row_index("gov"))
        await pilot.press("enter")  # expand the ensemble
        await pilot.pause()
        keys = [str(k.value) for k in table.rows]
        # Governor and its terminal child are in the active section (above "done").
        assert keys.index("gov") < keys.index("done")
        assert keys.index("wrk") < keys.index("done")
        # Active governor is not faded; both terminal tasks (standalone and governed) are.
        assert not any(s.style == "dim" for s in table.get_row("gov")[4]._spans)
        for task_id in ("wrk", "done"):
            slug = table.get_row(task_id)[4]
            assert slug._spans and all(s.style == "dim" for s in slug._spans), (
                f"{task_id} slug should be dim"
            )


# -- ensemble collapse (_group_section collapsed / Dashboard Enter) ---------------


def test_ensemble_row_replaces_children_when_collapsed() -> None:
    # A collapsed governor's single child becomes one ensemble placeholder row.
    governor = {**_TASK, "id": "gov", "slug": "orch", "governor_task_id": None}
    worker = {**_TASK, "id": "wrk", "slug": "worker", "governor_task_id": "gov"}
    result = _group_section([governor, worker], collapsed={"gov"})
    assert len(result) == 2
    gov_row, ens_row = result
    assert gov_row[0]["id"] == "gov"
    assert ens_row[0].get("_ensemble") is True
    assert ens_row[0]["_governor_id"] == "gov"
    assert ens_row[0]["_count"] == 1
    assert ens_row[1] == "└─ "  # sole child → last-child connector


def test_ensemble_row_multiple_children_collapsed() -> None:
    # All direct children collapse to one ensemble row; count reflects original child count.
    gov = {**_TASK, "id": "gov", "slug": "orch", "governor_task_id": None}
    w1 = {**_TASK, "id": "w1", "slug": "alpha", "governor_task_id": "gov"}
    w2 = {**_TASK, "id": "w2", "slug": "bravo", "governor_task_id": "gov"}
    w3 = {**_TASK, "id": "w3", "slug": "charlie", "governor_task_id": "gov"}
    result = _group_section([gov, w1, w2, w3], collapsed={"gov"})
    assert len(result) == 2  # governor + one ensemble row (not three child rows)
    ens = result[1][0]
    assert ens["_ensemble"] is True
    assert ens["_count"] == 3


def test_ensemble_not_emitted_when_no_children() -> None:
    # Collapsed flag on a non-governor (no children) is silently ignored — no ensemble row.
    solo = {**_TASK, "id": "solo", "slug": "lone", "governor_task_id": None}
    result = _group_section([solo], collapsed={"solo"})
    assert len(result) == 1
    assert not result[0][0].get("_ensemble")


def test_ensemble_connector_inherits_parent_continuation() -> None:
    # When a nested governor (itself a child) is collapsed, its ensemble row picks up the
    # parent's continuation bars in the prefix.
    root = {**_TASK, "id": "root", "slug": "root", "governor_task_id": None}
    mid = {**_TASK, "id": "mid", "slug": "mid", "governor_task_id": "root"}
    leaf = {**_TASK, "id": "leaf", "slug": "leaf", "governor_task_id": "mid"}
    # Collapse only the middle level.
    result = _group_section([root, mid, leaf], collapsed={"mid"})
    rows = [(r["id"] if not r.get("_ensemble") else "__ensemble__", p) for r, p in result]
    assert rows == [
        ("root", ""),
        ("mid", "└─ "),  # mid is last (and only) child of root → └─
        (
            "__ensemble__",
            "   └─ ",
        ),  # ensemble is child of mid; mid is last child → "   " continuation
    ]


def test_ensemble_unexpanded_shows_real_rows() -> None:
    # With no collapsed governors, the output is identical to the non-collapsed path.
    gov = {**_TASK, "id": "gov", "slug": "orch", "governor_task_id": None}
    wrk = {**_TASK, "id": "wrk", "slug": "worker", "governor_task_id": "gov"}
    expanded = _group_section([gov, wrk], collapsed=set())
    assert [(r["id"], p) for r, p in expanded] == [("gov", ""), ("wrk", "└─ ")]


def test_matches_always_passes_ensemble_rows() -> None:
    ensemble: dict[str, Any] = {"_ensemble": True, "_governor_id": "gov", "_count": 2}
    assert _matches(ensemble, "some-query-that-wont-match")
    assert _matches(ensemble, "")


# 2119: REQ-023.1.1
async def test_collapsed_ensemble_row_explains_hidden_child_count() -> None:
    # Governors start collapsed — the ensemble row is present on startup without pressing Enter.
    governor = {**_TASK, "id": "gov", "slug": "orchestrator", "governor_task_id": None}
    children = [
        {**_TASK, "id": "wrk-1", "slug": "worker-1", "governor_task_id": "gov"},
        {**_TASK, "id": "wrk-2", "slug": "worker-2", "governor_task_id": "gov"},
        # A grandchild distinguishes the required direct-child count (2) from all descendants (3).
        {**_TASK, "id": "leaf", "slug": "leaf", "governor_task_id": "wrk-1"},
    ]
    app = Dashboard(_FakeClient([governor, *children]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        keys = [str(k.value) for k in table.rows]
        # Initial state: governor present, child hidden behind ensemble sentinel.
        assert keys == ["gov", f"{_ENSEMBLE_KEY_PREFIX}gov"]
        assert all(child["id"] not in keys for child in children)
        assert f"{_ENSEMBLE_KEY_PREFIX}gov" in keys
        # Essential information comes first so normal slug-column truncation degrades gracefully.
        ens_row = table.get_row(f"{_ENSEMBLE_KEY_PREFIX}gov")
        slug = ens_row[4]
        assert slug.plain == "└─ ▸ 2 child tasks — enter to expand"
        assert slug.spans == [Span(0, len(slug.plain), "dim")]


# 2119: REQ-023.1.1
async def test_collapsed_ensemble_count_updates_with_the_current_snapshot() -> None:
    governor = {**_TASK, "id": "gov", "slug": "orchestrator", "governor_task_id": None}
    first = {**_TASK, "id": "wrk-1", "slug": "worker-1", "governor_task_id": "gov"}
    fake = _FakeClient([governor, first])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        sentinel = f"{_ENSEMBLE_KEY_PREFIX}gov"
        assert table.get_row(sentinel)[4].plain == "└─ ▸ 1 child task — enter to expand"

        fake._tasks.append({**_TASK, "id": "wrk-2", "slug": "worker-2", "governor_task_id": "gov"})
        app.action_refresh()
        await pilot.pause()
        assert table.get_row(sentinel)[4].plain == "└─ ▸ 2 child tasks — enter to expand"


# 2119: REQ-023.1.1
# 2119: REQ-023.2.1
# 2119: REQ-023.3.1
async def test_nested_governor_has_matching_disclosures_and_summary() -> None:
    root = {**_TASK, "id": "root", "slug": "root", "governor_task_id": None}
    middle = {**_TASK, "id": "middle", "slug": "middle", "governor_task_id": "root"}
    leaf = {**_TASK, "id": "leaf", "slug": "leaf", "governor_task_id": "middle"}
    app = Dashboard(_FakeClient([root, middle, leaf]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        table.move_cursor(row=table.get_row_index("root"))
        await pilot.press("enter")
        await pilot.pause()

        sentinel = f"{_ENSEMBLE_KEY_PREFIX}middle"
        assert table.get_row("middle")[4].plain == "└─ ▸ middle"
        summary = table.get_row(sentinel)[4]
        assert summary.plain == "   └─ ▸ 1 child task — enter to expand"
        assert summary.spans == [Span(0, len(summary.plain), "dim")]

        table.move_cursor(row=table.get_row_index("middle"))
        await pilot.press("enter")
        await pilot.pause()
        assert table.get_row("middle")[4].plain == "└─ ▾ middle"
        assert table.get_row("leaf")[4].plain == "   └─ leaf"


# 2119: REQ-023.1.1
# 2119: REQ-023.2.1
# 2119: REQ-023.3.1
async def test_terminal_governor_collapses_and_expands_active_child() -> None:
    governor = {
        **_TASK,
        "id": "gov",
        "slug": "done-governor",
        "governor_task_id": None,
        "state": "COMPLETE",
    }
    governed = {
        **_TASK,
        "id": "wrk",
        "slug": "active-worker",
        "governor_task_id": "gov",
        "state": "WORKING",
    }
    app = Dashboard(_FakeClient([governor, governed]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        sentinel = f"{_ENSEMBLE_KEY_PREFIX}gov"
        assert [str(key.value) for key in table.rows] == ["gov", sentinel]
        assert table.get_row("gov")[4].plain == "▸ done-governor"
        assert table.get_row(sentinel)[4].plain == "└─ ▸ 1 child task — enter to expand"

        table.move_cursor(row=table.get_row_index("gov"))
        await pilot.press("enter")
        await pilot.pause()
        assert [str(key.value) for key in table.rows] == ["gov", "wrk"]
        assert table.get_row("gov")[4].plain == "▾ done-governor"


# 2119: REQ-023.2.1
# 2119: REQ-023.3.1
async def test_enter_again_on_governor_expands_ensemble() -> None:
    # Governors start collapsed; Enter toggles: first press expands, second press collapses again.
    governor = {**_TASK, "id": "gov", "slug": "orchestrator", "governor_task_id": None}
    governed = {**_TASK, "id": "wrk", "slug": "worker", "governor_task_id": "gov"}
    fake = _FakeClient([governor, governed])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        table.move_cursor(row=table.get_row_index("gov"))
        assert table.get_row("gov")[4].plain == "▸ orchestrator"
        # First Enter → expand (starts collapsed).
        await pilot.press("enter")
        await pilot.pause()
        assert "wrk" in [str(k.value) for k in table.rows]
        assert f"{_ENSEMBLE_KEY_PREFIX}gov" not in [str(k.value) for k in table.rows]
        assert table.get_row("gov")[4].plain == "▾ orchestrator"
        # Second Enter → collapse again.
        await pilot.press("enter")
        await pilot.pause()
        keys = [str(k.value) for k in table.rows]
        assert "wrk" not in keys
        assert f"{_ENSEMBLE_KEY_PREFIX}gov" in keys
        assert table.get_row("gov")[4].plain == "▸ orchestrator"
        # Leave this session expanded so the fresh-session assertion below can detect leakage.
        await pilot.press("enter")
        await pilot.pause()
        assert "wrk" in [str(key.value) for key in table.rows]
        assert fake.applied == []
        assert fake.released == []
        assert fake.set_slugs == []
        assert fake.created == []
        assert fake.created_repos == []
        assert fake.updated_repos == []

    # A fresh dashboard starts from its own collapsed display state; expansion did not leak.
    second_app = Dashboard(fake)  # type: ignore[arg-type]
    async with second_app.run_test() as pilot:
        await pilot.pause()
        table = second_app.query_one("#tasks", DataTable)
        keys = [str(key.value) for key in table.rows]
        assert "wrk" not in keys
        assert f"{_ENSEMBLE_KEY_PREFIX}gov" in keys


# 2119: REQ-023.3.1
async def test_search_keeps_matching_governed_tasks_expanded() -> None:
    governor = {**_TASK, "id": "gov", "slug": "orchestrator", "governor_task_id": None}
    governed = {**_TASK, "id": "wrk", "slug": "matching-worker", "governor_task_id": "gov"}
    app = Dashboard(_FakeClient([governor, governed]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        app._query = "matching"
        app.action_refresh()
        await pilot.pause()
        table.move_cursor(row=table.get_row_index("gov"))

        assert [str(key.value) for key in table.rows] == ["gov", "wrk"]
        assert table.get_row("gov")[4].plain == "▾ orchestrator"
        await pilot.press("enter")
        await pilot.pause()
        assert [str(key.value) for key in table.rows] == ["gov", "wrk"]
        assert table.get_row("gov")[4].plain == "▾ orchestrator"

        app._query = ""
        app.action_refresh()
        await pilot.pause()
        assert [str(key.value) for key in table.rows] == [
            "gov",
            f"{_ENSEMBLE_KEY_PREFIX}gov",
        ]
        assert table.get_row("gov")[4].plain == "▸ orchestrator"


async def test_enter_on_non_governor_does_nothing() -> None:
    # Pressing Enter on a task with no governed children leaves the table unchanged.
    t1 = {**_TASK, "id": "t1", "slug": "alpha", "governor_task_id": None}
    t2 = {**_TASK, "id": "t2", "slug": "bravo", "governor_task_id": None}
    app = Dashboard(_FakeClient([t1, t2]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        before = [str(k.value) for k in table.rows]
        table.move_cursor(row=table.get_row_index("t1"))
        await pilot.press("enter")
        await pilot.pause()
        after = [str(k.value) for k in table.rows]
        assert before == after


async def test_search_expands_collapsed_ensembles_to_reach_their_children() -> None:
    # Governors start collapsed — a search must still reach children hidden behind "...".
    # The ensemble expands for the query; the collapse state is restored once the query is cleared.
    governor = {**_TASK, "id": "gov", "slug": "orchestrator", "governor_task_id": None}
    governed = {**_TASK, "id": "wrk", "slug": "worker-bee", "governor_task_id": "gov"}
    app = Dashboard(_FakeClient([governor, governed]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        # Initial state: child hidden behind ensemble placeholder (collapsed on startup).
        assert f"{_ENSEMBLE_KEY_PREFIX}gov" in [str(k.value) for k in table.rows]
        assert "wrk" not in [str(k.value) for k in table.rows]
        # Search for the collapsed child: it surfaces as a real row, no placeholder.
        app._query = "worker-bee"
        app.action_refresh()
        await pilot.pause()
        keys = [str(k.value) for k in table.rows]
        assert "wrk" in keys
        assert f"{_ENSEMBLE_KEY_PREFIX}gov" not in keys
        # Clear the query: the collapse state is restored (placeholder is back).
        app._query = ""
        app.action_refresh()
        await pilot.pause()
        keys = [str(k.value) for k in table.rows]
        assert f"{_ENSEMBLE_KEY_PREFIX}gov" in keys
        assert "wrk" not in keys


async def test_search_with_no_collapse_shows_matching_child_under_its_governor() -> None:
    # A matching child keeps its governor visible so the tree stays intact — even though the
    # governor's own text doesn't match the query.
    governor = {**_TASK, "id": "gov", "slug": "orchestrator", "governor_task_id": None}
    governed = {**_TASK, "id": "wrk", "slug": "worker-bee", "governor_task_id": "gov"}
    app = Dashboard(_FakeClient([governor, governed]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        app._query = "worker"
        app.action_refresh()
        keys = [str(k.value) for k in table.rows]
        # The governor is pulled up alongside its matching child (governor first, then child).
        assert keys == ["gov", "wrk"]


async def test_search_shows_governing_task_when_child_matches() -> None:
    # The user's request: a task visible in a search must show its governing task too.
    governor = {**_TASK, "id": "gov", "slug": "orchestrator", "governor_task_id": None}
    governed = {**_TASK, "id": "wrk", "slug": "worker-bee", "governor_task_id": "gov"}
    app = Dashboard(_FakeClient([governor, governed]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        app._query = "worker-bee"  # matches only the child
        app.action_refresh()
        keys = [str(k.value) for k in table.rows]
        assert "wrk" in keys
        assert "gov" in keys  # governor pulled up even though it doesn't match


async def test_search_shows_all_ancestors_when_deep_child_matches() -> None:
    # A multi-level chain: only the leaf matches, but every ancestor must stay visible.
    root = {**_TASK, "id": "root", "slug": "root-orch", "governor_task_id": None}
    mid = {**_TASK, "id": "mid", "slug": "mid-orch", "governor_task_id": "root"}
    leaf = {**_TASK, "id": "leaf", "slug": "leaf-worker", "governor_task_id": "mid"}
    app = Dashboard(_FakeClient([root, mid, leaf]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        app._query = "leaf-worker"  # matches only the deepest task
        app.action_refresh()
        keys = [str(k.value) for k in table.rows]
        assert set(keys) == {"root", "mid", "leaf"}  # whole chain visible


# -- multi-runner column -----------------------------------------------------------


def _col_labels(table: DataTable) -> list[str]:
    return [str(c.label) for c in table.columns.values()]


async def test_runner_column_absent_for_single_runner() -> None:
    # One registered runner → no "runner" column.
    tasks = [{**_TASK, "id": "t-a"}, {**_TASK, "id": "t-b"}]
    app = Dashboard(_FakeClient(tasks, runners=[{"id": "r1", "host": "host-a"}]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "runner" not in _col_labels(app.query_one("#tasks", DataTable))


async def test_runner_column_absent_with_no_runners() -> None:
    # No registered runners → no "runner" column.
    app = Dashboard(_FakeClient([_TASK], runners=[]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "runner" not in _col_labels(app.query_one("#tasks", DataTable))


async def test_runner_column_appears_for_multiple_runners() -> None:
    # Two registered runners → "runner" column present; cells show task runner_host values.
    tasks = [
        {**_TASK, "id": "t-a", "runner_host": "host-a"},
        {**_TASK, "id": "t-b", "runner_host": "host-b"},
    ]
    runners = [{"id": "r1", "host": "host-a"}, {"id": "r2", "host": "host-b"}]
    app = Dashboard(_FakeClient(tasks, runners=runners))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        labels = _col_labels(table)
        assert "runner" in labels
        runner_idx = labels.index("runner")
        assert table.get_row("t-a")[runner_idx].plain == "host-a"
        assert table.get_row("t-b")[runner_idx].plain == "host-b"


async def test_runner_column_appears_dynamically() -> None:
    # Start with one runner → no column. Feed refresh adds a second runner → column appears.
    fake = _FakeClient(
        [{**_TASK, "id": "t-a", "runner_host": "host-a"}],  # type: ignore[arg-type]
        runners=[{"id": "r1", "host": "host-a"}],
    )
    app = Dashboard(fake, refresh_interval=0.05)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        assert "runner" not in _col_labels(table)
        fake._tasks = [
            {**_TASK, "id": "t-a", "runner_host": "host-a"},
            {**_TASK, "id": "t-b", "runner_host": "host-b"},
        ]
        fake._runners = [{"id": "r1", "host": "host-a"}, {"id": "r2", "host": "host-b"}]
        fake.signal_change()
        await _settle(pilot, lambda: "runner" in _col_labels(table))
        assert "runner" in _col_labels(table)


async def test_runner_column_disappears_dynamically() -> None:
    # Start with two runners → column shown. Feed refresh drops to one → column gone.
    fake = _FakeClient(  # type: ignore[arg-type]
        [
            {**_TASK, "id": "t-a", "runner_host": "host-a"},
            {**_TASK, "id": "t-b", "runner_host": "host-b"},
        ],
        runners=[{"id": "r1", "host": "host-a"}, {"id": "r2", "host": "host-b"}],
    )
    app = Dashboard(fake, refresh_interval=0.05)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        assert "runner" in _col_labels(table)
        fake._tasks = [{**_TASK, "id": "t-a", "runner_host": "host-a"}]
        fake._runners = [{"id": "r1", "host": "host-a"}]
        fake.signal_change()
        await _settle(pilot, lambda: "runner" not in _col_labels(table))
        assert "runner" not in _col_labels(table)


async def test_runner_cell_is_dimmed_for_terminal_tasks() -> None:
    # Terminal tasks have their runner cell dimmed like all other cells.
    tasks = [
        {**_TASK, "id": "t-active", "runner_host": "host-a", "state": "WORKING"},
        {**_TASK, "id": "t-done", "runner_host": "host-b", "state": "COMPLETE"},
    ]
    runners = [{"id": "r1", "host": "host-a"}, {"id": "r2", "host": "host-b"}]
    app = Dashboard(_FakeClient(tasks, runners=runners))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        labels = _col_labels(table)
        assert "runner" in labels
        runner_idx = labels.index("runner")
        active_runner = table.get_row("t-active")[runner_idx]
        assert not any(s.style == "dim" for s in active_runner._spans)
        done_runner = table.get_row("t-done")[runner_idx]
        assert done_runner._spans and all(s.style == "dim" for s in done_runner._spans)


# -- vim-style hjkl navigation ------------------------------------------------------


async def test_pressing_jk_moves_the_task_table_cursor_like_arrow_keys() -> None:
    other = {**_TASK, "id": "task-second9999", "slug": "other"}
    app = Dashboard(_FakeClient([_TASK, other]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._current == _TASK["id"]  # starts on the first row
        await pilot.press("j")
        await pilot.pause()
        assert app._current == "task-second9999"
        await pilot.press("k")
        await pilot.pause()
        assert app._current == _TASK["id"]


def _collapsed_ensemble_app() -> Dashboard:
    # A collapsed governor's ensemble row sits between two real rows.
    # created_at controls order (newest first): gov (03:00) > wrk (02:00) > zzz-extra (01:00).
    governor = {
        **_TASK,
        "id": "gov",
        "slug": "orchestrator",
        "governor_task_id": None,
        "created_at": "2026-06-01T03:00:00",
    }
    governed = {
        **_TASK,
        "id": "wrk",
        "slug": "worker",
        "governor_task_id": "gov",
        "created_at": "2026-06-01T02:00:00",
    }
    extra = {
        **_TASK,
        "id": "zzz-extra",
        "slug": "zzz-extra",
        "governor_task_id": None,
        "created_at": "2026-06-01T01:00:00",
    }
    return Dashboard(_FakeClient([governor, governed, extra]))  # type: ignore[arg-type]


async def test_pressing_j_skips_the_ensemble_row_like_the_down_arrow() -> None:
    # A collapsed governor's ensemble row sits between two real rows; `j` must step straight past
    # it (_VimDataTable.action_cursor_down skips the sentinel), landing on the next real row.
    app = _collapsed_ensemble_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        # Collapse gov's ensemble directly, then rebuild — the Enter-key collapse path is covered
        # by test_enter_on_governor_collapses_to_ensemble_row; here we exercise the j/k skip over
        # the resulting sentinel row without coupling to key/focus event timing.
        app._collapsed.add("gov")
        app.action_refresh()
        await pilot.pause()
        row_keys = [str(k.value) for k in table.rows]
        assert row_keys == ["gov", f"{dashboard._ENSEMBLE_KEY_PREFIX}gov", "zzz-extra"]
        table.move_cursor(row=table.get_row_index("gov"))
        await pilot.pause()
        await pilot.press("j")  # from gov, steps over the ensemble row onto zzz-extra
        await pilot.pause()
        assert app._current == "zzz-extra"
        await pilot.press("k")  # and back up, over the ensemble row, onto gov
        await pilot.pause()
        assert app._current == "gov"


# 2119: REQ-023.4.1
async def test_arrow_keys_skip_the_ensemble_row_like_j_and_k() -> None:
    # The default arrow keys route through the same overridden cursor actions as j/k, so they
    # must skip the sentinel identically.
    app = _collapsed_ensemble_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        app._collapsed.add("gov")
        app.action_refresh()
        await pilot.pause()
        table.move_cursor(row=table.get_row_index("gov"))
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert app._current == "zzz-extra"
        await pilot.press("up")
        await pilot.pause()
        assert app._current == "gov"


async def test_navigating_over_an_ensemble_row_never_selects_the_sentinel() -> None:
    # The reported bug: the sentinel was briefly selected mid-traversal. The cursor must never
    # land on it, so _current is never the ensemble key at any observed step.
    app = _collapsed_ensemble_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        app._collapsed.add("gov")
        app.action_refresh()
        await pilot.pause()
        sentinel = f"{dashboard._ENSEMBLE_KEY_PREFIX}gov"
        table.move_cursor(row=table.get_row_index("gov"))
        await pilot.pause()
        assert app._current != sentinel
        for key in ("j", "j", "k", "k"):
            await pilot.press(key)
            await pilot.pause()
            assert app._current != sentinel
        # The cursor row is a real row too — never the sentinel.
        assert str(table.ordered_rows[table.cursor_row].key.value) != sentinel


# 2119: REQ-023.4.1
async def test_ensemble_row_as_the_last_row_is_not_landed_on_by_down_arrow() -> None:
    # When a collapsed ensemble is the last navigable row, pressing down keeps the cursor on the
    # real row above it rather than clamping onto the sentinel.
    governor = {
        **_TASK,
        "id": "gov",
        "slug": "orchestrator",
        "governor_task_id": None,
        "created_at": "2026-06-01T02:00:00",
    }
    governed = {
        **_TASK,
        "id": "wrk",
        "slug": "worker",
        "governor_task_id": "gov",
        "created_at": "2026-06-01T01:00:00",
    }
    app = Dashboard(_FakeClient([governor, governed]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        app._collapsed.add("gov")
        app.action_refresh()
        await pilot.pause()
        row_keys = [str(k.value) for k in table.rows]
        assert row_keys == ["gov", f"{dashboard._ENSEMBLE_KEY_PREFIX}gov"]
        table.move_cursor(row=table.get_row_index("gov"))
        await pilot.pause()
        await pilot.press("down")  # nothing real below the sentinel — stay on gov
        await pilot.pause()
        assert app._current == "gov"
        assert str(table.ordered_rows[table.cursor_row].key.value) == "gov"


async def test_pressing_jk_navigates_the_repos_table() -> None:
    fake = _FakeClient(
        [],
        repos=[
            {"id": "r1", "name": "r1", "git_url": "", "default_base": "main"},
            {"id": "r2", "name": "r2", "git_url": "", "default_base": "main"},
        ],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        table = app.screen.query_one("#repos", DataTable)
        assert table.cursor_row == 0
        await pilot.press("j")
        await pilot.pause()
        assert table.cursor_row == 1
        await pilot.press("k")
        await pilot.pause()
        assert table.cursor_row == 0


async def test_pressing_j_then_enter_picks_the_second_option_in_a_picker() -> None:
    # Proves `j` actually moves the OptionList highlight (not just that Enter still works).
    fake = _FakeClient(
        [],
        repos=["r1", "r2"],
        workflows=[{"name": "spike", "when_to_use": ""}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")  # opens the repo picker
        await pilot.pause()
        await pilot.press("j")  # move off r1 onto r2
        await pilot.press("enter")  # repo: r2
        await pilot.pause()
        await pilot.press("enter")  # workflow: spike (only one)
        await pilot.pause()
        await pilot.press("enter")  # submit an empty memo
        await pilot.pause()
        assert fake.created == [("r2", "spike", None, None, None, None)]


def _rendered_static_text(static: Static) -> str:
    """Every wrapped line of a mounted :class:`Static`, joined back into one normalized string —
    reconstructs the pre-wrap text (Rich word-wraps on spaces, no hyphenation) so it can be
    compared against the source, rather than probing for one word that could coincidentally
    also appear earlier in the text."""
    import ast as _ast
    import re as _re

    lines = [str(static.render_line(y)) for y in range(static.size.height)]
    # each Strip repr embeds its plain text as the first Segment(...) string literal; repr()
    # picks single- or double-quotes depending on the text's own apostrophes, so match either
    # and decode with ast.literal_eval rather than assuming one quote style.
    literals = _re.findall(r"Segment\((\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')", "\n".join(lines))
    words = " ".join(_ast.literal_eval(lit) for lit in literals)
    return " ".join(words.split())


async def test_workflow_descriptions_are_not_clipped_for_every_registered_workflow(
    tmp_path: Path,
) -> None:
    """Regression: ``#workflow-desc`` used to be a fixed 2 visible rows (a ``height: 4`` box
    with a 2-row border/padding overhead), so anything past two wrapped lines — e.g. the
    orchestrator's when_to_use — got cut off mid-sentence. It now auto-sizes to the wrapped
    text (capped, with scrolling as a fallback), so the full sentence renders for every
    workflow in the real registry, not just the short ones. Reconstructs each rendered
    description in full (not just checking a trailing word, which could false-pass if that
    word happens to also appear earlier in the text) and diffs it word-for-word against the
    source ``when_to_use``."""
    from panopticon.workflows.discovery import discover_workflows

    registry = discover_workflows(_home_workflows=tmp_path / "empty-home-workflows")
    entries = sorted(
        ({"name": name, "when_to_use": wf.when_to_use} for name, wf in registry.items()),
        key=lambda w: w["name"],
    )
    assert len(entries) >= 2  # sanity: exercising more than one workflow's description
    longest = max(entries, key=lambda w: len(w["when_to_use"]))
    assert len(longest["when_to_use"]) > 150  # sanity: the fixture still has a long one to clip

    fake = _FakeClient([], repos=["r1"], workflows=entries)
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")  # opens the repo picker
        await pilot.pause()
        await pilot.press("enter")  # only repo -> opens the workflow picker
        await pilot.pause()

        desc = app.screen.query_one("#workflow-desc", Static)
        for i, entry in enumerate(entries):
            if i:
                await pilot.press("j")
            await pilot.pause()
            await pilot.pause()  # auto-height re-layout settles a frame after the content update
            assert _rendered_static_text(desc) == " ".join(entry["when_to_use"].split()), (
                f"{entry['name']!r}'s description got clipped: {entry['when_to_use']!r}"
            )


async def test_workflow_description_overflowing_the_cap_scrolls_via_keyboard() -> None:
    """The description pane auto-sizes up to ``max-height: 8`` (previous test), but a
    description longer than that must still be fully readable from the keyboard — the option
    list keeps focus (`j`/`k`/arrows must keep navigating workflows), so the pane can't rely on
    its own focus to scroll; ``ctrl+d``/``ctrl+u`` drive it at the screen level instead."""
    head, tail = "HEADMARKER", "TAILMARKER"
    filler = " ".join(f"word{i}" for i in range(200))  # long enough to overflow max-height: 8
    long_desc = f"{head} {filler} {tail}"
    fake = _FakeClient(
        [], repos=["r1"], workflows=[{"name": "synthetic", "when_to_use": long_desc}]
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("enter")  # only repo -> opens the workflow picker
        await pilot.pause()
        await pilot.pause()

        scroll = app.screen.query_one("#workflow-desc-scroll", VerticalScroll)
        static = app.screen.query_one("#workflow-desc", Static)

        def visible_text() -> str:
            base = int(scroll.scroll_y)
            lines = [str(static.render_line(base + y)) for y in range(scroll.size.height)]
            return "\n".join(lines)

        # sanity: this description genuinely exceeds the cap and needs scrolling to read in full
        assert scroll.max_scroll_y > 0
        assert head in visible_text()
        assert tail not in visible_text()  # the tail is below the fold, unreachable without scroll

        for _ in range(20):  # comfortably more presses than needed to hit the bottom
            await pilot.press("ctrl+d")
            await pilot.pause()
        assert int(scroll.scroll_y) == int(scroll.max_scroll_y)
        assert tail in visible_text()  # now reachable via the keyboard scrolling path

        for _ in range(20):
            await pilot.press("ctrl+u")
            await pilot.pause()
        assert int(scroll.scroll_y) == 0
        assert head in visible_text()  # scrolls back up just as reliably


async def test_workflow_picker_still_fits_a_short_terminal(tmp_path: Path) -> None:
    """The auto-sizing description pane (previous test) must stay bounded by its
    ``max-height``, or a long description could push the picker box off a short terminal."""
    from panopticon.workflows.discovery import discover_workflows

    registry = discover_workflows(_home_workflows=tmp_path / "empty-home-workflows")
    entries = [{"name": name, "when_to_use": wf.when_to_use} for name, wf in registry.items()]

    fake = _FakeClient([], repos=["r1"], workflows=entries)
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test(size=(80, 16)) as pilot:  # a short terminal
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("enter")  # only repo -> opens the workflow picker
        await pilot.pause()

        box = app.screen.query_one("#workflow-choice-box")
        assert box.region.y >= 0
        assert box.region.y + box.region.height <= app.screen.size.height


async def test_workflows_screen_survives_two_workflows_sharing_one_file(tmp_path) -> None:
    # Regression: rows were keyed by source PATH, so a module defining two workflows
    # (e.g. an operator's spec_2119.py) raised DataTable.DuplicateKey and crashed the
    # whole dashboard on `w`. Rows are now keyed by workflow name (unique by
    # construction — discovery rejects duplicate names).
    shared = tmp_path / "spec_2119.py"
    shared.write_text("# two workflows, one module")
    pair = [
        {"name": "2119-auto", "when_to_use": "auto", "path": str(shared), "built_in": False},
        {"name": "2119-human", "when_to_use": "human", "path": str(shared), "built_in": False},
    ]
    app = Dashboard(_FakeClient([], workflows=pair))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")  # crashed here before the fix
        await pilot.pause()
        table = app.screen.query_one("#workflows", DataTable)
        assert table.row_count == 2  # both workflows listed, one row each


async def test_deleting_a_shared_file_workflow_names_every_sibling(tmp_path) -> None:
    # Deleting a workflow removes its FILE, which may define several workflows — the
    # confirmation must name all of them, not just the highlighted row.
    shared = tmp_path / "spec_2119.py"
    shared.write_text("# two workflows, one module")
    pair = [
        {"name": "2119-auto", "when_to_use": "auto", "path": str(shared), "built_in": False},
        {"name": "2119-human", "when_to_use": "human", "path": str(shared), "built_in": False},
    ]
    app = Dashboard(_FakeClient([], workflows=pair))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")
        await pilot.press("x")
        await pilot.pause()
        box = app.screen.query_one("#delete-workflow-box")
        prompt = " ".join(str(label.render()) for label in box.query(Label))
        assert "2119-auto" in prompt and "2119-human" in prompt
