from panopticon.core.cutover_runbook import (
    RUNBOOK_PATH,
    parse_enforced_mode_cutover_runbook,
    validate_enforced_mode_cutover_runbook,
)


# 2119: enforced-mode-cutover-runbook.3.3
def test_runner_registry_is_rechecked_immediately_before_service_stop() -> None:
    text = RUNBOOK_PATH.read_text()
    plan = parse_enforced_mode_cutover_runbook(text)
    s01 = plan.steps[1].action
    final_check = (
        'assert-runner-set "$EVIDENCE_DIR/S01-runners-immediately-before-service-stop.json"'
    )
    assert s01.rindex(final_check) < s01.rindex("kill-session -t service")
    weakened = text.replace(final_check, 'test -n "$OLD_RUNNER_ID"', 1)
    assert "enforced-mode-cutover-runbook.3.3" in validate_enforced_mode_cutover_runbook(weakened)
    for process in ("RUNNER", "DASHBOARD"):
        death_check = (
            'uv --directory "$APP_ROOT" run python -m panopticon.core.cutover_runbook '
            f'assert-process-replaced "$OLD_{process}_PID" "$OLD_{process}_START"'
        )
        before, separator, after = text.rpartition(death_check)
        assert separator
        missing_death_check = before + f'test -n "$OLD_{process}_START"' + after
        assert "enforced-mode-cutover-runbook.3.3" in validate_enforced_mode_cutover_runbook(
            missing_death_check
        )


# 2119: enforced-mode-cutover-runbook.4.18
def test_g09_capability_target_matches_both_recorded_liveness_identities() -> None:
    text = RUNBOOK_PATH.read_text()
    g09 = parse_enforced_mode_cutover_runbook(text).gates[8]
    assert g09.action.index("G09-target-at-capability.txt") < g09.action.index(
        'docker exec "$CANARY_CONTAINER"'
    )
    assert 'cmp "$EVIDENCE_DIR/S07-container-initial.txt"' in g09.action
    final_cmp = (
        'cmp "$EVIDENCE_DIR/G09-target-at-capability.txt" '
        '"$EVIDENCE_DIR/S07-container-after-keepalive.txt"'
    )
    assert final_cmp in g09.check
    weakened = text.replace(final_cmp, 'test -s "$EVIDENCE_DIR/G09-target-at-capability.txt"', 1)
    assert "enforced-mode-cutover-runbook.4.18" in validate_enforced_mode_cutover_runbook(weakened)
