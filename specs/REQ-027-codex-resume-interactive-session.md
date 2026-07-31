# REQ-027: Codex resume targets the interactive session, not a reviewer rollout

## Overview

The codex harness's launch argv (`harnesses/codex.py`, `Harness.argv`) resumes a task's prior
codex session by scanning `$CODEX_HOME/sessions` for any recorded `*.jsonl` rollout and running
`codex resume --last`. `CODEX_HOME` is the task's own per-task config volume, but it is not
exclusive to the task's own interactive session: the dual-review and test-honesty skills
(`dual-review`, `spec-2119`) dispatch fresh-context reviewers via `codex exec` *inside the same
task container*, sharing the same `CODEX_HOME`. Each reviewer invocation writes its own rollout
file into `$CODEX_HOME/sessions`, and being freshly written, that file becomes the newest one —
so `--last` resolves to a reviewer's `codex exec` thread rather than the task's own interactive
session. A `codex exec` rollout is not resumable as an interactive session, so codex falls back
to a blank fresh session, discarding the task's conversation history. Reproduced live
2026-07-31: after a host reboot respawned every long-running task container, this silently
orphaned 3 of 14 task sessions — every task whose most recent action before the respawn was
dispatching reviewers. Because dispatching reviewers is a normal step of this repository's own
2119 workflow, any task that reaches review is eventually exposed; a host reboot (or any other
container respawn) surfaces it.

Live workaround used while this was unfixed: `tmux respawn-pane` running
`docker exec <container> codex resume <session-id>` with a session id read by hand — not a
substitute for a durable fix, since it requires an operator to notice the orphaned pane and knows
which id to resume.

## Design

Resolve the resume target explicitly rather than trusting `--last`: scan
`$CODEX_HOME/sessions/**/*.jsonl`, read only the first line of each file (codex writes a
`session_meta` record as a rollout's first line, carrying the session's id and an
originator/source field distinguishing an interactive `codex-tui` launch from a `codex_exec` /
`exec` invocation), and resume the newest rollout whose originator identifies an interactive
session. Reviewer rollouts are skipped by construction, regardless of how recently they were
written.

**Rejected alternative:** record the session id at first launch instead of scanning. Rejected
because codex generates its session id after the process starts, so the id doesn't exist yet at
the point the harness builds first-run argv — capturing it would require the harness to observe
the running process after launch and persist the id somewhere durable, adding new state that can
drift from reality (e.g. if the container is killed before the id is captured, or the persisted
id is stale after a later respawn). The scan-based design needs no new persisted state, is
naturally idempotent, and retroactively heals every existing task's config volume without a
migration.

## Requirements

### REQ-027.1: Resume target selection

1. When `$CODEX_HOME/sessions` contains at least one rollout file whose first line identifies an
   interactive originator, the codex harness's launch argv MUST resume the newest such rollout by
   its recorded session id rather than by `--last`.
2. When more than one rollout file identifies an interactive originator, the codex harness MUST
   select the most recently written one.
3. A rollout file whose first line identifies a non-interactive (`codex exec`) originator MUST
   NOT be selected as the resume target.

### REQ-027.2: First-run fallback

1. When `$CODEX_HOME/sessions` contains no rollout files at all, the codex harness's launch argv
   MUST be the same first-run argv produced before this scan existed (no `resume` subcommand).
2. When `$CODEX_HOME/sessions` contains one or more rollout files but none identifies an
   interactive originator, the codex harness's launch argv MUST fall back to first-run argv
   rather than raising an error or resuming a non-interactive session.

### REQ-027.3: Malformed rollout tolerance

1. A rollout file whose first line is empty, not valid JSON, or lacks a recognizable originator
   field MUST be excluded from resume-target selection without the harness raising an error.

### REQ-027.4: Scan cost

1. Resume-target selection MUST determine each rollout file's originator by reading only that
   file's first line, never the file's full contents.

### REQ-027.5: Unchanged resume behavior otherwise

1. When the codex harness resumes a session (by either the newly-scanned interactive session id
   or, before this feature, `--last`), the flags accompanying the `resume` subcommand MUST be the
   same session flags used on a first run (`--dangerously-bypass-approvals-and-sandbox`,
   `--dangerously-bypass-hook-trust`, `--no-alt-screen`).
2. When the codex harness resumes a session on the agent's turn, the launch argv MUST append the
   interrupt prompt exactly as it did before this feature.
