"""Runner-side observation and delivery of pending workflow state-entry wakes."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Mapping
from typing import Protocol, cast

from panopticon.client import JsonObj
from panopticon.core.models import ContainerStatus, WakeStatus

OPT_OUT_ENV = "PANOPTICON_NO_STAGE_ENTRY_WAKE"
_log = logging.getLogger(__name__)

Delivery = Callable[[], None]
Dispatcher = Callable[[Delivery], None]


def _dispatch_in_thread(delivery: Delivery) -> None:
    threading.Thread(target=delivery, name="panopticon-stage-entry-wake", daemon=True).start()


class WakeClient(Protocol):
    def get_task(self, task_id: str) -> JsonObj: ...

    def get_stage_entry_briefing(self, task_id: str, entry_index: int) -> str: ...

    def record_stage_entry_wake(self, task_id: str, entry_index: int, status: str) -> JsonObj: ...


class PromptRunner(Protocol):
    def submit_prompt(self, task_id: str, prompt: str) -> bool: ...


class StageEntryWaker:
    """Dispatch pending entry wakes without blocking the host's serial lifecycle pass."""

    def __init__(
        self,
        client: WakeClient,
        runner: PromptRunner,
        *,
        runner_id: str | None = None,
        environ: Mapping[str, str] = os.environ,
        dispatch: Dispatcher = _dispatch_in_thread,
    ) -> None:
        self._client = client
        self._runner = runner
        self._runner_id = runner_id
        self._environ = environ
        self._dispatch = dispatch
        self._lock = threading.Lock()
        self._inflight: set[str] = set()
        self._observed: dict[str, str] = {}

    def wake(self, task: JsonObj) -> None:
        task_id = str(task["id"])
        owner = task.get("claimed_by")
        if self._runner_id is not None and owner not in (None, self._runner_id):
            return
        observed_at = task.get("updated_at")
        observed_live = task.get("container_status") == ContainerStatus.LIVE.value
        with self._lock:
            if isinstance(observed_at, str) and self._observed.get(task_id) == observed_at:
                return
            if task_id in self._inflight:
                return
            self._inflight.add(task_id)

        def deliver() -> None:
            try:
                self._deliver(
                    task,
                    observed_live=observed_live,
                    observed_at=observed_at if isinstance(observed_at, str) else None,
                )
            except Exception:
                _log.warning("stage-entry delivery failed for task %s", task_id, exc_info=True)
            finally:
                with self._lock:
                    self._inflight.discard(task_id)

        self._dispatch(deliver)

    def _deliver(
        self,
        task: JsonObj,
        *,
        observed_live: bool,
        observed_at: str | None,
    ) -> None:
        task_id = str(task["id"])
        full = self._client.get_task(task_id)
        history = cast(list[JsonObj], full["history"])
        if not history:
            self._remember(task_id, task.get("updated_at"))
            return
        pending = [
            index
            for index, entry in enumerate(history)
            if entry.get("wake_status") == WakeStatus.PENDING.value
            and observed_at is not None
            and isinstance(entry.get("at"), str)
            and str(entry["at"]) <= observed_at
        ]
        if not pending:
            self._remember(task_id, task.get("updated_at"))
            return

        if self._runner_id is not None and full.get("claimed_by") != self._runner_id:
            return
        if not observed_live:
            self._settle(task_id, pending, WakeStatus.SKIPPED)
            return
        if self._environ.get(OPT_OUT_ENV):
            self._settle(task_id, pending, WakeStatus.SKIPPED)
            return
        if full.get("container_status") != ContainerStatus.LIVE.value:
            return

        for entry_index in pending:
            current = self._client.get_task(task_id)
            if self._runner_id is not None and current.get("claimed_by") != self._runner_id:
                return
            if current.get("container_status") != ContainerStatus.LIVE.value:
                return
            current_history = cast(list[JsonObj], current["history"])
            if (
                entry_index >= len(current_history)
                or current_history[entry_index].get("wake_status") != WakeStatus.PENDING.value
            ):
                continue
            prompt = self._client.get_stage_entry_briefing(task_id, entry_index)
            if not self._runner.submit_prompt(task_id, prompt):
                return
            self._client.record_stage_entry_wake(task_id, entry_index, WakeStatus.DELIVERED.value)

    def _settle(self, task_id: str, pending: list[int], status: WakeStatus) -> None:
        for entry_index in pending:
            current = self._client.get_task(task_id)
            if self._runner_id is not None and current.get("claimed_by") != self._runner_id:
                return
            current_history = cast(list[JsonObj], current["history"])
            if (
                entry_index >= len(current_history)
                or current_history[entry_index].get("wake_status") != WakeStatus.PENDING.value
            ):
                continue
            self._client.record_stage_entry_wake(task_id, entry_index, status.value)

    def _remember(self, task_id: str, observed_at: object) -> None:
        if isinstance(observed_at, str):
            with self._lock:
                self._observed[task_id] = observed_at
