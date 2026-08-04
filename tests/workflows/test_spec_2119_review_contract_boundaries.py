"""Independent boundary assertions for the built-in 2119 review contract."""

from __future__ import annotations

from pathlib import Path

from panopticon.core.models import Skill
from panopticon.core.workflow import Workflow
from panopticon.workflows.discovery import discover_workflows

WORKFLOW_NAMES = ("2119-human-spec", "2119-auto-spec", "2119-auto-sol")
EXPECTED_REVIEW_DEFAULTS = {
    "2119-human-spec": "Workflow reviewer defaults: `claude:claude-fable-5, codex:gpt-5.6-sol`.",
    "2119-auto-spec": "Workflow reviewer defaults: `claude:claude-fable-5, codex:gpt-5.6-sol`.",
    "2119-auto-sol": "Workflow reviewer defaults: `codex:gpt-5.6-sol, codex:gpt-5.6-sol`.",
}
EXPECTED_TRIAGE_END = """End the triage summary PR comment with a
"Suggested placeholder issues" section. For each finding you rejected or deferred that is
nonetheless a genuinely good idea, add a one-paragraph entry: what the idea is, why it was
deferred rather than done now, and what an implementer would need to know. Omit findings
rejected as simply wrong — this section captures deferred value, not a changelog of the review.
Frame the section explicitly as recommendations for the user to react to (endorse, reject, or
edit) at the PR approval gate; `MERGING` reads this section back before filing issues.

Also publish the final review outputs and triage summary as task artifacts."""


def _review_skill(workflow: Workflow) -> Skill:
    return next(skill for skill in workflow.skills() if "review" in skill.name)


def test_review_models_are_pinned_independently_for_each_workflow() -> None:
    # 2119: REQ-028.8.1
    registry = discover_workflows(_home_workflows=Path("/nonexistent"))

    assert set(EXPECTED_REVIEW_DEFAULTS) == set(WORKFLOW_NAMES)
    for name, expected_defaults in EXPECTED_REVIEW_DEFAULTS.items():
        workflow = registry[name]
        assert workflow._honesty_reviewer_cmd() == (
            "codex exec --dangerously-bypass-approvals-and-sandbox -m gpt-5.6-sol"
        )
        assert _review_skill(workflow).instructions.splitlines()[0] == expected_defaults


def test_review_instructions_pin_the_triage_summary_ending() -> None:
    # 2119: REQ-033.1.1
    # 2119: REQ-033.4.1
    registry = discover_workflows(_home_workflows=Path("/nonexistent"))

    for name in WORKFLOW_NAMES:
        instructions = _review_skill(registry[name]).instructions
        assert instructions.endswith(EXPECTED_TRIAGE_END)
