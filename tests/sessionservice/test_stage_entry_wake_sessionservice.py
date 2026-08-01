"""Runner-side observation, skip behavior, and per-history-entry wake deduplication."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from panopticon.client import JsonObj
from panopticon.sessionservice import host as host_module
from panopticon.sessionservice.host import HostDaemon
from panopticon.sessionservice.stage_entry_wake import StageEntryWaker


def _inline(delivery: Callable[[], None]) -> None:
    delivery()


def _entry(
    state: str = "WORKING", *, trigger: str = "advance", wake_status: str = "pending"
) -> JsonObj:
    return {
        "at": f"t-{state}-{trigger}",
        "from_state": "WAITING",
        "to_state": state,
        "trigger": trigger,
        "note": None,
        "responsibilities": [],
        "wake_status": wake_status,
    }


def _task(*, container_status: str = "live", entry: JsonObj | None = None) -> JsonObj:
    current = entry or _entry()
    return {
        "id": "t1",
        "state": current["to_state"],
        "turn": "agent",
        "updated_at": "u1",
        "claimed_by": "host-1",
        "container_status": container_status,
        "history": [
            {
                **_entry("WAITING", trigger="start", wake_status="skipped"),
                "from_state": None,
            },
            current,
        ],
    }


class _Client:
    def __init__(self, task: JsonObj) -> None:
        self.task = task
        self.gets = 0
        self.records: list[tuple[str, int, str]] = []

    def get_task(self, task_id: str) -> JsonObj:
        assert task_id == self.task["id"]
        self.gets += 1
        return self.task

    def get_stage_entry_briefing(self, task_id: str, entry_index: int) -> str:
        assert task_id == self.task["id"]
        state = self.task["history"][entry_index]["to_state"]  # type: ignore[index]
        return f"You have entered {state}.\nDo the phase work. See /do-work."

    def record_stage_entry_wake(self, task_id: str, entry_index: int, status: str) -> JsonObj:
        self.records.append((task_id, entry_index, status))
        self.task["history"][entry_index]["wake_status"] = status  # type: ignore[index]
        return self.task


class _Runner:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.prompts: list[tuple[str, str]] = []

    def submit_prompt(self, task_id: str, prompt: str) -> bool:
        self.prompts.append((task_id, prompt))
        return self.result


def test_no_live_container_is_skipped_and_not_woken_after_a_later_respawn() -> None:
    # 2119: REQ-029.1.3
    task = _task(container_status="down")
    client, runner = _Client(task), _Runner()
    waker = StageEntryWaker(client, runner, runner_id="host-1", dispatch=_inline)

    waker.wake(task)
    task["container_status"] = "live"  # a later spawn/heal must not revive the skipped entry
    waker.wake(task)

    assert runner.prompts == []
    assert client.records == [("t1", 1, "skipped")]


def test_operator_opt_out_suppresses_wakes() -> None:
    # 2119: REQ-029.1.5
    task = _task()
    client, runner = _Client(task), _Runner()
    StageEntryWaker(
        client,
        runner,
        environ={"PANOPTICON_NO_STAGE_ENTRY_WAKE": "1"},
        dispatch=_inline,
    ).wake(task)
    assert runner.prompts == []


def test_opt_out_set_to_the_empty_string_does_not_suppress_wakes() -> None:
    # 2119: REQ-029.1.5
    task = _task()
    client, runner = _Client(task), _Runner()
    StageEntryWaker(
        client,
        runner,
        environ={"PANOPTICON_NO_STAGE_ENTRY_WAKE": ""},
        dispatch=_inline,
    ).wake(task)
    assert len(runner.prompts) == 1
    assert client.records == [("t1", 1, "delivered")]


def test_runner_does_not_wake_a_task_claimed_by_another_host() -> None:
    # 2119: REQ-029.1.1
    task = _task()
    task["claimed_by"] = "host-2"
    client, runner = _Client(task), _Runner()

    StageEntryWaker(client, runner, runner_id="host-1", dispatch=_inline).wake(task)

    assert runner.prompts == []
    assert client.records == []


def test_success_is_recorded_and_repoll_or_respawn_does_not_redeliver() -> None:
    # 2119: REQ-029.2.1
    # 2119: REQ-029.4.1
    # 2119: REQ-029.4.2
    task = _task()
    client, runner = _Client(task), _Runner()
    waker = StageEntryWaker(client, runner, dispatch=_inline)

    waker.wake(task)
    waker.wake(task)  # unchanged host-loop snapshot
    task["container_status"] = "down"
    waker.wake(task)  # respawn/heal cycle
    task["container_status"] = "live"
    waker.wake(task)

    assert runner.prompts == [("t1", "You have entered WORKING.\nDo the phase work. See /do-work.")]
    assert client.records == [("t1", 1, "delivered")]


def test_fresh_waker_honors_a_delivery_recorded_by_an_earlier_process() -> None:
    # 2119: REQ-029.4.2
    task = _task(entry=_entry(wake_status="delivered"))
    client, runner = _Client(task), _Runner()

    StageEntryWaker(client, runner, dispatch=_inline).wake(task)

    assert runner.prompts == []
    assert client.records == []


def test_reentry_into_the_same_state_gets_a_fresh_wake() -> None:
    # 2119: REQ-029.4.3
    task = _task()
    client, runner = _Client(task), _Runner()
    waker = StageEntryWaker(client, runner, dispatch=_inline)
    waker.wake(task)

    task["history"].append(_entry("WORKING", trigger="set-state"))  # type: ignore[union-attr]
    waker.wake(task)

    assert [prompt for _, prompt in runner.prompts] == [
        "You have entered WORKING.\nDo the phase work. See /do-work.",
        "You have entered WORKING.\nDo the phase work. See /do-work.",
    ]
    assert client.records == [("t1", 1, "delivered"), ("t1", 2, "delivered")]


def test_failed_delivery_is_left_pending_for_retry() -> None:
    # 2119: REQ-029.4.4
    task = _task()
    client, runner = _Client(task), _Runner(result=False)
    StageEntryWaker(client, runner, dispatch=_inline).wake(task)
    assert len(runner.prompts) == 1
    assert client.records == []
    assert task["history"][-1]["wake_status"] == "pending"  # type: ignore[index]


def test_all_pending_entries_are_delivered_in_history_order() -> None:
    # 2119: REQ-029.1.1
    # 2119: REQ-029.4.3
    task = _task()
    task["history"].append(_entry("REVIEWING", trigger="set-state"))  # type: ignore[union-attr]
    task["state"] = "REVIEWING"
    client, runner = _Client(task), _Runner()

    StageEntryWaker(client, runner, dispatch=_inline).wake(task)

    assert [prompt for _, prompt in runner.prompts] == [
        "You have entered WORKING.\nDo the phase work. See /do-work.",
        "You have entered REVIEWING.\nDo the phase work. See /do-work.",
    ]
    assert client.records == [("t1", 1, "delivered"), ("t1", 2, "delivered")]


def test_down_observation_is_skipped_even_if_container_heals_before_delivery() -> None:
    # 2119: REQ-029.1.3
    task = _task(container_status="down")
    client, runner = _Client(task), _Runner()
    queued: list[Callable[[], None]] = []
    waker = StageEntryWaker(client, runner, runner_id="host-1", dispatch=queued.append)

    waker.wake(task)
    task["container_status"] = "live"
    queued.pop()()

    assert runner.prompts == []
    assert client.records == [("t1", 1, "skipped")]


def test_delivery_is_dispatched_once_without_blocking_the_observation() -> None:
    # 2119: REQ-029.1.1
    task = _task()
    client, runner = _Client(task), _Runner()
    queued: list[Callable[[], None]] = []
    waker = StageEntryWaker(client, runner, dispatch=queued.append)

    waker.wake(task)
    waker.wake(task)

    assert len(queued) == 1
    assert runner.prompts == []
    queued.pop()()
    assert len(runner.prompts) == 1


def test_deferred_delivery_rechecks_runner_ownership() -> None:
    # 2119: REQ-029.1.1
    task = _task()
    task["claimed_by"] = "host-1"
    client, runner = _Client(task), _Runner()
    queued: list[Callable[[], None]] = []
    waker = StageEntryWaker(client, runner, runner_id="host-1", dispatch=queued.append)

    waker.wake(task)
    task["claimed_by"] = "host-2"
    queued.pop()()

    assert runner.prompts == []
    assert client.records == []


def test_unchanged_settled_entry_avoids_another_full_task_fetch() -> None:
    task = _task(entry=_entry(wake_status="delivered"))
    client, runner = _Client(task), _Runner()
    waker = StageEntryWaker(client, runner, dispatch=_inline)

    waker.wake(task)
    waker.wake(task)

    assert client.gets == 1
    assert runner.prompts == []


def test_host_tick_observes_each_task_for_stage_entry_wake() -> None:
    # 2119: REQ-029.1.1
    seen: list[str] = []

    class _Waker:
        def wake(self, task: JsonObj) -> None:
            seen.append(str(task["id"]))

    class _Spawner:
        def mark_healing(self, task: JsonObj) -> None:
            pass

        def spawn_one(self, task: JsonObj) -> None:
            pass

        def reconcile(self, task: JsonObj) -> None:
            pass

        def heal(self, task: JsonObj) -> None:
            pass

        def cleanup(self, task: JsonObj) -> None:
            pass

    class _Provisioner:
        def provision(self, task: JsonObj) -> None:
            pass

    class _Feed:
        def list_tasks_versioned(
            self, *, since: int = 0, wait: float | None = None
        ) -> tuple[list[JsonObj], int]:
            return [], since

    daemon = HostDaemon(
        _Feed(),  # type: ignore[arg-type]
        _Spawner(),  # type: ignore[arg-type]
        _Provisioner(),  # type: ignore[arg-type]
        waker=_Waker(),  # type: ignore[arg-type]
    )
    daemon.tick(
        [
            {"id": "t1", "state": "ITERATING", "claimed_by": None, "depends_on_task_ids": []},
            {"id": "t2", "state": "ITERATING", "claimed_by": None, "depends_on_task_ids": []},
        ]
    )
    assert seen == ["t1", "t2"]


def test_run_host_wires_the_stage_entry_waker_into_the_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 2119: REQ-029.1.1
    captured: dict[str, object] = {}

    class _HostDaemon:
        def __init__(
            self,
            client: object,
            spawner: object,
            provisioner: object,
            *,
            waker: object = None,
            interval: float,
            sleep: object,
        ) -> None:
            captured["waker"] = waker

        def run(self, *, until: object) -> None:
            captured["ran"] = True

    monkeypatch.setattr(host_module, "Spawner", lambda *args, **kwargs: object())
    monkeypatch.setattr(host_module, "Provisioner", lambda *args, **kwargs: object())
    monkeypatch.setattr(host_module, "HostDaemon", _HostDaemon)

    client, runner = _Client(_task()), _Runner()
    host_module.run_host(
        client,  # type: ignore[arg-type]
        runner,  # type: ignore[arg-type]
        runner_id="host-1",
        tasks_root="/tasks",
        cache=object(),  # type: ignore[arg-type]
        git=object(),  # type: ignore[arg-type]
        until=lambda: True,
        sleep=lambda _seconds: None,
    )

    assert isinstance(captured["waker"], StageEntryWaker)
    assert captured["ran"] is True
