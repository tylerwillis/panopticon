"""State-entry wake eligibility is derived deterministically when history entries are created."""

from __future__ import annotations

import pytest

from panopticon.core.models import Actor, Responsibility, WakeStatus
from panopticon.core.state import Complete, InitialState, State, TerminalState
from panopticon.core.workflow import Workflow


class _WakeWorkflow(Workflow):
    name = "wake-test"

    class Waiting(InitialState):
        label = "WAITING"
        turn_on_enter = Actor.AGENT
        transitions = ("WORKING",)

    class Working(State):
        label = "WORKING"
        responsibilities = (Responsibility(key="build", description="Build the feature."),)
        transitions = (Complete,)

    class UserTurn(State):
        label = "USER_TURN"
        turn_on_enter = Actor.USER
        transitions = (Complete,)

    class AgentTerminal(TerminalState):
        label = "AGENT_TERMINAL"
        turn_on_enter = Actor.AGENT

    initial = Waiting


def test_initial_entry_is_not_pending_for_stage_entry_wake() -> None:
    # 2119: REQ-024.1.2
    task = _WakeWorkflow().start_task("t1", "r1", at="t0")
    assert task.history[0].wake_status is WakeStatus.SKIPPED


@pytest.mark.parametrize("free_move", [False, True], ids=["advance", "free-move"])
def test_agent_nonterminal_entry_is_pending_for_both_entry_paths(free_move: bool) -> None:
    # 2119: REQ-024.1.1
    workflow = _WakeWorkflow()
    task = workflow.start_task("t1", "r1", at="t0")

    if free_move:
        workflow.force_transition(task, "WORKING", at="t1", trigger="set-state")
    else:
        workflow.apply_transition(task, "WORKING", at="t1", trigger="advance")

    assert task.history[-1].wake_status is WakeStatus.PENDING


@pytest.mark.parametrize("state", ["USER_TURN", "AGENT_TERMINAL"])
def test_user_turn_and_terminal_entries_are_not_pending(state: str) -> None:
    # 2119: REQ-024.1.4
    workflow = _WakeWorkflow()
    task = workflow.start_task("t1", "r1", at="t0")
    workflow.force_transition(task, state, at="t1", trigger="set-state")
    assert task.history[-1].wake_status is WakeStatus.SKIPPED
