from panopticon.core.cutover_runbook import (
    RUNBOOK_PATH,
    CutoverPlan,
    parse_enforced_mode_cutover_runbook,
)


def _plan() -> CutoverPlan:
    return parse_enforced_mode_cutover_runbook(RUNBOOK_PATH.read_text())


# 2119: enforced-mode-cutover-runbook.2.2
def test_both_new_work_clients_stop_before_the_quiescence_wait() -> None:
    action = _plan().steps[1].action
    wait = action.index("while :; do")
    assert action.index("kill-session -t runner") < wait
    assert action.index("kill-session -t dashboard") < wait


# 2119: enforced-mode-cutover-runbook.1.2, enforced-mode-cutover-runbook.4.5
# 2119: enforced-mode-cutover-runbook.5.4
def test_canary_inputs_and_all_gate_pass_records_are_scheduled() -> None:
    plan = _plan()
    s00 = plan.steps[0]
    assert "export CANARY_TASK_ID=replace-with-nonterminal-task-id" in s00.action
    assert "export KNOWN_TASK_NAME=replace-with-task-name-visible-on-phone-board" in s00.action
    assert 'test "$CANARY_TASK_ID" != replace-with-nonterminal-task-id' in s00.check
    assert 'test "$KNOWN_TASK_NAME" != replace-with-task-name-visible-on-phone-board' in s00.check
    scheduled = "\n".join(step.check for step in plan.steps)
    assert (
        "execute the Command and Check for G01 through G07 below in\nnumeric order"
        in RUNBOOK_PATH.read_text()
    )
    for gate in range(1, 12):
        marker = f"G{gate:02d}: PASS"
        assert marker in scheduled or marker in "\n".join(item.check for item in plan.gates[:7])
    assert "= 403" in plan.gates[4].action


# 2119: enforced-mode-cutover-runbook.3.5, enforced-mode-cutover-runbook.3.8
# 2119: enforced-mode-cutover-runbook.3.9
def test_all_enforced_exports_precede_the_service_child_launch() -> None:
    action = _plan().steps[5].action
    launch = action.index("tmux -L panopticon new-session")
    for assignment in (
        "export PANOPTICON_SERVICE_AUTH_MODE=enforced",
        'export PANOPTICON_SERVICE_AUTH_FILE="$AUTH_FILE_NAME"',
        'export PANOPTICON_BROWSER_ORIGINS="$PWA_ORIGIN"',
    ):
        assert action.index(assignment) < launch


# 2119: enforced-mode-cutover-runbook.4.1, enforced-mode-cutover-runbook.4.2
# 2119: enforced-mode-cutover-runbook.4.4
def test_get_gates_do_not_override_the_default_get_method() -> None:
    gates = {gate.item_id: gate for gate in _plan().gates}
    for gate_id in ("G01", "G02", "G04"):
        assert "--request" not in gates[gate_id].action


# 2119: enforced-mode-cutover-runbook.4.6
def test_g06_binds_the_exact_origin_to_both_independent_requests() -> None:
    lines = _plan().gates[5].action.splitlines()
    assert len(lines) == 2
    assert "--request OPTIONS" in lines[0]
    assert '"Origin: $PWA_ORIGIN"' in lines[0]
    assert '"Origin: $PWA_ORIGIN"' in lines[1]


# 2119: enforced-mode-cutover-runbook.4.7
def test_g07_fetches_the_installed_phone_board_source() -> None:
    action = _plan().gates[6].action
    assert 'curl --silent --show-error --fail "$PHONE_BOARD_URL"' in action


# 2119: enforced-mode-cutover-runbook.4.8
def test_g08_is_the_last_s04_check_before_s05() -> None:
    plan = _plan()
    assert plan.steps[4].check.splitlines()[-2:] == [
        'test ! -s "$EVIDENCE_DIR/G08-running.txt"',
        "printf 'G08: PASS\\n' >> \"$EVIDENCE_DIR/gates.txt\"",
    ]
    assert plan.steps[5].title == "Start the task service with exported enforced configuration"
