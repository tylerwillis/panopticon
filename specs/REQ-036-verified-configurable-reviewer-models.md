# REQ-036: Verified, configurable 2119 reviewer models

## Overview

The built-in 2119 review skills currently name Fable 5 and Sol 5.6 in prose, pass one of those
names to a CLI, and ask each reviewer to repeat the name in its report heading. That is asserted
identity, not evidence of the model that served the request. In particular, a fallback can leave
the report looking like an independent cross-family review even when the requested model did not
respond.

Reviewer selection remains task-container behavior. Each 2119 workflow supplies two ordered
default harness/model pairs, and a repo may replace either pair through its existing env-file
injection. The model portion uses the owning harness's opaque vocabulary, as task launch model
selection already does; the control plane does not learn provider model names or call an LLM.

Claude provides supported response evidence: `claude --print --output-format json` returns a
`modelUsage` object keyed by the responding model. Codex CLI 0.144.4 has a weaker interface:
`codex exec --json` emits a `thread.started` id but no model in stdout, while the correlated
persisted rollout contains a machine-written `turn_context.payload.model`. Codex verification may
use that correlated rollout field, but it is explicitly weaker because it is not part of the
command's documented JSON output contract. Absence or ambiguity of either CLI's evidence is a
failed dispatch, never permission to trust the requested model or the reviewer's prose.

## Requirements

### REQ-036.1: Reviewer configuration

1. Reviewer resolution MUST return two ordered atomic harness/model pairs by independently
   applying `PANOPTICON_2119_REVIEWER_1` and `PANOPTICON_2119_REVIEWER_2` `<harness>:<model>` repo
   env-file overrides to a built-in workflow's two defaults, splitting only on the first colon,
   preserving the model substring verbatim, retaining two Codex/Sol defaults for
   `2119-auto-sol`, and rejecting a missing harness, missing model, unsupported harness, or
   malformed pair before invoking a reviewer command.

### REQ-036.2: Machine verification

1. A Claude reviewer dispatch MUST run
   `claude --print --output-format json --safe-mode --dangerously-skip-permissions --model <requested>`
   so the sandboxed reviewer can inspect the diff without inheriting task hooks, parse the
   command's raw JSON stdout, identify exactly one responding `modelUsage` entry by matching its
   token counters to the top-level response usage while tolerating distinct auxiliary-model
   entries, and accept the response only when that responding key exactly equals the requested
   model string.
2. A Codex reviewer dispatch MUST correlate the sole `thread_id` in raw `codex exec --json`
   stdout with that thread's persisted rollout and accept the response only when exactly one
   machine-written `turn_context.payload.model` exists and equals the requested model string.
3. A reviewer dispatch MUST raise a typed failure carrying its failure kind, requested model,
   specific failure detail, and retry-or-reconfigure remediation and perform no PR-comment or
   artifact publication when the command exits nonzero, identity evidence is missing or
   ambiguous, or the observed model differs from the requested model.

### REQ-036.3: Evidence-bearing reports

1. A completed review PR comment MUST derive its heading from the verified responding model and
   contain the reviewer harness, requested model, verified responding model, verification source,
   reviewed commit, review round, and reviewer body without treating any model label in that body
   as identity evidence.
2. A nonzero reviewer command whose stderr contains `usage limit` or `unavailable`, matched
   case-insensitively, MUST produce an availability-classified dispatch failure while a
   successfully verified review body containing no findings produces a completed evidence-bearing
   review comment.
3. A reviewer dispatch MUST verify that its recorded reviewed commit exactly equals the checkout's
   current `HEAD` and bind that commit plus the selected base ref into the reviewer prompt.

### REQ-036.4: Review gates

1. The review gate MUST accept exactly two parsed evidence-bearing comments only when each has a
   supported verification source, equal requested and verified model strings, and the expected
   final commit and review round; it must reject every missing, unverified, mismatched, stale, or
   wrong-round comment before triage or review-responsibility resolution.

## Non-goals

- Panopticon does not translate aliases into canonical model identities. An operator must
  configure a model string that the selected CLI reports exactly, or verification fails.
- This change does not claim that Codex's current rollout field is a documented public output
  contract. A future Codex CLI that emits responding-model identity directly should replace the
  rollout correlation, subject to the same exact-match and evidence requirements.
- The Sol-only workflow remains a deliberate same-model variant; configuration complements it
  rather than removing the convenient workflow choice.
- All dispatch plumbing remains in the task container. The existing repository-wide determinism
  invariant already forbids control-plane LLM calls; this feature does not create a second model
  execution surface.
