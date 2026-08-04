"""Cumulative token telemetry persistence."""

from pathlib import Path

from panopticon.core.models import Repo
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike


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
