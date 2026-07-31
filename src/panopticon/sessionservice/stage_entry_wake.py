"""Runner-side observation and delivery of pending workflow state-entry wakes."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Protocol, cast

from panopticon.client import JsonObj
from panopticon.core.models import ContainerStatus, WakeStatus

OPT_OUT_ENV = "PANOPTICON_NO_STAGE_ENTRY_WAKE"


class WakeClient(Protocol):
    def get_task(self, task_id: str) -> JsonObj: ...

    def get_stage_entry_briefing(self, task_id: str, entry_index: int) -> str: ...

    def record_stage_entry_wake(self, task_id: str, entry_index: int, status: str) -> JsonObj: ...


class PromptRunner(Protocol):
    def submit_prompt(self, task_id: str, prompt: str) -> bool: ...


class StageEntryWaker:
    """Resolve the current entry's pending wake from one host-loop task observation."""

    def __init__(
        self,
        client: WakeClient,
        runner: PromptRunner,
        *,
        runner_id: str | None = None,
        environ: Mapping[str, str] = os.environ,
    ) -> None:
        self._client = client
        self._runner = runner
        self._runner_id = runner_id
        self._environ = environ

    def wake(self, task: JsonObj) -> None:
        task_id = str(task["id"])
        owner = task.get("claimed_by")
        if self._runner_id is not None and owner not in (None, self._runner_id):
            return
        full = self._client.get_task(task_id)
        history = cast(list[JsonObj], full["history"])
        if not history:
            return
        entry_index = len(history) - 1
        entry = history[entry_index]
        if entry.get("wake_status") != WakeStatus.PENDING.value:
            return

        if self._environ.get(OPT_OUT_ENV):
            self._client.record_stage_entry_wake(task_id, entry_index, WakeStatus.SKIPPED.value)
            return
        if task.get("container_status") != ContainerStatus.LIVE.value:
            self._client.record_stage_entry_wake(task_id, entry_index, WakeStatus.SKIPPED.value)
            return

        prompt = self._client.get_stage_entry_briefing(task_id, entry_index)
        if self._runner.submit_prompt(task_id, prompt):
            self._client.record_stage_entry_wake(task_id, entry_index, WakeStatus.DELIVERED.value)
