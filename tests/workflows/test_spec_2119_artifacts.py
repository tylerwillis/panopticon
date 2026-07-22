"""Review artifacts exposed by the two RFC 2119 workflows."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from panopticon.core.artifacts import mcp_uri
from panopticon.core.workflow import Workflow
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.workflows.discovery import discover_workflows

WORKFLOW_NAMES = ("2119-human-spec", "2119-auto-spec")


def _workflow(name: str) -> Workflow:
    workflow = discover_workflows(_home_workflows=Path("/nonexistent"))[name]
    assert workflow.name == name
    return workflow


def _responsibility_description(workflow: Workflow, state: str, key: str) -> str:
    return {item.key: item.description for item in workflow.responsibilities(state)}[key]


@pytest.mark.parametrize("workflow_name", WORKFLOW_NAMES)
def test_specifying_requires_a_visible_spec_artifact(workflow_name: str) -> None:
    # 2119: REQ-009.1.1
    workflow = _workflow(workflow_name)
    description = _responsibility_description(workflow, "SPECIFYING", "spec-artifact")
    assert "spec.md" in description
    assert "artifact" in description.lower()
    assert "upload" in description.lower()


@pytest.mark.parametrize("workflow_name", WORKFLOW_NAMES)
def test_briefing_surfaces_the_spec_artifact_uri_after_upload(
    workflow_name: str, tmp_path: Path
) -> None:
    # 2119: REQ-009.2.1
    workflow = _workflow(workflow_name)
    artifacts = FilesystemArtifactStore(tmp_path)
    task = workflow.start_task("t1", "r1", at="t0")
    uri = mcp_uri(task.id, "spec.md")

    assert uri not in asyncio.run(workflow.briefing(task, artifacts=artifacts))
    asyncio.run(artifacts.put(task.id, "spec.md", b"# Contract\n"))
    assert uri in asyncio.run(workflow.briefing(task, artifacts=artifacts))


@pytest.mark.parametrize("workflow_name", WORKFLOW_NAMES)
def test_reviewing_requires_a_visible_review_artifact(workflow_name: str) -> None:
    # 2119: REQ-009.3.1
    workflow = _workflow(workflow_name)
    description = _responsibility_description(workflow, "REVIEWING", "review-artifact")
    assert "review.md" in description
    assert "artifact" in description.lower()
    assert "upload" in description.lower()


@pytest.mark.parametrize("workflow_name", WORKFLOW_NAMES)
def test_building_retains_the_external_pr_url_responsibility(workflow_name: str) -> None:
    # 2119: REQ-009.4.1
    workflow = _workflow(workflow_name)
    description = _responsibility_description(workflow, "BUILDING", "url-recorded")
    assert "PR URL" in description
    assert "external URL" in description


@pytest.mark.parametrize("workflow_name", WORKFLOW_NAMES)
def test_open_pr_skill_references_the_spec_contract(workflow_name: str) -> None:
    # 2119: REQ-009.5.1
    workflow = _workflow(workflow_name)
    instructions = next(
        skill.instructions for skill in workflow.skills() if skill.name == "open-pr"
    )
    assert "spec.md" in instructions
    assert "artifact" in instructions.lower()
    assert "change contract" in instructions.lower()


@pytest.mark.parametrize("workflow_name", WORKFLOW_NAMES)
def test_spec_artifact_identifies_its_repository_source(workflow_name: str) -> None:
    # 2119: REQ-009.6.1
    workflow = _workflow(workflow_name)
    description = _responsibility_description(workflow, "SPECIFYING", "spec-artifact")
    assert "repository specification file" in description
    assert "mirror" in description.lower()


@pytest.mark.parametrize("workflow_name", WORKFLOW_NAMES)
def test_review_artifact_includes_the_fable_report(workflow_name: str) -> None:
    # 2119: REQ-009.7.1
    workflow = _workflow(workflow_name)
    description = _responsibility_description(workflow, "REVIEWING", "review-artifact")
    assert "final Fable 5 review report" in description


@pytest.mark.parametrize("workflow_name", WORKFLOW_NAMES)
def test_review_artifact_includes_every_finding_disposition(workflow_name: str) -> None:
    # 2119: REQ-009.8.1
    workflow = _workflow(workflow_name)
    description = _responsibility_description(workflow, "REVIEWING", "review-artifact")
    assert "every finding" in description
    assert "accepted or rejected" in description


@pytest.mark.parametrize("workflow_name", WORKFLOW_NAMES)
def test_review_artifact_includes_the_sol_report(workflow_name: str) -> None:
    # 2119: REQ-009.9.1
    workflow = _workflow(workflow_name)
    description = _responsibility_description(workflow, "REVIEWING", "review-artifact")
    assert "final Sol 5.6 review report" in description


@pytest.mark.parametrize("workflow_name", WORKFLOW_NAMES)
def test_review_artifact_includes_a_reason_for_every_disposition(workflow_name: str) -> None:
    # 2119: REQ-009.10.1
    workflow = _workflow(workflow_name)
    description = _responsibility_description(workflow, "REVIEWING", "review-artifact")
    assert "reason" in description
    assert "every finding" in description
