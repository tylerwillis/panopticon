"""Built-in RFC 2119 workflows."""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from panopticon.core.models import Actor, Responsibility, Skill
from panopticon.core.state import Complete, InitialState, State
from panopticon.harnesses.codex import CodexHarness
from panopticon.workflows.github_forge import GithubForgeWorkflow

SPEC_ARTIFACT = Responsibility(
    key="spec-artifact",
    description=(
        "Publish the specification as a task artifact so the user can review it with the "
        "dashboard `a` hotkey."
    ),
)
URL_RECORDED = Responsibility(
    key="url-recorded",
    description="Record the PR URL in the task's external URL field with the `set_url` MCP tool.",
)
REVIEWS_RECORDED = Responsibility(
    key="reviews-recorded",
    description=(
        "Both model reviews (Fable 5 and Sol 5.6) ran against the final diff and are posted as "
        "PR comments."
    ),
)
REVIEWS_RECORDED_SOL = Responsibility(
    key="reviews-recorded-sol",
    description=(
        "Both independent Sol 5.6 reviews ran against the final diff and are posted as PR comments."
    ),
)
FINDINGS_TRIAGED = Responsibility(
    key="findings-triaged",
    description=(
        "Every review finding is explicitly accepted or rejected with a reason; accepted fixes "
        "are implemented with gates re-run; a fresh re-review round ran if any must-fix was "
        "accepted (2 rounds max); the triage summary is a PR comment."
    ),
)
DEFERRED_ISSUES_FILED = Responsibility(
    key="deferred-issues-filed",
    description=(
        "Every suggested placeholder issue from the triage summary has been weighed against the "
        "PR's comments — filed with `gh issue create` if the user endorsed it or left it "
        "unaddressed, skipped if the user rejected it. Filing zero issues is a legal outcome."
    ),
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
    responsibilities = (REVIEWS_RECORDED, FINDINGS_TRIAGED)
    transitions = ("MERGING",)


class _Merging(State):
    label = "MERGING"
    description = "Shepherd the PR through the merge queue."
    advanced_by = Actor.AGENT
    responsibilities = (
        DEFERRED_ISSUES_FILED,
        Responsibility(key="pr-merged", description="The PR is merged."),
    )
    transitions = (Complete,)


_SOL_SPEC_INSTRUCTIONS = """The spec is the contract: requirements first, tests second,
code later.

1. If `.2119.yml` is missing, check for an open adoption PR before running `npx rfc2119 init`.
2. Write the next append-only `specs/REQ-NNN-<slug>.md` and run `npx rfc2119 lint`.
3. Annotate a genuine test for every MUST/SHALL requirement.
4. Run fresh-context test-honesty reviews with
   `codex exec --dangerously-bypass-approvals-and-sandbox -m gpt-5.6-sol`.
   The reviewer prompt must forbid edits. After each reviewer run, you MUST verify
   `git status --porcelain` is unchanged, then record every verdict.
5. Stop only after `npx rfc2119 check` exits 0.

Also publish the specification as a task artifact for the operator to review."""

_DEFERRED_ISSUES_TRIAGE_INSTRUCTIONS = """End the triage summary PR comment with a
"Suggested placeholder issues" section. For each finding you rejected or deferred that is
nonetheless a genuinely good idea, add a one-paragraph entry: what the idea is, why it was
deferred rather than done now, and what an implementer would need to know. Omit findings
rejected as simply wrong — this section captures deferred value, not a changelog of the review.
Frame the section explicitly as recommendations for the user to react to (endorse, reject, or
edit) at the PR approval gate; `MERGING` reads this section back before filing issues."""

_FABLE_SOL_REVIEW_INSTRUCTIONS = f"""Run two independent fresh-context reviews of the final
diff: Fable 5 through the Claude CLI and Sol 5.6 with
`codex exec --dangerously-bypass-approvals-and-sandbox -m gpt-5.6-sol`. Each review covers
correctness, simplicity, scope, and spec/test honesty. Reviewer prompts must forbid edits. After
each reviewer run, you MUST verify `git status --porcelain` is unchanged. Post both final review
reports as labeled PR comments.

Triage every finding against the code. Accept or reject each finding with a reason, implement every
accepted fix, and re-run the TESTING gates. If a MUST-FIX was accepted, run one fresh review round;
never exceed two rounds. Post the final triage as a PR comment.

{_DEFERRED_ISSUES_TRIAGE_INSTRUCTIONS}

Also publish the final review outputs and triage summary as task artifacts."""

_SOL_ONLY_REVIEW_INSTRUCTIONS = f"""Run two independent fresh-context Sol 5.6 reviews of the
final diff with `codex exec --dangerously-bypass-approvals-and-sandbox -m gpt-5.6-sol`. Each review
covers correctness, simplicity, scope, and spec/test honesty. Reviewer prompts must forbid edits.
After each reviewer run, you MUST verify `git status --porcelain` is unchanged. Post both final
review reports as labeled PR comments.

Triage every finding against the code. Accept or reject each finding with a reason, implement every
accepted fix, and re-run the TESTING gates. If a MUST-FIX was accepted, run one fresh review round;
never exceed two rounds. Post the final triage as a PR comment.

{_DEFERRED_ISSUES_TRIAGE_INSTRUCTIONS}

Also publish the final review outputs and triage summary as task artifacts."""

_DEFERRED_ISSUES_MERGE_INSTRUCTIONS = """## Before merging: file deferred-work issues

The `deferred-issues-filed` responsibility gates this stage ahead of `pr-merged`. Before running
the merge-queue steps below:

1. Re-read your triage summary PR comment's "Suggested placeholder issues" section (posted at the
   end of `REVIEWING`) together with any user comments left on the PR reacting to those
   suggestions.
2. For each suggested issue the user endorsed, or left without objection, file it with
   `gh issue create`, incorporating any user edits. Each issue MUST be self-contained: a title
   stating the idea, and a body carrying context — a link to the PR, a reference to the review
   comment it came from, why it was deferred rather than done now, and what implementing it would
   involve.
3. Skip any suggested issue the user explicitly rejected.
4. Filing zero issues — because there were no suggestions, or every suggestion was explicitly
   rejected — is a legal outcome. Resolve `deferred-issues-filed` either way once every
   suggestion has been considered."""


class _Spec2119Workflow(GithubForgeWorkflow):
    """Shared forge skills for the 2119 workflow family."""

    #: Whether stage-4 adversarial review uses Fable plus Sol; test honesty always uses Sol.
    fable_reviews: ClassVar[bool] = True

    def _honesty_reviewer_cmd(self) -> str:
        return "codex exec --dangerously-bypass-approvals-and-sandbox -m gpt-5.6-sol"

    def _spec_skill(self) -> Skill:
        return Skill(
            "spec-2119",
            "Stage 1: write the RFC 2119 spec + annotated tests, get the tests judged.",
            _SOL_SPEC_INSTRUCTIONS,
        )

    def _review_skill(self) -> Skill:
        if self.fable_reviews:
            return Skill(
                "dual-review",
                "Stage 4: adversarial Fable 5 + Sol 5.6 review of the diff, triage, fix.",
                _FABLE_SOL_REVIEW_INSTRUCTIONS,
            )
        return Skill(
            "dual-review-sol",
            "Stage 4: adversarial Sol 5.6-only review of the diff, triage, fix.",
            _SOL_ONLY_REVIEW_INSTRUCTIONS,
        )

    def skills(self) -> Sequence[Skill]:
        forge_skills = super().skills()
        open_pr = Skill(
            "open-pr",
            "Open a draft PR for this task's branch.",
            "1. Push the task's branch.\n"
            "2. Open a **draft** PR against the repo's base branch with `gh pr create --draft`. "
            "Title it for the change and reference the published specification artifact.\n"
            "3. Call the `set_url` MCP tool with the PR URL returned by `gh pr create`, so the "
            "dashboard's `p` hotkey opens it and the `url-recorded` responsibility can be "
            "resolved.",
        )
        babysit_merge = next(skill for skill in forge_skills if skill.name == "babysit-merge")
        deferred_issues_merge = Skill(
            babysit_merge.name,
            babysit_merge.description,
            _DEFERRED_ISSUES_MERGE_INSTRUCTIONS + "\n\n" + babysit_merge.instructions,
        )
        other_forge_skills = tuple(
            skill for skill in forge_skills if skill.name not in ("open-pr", "babysit-merge")
        )
        return (
            open_pr,
            *other_forge_skills,
            deferred_issues_merge,
            self._spec_skill(),
            self._review_skill(),
        )

    def image_layer(self) -> str:
        """Install the Codex reviewer CLI regardless of the task's primary harness."""
        return CodexHarness().image_layer()


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


class Spec2119AutoSol(_Spec2119Workflow):
    """Automatic 2119 lifecycle using Sol for both review layers."""

    name: ClassVar[str] = "2119-auto-sol"
    opt_in: ClassVar[bool] = True
    fable_reviews: ClassVar[bool] = False
    when_to_use: ClassVar[str] = (
        "Spec-driven 2119 lifecycle with automatic specification and Sol-only reviews."
    )

    class Specifying(_Specifying):
        advanced_by = Actor.AGENT

    class Building(_Building):
        pass

    class Testing(_Testing):
        pass

    class Reviewing(_Reviewing):
        responsibilities = (REVIEWS_RECORDED_SOL, FINDINGS_TRIAGED)

    class Merging(_Merging):
        pass

    initial = Specifying
