# REQ-001: Auto-advance on responsibilities met

## Overview

A workflow state gated on agent responsibilities is meant to move itself forward once
those responsibilities are resolved: the workflow overview already tells the agent
"Automatically advance to the next state" for any state whose `advanced_by` is the
agent. Today that sentence is aspirational only — resolving the last outstanding
responsibility just records the resolution; nothing transitions the task. The agent
must separately call the `advance` operation, and if its turn ends first (e.g. it
stops without remembering to call `advance`), the turn flips to the user and the task
sits there — done in substance, stuck in practice — until a human notices and prompts
it to continue.

This spec closes that gap at the point where a responsibility is resolved
(`resolve_responsibility`): when that call clears the last outstanding responsibility
for the task's current state, and the state is one the agent (not the user) advances,
and the state has a single well-defined `advance` transition, the task service performs
that transition immediately — the same transition `advance` would have performed, with
the same effects — with no further call required. States the user advances, and states
without an unambiguous `advance` transition, are unaffected: this spec does not change
who is allowed to move a task out of those states, only closes the specific gap where
an agent-advanced state's work is already done but nothing acts on it.

## Requirements

### REQ-001.1: Trigger and gating

1. When a `resolve_responsibility` call leaves the task's current state with no
   outstanding (`PENDING`) responsibilities, and that state's `advanced_by` is the
   agent, and that state has an available `advance` operation, the task service MUST
   perform the `advance` transition as part of that same call.
2. When a `resolve_responsibility` call leaves at least one outstanding (`PENDING`)
   responsibility for the task's current state, the task MUST remain in that state.
3. `resolve_responsibility` MUST NOT transition a task out of a state whose
   `advanced_by` is the user, regardless of how many of that state's responsibilities
   are resolved.
4. `resolve_responsibility` MUST NOT transition a task out of a state that has no
   available `advance` operation (e.g. more than one forward transition with none
   declared `advance`).
5. In the case of REQ-001.1.4, the task MUST remain in that state with every
   responsibility resolved.

### REQ-001.2: Parity with an explicit advance

1. A transition performed under REQ-001.1.1 MUST append the same history entry,
   invoke the workflow's `on_transition` hook the same way, and set the task's turn to
   the destination state's `turn_on_enter`, as an explicit `advance` operation would.
2. The task object returned by the `resolve_responsibility` call MUST reflect the
   post-transition state (the new `state`, the new `turn`, and the new current history
   entry) when REQ-001.1.1 applies.

### REQ-001.3: Scope

1. Auto-advance MUST be evaluated only as a direct effect of a `resolve_responsibility`
   call.
2. Entering a state as the result of any transition MUST NOT itself trigger a further
   auto-advance evaluation of that newly-entered state.
3. A `resolve_responsibility` call that is rejected before mutating the task (an
   unknown responsibility key, a `PENDING` status, or a `FAILED` status without a
   comment) MUST NOT transition the task.
