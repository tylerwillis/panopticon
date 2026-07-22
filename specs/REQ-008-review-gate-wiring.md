# REQ-008: Review gate wiring

## Overview

Workflows can opt into deterministic cross-model review by declaring a reviewer launch pair. On
entry to their review gate, the task service creates a governed review worker while the authoring
task waits on an explicit responsibility. A reviewer-creation failure is recorded but does not
undo the review-gate transition or prevent the user's free-move fallback.

This wiring preserves the built-in opt-out established by `REQ-002.27` in
`REQ-002-review-workflow-and-pairing.md`, which requires every built-in workflow to leave its review
pair unset.

## Requirements

### REQ-008.1: Opt-in review creation

1. Entering a state labelled `REVIEW` in a workflow with a declared review launch pair MUST create
   exactly one new task using the `review` workflow.

### REQ-008.2: Review governance

1. A review task created on review-gate entry MUST record the authoring task's id as its
   `governor_task_id`.

### REQ-008.3: Declared launch pair

1. A review task created on review-gate entry MUST record the authoring workflow's declared reviewer
   launch pair as its harness and starting model.

### REQ-008.4: Authoring responsibility

1. Entering `REVIEW` in a workflow with a declared review launch pair MUST seed a pending
   `review-addressed` responsibility on the authoring task's current history entry regardless of
   whether review-task creation succeeds.

### REQ-008.5: Waiting marker

1. Successful review-task creation SHOULD set the authoring task's blocked marker.

### REQ-008.6: Pairing is opt-in

1. Entering `REVIEW` in a workflow without a declared review launch pair MUST NOT create a review
   task.

### REQ-008.7: Failure preserves review entry

1. If review-task creation fails during entry to `REVIEW`, the authoring task's transition MUST
   still complete in `REVIEW`.

### REQ-008.8: Failure reason

1. If review-task creation fails during entry to `REVIEW`, the authoring task's current history
   note MUST record the creation failure's reason.

### REQ-008.9: Failure fallback

1. Review-task creation failure MUST NOT prevent a subsequent free state move out of the authoring
   task's `REVIEW` state.

### REQ-008.10: Fresh review round

1. Each re-entry to `REVIEW` in a workflow with a declared review launch pair MUST create a new
   review task rather than reuse a task from an earlier review round.

### REQ-008.11: Review-only trigger

1. Entering a state not labelled `REVIEW` in a workflow with a declared review launch pair MUST
   NOT create a review task.

### REQ-008.12: Non-paired responsibility

1. Entering `REVIEW` in a workflow without a declared review launch pair MUST NOT seed a
   `review-addressed` responsibility.
