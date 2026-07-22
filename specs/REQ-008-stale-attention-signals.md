# REQ-008: Fresh dashboard attention signals

## Overview

Keep the task service's persisted attention signals aligned with an operator addressing a task.
The user-to-agent turn handoff and every state change end the blocking condition for the state that
was being attended, while an agent or transition lifecycle effect can explicitly report a new block
afterward.

Each supported interactive harness reports the user-to-agent handoff at its earliest native input
event. A harness whose native event blocks prompt processing until its handler finishes completes
the task-service write before allowing prompt processing to continue.

The turn API carries no event provenance, so every actual turn-to-agent write is treated as this
handoff. The existing Stop behavior for live background work issues no turn write and is unchanged.

## Requirements

### REQ-008.1: User-to-agent handoff

1. Setting a task's turn to `agent` MUST persist its blocked marker as false in the same task
   mutation.

2. Setting a task's turn to `user` MUST preserve its blocked marker.

### REQ-008.2: State changes

1. Applying any task state change, whether a declared transition or a free move, MUST clear its
   existing blocked marker before transition lifecycle effects run.

2. A task state change and its resulting blocked marker MUST be persisted in one task mutation.

### REQ-008.3: Renewed block

1. Explicitly setting a task's blocked marker after an automatic clear MUST persist the requested
   value.

### REQ-008.4: Claude prompt signal

1. On its native `UserPromptSubmit` event, the Claude harness MUST complete the task-service
   turn-to-agent write before its command hook exits.

### REQ-008.5: Codex prompt signal

1. On its native `UserPromptSubmit` event, the Codex harness MUST complete the task-service
   turn-to-agent write before its command hook exits.

### REQ-008.6: Pi prompt signal

1. On its native `input` event, the Pi harness MUST complete the task-service turn-to-agent write
   before that event handler finishes.

### REQ-008.7: Dashboard feed handoff

1. On startup, the dashboard MUST render the task snapshot returned with its initialized
   task-service change-feed cursor.

### REQ-008.8: Blocked lifecycle documentation

1. The repository glossary MUST state that a turn-to-agent write clears the blocked marker.

2. The repository glossary MUST state that a task state change clears the blocked marker.

3. The repository glossary MUST state that a turn-to-user write preserves the blocked marker.

4. The repository glossary MUST state that an agent can explicitly set the blocked marker again
   after an automatic clear.

### REQ-008.9: Turn-signal timing documentation

1. The repository glossary MUST identify Claude's `UserPromptSubmit` command hook as a blocking
   pre-processing signal whose floor includes callback process startup and the task-service write.

2. The repository glossary MUST identify Codex's `UserPromptSubmit` command hook as a blocking
   pre-processing signal whose floor includes callback process startup and the task-service write.

3. The repository glossary MUST identify Pi's `input` event as a pre-processing signal whose
   handler waits for the task-service write.
