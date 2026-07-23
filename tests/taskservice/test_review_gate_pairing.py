"""Independent launch-pair variation for the ADR-0014 review gate."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from panopticon.core import Complete, InitialState, State, Workflow
from panopticon.core.models import Repo
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows.review import Review as ReviewWorker


class _AlternatePairedAuthoring(Workflow):
    name = "alternate-paired-authoring"
    review_harness = "pi"
    review_model = "openai/gpt-5"

    class Draft(InitialState):
        label = "DRAFT"
        transitions = ("REVIEW",)

    class Review(State):
        label = "REVIEW"
        transitions = (Complete,)

    initial = Draft


# 2119: REQ-009.3.1
async def test_review_worker_uses_an_alternate_declared_launch_pair(tmp_path: Path) -> None:
    ids: Iterator[str] = iter(("author", "reviewer"))
    times: Iterator[str] = iter(f"t{i}" for i in range(1, 10))
    service = TaskService(
        SqlAlchemyStore(),
        {
            "alternate-paired-authoring": _AlternatePairedAuthoring(),
            "review": ReviewWorker(),
        },
        FilesystemArtifactStore(tmp_path),
        clock=lambda: next(times),
        id_factory=lambda: next(ids),
    )
    await service.init()
    await service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    author = await service.create_task(
        "r1", "alternate-paired-authoring", harness="claude", starting_model="claude-fable-5"
    )

    await service.apply_operation(author.id, "advance")

    reviews = [task for task in await service.list_tasks() if task.workflow == "review"]
    assert len(reviews) == 1
    assert reviews[0].harness == "pi"
    assert reviews[0].starting_model == "openai/gpt-5"
