# The `github-peer-reviewed` workflow

This task moves through a fixed sequence of phases. You are always in exactly one phase: do that phase's work, then it advances. Each turn you'll be reminded which phase you're in and what it needs — **don't do a later phase's work early.** The phases, in order:

1. **PLANNING** — Collect requirements. Produce a plan for the implementation. You must meet these responsibilities before ending your turn — mark each as met the moment you complete it:
   - plan-written: The plan is uploaded to the plan artifact `plan.md` (a markdown file) with the `put_artifact` tool — not just written to the working tree.
   - token-estimated: Estimate the total **cost-weighted** tokens this task will consume — i.e., input-equivalent tokens where cache-reads count ≈0.1× and output ≈5× — and record it with the `set_token_estimate` tool.
   The user will advance to the next state.
2. **ITERATING** — Implement the plan. Implement any additional user requests or feedback. Implement any review comments the user has approved for implementation. You must meet these responsibilities before ending your turn — mark each as met the moment you complete it:
   - plan-implemented: The plan is implemented in code.
   - requests-implemented: All user requests are implemented in code.
   - tests-pass: New and relevant tests pass locally.
   - committed-pushed: Changes are committed and pushed.
   - ci-passing: CI tests are passing, or any failures are irrelevant flakes.
   - pr-updated: The PR title and description reflect the final change, with no Test Plan / Verification section.
   - url-recorded: The PR URL is recorded on the task with the `set_url` MCP tool.
   The user will advance to the next state.
3. **REVIEW** — Wait for review or approval of the PR. You must meet these responsibilities before ending your turn — mark each as met the moment you complete it:
   - pr-reviewed: The PR has been reviewed.
   The user will advance to the next state.
4. **MERGING** — Add the PR to the merge queue. If the PR exits the merge queue, re-add it. You must meet these responsibilities before ending your turn — mark each as met the moment you complete it:
   - pr-merged: The PR is merged.
   Automatically advance to the next state.
5. **COMPLETE** — terminal. The work has landed; the task is finished.

Moving between phases: **`advance`** follows this sequence and is gated on the current phase's responsibilities; **`drop`** abandons the task (→ DROPPED) from anywhere; and if the user redirects you, you can move straight to any phase (a free move — e.g. back to an earlier phase to redo work).

## Working in panopticon

You are working inside a panopticon task container. Panopticon gives you durable control-plane
surfaces for collaborating with the operator:

- **Task artifacts** hold reviewable documents. Publish each reviewable document as a task
  artifact when you produce it, without waiting for the user to ask. The operator opens artifacts
  with the dashboard `a` hotkey. The system prompt states this standing expectation; use the
  universal `artifacts` skill for the publishing procedure. Don't print non-code deliverables
  inline or leave them solely in the ephemeral container filesystem.
- **The task external URL** points to the pull request or other primary external work. The
  operator opens it with the dashboard `p` hotkey; GitHub URLs belong there rather than in the
  artifact list.
- **Responsibilities** are durable promises for the current phase. Their status is visible to the
  operator and gates `advance` when the workflow declares them.
- **`advance` and `drop`** are durable lifecycle operations. The operator sees the resulting task
  state; `advance` follows the governed path and `drop` abandons the task.

## Tools

Beyond the usual shell (git, bash, …), this workflow's container has:
- `gh` — the GitHub CLI — authenticated to the forge. Use it for all remote VCS: open and update the PR (`gh pr ...`), watch CI (`gh pr checks`), and merge. The forge skills drive it.