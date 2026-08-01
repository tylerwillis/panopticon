"""Reviewable artifacts and the built-in RFC 2119 workflow family."""

from __future__ import annotations

from pathlib import Path

import pytest

from panopticon.core.models import Repo, Skill
from panopticon.core.state import Complete, InitialState
from panopticon.core.workflow import InvalidWorkflow, Workflow
from panopticon.harnesses.codex import CodexHarness
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows.discovery import discover_workflows

WORKFLOW_NAMES = ("2119-human-spec", "2119-auto-spec", "2119-auto-sol")
EXPECTED_ARTIFACT_INSTRUCTIONS = (
    "An artifact is a durable task document that the user can review. Publish anything you want "
    "the user to review as an artifact, regardless of document type. Examples include a "
    "specification or spec summary, review outputs, a triage summary, and stage or gate reports, "
    "but these examples are not exhaustive. "
    "Use the `put_artifact` MCP tool, or send the artifact bytes with "
    "`PUT /tasks/{task_id}/artifacts/{name}` over REST. The operator opens published artifacts "
    "from the dashboard with the `a` hotkey. Artifacts complement the pull request and its "
    "dedicated external task URL; they do not replace either one. The substantial exception is "
    "GitHub URLs: record those in the task's external URL field so the dashboard `p` hotkey opens "
    "them, rather than publishing them as artifacts."
)
EXPECTED_SPEC_ARTIFACT_RESPONSIBILITY = (
    "Publish the specification as a task artifact so the user can review it with the dashboard "
    "`a` hotkey."
)
EXPECTED_SPECIFYING_RESPONSIBILITIES = (
    (
        "spec-written",
        "The feature's requirements exist under specs/ as numbered, individually addressable "
        "statements with exactly one normative keyword each (append-only IDs), and "
        "`npx rfc2119 lint` passes.",
    ),
    ("spec-artifact", EXPECTED_SPEC_ARTIFACT_RESPONSIBILITY),
    (
        "tests-annotated",
        "Every MUST-level requirement has at least one test annotated with its ID "
        "(`// 2119: REQ-...`); `npx rfc2119 check` reports no coverage gap. The tests may still "
        "fail — implementation comes in BUILDING.",
    ),
    (
        "tests-judged",
        "Fresh-context test-honesty reviews are recorded for every pending review and "
        "`npx rfc2119 check` exits 0.",
    ),
)
EXPECTED_URL_RESPONSIBILITY = (
    "Record the PR URL in the task's external URL field with the `set_url` MCP tool."
)
EXPECTED_DEFERRED_ISSUES_FILED_RESPONSIBILITY = (
    "Every suggested placeholder issue from the triage summary has been weighed against the "
    "PR's comments — filed with `gh issue create` if the user endorsed it or left it "
    "unaddressed, skipped if the user rejected it. Filing zero issues is a legal outcome."
)
EXPECTED_SOL_REVIEWING_RESPONSIBILITIES = (
    (
        "reviews-recorded-sol",
        "Both independent Sol 5.6 reviews ran against the final diff and are posted as PR comments.",
    ),
    (
        "findings-triaged",
        "Every review finding is explicitly accepted or rejected with a reason; accepted fixes "
        "are implemented with gates re-run; a fresh re-review round ran if any must-fix was "
        "accepted (2 rounds max); the triage summary is a PR comment.",
    ),
)
EXPECTED_DEFERRED_ISSUES_TRIAGE_INSTRUCTIONS = """End the triage summary PR comment with a
"Suggested placeholder issues" section. For each finding you rejected or deferred that is
nonetheless a genuinely good idea, add a one-paragraph entry: what the idea is, why it was
deferred rather than done now, and what an implementer would need to know. Omit findings
rejected as simply wrong — this section captures deferred value, not a changelog of the review.
Frame the section explicitly as recommendations for the user to react to (endorse, reject, or
edit) at the PR approval gate; `MERGING` reads this section back before filing issues."""
EXPECTED_FABLE_SOL_REVIEW_INSTRUCTIONS = f"""Run two independent fresh-context reviews of the final
diff: Fable 5 through the Claude CLI and Sol 5.6 with
`codex exec --dangerously-bypass-approvals-and-sandbox -m gpt-5.6-sol`. Each review covers
correctness, simplicity, scope, and spec/test honesty. Reviewer prompts must forbid edits. After
each reviewer run, you MUST verify `git status --porcelain` is unchanged. Post both final review
reports as labeled PR comments.

Triage every finding against the code. Accept or reject each finding with a reason, implement every
accepted fix, and re-run the TESTING gates. If a MUST-FIX was accepted, run one fresh review round;
never exceed two rounds. Post the final triage as a PR comment.

{EXPECTED_DEFERRED_ISSUES_TRIAGE_INSTRUCTIONS}

Also publish the final review outputs and triage summary as task artifacts."""
EXPECTED_SOL_ONLY_REVIEW_INSTRUCTIONS = f"""Run two independent fresh-context Sol 5.6 reviews of the
final diff with `codex exec --dangerously-bypass-approvals-and-sandbox -m gpt-5.6-sol`. Each review
covers correctness, simplicity, scope, and spec/test honesty. Reviewer prompts must forbid edits.
After each reviewer run, you MUST verify `git status --porcelain` is unchanged. Post both final
review reports as labeled PR comments.

Triage every finding against the code. Accept or reject each finding with a reason, implement every
accepted fix, and re-run the TESTING gates. If a MUST-FIX was accepted, run one fresh review round;
never exceed two rounds. Post the final triage as a PR comment.

{EXPECTED_DEFERRED_ISSUES_TRIAGE_INSTRUCTIONS}

Also publish the final review outputs and triage summary as task artifacts."""
EXPECTED_DEFERRED_ISSUES_MERGE_INSTRUCTIONS = """## Before merging: file deferred-work issues

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
EXPECTED_SOL_SPEC_INSTRUCTIONS = """The spec is the contract: requirements first, tests second,
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


def _workflow(name: str) -> Workflow:
    return discover_workflows(_home_workflows=Path("/nonexistent"))[name]


def _skill(workflow: Workflow, name: str) -> Skill:
    return next(skill for skill in workflow.skills() if skill.name == name)


def _responsibility_descriptions(workflow: Workflow, state: str) -> dict[str, str]:
    return {item.key: item.description for item in workflow.responsibilities(state)}


@pytest.mark.asyncio
async def test_every_task_exposes_the_core_artifact_skill(tmp_path: Path) -> None:
    # 2119: REQ-028.1.1
    class SkillLess(Workflow):
        name = "skill-less"

        class Only(InitialState):
            label = "ONLY"
            transitions = (Complete,)

        initial = Only

    class Skilled(Workflow):
        name = "skilled"

        class Only(InitialState):
            label = "ONLY"
            transitions = (Complete,)

        initial = Only

        def skills(self) -> tuple[Skill, ...]:
            return (Skill("workflow-skill", "Workflow skill.", "Do workflow work."),)

    workflows = discover_workflows(_home_workflows=Path("/nonexistent"))
    workflows.update({"skill-less": SkillLess(), "skilled": Skilled()})
    service = TaskService(SqlAlchemyStore(), workflows, FilesystemArtifactStore(tmp_path))
    await service.init()
    await service.create_repo(
        Repo(
            id="r1",
            name="acme/widgets",
            git_url="https://x/r1.git",
            enabled_workflows=list(workflows),
        )
    )
    governor = await service.create_task("r1", "spike")
    assert [skill.name for skill in await service.skills(governor.id)] == [
        "provision",
        "artifacts",
    ]
    for workflow_name, workflow in workflows.items():
        expected = ["provision", "artifacts", *(skill.name for skill in workflow.skills())]
        for _ in range(2):
            governor_id = governor.id if workflow_name == "review" else None
            harness = "codex" if workflow_name == "review" else None
            task = await service.create_task(
                "r1",
                workflow_name,
                governor_task_id=governor_id,
                harness=harness,
            )
            assert [skill.name for skill in await service.skills(task.id)] == expected


@pytest.mark.asyncio
async def test_artifact_skill_explains_both_write_mechanisms(tmp_path: Path) -> None:
    # 2119: REQ-028.1.1
    # 2119: REQ-028.2.1
    # 2119: REQ-028.3.1
    # 2119: REQ-028.10.1
    # 2119: REQ-028.11.1
    service = TaskService(
        SqlAlchemyStore(),
        {"spike": discover_workflows(_home_workflows=Path("/nonexistent"))["spike"]},
        FilesystemArtifactStore(tmp_path),
    )
    await service.init()
    await service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    task = await service.create_task("r1", "spike")
    instructions = next(
        skill.instructions for skill in await service.skills(task.id) if skill.name == "artifacts"
    )

    assert instructions == EXPECTED_ARTIFACT_INSTRUCTIONS


def test_reserved_artifact_surface_cannot_be_overwritten(tmp_path: Path) -> None:
    # 2119: REQ-028.1.1
    class SkillCollision(Workflow):
        name = "skill-collision"

        class Only(InitialState):
            label = "ONLY"
            transitions = (Complete,)

        initial = Only

        def skills(self) -> tuple[Skill, ...]:
            return (Skill("artifacts", "Override.", "Hide the core skill."),)

    class OperationCollision(Workflow):
        name = "operation-collision"

        class Only(InitialState):
            label = "ONLY"
            transitions = (Complete,)
            operations = {"artifacts": Complete}

        initial = Only

    for workflow in (SkillCollision(), OperationCollision()):
        with pytest.raises(InvalidWorkflow, match="duplicate agent surface name 'artifacts'"):
            TaskService(
                SqlAlchemyStore(),
                {workflow.name: workflow},
                FilesystemArtifactStore(tmp_path),
            )


@pytest.mark.asyncio
async def test_reserved_artifact_surface_cannot_be_added_by_rescan(tmp_path: Path) -> None:
    # 2119: REQ-028.1.1
    class SkillCollision(Workflow):
        name = "rescan-skill-collision"

        class Only(InitialState):
            label = "ONLY"
            transitions = (Complete,)

        initial = Only

        def skills(self) -> tuple[Skill, ...]:
            return (Skill("artifacts", "Override.", "Hide the core skill."),)

    service = TaskService(
        SqlAlchemyStore(),
        {},
        FilesystemArtifactStore(tmp_path),
        workflow_discovery=lambda: {"rescan-skill-collision": SkillCollision()},
    )

    with pytest.raises(InvalidWorkflow, match="duplicate agent surface name 'artifacts'"):
        await service.list_workflow_infos()


def test_specifying_has_one_artifact_responsibility() -> None:
    # 2119: REQ-028.4.1
    registry = discover_workflows(_home_workflows=Path("/nonexistent"))
    builtins = [workflow for name, workflow in registry.items() if name.startswith("2119-")]

    assert {workflow.name for workflow in builtins} == set(WORKFLOW_NAMES)
    for workflow in builtins:
        responsibilities = list(workflow.responsibilities("SPECIFYING"))
        assert tuple((item.key, item.description) for item in responsibilities) == (
            EXPECTED_SPECIFYING_RESPONSIBILITIES
        )


def test_2119_skills_publish_spec_and_review_material() -> None:
    # 2119: REQ-028.5.1
    # 2119: REQ-028.12.1
    # 2119: REQ-033.1.1
    # 2119: REQ-033.2.1
    # 2119: REQ-033.3.1
    # 2119: REQ-033.4.1
    registry = discover_workflows(_home_workflows=Path("/nonexistent"))
    builtins = [workflow for name, workflow in registry.items() if name.startswith("2119-")]

    assert {workflow.name for workflow in builtins} == set(WORKFLOW_NAMES)
    for workflow in builtins:
        spec_instructions = _skill(workflow, "spec-2119").instructions
        review_skills = [skill for skill in workflow.skills() if "review" in skill.name]
        expected_review = (
            EXPECTED_FABLE_SOL_REVIEW_INSTRUCTIONS
            if type(workflow).fable_reviews
            else EXPECTED_SOL_ONLY_REVIEW_INSTRUCTIONS
        )

        assert spec_instructions == EXPECTED_SOL_SPEC_INSTRUCTIONS
        assert review_skills
        for review_skill in review_skills:
            assert review_skill.instructions == expected_review


def test_building_retains_the_external_pr_url_responsibility() -> None:
    # 2119: REQ-028.6.1
    registry = discover_workflows(_home_workflows=Path("/nonexistent"))
    builtins = [workflow for name, workflow in registry.items() if name.startswith("2119-")]

    assert {workflow.name for workflow in builtins} == set(WORKFLOW_NAMES)
    for workflow in builtins:
        descriptions = _responsibility_descriptions(workflow, "BUILDING")
        assert descriptions["url-recorded"] == EXPECTED_URL_RESPONSIBILITY


def test_discovers_all_three_builtin_2119_workflows() -> None:
    # 2119: REQ-028.7.1
    registry = discover_workflows(_home_workflows=Path("/nonexistent"))

    assert {name: type(registry[name]).__name__ for name in WORKFLOW_NAMES} == {
        "2119-human-spec": "Spec2119Human",
        "2119-auto-spec": "Spec2119Auto",
        "2119-auto-sol": "Spec2119AutoSol",
    }


def test_auto_sol_uses_sol_for_both_review_layers() -> None:
    # 2119: REQ-028.8.1
    # 2119: REQ-028.12.1
    registry = discover_workflows(_home_workflows=Path("/nonexistent"))
    builtins = [workflow for name, workflow in registry.items() if name.startswith("2119-")]
    assert {workflow.name for workflow in builtins} == set(WORKFLOW_NAMES)
    for workflow in builtins:
        assert workflow._honesty_reviewer_cmd() == (
            "codex exec --dangerously-bypass-approvals-and-sandbox -m gpt-5.6-sol"
        )
        assert _skill(workflow, "spec-2119").instructions == EXPECTED_SOL_SPEC_INSTRUCTIONS

    auto_sol = _workflow("2119-auto-sol")
    auto_sol_spec = _skill(auto_sol, "spec-2119").instructions.lower()
    auto_sol_review = next(skill for skill in auto_sol.skills() if "review" in skill.name)

    assert type(auto_sol).fable_reviews is False
    assert auto_sol_spec == EXPECTED_SOL_SPEC_INSTRUCTIONS.lower()
    assert auto_sol_review.instructions == EXPECTED_SOL_ONLY_REVIEW_INSTRUCTIONS
    assert [skill.name for skill in auto_sol.skills() if "review" in skill.name] == [
        "dual-review-sol"
    ]
    assert (
        tuple((item.key, item.description) for item in auto_sol.responsibilities("REVIEWING"))
        == EXPECTED_SOL_REVIEWING_RESPONSIBILITIES
    )
    for name in ("2119-human-spec", "2119-auto-spec"):
        workflow = _workflow(name)
        assert type(workflow).fable_reviews is True
        assert (
            _skill(workflow, "dual-review").instructions == EXPECTED_FABLE_SOL_REVIEW_INSTRUCTIONS
        )
        assert [skill.name for skill in workflow.skills() if "review" in skill.name] == [
            "dual-review"
        ]
        assert tuple(item.key for item in workflow.responsibilities("REVIEWING")) == (
            "reviews-recorded",
            "findings-triaged",
        )


def test_codex_reviewers_use_container_isolation_and_verify_clean_tree() -> None:
    # 2119: REQ-028.12.1
    registry = discover_workflows(_home_workflows=Path("/nonexistent"))
    builtins = [workflow for name, workflow in registry.items() if name.startswith("2119-")]

    assert {workflow.name for workflow in builtins} == set(WORKFLOW_NAMES)
    for workflow in builtins:
        expected_review = (
            ("dual-review", EXPECTED_FABLE_SOL_REVIEW_INSTRUCTIONS)
            if type(workflow).fable_reviews
            else ("dual-review-sol", EXPECTED_SOL_ONLY_REVIEW_INSTRUCTIONS)
        )
        skills = workflow.skills()
        assert [skill.name for skill in skills] == [
            "open-pr",
            "babysit-ci",
            "babysit-merge",
            "spec-2119",
            expected_review[0],
        ]
        assert [(skill.name, skill.instructions) for skill in skills[-2:]] == [
            ("spec-2119", EXPECTED_SOL_SPEC_INSTRUCTIONS),
            expected_review,
        ]


def test_2119_open_pr_and_reviewer_cli_match_the_workflow_contract() -> None:
    for name in WORKFLOW_NAMES:
        workflow = _workflow(name)
        open_pr = _skill(workflow, "open-pr").instructions
        assert "published specification artifact" in open_pr
        assert "plan.md" not in open_pr
        assert workflow.image_layer() == CodexHarness().image_layer()


def test_merging_gains_deferred_issues_filed_before_pr_merged() -> None:
    # 2119: REQ-033.5.1
    # 2119: REQ-033.10.1
    registry = discover_workflows(_home_workflows=Path("/nonexistent"))
    builtins = [workflow for name, workflow in registry.items() if name.startswith("2119-")]
    assert {workflow.name for workflow in builtins} == set(WORKFLOW_NAMES)

    for workflow in builtins:
        responsibilities = list(workflow.responsibilities("MERGING"))
        assert [item.key for item in responsibilities] == ["deferred-issues-filed", "pr-merged"]
        assert responsibilities[0].description == EXPECTED_DEFERRED_ISSUES_FILED_RESPONSIBILITY


def test_babysit_merge_files_deferred_issues_before_the_merge_queue_steps() -> None:
    # 2119: REQ-033.6.1
    # 2119: REQ-033.7.1
    # 2119: REQ-033.8.1
    # 2119: REQ-033.9.1
    registry = discover_workflows(_home_workflows=Path("/nonexistent"))
    builtins = [workflow for name, workflow in registry.items() if name.startswith("2119-")]
    assert {workflow.name for workflow in builtins} == set(WORKFLOW_NAMES)

    base_instructions = _skill(_workflow("github-peer-reviewed"), "babysit-merge").instructions

    for workflow in builtins:
        instructions = _skill(workflow, "babysit-merge").instructions

        # The deferred-issue-filing instructions precede the untouched base merge-queue
        # instructions (proving nothing about the queue mechanics itself was disturbed) and are
        # exactly the required text — not merely a superset containing the right words.
        assert instructions.endswith(base_instructions)
        prefix = instructions[: -len(base_instructions)]
        assert prefix == EXPECTED_DEFERRED_ISSUES_MERGE_INSTRUCTIONS + "\n\n"


def test_duplicate_error_identifies_external_file_and_remediation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-028.9.1
    builtin_names = discover_workflows(_home_workflows=tmp_path / "absent").keys()
    home_workflows = tmp_path / "workflows"
    home_workflows.mkdir()
    monkeypatch.setattr(
        "panopticon.workflows.discovery.user_config_dir",
        lambda: tmp_path,
    )
    for index, workflow_name in enumerate(builtin_names):
        external = home_workflows / f"spec_2119_{index}.py"
        external.write_text(
            "from panopticon.core.state import Complete, InitialState\n"
            "from panopticon.core.workflow import Workflow\n"
            "class Duplicate(Workflow):\n"
            f"    name = {workflow_name!r}\n"
            "    class Only(InitialState):\n"
            "        label = 'ONLY'\n"
            "        transitions = (Complete,)\n"
            "    initial = Only\n"
        )

        with pytest.raises(ValueError) as exc_info:
            discover_workflows()

        assert str(exc_info.value) == (
            f"external workflow file {external}: duplicate workflow name "
            f"{workflow_name!r}; remove this external workflow file before restarting Panopticon"
        )
        with pytest.raises(ValueError, match="remove this external workflow file"):
            discover_workflows(_skip_duplicates=True)
        external.unlink()
