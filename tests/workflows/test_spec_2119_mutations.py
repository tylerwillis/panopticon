"""Targeted-mutation contract for the RFC 2119 adversarial review stage."""

from __future__ import annotations

from pathlib import Path

from panopticon.core.models import Skill
from panopticon.core.workflow import Workflow
from panopticon.workflows.discovery import discover_workflows

ROOT = Path(__file__).parents[2]
WORKFLOW_NAMES = ("2119-human-spec", "2119-auto-spec", "2119-auto-sol")
DUAL_MUTATION_RESPONSIBILITY = (
    "Both configured reviewer dispatches are machine-verified against the final diff and posted "
    "as evidence-bearing PR comments; each reviewer independently chooses every mutation it "
    "attempts, attempts at least one targeted mutation of a specific claimed property, and "
    "reports whether it was killed or survived, with a survivor treated as an evidence defect."
)
SOL_MUTATION_RESPONSIBILITY = (
    "Two independently dispatched fresh-context Sol reviewer attempts are machine-verified "
    "against the final diff and posted as evidence-bearing PR comments. The shared model provides "
    "no cross-model diversity; each reviewer independently chooses every mutation it attempts, "
    "attempts at least one targeted mutation of a specific claimed property, and reports whether "
    "it was killed or survived, with a survivor treated as an evidence defect."
)
TESTS_JUDGED_RESPONSIBILITY = (
    "Fresh-context test-honesty reviews are recorded for every pending review and "
    "`npx rfc2119 check` exits 0."
)
TARGETED_MUTATION_INSTRUCTIONS = """## Targeted mutation evidence

Each reviewer independently chooses every mutation it attempts and must attempt at least one
targeted mutation; the author must not choose or supply any mutation. Break a specific property on
which a review claim depends. Apply mutation writes only in a throwaway copy outside the working
tree, never in the task checkout. Run the affected tests and report the property broken and which
tests failed, or state plainly that the mutation survived. A surviving mutation is a defect in the
evidence even when the reviewed code is correct.

A kill shows only that a test can fail under the mutation; an unrelated assertion may have failed,
so a kill does not certify the test's reason or the claim. As the final action of every reviewer
attempt, verify the working tree is unchanged from the snapshot taken immediately before the
reviewer ran. Keep this targeted: do not introduce a mutation-testing framework or attempt
exhaustive mutations."""
TARGETED_MUTATION_DOCUMENTATION = """## Targeted mutation review

Killing a targeted mutation shows that an affected test can fail under that change; it does not
prove that the test failed for the intended reason, because an unrelated assertion can also kill
the mutation.

The Sol-only workflow makes two independent fresh-context dispatches of the same model. That
preserves independence from the author and between review contexts, but it does not provide
cross-model diversity; each dispatched reviewer chooses its own mutation."""


# 2119-spec: adversarial-review-mutations


def _workflow(name: str) -> Workflow:
    return discover_workflows(_home_workflows=Path("/nonexistent"))[name]


def _review_skill(workflow: Workflow) -> Skill:
    return next(skill for skill in workflow.skills() if skill.name.startswith("dual-review"))


# 2119: 1.1, 1.2, 1.7
def test_review_responsibilities_require_independent_targeted_mutations() -> None:
    for name in ("2119-human-spec", "2119-auto-spec"):
        reviewing = tuple(_workflow(name).responsibilities("REVIEWING"))
        assert reviewing[0].key == "reviews-recorded"
        assert reviewing[0].description == DUAL_MUTATION_RESPONSIBILITY

    reviewing = tuple(_workflow("2119-auto-sol").responsibilities("REVIEWING"))
    assert reviewing[0].key == "reviews-recorded-sol"
    assert reviewing[0].description == SOL_MUTATION_RESPONSIBILITY


# 2119: 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9
def test_review_skills_define_the_targeted_mutation_experiment() -> None:
    for name in WORKFLOW_NAMES:
        instructions = _review_skill(_workflow(name)).instructions
        assert instructions.count("## Targeted mutation evidence") == 1
        _, marker, tail = instructions.partition("## Targeted mutation evidence")
        body, separator, _ = tail.partition("\n\nTriage every finding against the code.")
        assert marker + body == TARGETED_MUTATION_INSTRUCTIONS
        assert separator


# 2119: 1.10
def test_mutation_review_does_not_change_test_honesty_responsibility() -> None:
    for name in WORKFLOW_NAMES:
        specifying = _workflow(name).responsibilities("SPECIFYING")
        tests_judged = next(item for item in specifying if item.key == "tests-judged")
        assert tests_judged.description == TESTS_JUDGED_RESPONSIBILITY


# 2119: 2.1, 2.2
def test_workflow_documentation_states_mutation_limits_and_sol_independence() -> None:
    documentation = (ROOT / "docs" / "harness-and-model-selection.md").read_text()
    assert documentation.count("## Targeted mutation review") == 1
    assert documentation.endswith(TARGETED_MUTATION_DOCUMENTATION + "\n")
