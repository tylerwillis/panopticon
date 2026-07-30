# REQ-010: Turn Flip and Blocked Marker

## Overview

This contract preserves the hook-driven live-turn handoff and the blocked attention marker. A turn
flip changes who has the ball inside the current workflow state; it is not a state transition.
Turn-to-user handoff preserves the marker, while operator-to-agent progress and state progress
clear stale blocked attention automatically.

## Requirements

### REQ-010.1: Hook turn flips

1. A Stop event with no reported live background task MUST set the task turn to `user`.
2. A Stop event reporting any background-task entry not identified by a known terminal status MUST preserve the task's current turn.
3. A UserPromptSubmit event MUST set the task turn to `agent` even when its payload reports live background work.

### REQ-010.2: Conservative background classification

1. Background-task entries with a missing, unknown, or malformed status MUST be treated as live.
2. Background-task entries whose status is `completed`, `failed`, `cancelled`, `canceled`, or `error`, compared without case or surrounding whitespace, MUST be treated as terminal.
3. REQUIREMENT REMOVED
4. An absent `background_tasks` field or an empty list MUST be treated as reporting no live background task.
5. A `background_tasks` field whose value is not a list MUST be treated as reporting live background work.

### REQ-010.3: Blocked lifecycle

1. REQUIREMENT REMOVED
2. REQUIREMENT REMOVED
3. Setting a task turn to `user` MUST preserve its blocked marker.
4. Setting a task turn to `agent` MUST persist its blocked marker as false in the same mutation.
5. Applying any task state change MUST clear its existing blocked marker before transition lifecycle effects run.
6. Explicitly updating a blocked marker after an automatic clear MUST persist the requested value.

## Amendment decision

- Malformed background data is handled conservatively at both levels: an unrecognized entry and
  a malformed top-level collection both keep the turn with the agent. Absence and a valid empty
  list continue to mean that no background work was reported.
- The blocked marker is an attention signal: turn-to-user preserves it, while turn-to-agent and
  state progress clear it. This aligns the wave-1 contract with the later-adopted REQ-008 lifecycle.
