# REQ-045: Remote session input and transcript

## Overview

Remote clients can already inspect and mutate task state through the task service, but the host
tmux session remains the only path to the agent's interactive input and visible output. This
contract closes that gap without moving tmux access into the control plane. Input is a durable
request consumed by the runner that owns the task, and transcript output is a bounded pane snapshot
published by that runner. Both paths remain deterministic and LLM-free.

An input response distinguishes queue acceptance from the later tmux side effect. A client supplies
an idempotency key, can inspect the resulting delivery record, and never receives a success response
that falsely claims text was typed before the runner confirms it. The recorded task turn is the
portable, harness-independent busy signal: input is accepted only while the user holds the turn.
This avoids pasting into an agent that is working, while acknowledging that tmux and the subsequent
control-plane acknowledgement cannot form one transaction.

Transcript reads use pane capture rather than harness rollouts. Pane capture works for every harness,
has bounded host and network cost, and reflects what an attached operator sees. It is intentionally
lossy, width-dependent, and limited to recent scrollback. The snapshot is not redacted: anyone with
read access must be trusted to see arbitrary terminal output, including secrets printed by the agent
or its tools. Accepted input and the latest pane snapshot are stored durably in the task-service
database and its backups without automatic retention or secret sanitisation; operators must treat
that database as holding the same credentials and sensitive content as agent sessions.

## Requirements

### REQ-045.1: Authenticated surface

1. `POST /tasks/{task_id}/session/input` MUST require either the master write principal or a cryptographically derived task principal matching `{task_id}` in enforced mode.
2. `GET /tasks/{task_id}/session/input/{delivery_id}` and `GET /tasks/{task_id}/session/transcript` MUST require task-service read authentication in enforced mode.
3. Runner-only input polling, transcript publication, and delivery settlement MUST require the master write principal in enforced mode.

### REQ-045.2: Honest input acceptance

1. An input request for an existing non-terminal task whose container is live, whose owning runner is live, and whose recorded turn is `user` MUST return HTTP 202 with a durable delivery identifier and `pending` status without claiming that tmux delivery has occurred.
2. An input request MUST reject a missing task with HTTP 404 and reject a terminal, non-live, unowned, disconnected, or agent-turn task with HTTP 409 without creating a delivery record.
3. An input request MUST reject empty text, text larger than 64 KiB encoded as UTF-8, C0/C1 terminal-control characters other than newline and tab, an idempotency key outside 8–128 ASCII characters from `[A-Za-z0-9._~-]`, or a non-boolean `submit` value with HTTP 422.

### REQ-045.3: Submit is explicit

1. A runner delivering a request whose `submit` value is `false` MUST bracketed-paste the text without sending an Enter key.
2. A runner delivering a request whose `submit` value is `true` MUST bracketed-paste the text and send exactly one Enter key.
3. Input containing a bracketed-paste terminator or a C0/C1 terminal-control character other than newline and tab MUST be rejected before delivery.

### REQ-045.4: Turn and attention semantics

1. Accepting or settling an input delivery MUST NOT directly change the task's `turn`, `blocked`, or `attention` fields.
2. The existing task-service turn write to `agent` MUST clear `blocked` and `attention` for submitted-input hook processing.

### REQ-045.5: Durable delivery lifecycle

1. The owning runner MUST process pending input records only while it still owns the task and the task remains live with the recorded turn set to `user`.
2. A successful tmux paste MUST settle the delivery as `delivered`.
3. A missing or vanished pane, readiness timeout, or tmux failure MUST settle the delivery as `failed` with the stable machine-readable reason `tmux-delivery-failed`.
4. The delivery-status endpoint MUST return the request's identifier, status, `submit` value, byte count, creation and settlement metadata, and failure reason without returning the submitted text.
5. Repeated client requests with the same task-scoped idempotency key and identical text and `submit` value MUST return the original delivery record without causing another tmux delivery.
6. Reusing a task-scoped idempotency key with different text or a different `submit` value MUST return HTTP 409 without altering the original delivery record.
7. The session-input API documentation MUST state that client retries are idempotent but a runner crash or settlement-write failure after the tmux side effect can cause a duplicate delivery.
8. A task MUST have at most one pending remote-input delivery at a time.

### REQ-045.6: Control-plane boundary

1. Input acceptance, input-status retrieval, delivery settlement, transcript publication, and transcript retrieval MUST remain available when the task-service host has no usable tmux or Docker executable or socket.
2. A runner-only session-I/O host operation MUST require the master authenticated principal and a runner identifier matching the runner that currently owns the task.
3. Stage-entry wake and remote-input writes to one task pane MUST be serialized and use disjoint tmux buffer names.

### REQ-045.7: Bounded pane transcript

1. The owning runner MUST publish the largest plain-text suffix of a tmux pane that fits the most recent 200 logical lines and 64 KiB encoded as UTF-8, truncating oldest content first on either limit.
2. The transcript endpoint MUST return the latest published text with source `pane`, a task-service `received_at` timestamp, pane `columns` and `rows`, `truncated` status, and publishing `runner_id`.
3. A transcript read for an existing task with no published snapshot MUST return HTTP 503 with the JSON body `{"detail":"session transcript unavailable"}`.
4. A transcript read after the task becomes non-live MUST retain and label the last published snapshot as stale.
5. Transcript publication and retrieval MUST preserve rendered Unicode pane text after removing ANSI escape sequences.

### REQ-045.8: Transcript confidentiality contract

1. The transcript endpoint MUST return the runner-published pane text unchanged without content redaction.
2. The transcript endpoint's API documentation MUST warn that the response can contain arbitrary terminal output, including credentials or other secrets printed in the pane.

## Non-goals

- Complete or width-independent conversation history is out of scope; clients receive recent pane
  content, not harness-specific rollout files.
- Interrupting a running agent, bypassing the task turn contract, and accepting input for later
  delivery after a container recovers are out of scope.
- Transactional exactly-once delivery across tmux and the task service is out of scope.
- Resolving the general stage-entry wake acknowledgement ambiguity in issue #101 is out of scope;
  the input idempotency key prevents duplicate client submissions but cannot remove that crash
  window.
