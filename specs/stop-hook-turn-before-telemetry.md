# Stop Hook Turn Before Telemetry

## Overview

The Stop callback carries two jobs with different reliability needs. Updating the task turn is a
correctness signal used by the dashboard and wake machinery; parsing a complete session transcript
and reporting its cost-weighted token total is telemetry whose runtime grows with the transcript.
Running those jobs serially under a fixed harness timeout lets a long session consume the callback's
budget before the control plane learns that the agent stopped.

The remedy is ordering plus lifetime isolation. An eligible Stop first updates the turn, then starts
token accounting in a detached worker which is not part of the bounded hook command's lifetime. The
worker retains the existing full-transcript, cost-weighted accounting semantics. Making the parser
faster or raising the timeout would only move a transcript-size cliff, so neither is the remedy.

Both supported command-hook harnesses are exposed. Codex renders the callback in
`~/.codex/config.toml`, and Claude renders the same callback in `~/.claude/settings.json`; both set a
three-second command timeout. The timeout remains useful defense in depth for interactive
availability, but token accounting no longer competes with the turn signal inside it.

This is another control-plane/reality divergence in the family tracked by
[Issue #159](https://github.com/tylerwillis/panopticon/issues/159): there the board can misstate agent
activity while background work runs, and its follow-up records transient `live` container status
without a reachable tmux session. Here the board can misstate agent activity after work ends.

## Requirements

### 1: Correctness-first Stop handling

1. A Stop callback eligible to hand control to the user MUST successfully request the `user` turn
   before it begins transcript parsing or token-report delivery.
2. Transcript-accounting duration MUST NOT prevent an eligible Stop callback from requesting the
   `user` turn within the callback deadline.
3. A Stop callback reporting live background work MUST preserve the existing turn while still
   arranging token accounting when it names a transcript.

### 2: Telemetry lifetime isolation

1. A Stop callback naming a readable transcript MUST arrange token accounting to continue outside
   the bounded command-hook process after that callback returns.
2. Deferred token accounting MUST report the complete transcript's cumulative cost-weighted token
   total when the transcript remains readable and the task service accepts the report.
3. Deferred token accounting MUST start without receiving or observing a timeout notification or
   error from the originating harness.

### 3: Harness wiring

1. Claude's Stop hook configuration MUST invoke the correctness-first callback with a finite
   three-second timeout as defense in depth.
2. Codex's Stop hook configuration MUST invoke the correctness-first callback with a finite
   three-second timeout as defense in depth.
