# REQ-001: Review workflow and pairing declarations

## Overview

ADR-0014 stack 1 introduces the internal review-task lifecycle and the workflow-level reviewer
launch-pair declaration. Review-task creation and author/reviewer inequality are later stacks.

## Requirements

### REQ-001.1: Internal built-in workflow

1. The built-in workflow registry MUST expose a workflow named `review`.

### REQ-001.2: Initial review state

1. A task created with the `review` workflow MUST start in `REVIEWING`.

### REQ-001.3: Minimal agent-driven lifecycle

1. The `REVIEWING` state MUST be advanced by the agent.

### REQ-001.4: Review inputs

1. The `review` workflow MUST provide exactly one workflow-specific skill.

### REQ-001.5: Review criteria

1. The review skill MUST direct the reviewer to assess correctness.

### REQ-001.6: Approval verdict

1. For an approval, the review skill MUST direct the reviewer to write no `review.md` or other verdict
artifact.

### REQ-001.7: Findings verdict

1. For findings, the review skill MUST direct the reviewer to write `review.md` on the governor task.

### REQ-001.8: Read-only reviewer

1. The review skill MUST forbid the reviewer from editing the governor's code.

### REQ-001.9: Optional review pair

1. `Workflow` MUST expose `review_harness` and `review_model` as optional declarations that default to
unset.

### REQ-001.10: Pair completeness

1. Workflow registration MUST reject a review launch pairing when exactly one of `review_harness` and
`review_model` is declared.

### REQ-001.11: Registered review harness

1. Workflow registration MUST reject a declared `review_harness` that is not registered.

### REQ-001.12: Hidden from operator menus

1. The `review` workflow MUST be hidden from operator workflow menus because review tasks are created
for a governor rather than picked directly.

### REQ-001.13: Agent owns the initial turn

1. A task created with the `review` workflow MUST give its initial turn to the agent.

### REQ-001.14: Ungated review

1. The `REVIEWING` state MUST have no responsibilities.

### REQ-001.15: Review transitions

1. The `REVIEWING` state MUST expose only the forward transition to `COMPLETE` and the inherited escape
to `DROPPED`.

### REQ-001.16: Single nonterminal state

1. The `review` workflow MUST declare no nonterminal state other than `REVIEWING`.

### REQ-001.17: Governor identification

1. The review skill MUST direct the reviewer to obtain the governor task id from its review task.

### REQ-001.18: Plan input

1. The review skill MUST direct the reviewer to read the governor's `plan.md` artifact.

### REQ-001.19: Change input

1. The review skill MUST direct the reviewer to inspect the governor's change through its recorded
pull-request URL or its branch and clone information.

### REQ-001.20: Clean-context boundary

1. The review skill MUST forbid retrieving or supplying the author's conversation as review input.

### REQ-001.21: Plan and scope assessment

1. The review skill MUST direct the reviewer to assess whether the change matches the plan without
unplanned scope.

### REQ-001.22: Simplicity assessment

1. The review skill MUST direct the reviewer to assess simplicity and net line count.

### REQ-001.23: Simplicity ladder

1. The review skill MUST order simplification preferences as deleting unnecessary code, reusing an
existing primitive, simplifying existing code, then adding the smallest new code necessary.

### REQ-001.24: Complete after approval

1. For an approval, the review skill MUST direct the reviewer to advance the review task to `COMPLETE`.

### REQ-001.25: Findings format

1. The review skill MUST direct the reviewer to organize actionable findings under `Must fix` and
`Suggestions` headings.

### REQ-001.26: Complete after findings

1. For findings, the review skill MUST direct the reviewer to advance the review task to `COMPLETE`.

### REQ-001.27: Built-in pairing defaults

1. Every built-in workflow MUST leave `review_harness` and `review_model` unset.
