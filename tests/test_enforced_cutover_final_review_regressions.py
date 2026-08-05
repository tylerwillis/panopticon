from panopticon.core.cutover_runbook import (
    RUNBOOK_PATH,
    assert_fresh_container_id,
    parse_enforced_mode_cutover_runbook,
    validate_enforced_mode_cutover_runbook,
)


def _plan():
    return parse_enforced_mode_cutover_runbook(RUNBOOK_PATH.read_text())


def test_prerequisites_canary_and_gate_schedule_are_fail_closed() -> None:
    s00 = _plan().steps[0]
    assert "set -Eeuo pipefail" in s00.action
    assert s00.check.count("merge-base --is-ancestor") == 1
    assert s00.check.count("gh run list") == 1
    assert s00.check.count("inspect-credential-file") == 1
    assert 'task["claimed_by"] == sys.argv[3]' in s00.check
    assert 'task["container_status"] != "gated"' in s00.check


# 2119: enforced-mode-cutover-runbook.1.6
def test_parser_reads_mutated_supplied_actions_and_requires_check_shells() -> None:
    text = RUNBOOK_PATH.read_text()
    changed_action = text.replace("set -Eeuo pipefail", "set -Euo pipefail", 1)
    assert (
        "set -Euo pipefail" in parse_enforced_mode_cutover_runbook(changed_action).steps[0].action
    )
    missing_check = text.replace(
        '### Check\n\n```sh\ntest "$ISSUE_202_COMMIT"',
        '### Check\n\ntest "$ISSUE_202_COMMIT"',
        1,
    )
    assert validate_enforced_mode_cutover_runbook(missing_check) == [
        "enforced-mode-cutover-runbook.1.3"
    ]
    changed_gate = text.replace('$SERVICE_URL/healthz")" = 200', '$SERVICE_URL/healthz")" = 204', 1)
    assert "= 204" in parse_enforced_mode_cutover_runbook(changed_gate).gates[0].action


def test_validator_rejects_instruction_to_restore_legacy_capabilities() -> None:
    weakening = RUNBOOK_PATH.read_text().replace(
        "Do not restore legacy\ncapability acceptance",
        "Restore legacy\ncapability acceptance",
        1,
    )
    assert validate_enforced_mode_cutover_runbook(weakening) == [
        "enforced-mode-cutover-runbook.2.9"
    ]


def test_runner_pid_is_exec_bound_and_unknown_callers_fail_closed() -> None:
    plan = _plan()
    assert '"exec env PANOPTICON_CONFIG=' in plan.steps[4].action
    assert "uncontrolled pane" in plan.steps[2].action
    assert "task-container-in-drain-set" in plan.steps[2].action


# 2119: enforced-mode-cutover-runbook.4.18
def test_fresh_canary_comparison_rejects_truncated_pre_cutover_ids(tmp_path) -> None:
    plan = _plan()
    assert "--all --quiet --no-trunc" in plan.steps[1].action
    assert "S03-all-container-ids-before-enforcement.txt" in plan.steps[3].check
    assert "S03-all-container-ids-before-enforcement.txt" in plan.steps[7].check
    assert "assert-fresh-container" in plan.gates[8].check
    truncated_inventory = RUNBOOK_PATH.read_text().replace(
        "docker ps --all --quiet --no-trunc --filter label=panopticon.task",
        "docker ps --all --quiet --filter label=panopticon.task",
        1,
    )
    assert "enforced-mode-cutover-runbook.4.18" in validate_enforced_mode_cutover_runbook(
        truncated_inventory
    )
    inventory = tmp_path / "ids"
    old_id = "a" * 64
    fresh_id = "b" * 64
    inventory.write_text(f"{old_id}\n")
    try:
        assert_fresh_container_id(old_id, inventory)
    except ValueError as error:
        assert "predates enforcement" in str(error)
    else:
        raise AssertionError("pre-enforcement container ID was accepted")
    assert_fresh_container_id(fresh_id, inventory)
    missing_negative_comparison = RUNBOOK_PATH.read_text().replace(
        'assert-fresh-container "$CANARY_CONTAINER_ID" "$EVIDENCE_DIR/S03-all-container-ids-before-enforcement.txt"',
        'test -n "$CANARY_CONTAINER_ID"',
        1,
    )
    assert "enforced-mode-cutover-runbook.4.18" in validate_enforced_mode_cutover_runbook(
        missing_negative_comparison
    )


# 2119: enforced-mode-cutover-runbook.5.3, enforced-mode-cutover-runbook.5.8
def test_live_cutover_schedules_resume_observation_for_each_restored_harness() -> None:
    s08 = _plan().steps[8]
    assert "S08-resume-observations.txt" in s08.check
    assert "transcript-visible-and-continuation-confirmed" in s08.check
    resume = (
        RUNBOOK_PATH.read_text().split("## Resume evidence", 1)[1].split("## What remains", 1)[0]
    )
    header = next(line for line in resume.splitlines() if line.startswith("| Harness |"))
    assert [cell.strip() for cell in header.strip("|").split("|")] == [
        "Harness",
        "Configuration-volume persistence",
        "Launcher resume selection",
        "Real CLI transcript acceptance",
    ]
    for harness in ("Claude", "Codex"):
        row = next(line for line in resume.splitlines() if line.startswith(f"| {harness} |"))
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        assert len(cells) == 4
        assert all(cell.startswith("Unit: (`unit`) ") for cell in cells[1:3])
        assert cells[3].startswith("Production-only: (`live-cutover`) ")
        assert f".{harness.lower()}" in cells[1]
        assert ("--continue" if harness == "Claude" else "interactive session ID") in cells[2]
        assert f"resumed real {harness}" in cells[3]
