from panopticon.core.cutover_runbook import (
    RUNBOOK_PATH,
    parse_enforced_mode_cutover_runbook,
    validate_enforced_mode_cutover_runbook,
)


# 2119: enforced-mode-cutover-runbook.4.17
def test_g09_rejects_fabricated_non_container_capability_evidence() -> None:
    text = RUNBOOK_PATH.read_text()
    g09_offset = text.index("### G09 —")
    real_read = (
        'test "$(docker exec "$CANARY_CONTAINER_ID" python -c \'import json; '
        'print(json.load(open("/run/secrets/panopticon-service-auth"))["task"][:5])\')" = ptc1.'
    )
    for counterfeit in (
        'test "$(printf ptc1.)" = ptc1.',
        'docker exec "$CANARY_CONTAINER_ID" true\ntest "$(printf ptc1.)" = ptc1.',
    ):
        fabricated = text[:g09_offset] + text[g09_offset:].replace(real_read, counterfeit, 1)
        assert fabricated != text
        assert "enforced-mode-cutover-runbook.4.17" in validate_enforced_mode_cutover_runbook(
            fabricated
        )
    real_inspect = (
        "docker inspect --format '{{.Id}} {{.State.Pid}} {{.State.StartedAt}}' "
        '"$CANARY_CONTAINER_ID"'
    )
    fabricated_inspect = text[:g09_offset] + text[g09_offset:].replace(
        real_inspect,
        'printf "%s %s %s\\n" "$CANARY_CONTAINER_ID" 1 "$CANARY_CONTAINER_STARTED"',
        1,
    )
    assert fabricated_inspect != text
    assert "enforced-mode-cutover-runbook.4.17" in validate_enforced_mode_cutover_runbook(
        fabricated_inspect
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
    plan = parse_enforced_mode_cutover_runbook(text)
    g09 = plan.gates[8]
    assert g09.action.index("G09-target-at-capability.txt") < g09.action.index(
        'docker exec "$CANARY_CONTAINER_ID"'
    )
    assert 'cmp "$EVIDENCE_DIR/S07-container-initial.txt"' in g09.action
    final_cmp = (
        'cmp "$EVIDENCE_DIR/G09-target-at-capability.txt" '
        '"$EVIDENCE_DIR/S07-container-after-keepalive.txt"'
    )
    assert final_cmp in g09.check
    assert f"{g09.action}\n{g09.check}" in plan.steps[7].check
    assert 'docker exec "$CANARY_CONTAINER"' not in plan.steps[7].check
    after_id_capture = plan.steps[7].check.split(
        'export CANARY_CONTAINER_ID="$(docker inspect --format \'{{.Id}}\' "$CANARY_CONTAINER")"',
        1,
    )[1]
    assert (
        "docker inspect --format '{{.State.StartedAt}}' \"$CANARY_CONTAINER_ID\""
        in after_id_capture
    )
    assert (
        "docker inspect --format '{{.State.StartedAt}}' \"$CANARY_CONTAINER\""
        not in after_id_capture
    )
    g09_offset = text.index("### G09 —")
    weakened = text[:g09_offset] + text[g09_offset:].replace(
        final_cmp, 'test -s "$EVIDENCE_DIR/G09-target-at-capability.txt"', 1
    )
    assert "enforced-mode-cutover-runbook.4.18" in validate_enforced_mode_cutover_runbook(weakened)
