"""Built-in RFC 2119 workflows."""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from panopticon.core.models import Actor, Responsibility, Skill
from panopticon.core.state import Complete, InitialState, State
from panopticon.harnesses.base import ReviewerConfig
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
        "Both configured reviewer dispatches are machine-verified against the final diff and "
        "posted as evidence-bearing PR comments."
    ),
)
REVIEWS_RECORDED_SOL = Responsibility(
    key="reviews-recorded-sol",
    description=(
        "Both configured reviewer dispatches are machine-verified against the final diff and "
        "posted as evidence-bearing PR comments."
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

_VERIFIED_REVIEW_INSTRUCTIONS = f"""Run two independent fresh-context reviews of the final diff
with the container-owned `panopticon.container.reviewers` dispatch helpers. Each review covers
correctness, simplicity, scope, and spec/test honesty. Reviewer prompts must forbid edits. After
each reviewer run, verify `git status --porcelain` is unchanged from the snapshot taken immediately
before that reviewer ran.

Reviewer selection is two ordered atomic `<harness>:<model>` pairs. The workflow defaults are
shown below. A repo env file may independently replace them with
`PANOPTICON_2119_REVIEWER_1` and `PANOPTICON_2119_REVIEWER_2`; split only on the first `:` so the
model remains opaque. Resolve and validate both pairs inside the task container before any
reviewer LLM call. A missing harness, missing model, unsupported harness, or malformed pair is an
actionable configuration failure.

Use `dispatch_reviews` to invoke each configured model explicitly and verify the responding
identity before posting anything. Claude verification uses the sole responding model key in
`claude --print --output-format json`'s `modelUsage`. Codex verification correlates the sole
`thread.started` id from `codex exec --json` with the persisted rollout's sole
`turn_context.payload.model`; this is weaker than a documented stdout identity field. In both
cases, the observed model must exactly equal the requested model. On command failure, missing or
ambiguous evidence, or mismatch, fail loudly and do not post or publish the review body. Report a
usage-limit or availability failure as a failed dispatch, never as a verified review with no
findings.

Never ask the reviewer to state its model name or create a model-labeled heading. Derive the model
label from the verified observation. Every completed PR comment must include the reviewer harness,
requested model, verified responding model, verification source, reviewed commit, review round,
and review body.

Do not count an unverified report in triage. Do not resolve the reviews-recorded responsibility
(or reviews-recorded-sol) until both PR comments carry valid verification evidence for the final
reviewed commit. Fetch exactly the two final evidence comments in configured slot order and call
`complete_review_stage`; its evidence gate must succeed before its triage and responsibility
callbacks run. If one dispatch publishes before the other fails, exclude or delete that orphan
before retrying so the gate receives exactly the final pair. Apply these same dispatch,
verification, comment, and failure rules to re-review rounds.

Triage every finding against the code. Accept or reject each finding with a reason, implement every
accepted fix, and re-run the TESTING gates. If a MUST-FIX was accepted, run one fresh review round;
never exceed two rounds. Post the final triage as a PR comment.

{_DEFERRED_ISSUES_TRIAGE_INSTRUCTIONS}

Also publish the final review outputs and triage summary as task artifacts."""

_DEFERRED_ISSUES_MERGE_INSTRUCTIONS = """## Before merging: file deferred-work issues

The `deferred-issues-filed` responsibility gates this stage ahead of `pr-merged`. Before running
the merge-queue steps below:

1. If `deferred-issues-filed` is already resolved — this is a re-invocation, e.g. while a merge
   watcher waits on CI — skip straight to the merge-queue steps below; do not re-file.
2. Otherwise, re-read your triage summary PR comment's "Suggested placeholder issues" section
   (posted at the end of `REVIEWING`) together with any user comments left on the PR reacting to
   those suggestions.
3. For each suggested issue the user endorsed, or left without objection, file it with
   `gh issue create`, incorporating any user edits. Each issue MUST be self-contained: a title
   stating the idea, and a body carrying context — a link to the PR, a reference to the review
   comment it came from, why it was deferred rather than done now, and what implementing it would
   involve.
4. Skip any suggested issue the user explicitly rejected.
5. Filing zero issues — because there were no suggestions, or every suggestion was explicitly
   rejected — is a legal outcome. Resolve `deferred-issues-filed` either way once every
   suggestion has been considered."""


class _Spec2119Workflow(GithubForgeWorkflow):
    """Shared forge skills for the 2119 workflow family."""

    #: Whether stage-4 adversarial review uses Fable plus Sol; test honesty always uses Sol.
    fable_reviews: ClassVar[bool] = True
    reviewers: ClassVar[tuple[ReviewerConfig, ReviewerConfig]] = (
        ReviewerConfig("claude", "claude-fable-5"),
        ReviewerConfig("codex", "gpt-5.6-sol"),
    )

    def _honesty_reviewer_cmd(self) -> str:
        return "codex exec --dangerously-bypass-approvals-and-sandbox -m gpt-5.6-sol"

    def _spec_skill(self) -> Skill:
        return Skill(
            "spec-2119",
            "Stage 1: write the RFC 2119 spec + annotated tests, get the tests judged.",
            _SOL_SPEC_INSTRUCTIONS,
        )

    def _review_skill(self) -> Skill:
        defaults = ", ".join(f"{item.harness}:{item.model}" for item in self.reviewers)
        if self.fable_reviews:
            return Skill(
                "dual-review",
                "Stage 4: verified configurable dual review of the diff, triage, fix.",
                f"Workflow reviewer defaults: `{defaults}`.\n\n{_VERIFIED_REVIEW_INSTRUCTIONS}",
            )
        return Skill(
            "dual-review-sol",
            "Stage 4: verified configurable dual review with Sol defaults, triage, fix.",
            f"Workflow reviewer defaults: `{defaults}`.\n\n{_VERIFIED_REVIEW_INSTRUCTIONS}",
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
    reviewers: ClassVar[tuple[ReviewerConfig, ReviewerConfig]] = (
        ReviewerConfig("codex", "gpt-5.6-sol"),
        ReviewerConfig("codex", "gpt-5.6-sol"),
    )
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
