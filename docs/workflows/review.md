# `review`

A hidden worker that reviews another task's plan and change from a clean, different-harness
agent context. It never edits the author's code: it either approves or leaves structured,
actionable findings on the task it governs.

```
REVIEWING → COMPLETE
```

(plus `DROPPED`, reachable from REVIEWING.)

**When it runs:** an authoring workflow that declares a reviewer harness and model enters its
`REVIEW` state. The task service creates a governed `review` task and requires its harness to
differ from the authoring task's harness.

This workflow is **hidden** from the workflow and task-creation pickers. It is infrastructure
for governed review, not a workflow you select for ordinary work. Direct API creation requires
a valid `governor_task_id` and a different harness from that governor.

## Lifecycle

| State | What happens | Who advances |
|---|---|---|
| **REVIEWING** | The reviewer reads the governor's recorded facts, `plan.md`, and PR or branch diff; assesses correctness, scope, simplicity, and net line count; then approves or publishes findings. | **The review agent**, immediately after recording its verdict. |
| **COMPLETE** | Terminal. The verdict has been delivered. | n/a |

REVIEWING has no responsibilities of its own. The authoring task carries the gate: when the
worker is created, the author enters REVIEW with a pending `review-addressed` responsibility
and a blocked marker. Completing the worker records the verdict; the author still has to
address findings and resolve its own gate.

## Review boundary

The worker receives only durable task evidence:

- the governor's recorded URL, branch, clone, slug, and memo;
- the governor's `plan.md` artifact;
- the GitHub PR diff, or the recorded branch diff when there is no PR.

The author's conversation is explicitly excluded from review input. The reviewer may inspect
the governor's checkout but must not modify it; any fix belongs to the author and a later fresh
review.

## Verdicts

- **Approve:** state the approval briefly, write no verdict artifact, and advance the review
  task to COMPLETE.
- **Findings:** write `review.md` on the governor task with concrete **Must fix** and optional
  **Suggestions** sections, then advance the review task to COMPLETE.

## Skills

- **`review-change`** finds the governor, reads its plan, inspects its PR or branch diff,
  applies the workflow's simplicity ladder, and records one of the two verdicts.

The worker also has read-only access to `gh` when the governor records a GitHub PR URL.

## Related

- [Tasks](../tasks.md): governors, artifacts, and lifecycle state.
- [Harness and model selection](../harness-and-model-selection.md): how task launch pairs are
  resolved.
- [Workflow catalog](README.md).
