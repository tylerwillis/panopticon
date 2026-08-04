"""Cumulative token telemetry persistence."""

import asyncio
from pathlib import Path

from panopticon.core.models import Actor, Repo, Task
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike


class _DelayedTokenStore(SqlAlchemyStore):
    def __init__(self) -> None:
        super().__init__()
        self.token_write_started = asyncio.Event()
        self.allow_token_write = asyncio.Event()

    async def _set_tokens_used_max(self, task_id: str, tokens_used: int, updated_at: str) -> Task:
        self.token_write_started.set()
        await self.allow_token_write.wait()
        return await super()._set_tokens_used_max(task_id, tokens_used, updated_at)


async def test_delayed_token_report_cannot_overwrite_newer_cumulative_total(
    tmp_path: Path,
) -> None:
    service = TaskService(
        SqlAlchemyStore(),
        {"spike": Spike()},
        FilesystemArtifactStore(tmp_path),
    )
    await service.init()
    await service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    task = await service.create_task("r1", "spike")

    await service.set_tokens_used(task.id, 2_000)
    delayed_older_report = await service.set_tokens_used(task.id, 1_000)

    assert delayed_older_report.tokens_used == 2_000
    assert (await service.get_task(task.id)).tokens_used == 2_000


async def test_delayed_token_write_does_not_revert_a_newer_turn_update(tmp_path: Path) -> None:
    store = _DelayedTokenStore()
    service = TaskService(store, {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    await service.init()
    await service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    task = await service.create_task("r1", "spike")

    token_write = asyncio.create_task(service.set_tokens_used(task.id, 1_000))
    await store.token_write_started.wait()
    await service.set_turn(task.id, Actor.AGENT)
    store.allow_token_write.set()
    await token_write

    persisted = await service.get_task(task.id)
    assert persisted.turn is Actor.AGENT
    assert persisted.tokens_used == 1_000
