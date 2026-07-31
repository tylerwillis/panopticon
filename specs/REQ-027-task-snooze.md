# REQ-027: Operator task snooze

## Overview

Operators can temporarily mute tasks that are waiting on facts the control plane cannot verify,
without changing task lifecycle or turn state. The task service records the operator's requested
deadline; only the dashboard compares that value with its display clock.

The fixed twelve-hour action means “not today” and deliberately has no configuration surface.
`9999-12-31T23:59:59+00:00` is the reserved timestamp for an indefinite snooze, allowing both
finite and sticky snoozes to use the single optional `Task.snoozed_until` fact while `null`
unambiguously clears it.

This contract couples to dependency-gating's `Task.attention` field. Its agent-set marker is new
information and pierces an operator's earlier snooze decision. Snooze otherwise becomes the third
dim presentation with this precedence: attention orange, snoozed, held/gated, normal.

## Requirements

### REQ-027.1: Recorded snooze fact

1. `Task.snoozed_until` and both REST task representations MUST expose an optional ISO-8601
   timestamp whose initial value is `null`.
2. `PUT /tasks/{id}/snooze` with an `until` timestamp or `null` MUST persist that caller-supplied
   value exactly, return the updated task, and leave its lifecycle state, turn, and blocked marker
   unchanged.
3. The task service MUST preserve a recorded finite deadline whether it is before or after the
   current time, leaving activation and expiry entirely to dashboard display derivation.

### REQ-027.2: Fixed dashboard controls

1. Pressing `e` on a highlighted task that is not actively snoozed MUST record a deadline exactly
   twelve hours after the injected dashboard display time, with no configuration lookup.
2. Pressing `e` on a highlighted task with an active finite or indefinite snooze MUST clear
   `snoozed_until`.
3. Pressing `E` on a highlighted task MUST record the reserved indefinite-snooze timestamp, and
   the task-table `e` and `E` bindings must each occur exactly once in the dashboard keymap.

### REQ-027.3: Time-aware presentation

1. Before a finite deadline, the dashboard MUST dim every cell in the task row and visibly replace
   its turn text with `snoozed · <remaining duration> left`, including `snoozed · 4h left` when
   exactly four hours remain; the reserved indefinite deadline uses the visible text `snoozed`.
2. At or after a finite deadline, the dashboard MUST resume the task's ordinary attention
   derivation without clearing or otherwise mutating the recorded deadline.
3. Dashboard snooze writes and rendering comparisons MUST obtain the current time from an
   injectable display-clock seam.
4. An active `Task.attention` marker MUST pierce a snooze and render the ordinary orange user
   attention signal, while an unpierced snooze takes precedence over dependency-held or gated
   presentation.

## Non-goals

- Snoozing does not change task state, turn, blocked, attention, dependencies, claims, or
  registrations.
- The control plane does not schedule wakeups or compare deadlines with a clock.
- The twelve-hour duration is not configurable.
