"""RFC 2119 workflows with human-reviewable specification and review artifacts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from panopticon.core.artifacts import ArtifactStore, mcp_uri
from panopticon.core.models import Actor, Responsibility, Skill, Task
from panopticon.core.state import Complete, InitialState, State
from panopticon.workflows.github_forge import GithubForgeWorkflow

SPEC_ARTIFACT = Responsibility(
    key="spec-artifact",
    description=(
        "Upload a `spec.md` task artifact that mirrors the contents of the repository "
        "specification file and identifies that repository specification file."
    ),
)
REVIEW_ARTIFACT = Responsibility(
    key="review-artifact",
    description=(
        "Upload a `review.md` task artifact containing the final Fable 5 review report, the "
        "final Sol 5.6 review report, and for every finding its accepted or rejected disposition "
        "and reason."
    ),
)
URL_RECORDED = Responsibility(
    key="url-recorded",
    description=("Record the PR URL in the task's external URL field with the `set_url` MCP tool."),
)


class _Specifying(InitialState):
    label = "SPECIFYING"
    description = (
        "Write the feature as an RFC 2119 spec plus annotated tests, then have the tests judged "
        "by fresh-context reviewers."
    )
    responsibilities = (
        Responsibility(
            key="spec-written",
            description=(
                "The feature's requirements exist under specs/ as numbered, individually "
                "addressable statements with exactly one normative keyword each (append-only "
                "IDs), and `npx rfc2119 lint` passes."
            ),
        ),
        SPEC_ARTIFACT,
        Responsibility(
            key="tests-annotated",
            description=(
                "Every MUST-level requirement has at least one test annotated with its ID "
                "(`// 2119: REQ-...`); `npx rfc2119 check` reports no coverage gap. The tests may "
                "still fail — implementation comes in BUILDING."
            ),
        ),
        Responsibility(
            key="tests-judged",
            description=(
                "Fresh-context test-honesty reviews are recorded for every pending review and "
                "`npx rfc2119 check` exits 0."
            ),
        ),
    )
    transitions = ("BUILDING",)


class _Building(State):
    label = "BUILDING"
    description = "Implement the spec, nothing more."
    advanced_by = Actor.AGENT
    responsibilities = (
        Responsibility(
            key="spec-implemented",
            description="Every requirement in the spec is implemented in code.",
        ),
        Responsibility(
            key="committed",
            description=(
                "Work is committed in small reviewable commits; 2119 scaffolding, if newly "
                "adopted, is its own clearly-labeled commit."
            ),
        ),
        Responsibility(
            key="pr-opened",
            description="A draft PR is open for the task branch (the `open-pr` skill).",
        ),
        URL_RECORDED,
    )
    transitions = ("TESTING",)


class _Testing(State):
    label = "TESTING"
    description = (
        "Prove it: full test suite, `npx rfc2119 check`, and the repo's own gate locally; then "
        "PR CI green."
    )
    advanced_by = Actor.AGENT
    responsibilities = (
        Responsibility(
            key="suite-green",
            description="The full test suite passes locally (2119's check never runs it for you).",
        ),
        Responsibility(
            key="check-green",
            description="`npx rfc2119 check` exits 0 against the implemented change.",
        ),
        Responsibility(
            key="repo-gate-green",
            description=(
                "The repo's own gate (e.g. `make check`), run under pipefail, is green — or the "
                "repo has no such gate."
            ),
        ),
        Responsibility(key="ci-green", description="The PR's CI is green (`babysit-ci`)."),
    )
    transitions = ("REVIEWING",)


class _Reviewing(State):
    label = "REVIEWING"
    description = (
        "Run adversarial dual-model review, triage its findings, and fix accepted defects."
    )
    responsibilities = (
        Responsibility(
            key="reviews-recorded",
            description=(
                "Both model reviews (Fable 5 and Sol 5.6) ran against the final diff and are "
                "posted as PR comments."
            ),
        ),
        Responsibility(
            key="findings-triaged",
            description=(
                "Every review finding is explicitly accepted or rejected with a reason; accepted "
                "fixes are implemented with gates re-run; a fresh re-review round ran if any "
                "must-fix was accepted (2 rounds max); the triage summary is a PR comment."
            ),
        ),
        REVIEW_ARTIFACT,
    )
    transitions = ("MERGING",)


class _Merging(State):
    label = "MERGING"
    description = "Shepherd the PR through the merge queue."
    advanced_by = Actor.AGENT
    responsibilities = (Responsibility(key="pr-merged", description="The PR is merged."),)
    transitions = (Complete,)


_SPEC_2119_INSTRUCTIONS = """The spec is the contract: requirements first, tests second, code later.

1. If `.2119.yml` is missing, check for an open adoption PR before running `npx rfc2119 init`.
2. Write the next append-only `specs/REQ-NNN-<slug>.md` and run `npx rfc2119 lint`.
3. Upload `spec.md` with `put_artifact`. It must mirror the repository specification file and
   identify that source file so the user can review it from the dashboard.
4. Annotate a genuine test for every MUST/SHALL requirement.
5. Run fresh-context test-honesty reviews and record every verdict.
6. Stop only after `npx rfc2119 check` exits 0. In `2119-human-spec`, hand the spec back to the
   user for approval; `2119-auto-spec` advances automatically.

Changing an approved human-gated spec voids its approval: stop, mark the task blocked, show the
spec diff and rationale, and wait for re-approval before implementing the amendment."""

_DUAL_REVIEW_INSTRUCTIONS = """Run two independent fresh-context reviews of the final diff: Fable
5 through the Claude CLI and Sol 5.6 through the Codex CLI. Each review covers correctness,
simplicity, scope, and spec/test honesty. Post both final review reports as labeled PR comments.

Triage every finding against the code. Accept or reject each finding with a reason, implement every
accepted fix, and re-run the TESTING gates. If a MUST-FIX was accepted, run one fresh review round;
never exceed two rounds. Post the final triage as a PR comment.

Finally upload a visible `review.md` task artifact containing the final Fable 5 review report, the
final Sol 5.6 review report, and every finding's accepted or rejected disposition and reason. Then
stop so the user can review the PR and the artifact."""


class _Spec2119Workflow(GithubForgeWorkflow):
    """Shared forge skills and artifact briefing for the two 2119 lifecycles."""

    SPEC_ARTIFACT_NAME: ClassVar[str] = "spec.md"

    @classmethod
    def spec_uri(cls, task_id: str) -> str:
        return mcp_uri(task_id, cls.SPEC_ARTIFACT_NAME)

    async def _briefing_extras(self, task: Task, *, artifacts: ArtifactStore) -> Sequence[str]:
        if self.SPEC_ARTIFACT_NAME not in await artifacts.list(task.id):
            return ()
        return (
            f"This task's specification is the `{self.SPEC_ARTIFACT_NAME}` artifact — read it at "
            f"`{self.spec_uri(task.id)}`.",
        )

    def skills(self) -> Sequence[Skill]:
        forge_skills = tuple(skill for skill in super().skills() if skill.name != "open-pr")
        return (
            Skill(
                "open-pr",
                "Open a draft PR for this task's branch.",
                "Push the task branch and open a draft PR against the base branch with `gh pr "
                "create --draft`. Identify the `spec.md` task artifact as the change contract in "
                "the PR description. Record the returned PR URL in the task's external URL field "
                "with the `set_url` MCP tool so the dashboard's `p` hotkey opens it.",
            ),
            *forge_skills,
            Skill(
                "spec-2119",
                "Stage 1: write the RFC 2119 spec + annotated tests, get the tests judged.",
                _SPEC_2119_INSTRUCTIONS,
            ),
            Skill(
                "dual-review",
                "Stage 4: adversarial Fable 5 + Sol 5.6 review of the diff, triage, fix.",
                _DUAL_REVIEW_INSTRUCTIONS,
            ),
        )


class Spec2119Human(_Spec2119Workflow):
    """2119 lifecycle with a user approval gate after specification."""

    name: ClassVar[str] = "2119-human-spec"
    opt_in: ClassVar[bool] = True
    when_to_use: ClassVar[str] = (
        "Spec-driven 2119 lifecycle with a human spec gate: you approve the spec, then review "
        "the final PR before merge."
    )

    class Specifying(_Specifying):
        pass

    class Building(_Building):
        pass

    class Testing(_Testing):
        pass

    class Reviewing(_Reviewing):
        pass

    class Merging(_Merging):
        pass

    initial = Specifying


class Spec2119Auto(_Spec2119Workflow):
    """2119 lifecycle whose specification phase advances without a user gate."""

    name: ClassVar[str] = "2119-auto-spec"
    opt_in: ClassVar[bool] = True
    when_to_use: ClassVar[str] = (
        "Spec-driven 2119 lifecycle without the spec gate; you still review the final PR before "
        "merge."
    )

    class Specifying(_Specifying):
        advanced_by = Actor.AGENT

    class Building(_Building):
        pass

    class Testing(_Testing):
        pass

    class Reviewing(_Reviewing):
        pass

    class Merging(_Merging):
        pass

    initial = Specifying
