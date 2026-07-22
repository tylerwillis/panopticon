"""ADR-0014 review-gate wiring through the real task-service boundary."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from panopticon.core import Complete, InitialState, State, Workflow
from panopticon.core.models import Repo, Status
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


async def _make_service(tmp_path: Path) -> TaskService:
    ids: Iterator[str] = iter(f"id{i}" for i in range(1, 100))
    times: Iterator[str] = iter(f"t{i}" for i in range(1, 100))
    service = TaskService(
        SqlAlchemyStore(),
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


# 2119: REQ-004.1.1
# 2119: REQ-004.2.1
# 2119: REQ-004.3.1
# 2119: REQ-004.4.1
# 2119: REQ-004.5.1
async def test_review_entry_creates_governed_worker_and_blocks_author(tmp_path: Path) -> None:
    service = await _make_service(tmp_path)
    author = await service.create_task(
        "r1", "paired-authoring", harness="claude", starting_model="claude-fable-5"
    )

    await service.apply_operation(author.id, "advance")

    tasks = await service.list_tasks()
    reviews = [task for task in tasks if task.workflow == "review"]
    assert len(reviews) == 1
    assert reviews[0].governor_task_id == author.id
    assert reviews[0].harness == "codex"
    assert reviews[0].starting_model == "gpt-5.6-sol:high"
    reloaded = await service.get_task(author.id)
    assert reloaded.blocked is True
    assert [
        (responsibility.key, responsibility.status)
        for responsibility in reloaded.current_entry.responsibilities
    ] == [("review-addressed", Status.PENDING)]


# 2119: REQ-004.6.1
# 2119: REQ-004.12.1
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


# 2119: REQ-004.11.1
async def test_paired_workflow_does_not_create_review_worker_outside_review(
    tmp_path: Path,
) -> None:
    service = await _make_service(tmp_path)
    author = await service.create_task("r1", "paired-authoring", harness="claude")

    await service.set_state(author.id, "WORKING")

    assert [task.workflow for task in await service.list_tasks()] == ["paired-authoring"]


# 2119: REQ-004.4.1
# 2119: REQ-004.7.1
# 2119: REQ-004.8.1
# 2119: REQ-004.9.1
async def test_review_creation_failure_is_recorded_nonfatal_and_allows_free_move(
    tmp_path: Path,
) -> None:
    service = await _make_service(tmp_path)
    author = await service.create_task("r1", "paired-authoring", harness="codex")

    transitioned = await service.apply_operation(author.id, "advance")

    assert transitioned.state == "REVIEW"
    assert [r.key for r in transitioned.current_entry.responsibilities] == ["review-addressed"]
    assert transitioned.current_entry.note is not None
    assert "harness" in transitioned.current_entry.note.lower()
    assert [task.workflow for task in await service.list_tasks()] == ["paired-authoring"]

    moved = await service.set_state(author.id, "COMPLETE")
    assert moved.state == "COMPLETE"


# 2119: REQ-004.10.1
async def test_each_review_reentry_creates_a_fresh_worker(tmp_path: Path) -> None:
    service = await _make_service(tmp_path)
    author = await service.create_task("r1", "paired-authoring", harness="claude")

    await service.apply_operation(author.id, "advance")
    await service.set_state(author.id, "DRAFT")
    await service.apply_operation(author.id, "advance")

    reviews = [task for task in await service.list_tasks() if task.workflow == "review"]
    assert len(reviews) == 2
    assert len({task.id for task in reviews}) == 2
    assert all(task.governor_task_id == author.id for task in reviews)
