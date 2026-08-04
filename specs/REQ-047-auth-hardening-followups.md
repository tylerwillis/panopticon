# REQ-047: Authentication hardening follow-ups

## Overview

This contract closes five independent, defense-in-depth follow-ups from the task-service
authentication review. The work keeps the original risk calibration: configured tokens are already
random, constrained to at least twelve characters, and drawn from a broad alphabet, so hiding their
length is hardening rather than remediation of a demonstrated attack.

The follow-ups replace length-preserving MCP response masking with a constant marker, separate
runtime-credential cleanup from destruction of already-exited task evidence, scope log redaction to
the applications whose credentials it protects, move integrated-stack logs into private per-user
state, and make the health endpoint's HEAD semantics explicit.

The health decision is to expose `HEAD /healthz` alongside `GET /healthz`. A successful HEAD reveals
no more readiness information than the already-public GET, while supporting probes that intentionally
avoid response bodies.

Items concerning the permissive-mode request inspection ceiling, recursive semantic JSON traversal,
and assignment to Starlette's private `request._body` attribute are excluded. The separately tracked
`permissive-mode-fate` task is specifying removal of permissive mode after the direct-to-enforced
deployment succeeded. The current middleware also reuses this inspection for authenticated enforced
requests under REQ-035.44 and REQ-035.46, so removing permissive mode will not by itself eliminate
every affected code path; that follow-up must preserve the enforced-mode rejection contract.

## Requirements

### REQ-047.1: Constant-width MCP redaction

1. MCP response streaming MUST replace every configured token occurrence with the same fixed
   `[redacted]` byte marker regardless of the token's length, including occurrences split across any
   response-chunk boundary.
2. MCP response streaming MUST emit a complete non-secret SSE event without retaining bytes merely
   because its payload suffix immediately before the terminating blank line is a configured token
   prefix.

### REQ-047.2: Post-mortem evidence and credential cleanup

1. Cleanup of a terminal container task whose container has already exited MUST remove every
   remaining runtime authentication snapshot for that task without killing its tmux session or
   force-removing its container.
2. Cleanup of a terminal container task whose container is still running MUST stop its backend and
   remove every remaining runtime authentication snapshot before deleting its workspace.
3. Spawning a replacement for a task with preserved post-mortem resources MUST remove the stale tmux
   session and container before starting the replacement.

### REQ-047.3: Application-scoped log redaction

1. Importing the task service and creating, running, or closing authenticated application
   lifespans MUST leave the process-wide log-record factory, `logging.Logger.makeRecord`, and
   `logging.Handler.handle` identities unchanged and restore the pre-lifespan
   `logging.Logger.handle` identity whenever no authenticated lifespan remains active.
2. During an authenticated task-service application's lifespan, each configured token MUST be
   replaced by the exact literal marker `[redacted]` in messages, arguments, caller-supplied extra
   fields, exception text, and stack information in every log record handled in the process,
   regardless of logger namespace.
3. Ending an authenticated application's lifespan MUST discard its redaction tokens without
   disabling redaction for another application whose lifespan overlaps it.

### REQ-047.4: Private integrated-stack logs

1. Integrated stack startup MUST persist service and runner logs beneath a Panopticon per-user state
   directory resolved as `$PANOPTICON_STATE`, then `$XDG_STATE_HOME/panopticon`, then
   `~/.local/state/panopticon`, instead of a shared temporary directory.
2. Integrated stack startup MUST create its log directory with mode `0700` and its service and runner
   log files with mode `0600` without following a pre-existing symbolic link at any of those paths.
3. Integrated stack startup MUST continue sending service and runner output to their tmux panes while
   persisting the same output in the private log files.

### REQ-047.5: Public HEAD health probe

1. In disabled, permissive, and enforced authentication modes, unauthenticated `HEAD /healthz` MUST
   return the same success status and response headers as unauthenticated `GET /healthz`, with an
   empty response body.

## Non-goals

- Changing token generation, entropy requirements, or the twelve-character minimum is out of scope.
- Encrypting redaction markers or attempting to conceal response length beyond using a constant
  marker is out of scope.
- Preserving runtime credential snapshots for post-mortem inspection is forbidden; only
  non-credential container and tmux evidence is retained.
- Adding log rotation, retention policy, or a general logging subsystem is out of scope.
- Changing the existing public `GET /healthz` readiness payload is out of scope.
- Hardening request-body inspection is deferred to the separately tracked follow-ups; any redesign
  must account for its enforced-mode use as well as the pending permissive-mode decision.
