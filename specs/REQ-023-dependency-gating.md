# REQ-023: Dependency Gating

## Overview

Task dependencies currently record intent without affecting execution or display. This contract
makes one readiness policy visible and enforceable across the deterministic control plane, session
service, and terminal dashboard: a dependency clears the gate when it reaches a workflow terminal
state, except that `DROPPED` remains blocking because the upstream work is missing.

The session service applies that policy by reconsidering the existing task rows on every spawner
pass. No transition hook or wakeup subsystem is introduced. A task that becomes ready follows the
ordinary claim, spawn, and initial-prompt path. A task whose container is already live remains a
display-honesty case only: injecting input to wake an idle live session is a separate feature and
is not part of this contract.

REQ-010 continues to own the persisted `Task.turn` and hook-driven turn-flip contract. Dependency
gating changes only how the dashboard presents that stored value while the task is held.

Governor tasks have a second, display-only source of waiting. Children point back to their governor
through `governor_task_id`; while any child remains non-terminal, a governor whose stored turn is
`user` is normally dispatching and waiting rather than asking the operator to act. The held
presentation favors that common dispatch-then-wait case only while the agent has not escalated
attention. A governor can rarely need real user input mid-flight, so an agent-settable attention
marker restores the ordinary orange user-turn signal while the verified wait continues. If an
agent fails to set the marker before a genuine mid-flight question, that task remains held; this is
an accepted limitation because the detail pane and live session remain available, while the
ordinary orange queue for tasks without a proven upstream wait never depends on agent behavior.

## Requirements

### REQ-023.1: Spawn-on-ready

1. An unclaimed non-terminal task with at least one non-terminal dependency MUST be excluded from
   the spawner's candidates.
2. An unclaimed non-terminal task with a `DROPPED` dependency MUST be excluded from the spawner's
   candidates.
3. On the first spawner pass after every dependency is in a workflow terminal state other than
   `DROPPED`, the task MUST be eligible for the ordinary claim-and-spawn flow, including delivery
   of its configured initial prompt.
4. A task with no dependencies MUST retain the existing unclaimed non-terminal spawn eligibility.

### REQ-023.2: Composed container status

1. A non-terminal unclaimed task with at least one non-terminal or `DROPPED` dependency MUST
   compose to the `gated` container status.
2. The `gated` container status MUST take precedence over `queued` while preserving terminal-task
   status as the higher-precedence `–`.
3. Task-service task responses MUST derive dependency-gating status from the current states and
   workflow terminality of the referenced tasks.
4. The container-status composer MUST remain a pure function of its supplied task and dependency
   facts.

### REQ-023.3: Held dashboard presentation

1. For any task with at least one non-terminal dependency and no attention marker, the dashboard
   turn column MUST render dim `held` text without yellow, orange, or other attention styling, with
   the `held N` form from REQ-023.5 when the governor rule also applies.
2. Held presentation MUST leave the task's persisted `turn` and the `Actor` values governed by
   REQ-010 unchanged.
3. The dashboard detail pane for a task with dependencies MUST show the number of blocking
   dependencies, the total dependency count, and every dependency's current slug and state.
4. A `DROPPED` dependency in the detail pane MUST be marked as needing operator action to edit the
   dependencies or drop the dependent task.
5. A gated task MUST remain visible as a dashboard task row before any container is claimed or
   spawned.

### REQ-023.4: Dependency graph policy

1. Setting dependencies MUST reject any proposed direct or indirect cycle in the resulting
   dependency graph.
2. A cycle rejection MUST identify the cycle and advise the operator to edit the dependency set.
3. A rejected dependency update MUST leave the task's previously recorded dependencies unchanged.

### REQ-023.5: Governor held presentation

1. A task whose stored turn is `user`, which governs at least one non-terminal child, and which has
   no attention marker MUST render dim `held N` in the dashboard turn column, where `N` is its
   non-terminal child count, without yellow, orange, or other attention styling.
2. A governor whose governed children are all terminal MUST retain the normal rendering of its
   stored turn.
3. The dashboard detail pane for a governor with non-terminal children MUST list every such child's
   current slug and state.

### REQ-023.6: Attention can only escalate

1. The dashboard MUST enforce this invariant: THE AGENT CAN ONLY ESCALATE ATTENTION, NEVER
   DE-ESCALATE IT; demotion of a stored `turn=user` task requires control-plane-verifiable facts
   that it has a non-terminal dependency or governs a non-terminal child, the attention marker only
   adds attention inside such a proven wait, and a task with no provable upstream wait renders
   attention-orange on `turn=user` regardless of that marker.
2. Setting a task's attention marker through the task-service API MUST persist the requested
   boolean without changing its turn or blocked marker.
3. Setting a task's blocked marker MUST preserve its attention marker.
4. Setting a task's turn to `user` MUST preserve its attention marker.
5. The REQ-008 user-prompt turn-to-agent write MUST clear the attention marker in the same task
   mutation.
6. The agent-facing MCP surface MUST expose the attention marker as an escalation-only operation
   whose description does not claim that clearing it can suppress ordinary user-turn attention.
7. The orchestrator `spawn-task` skill MUST tell the agent to set the attention marker before
   soliciting user input while governed children remain non-terminal.
8. A task with its blocked marker set MUST retain the existing red blocked rendering regardless of
   dependency waits, governed-child waits, or the attention marker.

## Non-goals

- Dependency completion does not inject input into or otherwise wake an already-live dependent
  session.
- Dependency gating does not add lifecycle hooks, scheduler state, or another polling mechanism.
- Dependency gating does not amend the stored-turn semantics in REQ-010.
- Governor-derived held presentation does not gate spawning, wake or suspend the governor's live
  session, or otherwise change control-plane state.
- Clearing the attention marker does not create a dashboard demotion unless the control plane
  independently proves a dependency or governed-child wait.
