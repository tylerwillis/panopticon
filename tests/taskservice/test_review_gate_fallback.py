"""Destination coverage for the non-fatal review-gate fallback."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from panopticon.core import Complete, InitialState, State, Workflow
from panopticon.core.models import Repo
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows.review import Review as ReviewWorker


class _PairedAuthoring(Workflow):
    name = "fallback-paired-authoring"
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


async def _make_service(tmp_path: Path) -> TaskService:
    ids: Iterator[str] = iter(("author", "reviewer"))
    times: Iterator[str] = iter(f"t{i}" for i in range(1, 20))
    service = TaskService(
        SqlAlchemyStore(),
        {"fallback-paired-authoring": _PairedAuthoring(), "review": ReviewWorker()},
        FilesystemArtifactStore(tmp_path),
        clock=lambda: next(times),
        id_factory=lambda: next(ids),
    )
    await service.init()
    await service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    return service


# 2119: REQ-009.9.1
@pytest.mark.parametrize("destination", ["DRAFT", "WORKING", "COMPLETE", "DROPPED"])
async def test_review_creation_failure_allows_every_free_move_destination(
    tmp_path: Path, destination: str
) -> None:
    service = await _make_service(tmp_path)
    author = await service.create_task("r1", "fallback-paired-authoring", harness="codex")
    await service.apply_operation(author.id, "advance")
    assert (await service.get_task(author.id)).state == "REVIEW"

    await service.set_state(author.id, destination)

    assert (await service.get_task(author.id)).state == destination
