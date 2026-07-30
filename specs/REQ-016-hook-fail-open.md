# REQ-016: Fail-open in-container hooks

## Overview

Panopticon's injected in-container hooks perform best-effort control-plane bookkeeping and cannot
be allowed to freeze an interactive harness. Losing a turn signal is cosmetic and recoverable;
holding the harness input path is an outage.

This contract covers every Panopticon-injected callback that contacts the control plane: Claude's
`Stop` and `UserPromptSubmit` command hooks; Claude's `PreToolUse` and `PostToolUse` command hooks
matched to `AskUserQuestion`; Codex's `Stop` and `UserPromptSubmit` command hooks; and Pi's
`agent_end` and `input` extension handlers. A control-plane failure includes connection, response,
protocol, and status failures as well as a request that remains non-responsive.

Invocation begins when the harness dispatches the command hook or extension handler. Returning
control means that a command hook process exits or a Pi extension handler settles. A successful
return is exit status zero for a command hook and fulfilled, non-throwing completion for a Pi
handler. The time bound applies to the complete callback, not independently to each request it
makes.

## Requirements

### REQ-016.1: Bounded input path

1. Every Panopticon-injected in-container hook MUST return control to its originating harness
   within three seconds of invocation, regardless of the control-plane outcome.

### REQ-016.2: Fail-open completion

1. A Panopticon-injected in-container hook that encounters a control-plane failure MUST return
   successfully without surfacing the failure to its originating harness.

### REQ-016.3: Successful turn signals

1. When no control-plane failure occurs, every Panopticon-injected turn-flip hook MUST produce the
   event outcome in this table:

   | Harness | Event | Resulting task turn |
   | --- | --- | --- |
   | Claude | `Stop` with no live background work | `user` |
   | Claude | `Stop` reporting live background work | unchanged |
   | Claude | `UserPromptSubmit` | `agent` |
   | Claude | `AskUserQuestion` `PreToolUse` | `user` |
   | Claude | `AskUserQuestion` `PostToolUse` | `agent` |
   | Codex | `Stop` with no live background work | `user` |
   | Codex | `Stop` reporting live background work | unchanged |
   | Codex | `UserPromptSubmit` | `agent` |
   | Pi | `agent_end` | `user` |
   | Pi | `input` | `agent` |
