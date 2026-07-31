# REQ-031: Docker Daemon Preflight & Spawn-Loop Resilience

## Overview

After a host reboot, OrbStack/Docker Desktop does not auto-start. `panopticon start` (and the
per-host session-service daemon it launches) previously proceeded straight into spawning task
containers against a dead Docker daemon: every spawn attempt failed, burning each task's
crash-loop respawn budget until the whole board read `failed`, and the operator had to start
Docker by hand and then claim-release every task individually to recover.

This contract adds a fail-loud preflight at the two places that start spawning containers — the
operator CLI (`panopticon start`/`panopticon host`) and the per-host session-service daemon
process itself — so an unreachable Docker daemon is refused with an actionable message instead of
being spawned into failure. It does not launch or otherwise manage the Docker daemon/OrbStack/
Docker Desktop process; detecting and reporting is the full scope (see Non-goals).

Because the daemon can also become unreachable while the session-service daemon is already
running (not just at its own startup), this contract additionally has the spawn loop distinguish
an unreachable Docker daemon — an environmental condition — from a task-specific crash: a spawn or
respawn attempt deferred for this reason does not consume the task's crash-loop respawn budget and
does not mark the task's lifecycle `failed`. Because the task's claim and respawn budget are left
untouched, it self-recovers once the daemon becomes reachable again, with no state for the
operator to hand-clear.

## Requirements

### REQ-031.1: Operator CLI startup preflight

1. `panopticon start` MUST NOT start the background task-service or runner sessions when the
   Docker daemon is unreachable.
2. `panopticon host` MUST NOT start the background task-service or runner sessions when the
   Docker daemon is unreachable.
3. Refusing to start under REQ-031.1.1 or REQ-031.1.2 MUST print a message that states the Docker
   daemon is unreachable and names a remediation for both macOS (OrbStack or Docker Desktop) and
   Linux (the system service manager).
4. Refusing to start under REQ-031.1.1 or REQ-031.1.2 MUST return a non-zero exit status.
5. `panopticon start` and `panopticon host` MUST start the background task-service and runner
   sessions, as before, when the Docker daemon is reachable.

### REQ-031.2: Session-service host daemon startup preflight

1. The per-host session-service daemon process (`python -m panopticon.sessionservice.host`) MUST
   refuse to begin its spawn/heal loop when the Docker daemon is unreachable at its own startup,
   instead of entering a loop that would fail every spawn attempt.
2. Refusing to start under REQ-031.2.1 MUST report a message that states the Docker daemon is
   unreachable and names a remediation for both macOS and Linux.

### REQ-031.3: Spawn loop distinguishes daemon-down from task-specific crashes

1. When the Docker daemon is unreachable, `Spawner.spawn_one` MUST defer spawning a task whose
   workflow is not a shell workflow, without claiming it.
2. When the Docker daemon is unreachable, `Spawner.heal` MUST defer respawning an orphaned task
   whose workflow is not a shell workflow, without incrementing that task's crash-loop respawn
   counter.
3. When the Docker daemon is unreachable, `Spawner.heal` MUST NOT report the deferred task's
   lifecycle as `failed`.
4. Docker daemon reachability MUST NOT affect `Spawner.spawn_one` or `Spawner.heal` for a
   shell-workflow task.
5. After a period of Docker daemon unreachability during which `Spawner.heal` deferred respawning
   an orphaned task one or more times, `Spawner.heal` MUST, once the daemon is reachable again,
   respawn that task using the crash-loop respawn count it held before the daemon became
   unreachable — the deferred attempts leave that count exactly where it was.
6. `Spawner.spawn_one` and `Spawner.heal` SHOULD log a message identifying Docker daemon
   unavailability as a runner-level condition distinct from a task-specific spawn failure.

## Non-goals

- Auto-launching or otherwise managing the Docker daemon, OrbStack, or Docker Desktop process
  (e.g. `open -a Docker`) is out of scope; the preflight and spawn-loop deferral only detect and
  report, per the operating manual's determinism/module-boundary conventions keeping platform
  service management out of the control plane.
- Distinguishing Docker-daemon-down from a task-specific condition in `Spawner.reconcile`,
  `Spawner.cleanup`, or `Spawner.mark_healing` is out of scope for this change.
- Any retry/backoff scheduling beyond the session-service daemon's existing per-tick loop cadence
  is out of scope; deferred spawns and respawns are picked back up on a later ordinary tick once
  the daemon is reachable, with no new polling mechanism introduced.
