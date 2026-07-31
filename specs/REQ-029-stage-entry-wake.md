# REQ-029: Stage-entry wake

## Overview

Changing a task's workflow state gives the agent a new phase to execute, but changing the
recorded turn does not wake an idle agent CLI. The session service therefore delivers a submitted
stage-entry briefing to an already-live task session. The task service remains a deterministic
source of state-entry and delivery facts; the runner that owns the host tmux session performs the
external input injection.

The initial workflow entry is excluded because first-spawn prompt delivery already starts a new
session. A later entry is handled independently even when it enters the same state as an earlier
entry. Delivery is best-effort when the pane is unavailable, and an operator can disable the
feature without changing workflow behavior.

## Requirements

### REQ-029.1: Eligible state entries

1. On its next observation pass, the owning session service MUST submit a stage-entry wake when a
   live task has newly entered a non-terminal state whose `turn_on_enter` is the agent, whether the
   entry was produced by a declared transition or a free move.
2. The initial history entry created with the task MUST NOT cause a stage-entry wake.
3. A state entry observed without a live task container MUST NOT receive a later stage-entry wake
   merely because the task is subsequently spawned or healed.
4. An entry into a terminal state or a state whose `turn_on_enter` is the user MUST NOT cause a
   stage-entry wake.
5. When the session-service process has `PANOPTICON_NO_STAGE_ENTRY_WAKE` set to a non-empty value,
   it MUST NOT submit stage-entry wakes.

### REQ-029.2: Wake briefing

1. A submitted stage-entry wake MUST begin with the line `You have entered <STATE>.` and then
   include that entry's workflow-state description, current responsibility statuses, and a
   pointer to the task's relevant agent skills.
2. The stage-entry wake text MUST be rendered deterministically from the recorded task and
   workflow metadata without an LLM call.

### REQ-029.3: Tmux delivery

1. The session service MUST wait for the task pane's bracketed-paste readiness signal, load the
   wake text into a tmux buffer, paste it with bracketed-paste mode, and send one Enter key to
   submit it.
2. A missing pane, a pane that disappears, or a readiness timeout MUST leave the entry
   undelivered without failing the host's task-processing pass.

### REQ-029.4: Per-entry delivery tracking

1. A successful wake submission MUST be recorded durably against the specific history entry that
   caused it.
2. Repeated observation passes and container respawns or heals MUST NOT submit another wake for a
   history entry whose successful delivery is already recorded.
3. Re-entering a previously visited state MUST create a separately deliverable wake for the new
   history entry.
4. A failed tmux delivery attempt MUST NOT be recorded as successfully delivered.

### REQ-029.5: Control-plane boundary

1. The task service MUST limit its role to deterministic state-entry and wake-delivery records,
   leaving tmux inspection and input injection to the owning session service.
2. The recorded delivery status for each history entry MUST survive store reloads and be exposed
   through the task service's REST representation.
