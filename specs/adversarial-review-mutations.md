# Targeted mutations in adversarial review

## Overview

Panopticon's RFC 2119 workflows run adversarial PR review after implementation and testing. A
reviewer's reading can judge whether evidence looks persuasive, but it cannot establish the
factual counterfactual that removing a claimed property makes an affected test fail. Each
configured review dispatch therefore adds a small falsification experiment: the fresh-context
reviewer chooses a mutation that breaks a property on which a review claim depends, applies it in
a throwaway copy, and runs the affected tests.

This is deliberately not general-purpose mutation testing. The experiment is reviewer-chosen,
targeted at a named property, and reported alongside the ordinary review. A surviving mutant is a
defect in the evidence even when the implementation is correct. A killed mutant raises the floor
but does not certify the evidence: an unrelated assertion can kill the mutant without testing the
intended reason.

The Sol-only workflow still makes two independent fresh-context dispatches. They share a model,
so this provides independence from the author and between review contexts, but not cross-model
diversity. Each dispatched reviewer chooses and executes its own falsification attempt; the author
does not supply the mutation.

## Requirements

### 1: Workflow review contract

1. The `reviews-recorded` responsibility description MUST state that both configured reviewer dispatches are machine-verified against the final diff, posted as evidence-bearing PR comments, and accompanied by each reviewer choosing every mutation it attempts and attempting at least one targeted mutation.
2. The `reviews-recorded-sol` responsibility description MUST state that two independently dispatched fresh-context Sol reviewer attempts are machine-verified against the final diff, posted as evidence-bearing PR comments, and accompanied by each reviewer choosing every mutation it attempts and attempting at least one targeted mutation without claiming cross-model diversity.
3. The adversarial-review instructions' `Targeted mutation evidence` section MUST require the dispatched reviewer rather than the author to choose each mutation.
4. The adversarial-review instructions' `Targeted mutation evidence` section MUST require each mutation to break a specific property on which a review claim depends.
5. The adversarial-review instructions' `Targeted mutation evidence` section MUST permit mutation writes only in a throwaway copy outside the working tree.
6. The adversarial-review instructions' `Targeted mutation evidence` section MUST require the reviewer to run the affected tests and report the property broken plus the resulting failures, or state plainly that the mutation survived.
7. The adversarial-review instructions' `Targeted mutation evidence` section MUST state that a surviving mutation is a defect in the evidence even when the reviewed code is correct.
8. The adversarial-review instructions' `Targeted mutation evidence` section MUST require each reviewer attempt to end by verifying that the working tree is unchanged from its pre-review snapshot.
9. The adversarial-review instructions' `Targeted mutation evidence` section MUST keep the procedure targeted to at least one reviewer-chosen mutation per dispatch without introducing exhaustive mutation testing or a mutation-testing framework.
10. The `tests-judged` responsibility description MUST remain unchanged by this feature.

### 2: Limits and reviewer independence

1. Workflow documentation's `Targeted mutation review` section MUST state that killing a mutation shows only that an affected test can fail under the change and does not prove that the test failed for the intended reason.
2. Workflow documentation's `Targeted mutation review` section MUST explain that the Sol-only workflow uses two independent fresh-context dispatches of the same model, preserving independence from the author while providing no cross-model diversity.

## Non-goals

- Per-requirement test-honesty verdicts remain the upstream `rfc2119` tool's concern.
- This feature does not add mutation generation, AST mutation, mutation scoring, or exhaustive
  mutation runs.
- Reviewers receive no broader permission to edit the task working tree.
