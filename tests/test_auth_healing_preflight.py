"""Credential preflight at the orphan-healing visibility boundary."""

from __future__ import annotations

import pytest

from panopticon.sessionservice.spawner import Spawner


def test_invalid_credential_is_rejected_before_marking_an_orphan_healing() -> None:
    effects: list[str] = []

    class Client:
        def report_lifecycle(self, *_args: object, **_kwargs: object) -> None:
            effects.append("lifecycle")

    class Executions:
        def is_shell(self, _workflow: object) -> bool:
            return False

    class InvalidRunner:
        def validate_configuration(self) -> None:
            raise ValueError("invalid credential")

        def has_session(self, _task_id: str) -> bool:
            return False

        def delete_workspace_contents(self, _path: str) -> None:
            effects.append("workspace")

    spawner = Spawner(  # type: ignore[arg-type]
        Client(),
        InvalidRunner(),
        runner_id="runner",
        cache=object(),
        tasks_root="/tasks",
        executions=Executions(),
    )
    task = {
        "id": "task",
        "state": "ITERATING",
        "claimed_by": "runner",
        "workflow": "spike",
        "container_status": "down",
    }

    with pytest.raises(ValueError, match="invalid credential"):
        spawner.mark_healing(task)

    assert effects == []
