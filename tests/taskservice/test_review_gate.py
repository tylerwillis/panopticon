"""ADR-0014 review-gate wiring through the real task-service boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from panopticon.core import Complete, InitialState, State, Workflow
from panopticon.core.models import Repo, Status, Task
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows.review import Review as ReviewWorker


class _PairedAuthoring(Workflow):
    name = "paired-authoring"
    review_harness = "codex"
    review_model = "gpt-5.6-sol:high"

    class Draft(InitialState):
        label = "DRAFT"
        transitions = ("REVIEW",)

    class Working(State):
        label = "WORKING"
        transitions = (Complete,)

    class Review(State):
        label = "REVIEW"
        transitions = (Complete,)

    initial = Draft


class _UnpairedAuthoring(Workflow):
    name = "unpaired-authoring"

    class Draft(InitialState):
        label = "DRAFT"
        transitions = ("REVIEW",)

    class Review(State):
        label = "REVIEW"
        transitions = (Complete,)

    initial = Draft


class _ReviewStoreFailure(RuntimeError):
    pass


class _FailingReviewStore(SqlAlchemyStore):
    async def _create_task(self, task: Task) -> None:
        if task.workflow == "review":
            raise _ReviewStoreFailure("review persistence unavailable")
        await super()._create_task(task)


async def _make_service(tmp_path: Path, *, store: SqlAlchemyStore | None = None) -> TaskService:
    ids: Iterator[str] = iter(f"id{i}" for i in range(1, 100))
    times: Iterator[str] = iter(f"t{i}" for i in range(1, 100))
    service = TaskService(
        store or SqlAlchemyStore(),
        {
            "paired-authoring": _PairedAuthoring(),
            "unpaired-authoring": _UnpairedAuthoring(),
            "review": ReviewWorker(),
        },
        FilesystemArtifactStore(tmp_path),
        clock=lambda: next(times),
        id_factory=lambda: next(ids),
    )
    await service.init()
    await service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    return service


# 2119: REQ-009.1.1
# 2119: REQ-009.2.1
# 2119: REQ-009.3.1
# 2119: REQ-009.4.1
# 2119: REQ-009.5.1
async def test_review_entry_creates_governed_worker_and_blocks_author(tmp_path: Path) -> None:
    service = await _make_service(tmp_path)
    author = await service.create_task(
        "r1", "paired-authoring", harness="claude", starting_model="claude-fable-5"
    )
    before_ids = {task.id for task in await service.list_tasks()}

    await service.apply_operation(author.id, "advance")

    tasks = await service.list_tasks()
    reviews = [task for task in tasks if task.workflow == "review"]
    assert len(reviews) == 1
    assert {task.id for task in tasks} - before_ids == {reviews[0].id}
    assert reviews[0].governor_task_id == author.id
    assert reviews[0].harness == "codex"
    assert reviews[0].starting_model == "gpt-5.6-sol:high"
    reloaded = await service.get_task(author.id)
    assert reloaded.blocked is True
    assert [
        (responsibility.key, responsibility.status)
        for responsibility in reloaded.current_entry.responsibilities
    ] == [("review-addressed", Status.PENDING)]


# 2119: REQ-009.6.1
# 2119: REQ-009.12.1
async def test_unpaired_review_entry_does_not_create_worker_or_responsibility(
    tmp_path: Path,
) -> None:
    service = await _make_service(tmp_path)
    author = await service.create_task("r1", "unpaired-authoring", harness="claude")

    await service.apply_operation(author.id, "advance")

    assert [task.workflow for task in await service.list_tasks()] == ["unpaired-authoring"]
    reloaded = await service.get_task(author.id)
    assert reloaded.state == "REVIEW"
    assert reloaded.current_entry.responsibilities == []


# 2119: REQ-009.6.1
# 2119: REQ-009.12.1
async def test_unpaired_free_move_into_review_has_no_review_gate_effects(tmp_path: Path) -> None:
    service = await _make_service(tmp_path)
    author = await service.create_task("r1", "unpaired-authoring", harness="claude")

    await service.set_state(author.id, "REVIEW")

    assert [task.workflow for task in await service.list_tasks()] == ["unpaired-authoring"]
    reloaded = await service.get_task(author.id)
    assert reloaded.state == "REVIEW"
    assert reloaded.current_entry.responsibilities == []


# 2119: REQ-009.1.1
# 2119: REQ-009.4.1
async def test_paired_free_move_into_review_runs_the_review_gate(tmp_path: Path) -> None:
    service = await _make_service(tmp_path)
    author = await service.create_task("r1", "paired-authoring", harness="claude")

    await service.set_state(author.id, "REVIEW")

    reviews = [task for task in await service.list_tasks() if task.workflow == "review"]
    assert len(reviews) == 1
    assert reviews[0].governor_task_id == author.id
    assert reviews[0].harness == "codex"
    assert reviews[0].starting_model == "gpt-5.6-sol:high"
    reloaded = await service.get_task(author.id)
    assert reloaded.state == "REVIEW"
    assert [(item.key, item.status) for item in reloaded.current_entry.responsibilities] == [
        ("review-addressed", Status.PENDING)
    ]


# 2119: REQ-009.11.1
@pytest.mark.parametrize("destination", ["DRAFT", "WORKING", "COMPLETE", "DROPPED"])
async def test_paired_workflow_does_not_create_review_worker_outside_review(
    tmp_path: Path, destination: str
) -> None:
    service = await _make_service(tmp_path)
    author = await service.create_task("r1", "paired-authoring", harness="claude")

    await service.set_state(author.id, destination)

    assert [task.workflow for task in await service.list_tasks()] == ["paired-authoring"]
    reloaded = await service.get_task(author.id)
    assert reloaded.state == destination
    assert reloaded.current_entry.responsibilities == []


# 2119: REQ-009.4.1
# 2119: REQ-009.7.1
# 2119: REQ-009.8.1
# 2119: REQ-009.9.1
async def test_review_creation_failure_is_recorded_nonfatal_and_allows_free_move(
    tmp_path: Path,
) -> None:
    service = await _make_service(tmp_path)
    author = await service.create_task("r1", "paired-authoring", harness="codex")

    transitioned = await service.apply_operation(author.id, "advance")

    reloaded = await service.get_task(author.id)
    assert transitioned.state == reloaded.state == "REVIEW"
    assert [
        (responsibility.key, responsibility.status)
        for responsibility in reloaded.current_entry.responsibilities
    ] == [("review-addressed", Status.PENDING)]
    assert reloaded.current_entry.note is not None
    assert reloaded.current_entry.note.startswith("Review task creation failed:")
    assert "harness must differ" in reloaded.current_entry.note
    assert [task.workflow for task in await service.list_tasks()] == ["paired-authoring"]

    moved = await service.set_state(author.id, "COMPLETE")
    assert moved.state == "COMPLETE"
    assert (await service.get_task(author.id)).state == "COMPLETE"


# 2119: REQ-009.4.1
# 2119: REQ-009.7.1
# 2119: REQ-009.8.1
# 2119: REQ-009.9.1
async def test_disabled_review_workflow_is_a_nonfatal_creation_failure(tmp_path: Path) -> None:
    service = await _make_service(tmp_path)
    await service.update_repo("r1", {"disabled_workflows": ["review"]})
    author = await service.create_task("r1", "paired-authoring", harness="claude")

    await service.apply_operation(author.id, "advance")

    reloaded = await service.get_task(author.id)
    assert reloaded.state == "REVIEW"
    assert [(item.key, item.status) for item in reloaded.current_entry.responsibilities] == [
        ("review-addressed", Status.PENDING)
    ]
    assert reloaded.current_entry.note is not None
    assert reloaded.current_entry.note.startswith("Review task creation failed:")
    assert "not enabled" in reloaded.current_entry.note
    assert [task.workflow for task in await service.list_tasks()] == ["paired-authoring"]

    moved = await service.set_state(author.id, "COMPLETE")
    assert moved.state == "COMPLETE"
    assert (await service.get_task(author.id)).state == "COMPLETE"


# 2119: REQ-009.4.1
# 2119: REQ-009.7.1
# 2119: REQ-009.8.1
# 2119: REQ-009.9.1
async def test_review_store_failure_is_recorded_nonfatal_and_allows_free_move(
    tmp_path: Path,
) -> None:
    service = await _make_service(tmp_path, store=_FailingReviewStore())
    author = await service.create_task("r1", "paired-authoring", harness="claude")

    await service.apply_operation(author.id, "advance")

    reloaded = await service.get_task(author.id)
    assert reloaded.state == "REVIEW"
    assert [(item.key, item.status) for item in reloaded.current_entry.responsibilities] == [
        ("review-addressed", Status.PENDING)
    ]
    assert reloaded.current_entry.note == (
        "Review task creation failed: review persistence unavailable"
    )
    assert [task.workflow for task in await service.list_tasks()] == ["paired-authoring"]

    await service.set_state(author.id, "COMPLETE")
    assert (await service.get_task(author.id)).state == "COMPLETE"


# 2119: REQ-009.1.1
async def test_concurrent_review_entry_creates_exactly_one_worker(tmp_path: Path) -> None:
    service = await _make_service(tmp_path)
    author = await service.create_task("r1", "paired-authoring", harness="claude")

    results = await asyncio.gather(
        service.apply_operation(author.id, "advance"),
        service.apply_operation(author.id, "advance"),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, BaseException) for result in results) == 1
    reviews = [task for task in await service.list_tasks() if task.workflow == "review"]
    assert len(reviews) == 1
    assert reviews[0].governor_task_id == author.id
    reloaded = await service.get_task(author.id)
    assert [entry.to_state for entry in reloaded.history] == ["DRAFT", "REVIEW"]


# 2119: REQ-009.1.1
# 2119: REQ-009.4.1
# 2119: REQ-009.10.1
async def test_each_review_reentry_creates_a_fresh_worker(tmp_path: Path) -> None:
    service = await _make_service(tmp_path)
    author = await service.create_task("r1", "paired-authoring", harness="claude")

    await service.apply_operation(author.id, "advance")
    await service.set_state(author.id, "DRAFT")
    await service.set_state(author.id, "REVIEW")
    await service.set_state(author.id, "DRAFT")
    await service.apply_operation(author.id, "advance")

    reviews = [task for task in await service.list_tasks() if task.workflow == "review"]
    assert len(reviews) == 3
    assert len({task.id for task in reviews}) == 3
    assert all(task.governor_task_id == author.id for task in reviews)
    reloaded = await service.get_task(author.id)
    assert [(item.key, item.status) for item in reloaded.current_entry.responsibilities] == [
        ("review-addressed", Status.PENDING)
    ]
