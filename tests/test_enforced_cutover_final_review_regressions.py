import re
from types import SimpleNamespace

from panopticon.core.cutover_runbook import (
    RUNBOOK_PATH,
    assert_container_started_after,
    assert_fresh_container_id,
    assert_process_replaced,
    assert_runner_process,
    assert_runner_set,
    parse_enforced_mode_cutover_runbook,
    process_start,
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
    baseline = parse_enforced_mode_cutover_runbook(text)
    for original in (*baseline.steps, *baseline.gates):
        for field in ("action", "check"):
            shell = getattr(original, field)
            marker = f": supplied-{original.item_id}-{field}"
            heading = f"{'##' if original.item_id.startswith('S') else '###'} {original.item_id} —"
            offset = text.index(heading)
            mutated = text[:offset] + text[offset:].replace(shell, f"{shell}\n{marker}", 1)
            reparsed = parse_enforced_mode_cutover_runbook(mutated)
            items = reparsed.steps if original.item_id.startswith("S") else reparsed.gates
            actual = next(item for item in items if item.item_id == original.item_id)
            assert marker in getattr(actual, field)
            assert validate_enforced_mode_cutover_runbook(mutated) == []


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
    assert "assert-container-started-after" in plan.gates[8].check
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
    assert_container_started_after("2026-08-05T00:00:01Z", "2026-08-05T00:00:00Z")
    try:
        assert_container_started_after("2026-08-05T00:00:00Z", "2026-08-05T00:00:00Z")
    except ValueError as error:
        assert "after enforcement" in str(error)
    else:
        raise AssertionError("container not started after enforcement was accepted")
    try:
        assert_container_started_after("2026-08-04T23:59:59Z", "2026-08-05T00:00:00Z")
    except ValueError as error:
        assert "after enforcement" in str(error)
    else:
        raise AssertionError("pre-enforcement container timestamp was accepted")
    missing_negative_comparison = RUNBOOK_PATH.read_text().replace(
        'assert-fresh-container "$CANARY_CONTAINER_ID" "$EVIDENCE_DIR/S03-all-container-ids-before-enforcement.txt"',
        'test -n "$CANARY_CONTAINER_ID"',
        1,
    )
    assert "enforced-mode-cutover-runbook.4.18" in validate_enforced_mode_cutover_runbook(
        missing_negative_comparison
    )
    swapped_timestamps = RUNBOOK_PATH.read_text().replace(
        'assert-container-started-after "$CANARY_CONTAINER_STARTED" "$ENFORCEMENT_STARTED_AT"',
        'assert-container-started-after "$ENFORCEMENT_STARTED_AT" "$CANARY_CONTAINER_STARTED"',
        1,
    )
    assert "enforced-mode-cutover-runbook.4.18" in validate_enforced_mode_cutover_runbook(
        swapped_timestamps
    )
    text = RUNBOOK_PATH.read_text()
    g09_offset = text.index("### G09 —")
    different_inspected_container = text[:g09_offset] + text[g09_offset:].replace(
        'docker exec "$CANARY_CONTAINER"', 'docker exec "$OTHER_CONTAINER"', 1
    )
    assert "enforced-mode-cutover-runbook.4.18" in validate_enforced_mode_cutover_runbook(
        different_inspected_container
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
        allowed = {"unit", "integration", "dry-run", "live-cutover"}
        assert [value for value in re.findall(r"`([^`]+)`", cells[1]) if value in allowed] == [
            "unit"
        ]
        assert [value for value in re.findall(r"`([^`]+)`", cells[2]) if value in allowed] == [
            "unit"
        ]
        assert [value for value in re.findall(r"`([^`]+)`", cells[3]) if value in allowed] == [
            "live-cutover"
        ]
        assert f".{harness.lower()}" in cells[1]
        assert ("--continue" if harness == "Claude" else "interactive session ID") in cells[2]
        assert f"resumed real {harness}" in cells[3]


# 2119: enforced-mode-cutover-runbook.3.3, enforced-mode-cutover-runbook.4.3
def test_complete_runner_set_and_process_binding_fail_closed(tmp_path) -> None:
    registry = tmp_path / "runners.json"
    registry.write_text('[{"id":"old","host":"local"}]')
    assert_runner_set(registry, "old")
    for invalid in (
        "[]",
        '[{"id":"other"}]',
        '[{"id":"old"},{"id":"old"}]',
        '[{"id":"old"},{"id":"remote"}]',
    ):
        registry.write_text(invalid)
        try:
            assert_runner_set(registry, "old")
        except ValueError as error:
            assert "controlled set" in str(error)
        else:
            raise AssertionError(f"uncontrolled runner registry was accepted: {invalid}")
    registry.write_text("[]")
    assert_runner_set(registry)
    current_pid = 123
    current_start = "Mon Aug  4 23:00:00 2026"

    def running_ps(argv, **kwargs):
        del kwargs
        stdout = current_start if argv[-1] == str(current_pid) else ""
        return SimpleNamespace(stdout=stdout)

    assert process_start(current_pid, run=running_ps) == current_start
    try:
        assert_process_replaced(current_pid, current_start, run=running_ps)
    except ValueError as error:
        assert "still alive" in str(error)
    else:
        raise AssertionError("original live process was accepted as replaced")
    assert_process_replaced(2**31 - 1, current_start, run=running_ps)
    command = tmp_path / "runner-command.txt"
    command.write_text("exec env PANOPTICON_RUNNER_ID=new uv run python -m runner\n")
    assert_runner_process(current_pid, current_start, command, "new", run=running_ps)
    try:
        assert_runner_process(current_pid, "wrong start", command, "new", run=running_ps)
    except ValueError as error:
        assert "identity changed" in str(error)
    else:
        raise AssertionError("mismatched runner start time was accepted")
    for invalid_command in (
        "env PANOPTICON_RUNNER_ID=new uv run python -m runner",
        "exec env PANOPTICON_RUNNER_ID=old uv run python -m runner",
    ):
        command.write_text(invalid_command)
        try:
            assert_runner_process(current_pid, current_start, command, "new", run=running_ps)
        except ValueError as error:
            assert "exec-bound" in str(error)
        else:
            raise AssertionError(f"unbound runner process was accepted: {invalid_command}")
    plan = _plan()
    assert "pane_start_command" in plan.steps[4].action
    assert "PANOPTICON_RUNNER_ID='$NEW_RUNNER_ID'" in plan.steps[4].check
    assert (
        'assert-runner-set "$EVIDENCE_DIR/G03-runners.json" "$NEW_RUNNER_ID"' in plan.gates[2].check
    )
    assert "assert-runner-process" in plan.gates[2].check
    assert "assert-process-replaced" in plan.gates[10].check
    assert 'assert-process-replaced "$OLD_RUNNER_PID" "$OLD_RUNNER_START"' in plan.gates[10].check
    assert (
        'assert-process-replaced "$OLD_DASHBOARD_PID" "$OLD_DASHBOARD_START"'
        in plan.gates[10].check
    )
    text = RUNBOOK_PATH.read_text()
    dashboard_check = 'uv --directory "$APP_ROOT" run python -m panopticon.core.cutover_runbook assert-process-replaced "$OLD_DASHBOARD_PID" "$OLD_DASHBOARD_START"'
    before, separator, after = text.rpartition(dashboard_check)
    assert separator
    missing_dashboard_death = before + 'test -n "$OLD_DASHBOARD_START"' + after
    assert "enforced-mode-cutover-runbook.3.3" in validate_enforced_mode_cutover_runbook(
        missing_dashboard_death
    )
    for required in (
        'assert-runner-set "$EVIDENCE_DIR/G03-runners.json" "$NEW_RUNNER_ID"',
        'assert-runner-process "$NEW_RUNNER_PID" "$NEW_RUNNER_START" "$EVIDENCE_DIR/S04-runner-start-command.txt" "$NEW_RUNNER_ID"',
    ):
        disconnected = RUNBOOK_PATH.read_text().replace(required, 'test -n "$NEW_RUNNER_ID"', 1)
        assert "enforced-mode-cutover-runbook.4.3" in validate_enforced_mode_cutover_runbook(
            disconnected
        )
