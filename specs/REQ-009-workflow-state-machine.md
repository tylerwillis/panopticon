# REQ-009: Workflow State Machine

## Overview

This contract preserves the deterministic workflow graph, its derived operations, state-entry
turn assignment, responsibility gate, and the user's off-graph free-move authority.

## Requirements

### REQ-009.1: Legal transitions

1. A normal transition from a non-terminal state MUST reject a destination outside that state's resolved direct transitions, including transitions inherited from its state-class hierarchy.
2. A normal transition MUST reject every attempt to leave a terminal state.
3. Workflow graph construction MUST resolve a transition expressed as a state-class reference.
4. Workflow graph construction MUST resolve a transition expressed as an existing state-label reference.
5. Workflow graph construction MUST reject a transition reference to an unknown state label.
6. Workflow graph construction MUST reject distinct states that declare the same label.

### REQ-009.2: Derived operations

1. Operations for every non-terminal state MUST include `drop` targeting `DROPPED`.
2. When a non-terminal state has exactly one non-`DROPPED` direct transition and no declared `advance`, its operations MUST derive `advance` targeting that transition.
3. When a non-terminal state has zero or multiple non-`DROPPED` direct transitions and no declared `advance`, its operations MUST NOT derive `advance`.
4. Every declared operation MUST target one of the declaring state's resolved direct transitions.
5. A terminal state MUST expose no operations.

### REQ-009.3: Turn derivation

1. A newly created task MUST use the workflow's configured `InitialState` as its state.
2. Every successful transition MUST assign the live turn from the destination state's `turn_on_enter` value, including the user turn for terminal states.
3. State-entry turn assignment MUST remain independent of the state's `advanced_by` actor.
4. A newly created task MUST begin on the user turn.
5. A newly created task MUST have an initial history entry whose source is null, destination is the initial state, and trigger is `start`.

### REQ-009.4: Responsibility gating

1. Entering a state MUST seed a fresh pending copy of each responsibility declared by that destination onto its history entry.
2. A normal non-drop transition MUST reject departure while any current-entry responsibility remains pending.
3. Resolving a responsibility as failed MUST require a non-blank explanatory comment.
4. A legal normal transition MUST be allowed after every current-entry responsibility is resolved as met or as failed with a comment.
5. A transition to `DROPPED` MUST bypass unresolved responsibilities.

### REQ-009.5: Free moves

1. A free move to an existing state MUST bypass the declared transition graph.
2. A free move MUST reject a destination that is not a state in the workflow.
3. A successful free move MUST set the task state to its destination.
4. A free move to an existing state MUST bypass unresolved responsibilities.
5. A free move to an existing state MUST be allowed when its source is terminal.
6. A successful free move MUST assign the live turn from the destination state's `turn_on_enter` value.
7. A successful free move MUST append a history entry recording its source and destination.
8. A successful free move MUST seed fresh pending destination responsibilities onto its appended history entry.

## Open questions for human approval

- None identified. Reopening a terminal task through a free move is broad authority, but both the
  implementation and service-level golden tests explicitly describe it as intentional.
