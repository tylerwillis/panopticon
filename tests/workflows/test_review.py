"""Golden contract for the internal, governed cross-model review worker."""

from __future__ import annotations

from pathlib import Path

from panopticon.core import Actor
from panopticon.workflows import Review
from panopticon.workflows.discovery import discover_workflows

WF = Review()


def _instructions() -> str:
    return next(iter(WF.skills())).instructions


def _verdict_section(name: str, next_name: str | None = None) -> str:
    instructions = _instructions()
    section = instructions.split(f"**{name}**", 1)[1]
    if next_name is not None:
        section = section.split(f"**{next_name}**", 1)[0]
    return section


# 2119: REQ-001.1
def test_review_is_a_hidden_builtin(tmp_path: Path) -> None:
    registry = discover_workflows(_home_workflows=tmp_path / "no-home-workflows")
    assert registry["review"].name == "review"
    assert WF.hidden is True


# 2119: REQ-001.2
# 2119: REQ-001.13
def test_review_starts_on_the_agent_turn() -> None:
    task = WF.start_task("review-1", "repo-1", at="t0")
    assert task.state == "REVIEWING"
    assert task.turn is Actor.AGENT
    assert task.workflow == "review"
    assert [entry.to_state for entry in task.history] == ["REVIEWING"]


# 2119: REQ-001.3
# 2119: REQ-001.14
def test_reviewing_is_agent_advanced_and_ungated() -> None:
    assert WF.advanced_by("REVIEWING") is Actor.AGENT
    assert list(WF.responsibilities("REVIEWING")) == []


# 2119: REQ-001.15
# 2119: REQ-001.16
def test_reviewing_is_the_only_nonterminal_state() -> None:
    assert list(WF.labels()) == ["REVIEWING", "COMPLETE", "DROPPED"]
    assert set(WF.transitions("REVIEWING")) == {"COMPLETE", "DROPPED"}
    assert WF.operations("REVIEWING") == {"advance": "COMPLETE", "drop": "DROPPED"}


def test_reviewing_can_complete_or_drop() -> None:
    completed = WF.start_task("review-1", "repo-1", at="t0")
    WF.apply_transition(completed, "COMPLETE", at="t1", trigger="advance")
    assert completed.state == "COMPLETE"

    dropped = WF.start_task("review-2", "repo-1", at="t0")
    WF.apply_transition(dropped, "DROPPED", at="t1", trigger="drop")
    assert dropped.state == "DROPPED"


# 2119: REQ-001.4
def test_review_exposes_one_review_skill() -> None:
    skills = tuple(WF.skills())
    assert len(skills) == 1
    assert skills[0].name == "review-change"
    assert skills[0].description
    assert skills[0].instructions


# 2119: REQ-001.17
# 2119: REQ-001.18
# 2119: REQ-001.19
# 2119: REQ-001.20
def test_review_skill_collects_only_governor_artifacts_and_change() -> None:
    instructions = _instructions()
    normalized = " ".join(instructions.split())
    inspect_change = normalized.split("3. **Inspect the change.**", 1)[1].split(
        "4. **Assess correctness", 1
    )[0]
    assert (
        "Call `get_task` with your own review task id and read its `governor_task_id`."
        in normalized
    )
    assert (
        "Call `list_artifacts` on the governor task id, then read its `plan.md` through the "
        "returned MCP resource URI."
    ) in normalized
    assert (
        "If the governor has a recorded `url`, run `gh pr view <url>` and `gh pr diff <url>`."
        in inspect_change
    )
    assert (
        "Otherwise use its recorded `branch` and `clone`: inspect the clone directly when it is "
        "accessible, or run `git fetch origin <branch>`"
    ) in inspect_change
    assert "run `git diff <base>...FETCH_HEAD`" in inspect_change
    assert (
        "The author's conversation must not be supplied as review input. Do not retrieve, request, "
        "or use it even if it is supplied anyway"
    ) in normalized


# 2119: REQ-001.5
# 2119: REQ-001.21
# 2119: REQ-001.22
def test_review_skill_covers_correctness_scope_and_simplicity() -> None:
    instructions = _instructions().lower()
    assert (
        "assess correctness and whether the change matches the plan without unplanned scope"
        in instructions
    )
    assert "assess simplicity and net line count" in instructions


# 2119: REQ-001.23
def test_review_skill_orders_the_simplicity_ladder() -> None:
    instructions = _instructions().lower()
    rungs = (
        "delete unnecessary code",
        "reuse an existing primitive",
        "simplify existing code",
        "add the smallest new code necessary",
    )
    positions = [instructions.index(rung) for rung in rungs]
    assert positions == sorted(positions)


# 2119: REQ-001.6
# 2119: REQ-001.24
def test_approval_writes_no_verdict_artifact_and_completes() -> None:
    approval = _verdict_section("Approve", "Findings")
    normalized = " ".join(approval.split())
    assert "Write no artifact: no `review.md` or other verdict artifact." in normalized
    assert "put_artifact" not in approval
    assert "Call the `advance` operation to move this review task to `COMPLETE`." in approval


# 2119: REQ-001.7
# 2119: REQ-001.25
# 2119: REQ-001.26
def test_findings_are_written_to_the_governor_and_complete() -> None:
    findings = _verdict_section("Findings")
    normalized = " ".join(findings.split())
    assert 'put_artifact(task_id=<governor_task_id>, name="review.md"' in findings
    assert "## Must fix" in findings
    assert "## Suggestions" in findings
    assert "Keep every finding concrete and actionable." in findings
    assert "Then call the `advance` operation to move this review task to `COMPLETE`." in normalized


# 2119: REQ-001.8
def test_review_skill_forbids_code_edits() -> None:
    assert "Never edit the governor's code" in _instructions()


# 2119: REQ-001.27
def test_builtins_do_not_declare_review_pairs(tmp_path: Path) -> None:
    registry = discover_workflows(_home_workflows=tmp_path / "no-home-workflows")
    assert registry
    assert all(workflow.review_harness is None for workflow in registry.values())
    assert all(workflow.review_model is None for workflow in registry.values())
