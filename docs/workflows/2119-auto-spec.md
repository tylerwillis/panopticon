# `2119-auto-spec`

Runs the RFC 2119 specification, implementation, and proof stages autonomously, then waits
for you to approve the reviewed PR. It keeps the spec-first contract and cross-provider final
review of [`2119-human-spec`](2119-human-spec.md), but removes the human specification gate.

```
SPECIFYING → BUILDING → TESTING → REVIEWING → MERGING → COMPLETE
```

(plus `DROPPED`, reachable from any state.)

**When to use:** GitHub changes where the agent may proceed from an honestly tested
specification into implementation without waiting, while the final reviewed PR still needs
your approval.

This workflow is **opt-in**: enable it for a repo before it appears in the task-creation
picker.

## Lifecycle

| State | What happens | Who advances |
|---|---|---|
| **SPECIFYING** | The agent writes individually addressable requirements under `specs/`, annotates genuine tests for every MUST-level requirement, obtains fresh-context test-honesty verdicts, and publishes the specification as a task artifact. | **The agent**, which proceeds directly to implementation once the specification responsibilities are resolved. |
| **BUILDING** | The agent implements the specification, makes reviewable commits, opens a draft PR, and records its URL on the task. | **The agent**, once every building responsibility is resolved. |
| **TESTING** | The agent runs the full test suite, the 2119 check, the repo's own local gate, and PR CI. | **The agent**, once every gate is green. |
| **REVIEWING** | Two fresh-context reviewers inspect the final diff, attempt targeted mutations, and publish evidence-bearing PR comments. The agent triages every finding, applies accepted fixes, reruns the testing gates, and publishes the review outputs and triage summary as task artifacts. | **You**: advancing to MERGING is your approval of the reviewed PR. |
| **MERGING** | The agent considers the triage summary's suggested follow-up issues, files those you endorsed or did not reject, and shepherds the PR through the merge queue. | **The agent**, which advances itself once the PR is merged. |
| **COMPLETE** | Terminal. The change has landed. | n/a |

If the agent accepts a must-fix, it runs one fresh review round after retesting. Review is
capped at two rounds.

## Reviewer defaults

The test-honesty reviewer defaults to `codex:gpt-5.6-sol`. The two final-review slots
default to `claude:claude-fable-5` and `codex:gpt-5.6-sol`, providing cross-provider review.
Repository reviewer settings can replace any of those defaults without changing the task's
interactive harness or model.

Final-review comments are accepted only when the dispatch helper verifies the responding
model, reviewed commit, and review round. Each reviewer chooses its own mutation and applies
it only in a throwaway copy, not the task checkout.

## Your part and the agent's part

- **You**: react to suggested follow-up issues in the triage PR comment and approve the
  reviewed PR by advancing from REVIEWING.
- **The agent**: writes and publishes the contract, creates and gets the tests judged,
  implements only the specified change, proves it locally and in CI, runs and triages the
  final reviews, and merges after your approval.

## Skills

- **`spec-2119`** writes the specification and annotated tests, dispatches test-honesty
  reviews, records their verdicts, and publishes the specification artifact.
- **`open-pr`** opens a draft PR that references the published specification and records
  its URL on the task.
- **`babysit-ci`** watches CI and repairs failures or base conflicts.
- **`dual-review`** dispatches the configured final reviewers, verifies their evidence,
  requires targeted mutations, and drives triage and any re-review.
- **`babysit-merge`** files approved or unopposed deferred-work issues, then shepherds the
  PR through the merge queue.

## Related

- [`2119-human-spec`](2119-human-spec.md): adds a human approval gate after specification.
- [`2119-auto-sol`](2119-auto-sol.md): the same automatic lifecycle with two Sol final-review
  slots by default.
- [Workflow catalog](README.md).
