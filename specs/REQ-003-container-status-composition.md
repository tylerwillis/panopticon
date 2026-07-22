# REQ-003: Container Status Composition

## Overview

This contract preserves the pure composition of terminal state, claim ownership, container
registration, runner liveness, and reported lifecycle phase into one displayed container status.
The requirements are ordered by precedence.

## Requirements

### REQ-003.1: Ordered status composition

1. A terminal task MUST compose to `–` regardless of claim, registration, runner-liveness, or lifecycle-phase inputs.
2. A non-terminal unclaimed task MUST compose to `queued` regardless of registration, runner-liveness, or lifecycle-phase inputs.
3. A claimed non-terminal task with an open container registration MUST compose to `live` regardless of runner-liveness or lifecycle-phase inputs.
4. A claimed, non-terminal, unregistered task whose runner is not live MUST compose to `disconnected` regardless of lifecycle-phase input.
5. A claimed, non-terminal, unregistered task with a live runner and a reported lifecycle phase MUST compose to the status having the same value as that phase.
6. A claimed, non-terminal, unregistered task with a live runner and no lifecycle phase MUST compose to `down`.

### REQ-003.2: Lifecycle phase values

1. Reported `healing`, `claiming`, `preparing`, `building`, `starting`, `awaiting`, and `failed` lifecycle phases MUST each have a matching container-status value.

## Open questions for human approval

- None identified. The first-match ordering is explicit in both implementation documentation and
  pairwise precedence tests; complete combinatorial coverage is a test gap rather than a spec
  ambiguity.
