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
SPEC_SKILL_ARTIFACT_SENTENCE = (
    "Also publish the specification as a task artifact for the operator to review."
)
REVIEW_SKILL_ARTIFACT_SENTENCE = (
    "Also publish the final review outputs and triage summary as task artifacts."
)
EXPECTED_FABLE_SOL_REVIEW_INSTRUCTIONS = """Run two independent fresh-context reviews of the final
diff: Fable 5 through the Claude CLI and Sol 5.6 through the Codex CLI. Each review covers
correctness, simplicity, scope, and spec/test honesty. Post both final review reports as labeled
PR comments.

Triage every finding against the code. Accept or reject each finding with a reason, implement every
accepted fix, and re-run the TESTING gates. If a MUST-FIX was accepted, run one fresh review round;
never exceed two rounds. Post the final triage as a PR comment.

Also publish the final review outputs and triage summary as task artifacts."""
EXPECTED_SOL_ONLY_REVIEW_INSTRUCTIONS = """Run two independent fresh-context Sol 5.6 reviews of the
final diff through the Codex CLI. Each review covers correctness, simplicity, scope, and spec/test
honesty. Post both final review reports as labeled PR comments.

Triage every finding against the code. Accept or reject each finding with a reason, implement every
accepted fix, and re-run the TESTING gates. If a MUST-FIX was accepted, run one fresh review round;
never exceed two rounds. Post the final triage as a PR comment.

Also publish the final review outputs and triage summary as task artifacts."""
EXPECTED_FABLE_SPEC_INSTRUCTIONS = """The spec is the contract: requirements first, tests second,
code later.

1. If `.2119.yml` is missing, check for an open adoption PR before running `npx rfc2119 init`.
2. Write the next append-only `specs/REQ-NNN-<slug>.md` and run `npx rfc2119 lint`.
3. Annotate a genuine test for every MUST/SHALL requirement.
4. Run fresh-context test-honesty reviews with `claude --print --model claude-fable-5` and record
   every verdict.
5. Stop only after `npx rfc2119 check` exits 0.

Also publish the specification as a task artifact for the operator to review."""
EXPECTED_SOL_SPEC_INSTRUCTIONS = """The spec is the contract: requirements first, tests second,
code later.

1. If `.2119.yml` is missing, check for an open adoption PR before running `npx rfc2119 init`.
2. Write the next append-only `specs/REQ-NNN-<slug>.md` and run `npx rfc2119 lint`.
3. Annotate a genuine test for every MUST/SHALL requirement.
4. Run fresh-context test-honesty reviews with `codex exec --sandbox read-only -m gpt-5.6-sol
   -c model_reasoning_effort="high" -` and record every verdict.
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
    # 2119: REQ-024.1.1
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

    service = TaskService(
        SqlAlchemyStore(),
        {"skill-less": SkillLess(), "skilled": Skilled()},
        FilesystemArtifactStore(tmp_path),
    )
    await service.init()
    await service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    skill_less_task = await service.create_task("r1", "skill-less")
    skilled_task = await service.create_task("r1", "skilled")

    assert [skill.name for skill in await service.skills(skill_less_task.id)] == [
        "provision",
        "artifacts",
    ]
    assert [skill.name for skill in await service.skills(skilled_task.id)] == [
        "provision",
        "artifacts",
        "workflow-skill",
    ]


@pytest.mark.asyncio
async def test_artifact_skill_explains_both_write_mechanisms(tmp_path: Path) -> None:
    # 2119: REQ-024.2.1
    # 2119: REQ-024.3.1
    # 2119: REQ-024.10.1
    # 2119: REQ-024.11.1
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
    # 2119: REQ-024.1.1
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


def test_specifying_has_one_artifact_responsibility() -> None:
    # 2119: REQ-024.4.1
    registry = discover_workflows(_home_workflows=Path("/nonexistent"))
    builtins = [workflow for name, workflow in registry.items() if name.startswith("2119-")]

    assert {workflow.name for workflow in builtins} == set(WORKFLOW_NAMES)
    for workflow in builtins:
        responsibilities = list(workflow.responsibilities("SPECIFYING"))
        assert tuple((item.key, item.description) for item in responsibilities) == (
            EXPECTED_SPECIFYING_RESPONSIBILITIES
        )


@pytest.mark.parametrize("workflow_name", WORKFLOW_NAMES)
def test_2119_skills_publish_spec_and_review_material(workflow_name: str) -> None:
    # 2119: REQ-024.5.1
    workflow = _workflow(workflow_name)
    spec_instructions = _skill(workflow, "spec-2119").instructions.lower()
    review_skill = next(skill for skill in workflow.skills() if "review" in skill.name)
    review_instructions = review_skill.instructions.lower()

    assert spec_instructions.count(SPEC_SKILL_ARTIFACT_SENTENCE.lower()) == 1
    assert review_instructions.count(REVIEW_SKILL_ARTIFACT_SENTENCE.lower()) == 1


@pytest.mark.parametrize("workflow_name", WORKFLOW_NAMES)
def test_building_retains_the_external_pr_url_responsibility(workflow_name: str) -> None:
    # 2119: REQ-024.6.1
    descriptions = _responsibility_descriptions(_workflow(workflow_name), "BUILDING")

    assert descriptions["url-recorded"] == EXPECTED_URL_RESPONSIBILITY


def test_discovers_all_three_builtin_2119_workflows() -> None:
    # 2119: REQ-024.7.1
    registry = discover_workflows(_home_workflows=Path("/nonexistent"))

    assert set(WORKFLOW_NAMES) <= set(registry)


def test_auto_sol_uses_sol_for_both_review_layers() -> None:
    # 2119: REQ-024.8.1
    auto_sol = _workflow("2119-auto-sol")
    auto_sol_spec = _skill(auto_sol, "spec-2119").instructions.lower()
    auto_sol_review = next(skill for skill in auto_sol.skills() if "review" in skill.name)

    assert type(auto_sol).fable_reviews is False
    assert auto_sol._honesty_reviewer_cmd() == (
        'codex exec --sandbox read-only -m gpt-5.6-sol -c model_reasoning_effort="high" -'
    )
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
        assert workflow._honesty_reviewer_cmd() == "claude --print --model claude-fable-5"
        assert _skill(workflow, "spec-2119").instructions == EXPECTED_FABLE_SPEC_INSTRUCTIONS
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


def test_2119_open_pr_and_reviewer_cli_match_the_workflow_contract() -> None:
    for name in WORKFLOW_NAMES:
        workflow = _workflow(name)
        open_pr = _skill(workflow, "open-pr").instructions
        assert "published specification artifact" in open_pr
        assert "plan.md" not in open_pr
        assert workflow.image_layer() == CodexHarness().image_layer()


def test_duplicate_error_identifies_external_file_and_remediation(tmp_path: Path) -> None:
    # 2119: REQ-024.9.1
    builtin_names = discover_workflows(_home_workflows=tmp_path / "absent").keys()
    for index, workflow_name in enumerate(builtin_names):
        external = tmp_path / f"spec_2119_{index}.py"
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
            discover_workflows(_home_workflows=tmp_path)

        assert str(exc_info.value) == (
            f"external workflow file {external}: duplicate workflow name "
            f"{workflow_name!r}; remove this external workflow file before restarting Panopticon"
        )
        with pytest.raises(ValueError, match="remove this external workflow file"):
            discover_workflows(_home_workflows=tmp_path, _skip_duplicates=True)
        external.unlink()
