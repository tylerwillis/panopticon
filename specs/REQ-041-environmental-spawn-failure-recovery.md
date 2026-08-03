# REQ-041: Environmental spawn failure recovery

## Overview

The session-service spawn path reports every exception as a task lifecycle failure. That is
appropriate after the task's tmux session exists and its agent launcher reports an actionable
problem, such as missing credentials: the report must remain visible until an operator fixes the
problem and releases the claim. It is not appropriate before a task session exists, when an image
build or `docker run` command can fail because the host is under transient resource pressure. A
task that never reached its agent must remain eligible for bounded self-healing.

The classification boundary is the spawn milestone, not a Docker exit code. Docker build exit 1
and Docker run exit 125 each cover both transient daemon conditions and persistent input errors,
so the numeric code cannot reliably say whether a retry will work. Before the task tmux session is
established, failures belong to host-side spawn infrastructure and are retried under the existing
crash-loop guard. After the session is established, an explicit launcher lifecycle failure belongs
to the task and remains latched with its diagnostic.

Unlike REQ-031.3's daemon-unreachable preflight, a command was actually attempted before a
spawn-time infrastructure failure was observed. Each failed respawn therefore remains charged to
the existing crash-loop budget. This allows transient failures to recover automatically while
bounding persistent image or Docker configuration failures at `MAX_RESPAWNS`; the existing
`RESPAWN_RESET_SECONDS` survivor rule remains the only way an attempted-respawn burst earns a new
budget. A daemon-unreachable preflight still consumes no attempt and refreshes only the existing
budget timestamp, as specified by REQ-031.3.

## Requirements

### REQ-041.1: Pre-session spawn failures remain recoverable

1. A non-zero Docker image-build command before the task tmux session is established MUST leave
   the claimed non-terminal task eligible for a later `SessionSpawner.heal` attempt without an
   operator releasing its claim.
2. A non-zero `docker run` command before the task tmux session is established MUST leave the
   claimed non-terminal task eligible for a later `SessionSpawner.heal` attempt without an
   operator releasing its claim.
3. A pre-session failure covered by REQ-041.1.1 or REQ-041.1.2 MUST clear its in-progress lifecycle
   report instead of reporting the task lifecycle as `failed`.

### REQ-041.2: Post-session launcher failures remain latched

1. An agent-launcher lifecycle failure reported after the task tmux session is established MUST
   retain its `failed` lifecycle status and actionable detail until an operator releases the
   task's claim.
2. While the claim for a task covered by REQ-041.2.1 remains held,
   `SessionSpawner.mark_healing` and `SessionSpawner.heal` MUST perform no lifecycle report or
   clear, no claim release or reclaim, and no runner operation for that task.

### REQ-041.3: Classification boundary

1. Spawn failure recovery MUST classify failures by whether the task tmux session was established,
   rather than interpreting a Docker command's numeric exit code as a reliable task-versus-host
   diagnosis.

### REQ-041.4: Bounded retries

1. A respawn attempt that reaches an image-build or `docker run` command and fails before session
   establishment MUST retain the attempt already consumed from that task's crash-loop budget.
2. Repeated pre-session spawn failures MUST stop automatic respawning when the task reaches
   `MAX_RESPAWNS` within `RESPAWN_RESET_SECONDS`.
3. A daemon-unreachable preflight that defers before attempting an image-build or `docker run`
   command MUST preserve the zero-cost budget behavior specified by REQ-031.3.

## Non-goals

- Changing the launcher-to-task-service lifecycle reporting protocol is out of scope.
- Parsing Docker stderr or assigning permanent semantics to Docker exit codes is out of scope.
- Adding a new retry scheduler, backoff policy, or crash-loop counter is out of scope; recovery uses
  the host daemon's existing tick and `SessionSpawner` budget.
- Automatically retrying shell-workflow failures is out of scope; shell workflows retain their
  existing run-once behavior.
