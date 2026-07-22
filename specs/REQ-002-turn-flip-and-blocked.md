# REQ-002: Turn Flip and Blocked Marker

## Overview

This contract preserves the hook-driven live-turn handoff and the independent deliberate blocked
marker. A turn flip changes who has the ball inside the current workflow state; it is not a state
transition.

## Requirements

### REQ-002.1: Hook turn flips

1. A Stop event with no reported live background task MUST set the task turn to `user`.
2. A Stop event reporting any background-task entry not identified by a known terminal status MUST preserve the task's current turn.
3. A UserPromptSubmit event MUST set the task turn to `agent` even when its payload reports live background work.

### REQ-002.2: Conservative background classification

1. Background-task entries with a missing, unknown, or malformed status MUST be treated as live.
2. Background-task entries whose status is `completed`, `failed`, `cancelled`, `canceled`, or `error`, compared without case or surrounding whitespace, MUST be treated as terminal.
3. REQUIREMENT REMOVED
4. An absent `background_tasks` field or an empty list MUST be treated as reporting no live background task.
5. A `background_tasks` field whose value is not a list MUST be treated as reporting live background work.

### REQ-002.3: Blocked orthogonality

1. Changing the task turn MUST NOT change its blocked marker.
2. A true blocked marker MUST remain true until an explicit blocked-marker update sets it to false.

## Amendment decision

- Malformed background data is handled conservatively at both levels: an unrecognized entry and
  a malformed top-level collection both keep the turn with the agent. Absence and a valid empty
  list continue to mean that no background work was reported.
