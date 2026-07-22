# REQ-001: Bulk respawn down dashboard tasks

## Overview

Provide a filter-shaped dashboard action for restarting all tasks that the task service currently
reports as down, while retaining confirmation and the existing claim-release respawn behavior.

Failed tasks are excluded: `failed` communicates a spawn error that may still require diagnosis,
whereas this action targets the explicit `down` recovery set after container loss.

## Requirements

### REQ-001.1: Invocation

1. The dashboard MUST make the bulk-respawn action available through `Ctrl+R`.

2. The dashboard help surface MUST identify `Ctrl+R` as the action for respawning all down tasks.

### REQ-001.2: Confirmation modal

1. When at least one task in the dashboard's latest task-service snapshot has `container_status`
   equal to `down`, invoking bulk respawn MUST open a confirmation modal containing every and only
   those down tasks.

2. Each task in the bulk-respawn confirmation modal MUST be represented by an ID prefix, its slug
   value or `–` when unset, and a leading excerpt of its memo when present.

### REQ-001.3: Cancellation

1. Pressing Escape in the bulk-respawn confirmation modal MUST dismiss it without issuing any
   claim-release request.

### REQ-001.4: Confirmation

1. For each listed task that the task service reports as `down` when its confirmation-time check
   occurs, the dashboard MUST invoke the same claim-release respawn behavior as the single-task
   respawn action exactly once.

2. Confirmation MUST process listed tasks sequentially in their displayed order.

3. After a successfully completed confirmation, the dashboard MUST report `respawned N` on one
   line, where `N` is the number of claims released by that confirmation.

4. A listed task that the task service reports as not `down` when its confirmation-time check
   occurs MUST have no claim-release request issued by that confirmation.

5. Skipping a listed task that is no longer `down` MUST produce no task-specific notification.

### REQ-001.5: Empty set

1. When no task in the dashboard's latest task-service snapshot has `container_status` equal to
   `down`, invoking bulk respawn MUST report `no down tasks`.

2. When no task in the dashboard's latest task-service snapshot has `container_status` equal to
   `down`, invoking bulk respawn MUST avoid opening a modal.
