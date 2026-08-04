from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from panopticon.core.cutover_runbook import (
    assert_complete_inventory,
    gate_http_status_passes,
    inspect_cutover_credential_file,
    is_fresh_post_enforcement_spawn,
    named_tasks_match,
    parse_enforced_mode_cutover_runbook,
    render_enforced_mode_cutover_runbook,
    validate_enforced_mode_cutover_runbook,
)

ROOT = Path(__file__).parents[1]
RUNBOOK = ROOT / "docs" / "runbooks" / "enforced-mode-cutover.md"


# 2119-spec: enforced-mode-cutover-runbook


def _runbook() -> str:
    return RUNBOOK.read_text()


def _section(text: str, heading: str, *, level: int = 2) -> str:
    marker = "#" * level
    match = re.search(
        rf"^{marker} {re.escape(heading)}\n(?P<body>.*?)(?=^{marker} |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, heading
    return match.group("body")


def _step(text: str, step_id: str) -> str:
    match = re.search(
        rf"^## {step_id} — [^\n]*\n(?P<body>.*?)(?=^## S\d{{2}} — |^## Rollback|^## The eleven gates|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, step_id
    return match.group("body")


def _gate(text: str, gate_id: str) -> str:
    gates = _section(text, "The eleven gates")
    match = re.search(
        rf"^### {gate_id} — [^\n]*\n(?P<body>.*?)(?=^### G\d{{2}} — |^## |\Z)",
        gates,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, gate_id
    return match.group("body")


def _ordered(haystack: str, *needles: str) -> None:
    positions = [haystack.index(needle) for needle in needles]
    assert positions == sorted(positions), needles


# 2119: 1.1, 1.2, 1.3, 1.4, 1.5
def test_runbook_is_offline_ordered_checkable_and_evidence_scoped() -> None:
    text = _runbook()
    plan = parse_enforced_mode_cutover_runbook(text)
    assert plan.step_ids == tuple(f"S{number:02d}" for number in range(10))
    assert tuple(step.action.effect for step in plan.steps.values()) == (
        "prerequisite-verification",
        "quiescence",
        "inventory",
        "container-drain",
        "long-lived-client-restart",
        "enforced-service-restart",
        "post-restart-gates",
        "canary-verification",
        "bulk-respawn",
        "evidence-recording",
    )
    assert plan.gate_ids == tuple(f"G{number:02d}" for number in range(1, 12))
    assert plan.original_gate_ids == tuple(f"G{number:02d}" for number in range(1, 8))
    assert plan.additional_gate_ids == ("G08", "G09", "G10", "G11")
    assert plan.offline_after_step == "S03"
    assert all(step.action.argv for step in plan.steps.values())
    assert all(step.check.argv for step in plan.steps.values())
    assert all(step.action.argv != step.check.argv for step in plan.steps.values())
    assert all(
        step.action.observation_channel != step.check.observation_channel
        for step in plan.steps.values()
    )
    assert all(step.expected and step.failure_action for step in plan.steps.values())
    assert all(step.evidence_level in {"authoring", "cutover-only"} for step in plan.steps.values())
    assert {step.evidence_level for step in plan.steps.values()} == {"authoring", "cutover-only"}
    all_executable_items = set(plan.steps) | set(plan.gates)
    assert set(plan.authoring_items).isdisjoint(plan.production_only_items)
    assert set(plan.authoring_items) | set(plan.production_only_items) == all_executable_items
    assert all(
        not step.action.invokes_task_service and not step.check.invokes_task_service
        for step_id, step in plan.steps.items()
        if step_id > "S03"
    )
    assert all(step.check.verifies == step.action.effect for step in plan.steps.values())
    assert all(step.expected_from == "check" for step in plan.steps.values())
    assert all(step.failure_on == "unexpected-check-result" for step in plan.steps.values())
    assert all(step.evidence_source for step in plan.steps.values())
    for step in plan.steps.values():
        for shell in (step.action.shell, step.check.shell):
            subprocess.run(
                ["bash", "--noprofile", "--norc", "-n"],
                input=shell,
                text=True,
                check=True,
            )
    steps = re.findall(r"^## (S\d{2}) — ", text, re.MULTILINE)
    assert steps == [f"S{number:02d}" for number in range(10)]
    assert "No step after S03 depends on reading this file from the task service" in text
    for step_id in steps:
        section = _step(text, step_id)
        labels = ("Action", "Check", "Expected", "Failure action", "Evidence status")
        _ordered(section, *(f"### {label}" for label in labels))
        for label in labels:
            assert _section(section, label, level=3).strip(), (step_id, label)
        assert "```sh" in _section(section, "Action", level=3)
        assert "```sh" in _section(section, "Check", level=3)
        assert re.match(
            r"^(STOP|ROLL BACK)\b",
            _section(section, "Failure action", level=3).strip(),
        )
        assert re.search(
            r"^(Authoring:|Cutover-only:)",
            _section(section, "Evidence status", level=3).strip(),
        )
    assert re.findall(r"^### G(\d{2}) — ", text, re.MULTILINE) == [
        f"{number:02d}" for number in range(1, 12)
    ]
    assert "G01–G07: PR #163 deployment gates" in text
    assert "G08–G11: issue #203 cutover additions" in text


# 2119: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9
def test_runbook_blocks_on_security_fix_quiesces_and_uses_direct_drain_evidence() -> None:
    text = _runbook()
    plan = parse_enforced_mode_cutover_runbook(text)
    assert plan.steps["S00"].check.argv == (
        "git",
        "merge-base",
        "--is-ancestor",
        "$ISSUE_202_COMMIT",
        "$DEPLOY_REV",
    )
    assert plan.steps["S00"].check.requires_green_run_for == "$ISSUE_202_COMMIT"
    assert plan.steps["S00"].failure_action == "stop-before-quiescence"
    assert plan.steps["S01"].quiesces == frozenset({"create", "respawn", "resume"})
    assert plan.steps["S01"].freeze_before_wait is True
    assert plan.steps["S01"].waits_for == "recorded-stopping-points"
    assert plan.steps["S02"].identity_fields == ("pid", "start_time")
    assert plan.steps["S02"].inventory_targets == (
        "all-running-task-containers",
        "all-credential-bearing-long-lived-processes",
    )
    assert all(
        entry.identity_fields == ("pid", "start_time")
        for entry in plan.steps["S02"].inventory_entries
    )
    assert plan.steps["S02"].check.asserts_discovered_equals_recorded is True
    assert plan.steps["S03"].action.argv[:2] == ("docker", "stop")
    assert plan.steps["S03"].action.scope == "all-running-task-containers"
    assert plan.steps["S03"].check.argv == (
        "docker",
        "ps",
        "--quiet",
        "--filter",
        "label=panopticon.task",
    )
    assert plan.steps["S03"].check.assertion == "stdout-empty"
    assert plan.steps["S03"].check.expected_container_count == 0
    assert plan.steps["S03"].check.observation_source == "docker-running-container-list"
    assert plan.permissive_counter_role == "corroboration-only"
    assert plan.allow_legacy_pt1 is False
    assert plan.allow_live_remint is False
    assert plan.allow_scoping_revert is False
    prerequisite = _step(text, "S00")
    _ordered(prerequisite, "ISSUE_202_COMMIT=", "DEPLOY_REV=", "merge-base --is-ancestor", "gh run view")
    assert 'git merge-base --is-ancestor "$ISSUE_202_COMMIT" "$DEPLOY_REV"' in prerequisite
    assert "--exit-status" in prerequisite
    assert "issue #202 closing commit is an ancestor" in prerequisite
    assert "STOP before quiescence" in prerequisite

    quiesce = _step(text, "S01")
    assert "stop accepting new work" in quiesce
    assert "creation, respawn, and resume are frozen" in quiesce
    assert "in-flight turns" in quiesce and "recorded stopping point" in quiesce

    inventory = _step(text, "S02")
    for evidence in ("docker ps", "panopticon.task", "pane_pid", "ps -o lstart="):
        assert evidence in inventory
    assert "PID and start time" in inventory

    drain = _step(text, "S03")
    _ordered(drain, "docker stop", "docker ps", "--quiet")
    assert "zero running task containers" in drain
    assert "Expected output: empty" in drain
    assert "nonzero drain" in drain and "STOP" in drain
    assert "weak corroborating signal" in drain
    assert "never a gate" in drain

    forbidden = _section(text, "Rejected strategies")
    assert "Do not add a `pt1` compatibility window" in forbidden
    assert "Do not re-mint or replace credentials inside running containers" in forbidden
    assert "Do not revert or weaken PR #163" in forbidden


# 2119: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10
def test_runbook_replaces_identified_survivors_and_starts_with_validated_configuration() -> None:
    text = _runbook()
    plan = parse_enforced_mode_cutover_runbook(text)
    restart_plan = plan.steps["S04"]
    assert plan.execution_order.index("restart-runner") < plan.execution_order.index(
        "enable-enforced-authentication"
    )
    assert plan.execution_order.index("restart-dashboard") < plan.execution_order.index(
        "enable-enforced-authentication"
    )
    assert restart_plan.restarts == ("runner", "dashboard")
    assert restart_plan.identity_fields == ("pid", "start_time")
    assert restart_plan.require_new_identity is True
    assert restart_plan.identity_subject == "active-runner"
    assert restart_plan.identity_comparison == "pid-start-pair-different"
    assert restart_plan.survivor_disposition == "restarted-or-confirmed-dead"
    assert restart_plan.reconciles == "every-inventoried-survivor"
    assert restart_plan.post_change_probe_is_survivor_evidence is False
    service_plan = plan.steps["S05"]
    assert service_plan.action.environment["PANOPTICON_SERVICE_AUTH_MODE"] == "enforced"
    assert service_plan.action.environment["PANOPTICON_SERVICE_AUTH_FILE"] == "$AUTH_FILE_NAME"
    assert service_plan.action.environment["PANOPTICON_BROWSER_ORIGINS"] == "$PWA_ORIGIN"
    assert service_plan.action.resolved_environment["PANOPTICON_SERVICE_AUTH_FILE"] == (
        service_plan.secrets_root / service_plan.auth_file_name
    )
    assert service_plan.credential_file_checks == (
        "regular",
        "not-symlink",
        "service-owner",
        "mode-0600",
        "nonempty-write",
        "distinct-nonempty-read",
        "no-value-output",
    )
    assert service_plan.supervisor_environment_updates == ("PANOPTICON_BROWSER_ORIGINS",)
    assert service_plan.supervisor_environment_values["PANOPTICON_BROWSER_ORIGINS"] == "$PWA_ORIGIN"
    assert service_plan.auth_file_reference_kind == "filename-under-secrets-root"
    assert service_plan.auth_file_path == service_plan.secrets_root / service_plan.auth_file_name
    assert "/" not in service_plan.auth_file_name
    assert service_plan.browser_origin_constraints == (
        "scheme",
        "host",
        "port",
        "no-path-query-fragment-credentials-or-trailing-slash",
    )
    assert service_plan.credential_validation_cases == (
        "nonempty-write",
        "nonempty-read",
        "read-write-distinct",
        "no-token-output",
    )
    assert service_plan.credential_validation_command == (
        "python",
        "-m",
        "panopticon.core.cutover_runbook",
        "inspect-credential-file",
        "$PANOPTICON_CONFIG/secrets/$AUTH_FILE_NAME",
    )
    restart = _step(text, "S04")
    _ordered(restart, "kill-session -t runner", "kill-session -t dashboard", "panopticon start")
    for identity in ("old PID", "old start time", "new PID", "new start time"):
        assert identity in restart
    assert "new PID/start-time pair differs" in restart
    assert "kill -0" in restart and "confirmed dead" in restart
    assert "freshly launched CLI proves nothing about a survivor" in restart

    service = _step(text, "S05")
    service_action = _section(service, "Action", level=3)
    service_check = _section(service, "Check", level=3)
    assert text.index("## S04 —") < text.index("## S05 —")
    for assignment in (
        "PANOPTICON_SERVICE_AUTH_MODE=enforced",
        "PANOPTICON_SERVICE_AUTH_FILE=",
        "PANOPTICON_BROWSER_ORIGINS=",
    ):
        assert assignment in service_action
    assert "scheme, host, and port only" in service
    assert "tmux -L panopticon set-environment -g PANOPTICON_BROWSER_ORIGINS" in service
    assert "regular file" in service_check and "not a symbolic link" in service_check
    assert "mode 0600" in service_check and "owned by the service user" in service_check
    assert "nonempty `write` array" in service_check
    assert "distinct nonempty `read` array" in service_check
    assert "must not print token values" in service_check.lower()
    assert "kill-session -t service" in service_action
    assert "panopticon host" in service_action


# 2119: enforced-mode-cutover-runbook.3.10
@pytest.mark.parametrize(
    "payload",
    [
        {"write": [], "read": ["r"]},
        {"write": ["w"], "read": []},
        {"write": "w", "read": ["r"]},
        {"write": ["same"], "read": ["same"]},
        {"write": ["w", "shared"], "read": ["shared", "r"]},
    ],
)
def test_credential_metadata_inspection_rejects_invalid_arrays_without_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], payload: object
) -> None:
    path = tmp_path / "service-auth.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="credential metadata"):
        inspect_cutover_credential_file(path)
    output = capsys.readouterr()
    assert output.out == "" and output.err == ""
    assert "same" not in str(output) and '"w"' not in str(output) and '"r"' not in str(output)


# 2119: enforced-mode-cutover-runbook.3.10
def test_credential_metadata_inspection_accepts_disjoint_arrays_without_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "service-auth.json"
    path.write_text(json.dumps({"write": ["secret-write"], "read": ["secret-read"]}))
    result = inspect_cutover_credential_file(path)
    assert result.write_count == 1 and result.read_count == 1
    output = capsys.readouterr()
    assert output.out == "" and output.err == ""


# 2119: enforced-mode-cutover-runbook.3.6
def test_credential_inspection_accepts_safe_owner_and_rejects_wrong_owner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "service-auth.json"
    path.write_text(json.dumps({"write": ["secret-write"], "read": ["secret-read"]}))
    path.chmod(0o600)
    actual_uid = path.stat().st_uid
    assert inspect_cutover_credential_file(path, service_uid=actual_uid).write_count == 1
    with pytest.raises(ValueError, match="credential metadata"):
        inspect_cutover_credential_file(path, service_uid=actual_uid + 1)


# 2119: enforced-mode-cutover-runbook.2.3
def test_inventory_completeness_rejects_any_omitted_discovered_process() -> None:
    discovered = {("container", "c1"), ("runner", "r1"), ("dashboard", "d1")}
    recorded = discovered.copy()
    assert_complete_inventory(discovered, recorded)
    for omitted in discovered:
        with pytest.raises(ValueError, match="inventory incomplete"):
            assert_complete_inventory(discovered, recorded - {omitted})


# 2119: enforced-mode-cutover-runbook.4.4
@pytest.mark.parametrize("status", [401, 403])
def test_g04_rejects_each_forbidden_runtime_status(status: int) -> None:
    assert gate_http_status_passes("G04", status) is False
    assert gate_http_status_passes("G04", 200) is True


# 2119: enforced-mode-cutover-runbook.4.7
def test_g07_compares_independently_observed_names_exactly() -> None:
    assert named_tasks_match("installed-phone-board", "same", "authenticated-fleet-api", "same")
    assert not named_tasks_match(
        "installed-phone-board", "same", "authenticated-fleet-api", "different"
    )
    with pytest.raises(ValueError, match="independent sources"):
        named_tasks_match("fixture", "same", "fixture", "same")


# 2119: enforced-mode-cutover-runbook.4.18
def test_g09_spawn_timestamp_must_be_strictly_after_enforcement() -> None:
    assert is_fresh_post_enforcement_spawn(enforcement_started=100.0, container_created=100.1)
    assert not is_fresh_post_enforcement_spawn(
        enforcement_started=100.0, container_created=100.0
    )
    assert not is_fresh_post_enforcement_spawn(enforcement_started=100.0, container_created=99.9)


# 2119: enforced-mode-cutover-runbook.3.6
def test_credential_inspection_rejects_unsafe_files_without_printing_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    regular = tmp_path / "service-auth.json"
    regular.write_text(json.dumps({"write": ["secret-write"], "read": ["secret-read"]}))
    regular.chmod(0o640)
    symlink = tmp_path / "linked.json"
    symlink.symlink_to(regular)
    directory = tmp_path / "directory.json"
    directory.mkdir()
    for path in (regular, symlink, directory):
        with pytest.raises(ValueError, match="credential metadata"):
            inspect_cutover_credential_file(path)
    output = capsys.readouterr()
    assert output.out == "" and output.err == ""
    assert "secret-write" not in str(output) and "secret-read" not in str(output)


# 2119: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 4.10, 4.11, 4.12, 4.13, 4.14, 4.15
def test_runbook_contains_executable_original_and_adversarial_cutover_gates() -> None:
    text = _runbook()
    plan = parse_enforced_mode_cutover_runbook(text)
    assert plan.gates["G01"].http == ("GET", "/healthz", "none", 200)
    assert plan.gates["G02"].http == ("GET", "/tasks", "none", 401)
    assert "Authorization" not in plan.gates["G02"].command.headers
    assert plan.gates["G03"].runner_identity_fields == ("pid", "start_time")
    assert plan.gates["G03"].requires_authenticated_liveness is True
    assert plan.gates["G03"].identity_subject == "active-runner"
    assert plan.gates["G03"].expected_registration == "success-and-same-runner-live"
    assert plan.gates["G03"].command.auth == "runner-write-token"
    assert plan.gates["G03"].command.runner_identity_fields == ("pid", "start_time")
    assert plan.gates["G03"].command.runner_subject == "recorded-active-runner"
    assert plan.gates["G04"].http == ("GET", "/tasks", "read", "not-401-or-403")
    assert plan.gates["G04"].origin == "$PWA_ORIGIN"
    assert plan.gates["G04"].token_source == "credential-read-array"
    assert plan.gates["G05"].http == (
        "PUT",
        "/tasks/$CANARY_TASK_ID/turn",
        "read",
        401,
    )
    assert plan.gates["G05"].token_source == "credential-read-array"
    assert plan.gates["G06"].cors == {
        "preflight_echo": "$PWA_ORIGIN",
        "actual_echo": "$PWA_ORIGIN",
        "credentials_header": "absent-both",
    }
    assert plan.gates["G07"].compares_named_task is True
    assert plan.gates["G07"].comparison_sources == ("installed-phone-board", "authenticated-fleet-api")
    assert plan.gates["G07"].command.inputs == (
        "installed-phone-board-task-name",
        "authenticated-fleet-api-task-name",
    )
    assert plan.gates["G07"].command.comparison == "exact-equality"
    assert plan.gates["G08"].expected_container_count == 0
    assert plan.gates["G08"].immediately_before == "enforced-service-restart"
    assert plan.gates["G09"].real_container is True
    assert plan.gates["G09"].fresh_spawn is True
    assert plan.gates["G09"].capability_source == "mounted-container-credential"
    assert plan.gates["G09"].capability_prefix == "ptc1."
    assert plan.gates["G09"].liveness_events == (":ok", ":keepalive")
    assert plan.gates["G09"].minimum_seconds == 5
    assert plan.gates["G10"].identity_fields == ("pid", "start_time")
    assert plan.gates["G10"].identity_subject == "active-runner"
    assert plan.gates["G10"].comparison == "newer-start-time"
    assert plan.gates["G10"].requires_changed_identity_pair is True
    assert plan.gates["G11"].covers_all_inventory_survivors is True
    assert plan.gates["G11"].identity_fields == ("pid", "start_time")
    assert plan.gates["G11"].allowed_dispositions == ("restarted", "confirmed-dead")
    assert plan.gates["G06"].preflight_origin_comparison == "exact"
    assert plan.gates["G06"].preflight_command.comparison == "exact-string-equality"
    assert plan.gates["G06"].actual_origin_comparison == "exact"
    assert plan.gates["G06"].credentials_absence_checks == ("preflight", "actual")
    assert plan.gates["G09"].liveness_order == (":ok", ":keepalive")
    assert plan.gates["G09"].minimum_elapsed_seconds == 5
    assert plan.gates["G09"].spawn_time_comparison == "container-created-after-enforcement-start"
    expectations = {
        "G01": ("curl", "/healthz", "200"),
        "G02": ("curl", "/tasks", "401"),
        "G03": ("runner PID", "runner start time", "/runners/", "live"),
        "G04": ("Authorization: Bearer", "Origin:", "GET /tasks", "not 401 or 403"),
        "G05": ("PUT", "/tasks/$CANARY_TASK_ID/turn", "read token", "401"),
        "G06": ("OPTIONS", "Access-Control-Allow-Origin", "Access-Control-Allow-Credentials"),
        "G07": ("named task", "authenticated fleet API", "installed phone"),
        "G08": ("docker ps", "panopticon.task", "Expected output: empty"),
        "G09": ("real container", "ptc1.", ":ok", ":keepalive", "five seconds"),
        "G10": ("old PID", "old start time", "new PID", "new start time"),
        "G11": ("inventory", "PID", "start time", "restarted or confirmed dead"),
    }
    for gate_id, phrases in expectations.items():
        gate = _gate(text, gate_id)
        for phrase in phrases:
            assert phrase in gate, (gate_id, phrase)
        for label in ("Command", "Expected", "Failure action", "Record"):
            assert _section(gate, label, level=4).strip(), (gate_id, label)
        assert "STOP" in _section(gate, "Failure action", level=4)
        assert "assert" in _section(gate, "Command", level=4).lower()

    cors = _gate(text, "G06")
    assert cors.count("Access-Control-Allow-Origin: $PWA_ORIGIN") >= 2
    assert "preflight headers" in cors and "actual-response headers" in cors
    assert "must be absent from both" in cors
    assert "Access-Control-Allow-Credentials: true" in cors


# 2119: 5.3, 5.4, 5.5, 5.8
def test_runbook_scopes_resume_evidence_and_canaries_before_bulk_respawn() -> None:
    text = _runbook()
    plan = parse_enforced_mode_cutover_runbook(text)
    assert plan.resume_evidence["claude"].level == "unit"
    assert plan.resume_evidence["codex"].level == "unit"
    assert plan.steps["S07"].canary_count == 1
    assert plan.steps["S07"].required_gates == ("G09",)
    assert plan.steps["S08"].requires_completed_step == "S07"
    assert plan.steps["S08"].requires_task_disposition is True
    assert plan.steps["S08"].task_scope == "every-intended-nonterminal-task"
    assert plan.steps["S08"].allowed_dispositions == ("live", "task-specific-failure")
    assert plan.steps["S08"].records_task_specific_failures is True
    assert plan.resume_evidence["claude"].all_claims_classified is True
    assert plan.resume_evidence["codex"].all_claims_classified is True
    assert set(plan.resume_evidence["claude"].claims) == {
        "configuration-volume-persistence",
        "launcher-continuation-selection",
        "real-cli-transcript-acceptance",
    }
    assert set(plan.resume_evidence["codex"].claims) == {
        "configuration-volume-persistence",
        "explicit-session-selection",
        "real-cli-transcript-acceptance",
    }
    assert {
        name: claim.level for name, claim in plan.resume_evidence["claude"].claims.items()
    } == {
        "configuration-volume-persistence": "unit",
        "launcher-continuation-selection": "unit",
        "real-cli-transcript-acceptance": "live-cutover",
    }
    assert {
        name: claim.level for name, claim in plan.resume_evidence["codex"].claims.items()
    } == {
        "configuration-volume-persistence": "unit",
        "explicit-session-selection": "unit",
        "real-cli-transcript-acceptance": "live-cutover",
    }
    assert all(claim.level in {"unit", "integration", "dry-run", "live-cutover"} for claim in plan.resume_evidence["claude"].claims.values())
    assert all(claim.level in {"unit", "integration", "dry-run", "live-cutover"} for claim in plan.resume_evidence["codex"].claims.values())
    assert plan.gates["G09"].capability_source == "mounted-container-credential"
    assert plan.gates["G09"].capability_prefix == "ptc1."
    assert plan.gates["G09"].real_container is True
    assert plan.gates["G09"].fresh_spawn is True
    assert plan.gates["G09"].liveness_events == (":ok", ":keepalive")
    assert plan.gates["G09"].minimum_elapsed_seconds == 5
    evidence = _section(text, "Resume evidence")
    claude = _section(evidence, "Claude", level=3)
    codex = _section(evidence, "Codex", level=3)
    for block in (claude, codex):
        assert "Evidence level:" in block
        assert "What was exercised:" in block
        assert "What remains unproven:" in block
    assert "unit" in claude and "--continue" in claude
    assert "unit" in codex and "explicit session identifier" in codex
    canary = _step(text, "S07")
    bulk = _step(text, "S08")
    assert "one real canary" in canary
    assert "G09" in canary and "before bulk respawn" in canary
    assert text.index("## S07 —") < text.index("## S08 —")
    assert "every intended nonterminal task" in bulk
    assert "live or a task-specific failure disposition" in bulk


# 2119: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 6.9
def test_runbook_has_safe_rollback_honest_unknowns_and_single_followup_home() -> None:
    text = _runbook()
    plan = parse_enforced_mode_cutover_runbook(text)
    assert plan.rollback.triggers == (
        "prerequisite-failure",
        "nonzero-drain",
        "stale-long-lived-client",
        "service-startup-failure",
        "security-gate-failure",
        "browser-gate-failure",
        "canary-failure",
    )
    assert plan.rollback.actions == (
        "keep-containers-stopped",
        "restore-last-known-good-service",
        "restart-runner-and-dashboard",
        "repeat-inventory",
    )
    assert plan.rollback.allow_legacy is False
    assert plan.rollback.expect_killed_process is False
    assert plan.rollback.keep_containers_stopped_until_service_restored is True
    assert plan.rollback.keep_containers_stopped_until_clients_restored is True
    assert plan.followup_issue == 203
    assert plan.open_duplicate_issues is False
    assert plan.cutover_evidence_body == "recorded-gate-and-step-evidence"
    assert plan.followup_body == "newly-discovered-followup-work"
    assert plan.steps["S09"].action.argv[:4] == (
        "gh",
        "issue",
        "comment",
        "203",
    )
    assert "--body-file" in plan.steps["S09"].action.argv
    assert set(plan.production_only_items) == {
        item for item, _ in re.findall(r"^- (S\d{2}|G\d{2}): production-only — (.+)$", text, re.MULTILINE)
    }
    executable_cutover_only = {
        step_id
        for step_id, step in plan.steps.items()
        if step.evidence_level == "cutover-only" and step.action.shell
    } | {
        gate_id
        for gate_id, gate in plan.gates.items()
        if gate.evidence_level == "cutover-only" and gate.command.shell
    }
    assert set(plan.production_only_items) == executable_cutover_only
    assert plan.production_unknowns == frozenset(
        {"process-identity", "credential-file", "network", "phone", "real-container"}
    )
    rollback = _section(text, "Rollback")
    for trigger in (
        "prerequisite failure",
        "nonzero drain",
        "stale long-lived client",
        "service startup failure",
        "security gate failure",
        "browser gate failure",
        "canary failure",
    ):
        assert trigger in rollback.lower()
    _ordered(
        rollback,
        "keep every task container stopped",
        "restore the last known-good service configuration",
        "restart the runner and dashboard",
        "repeat S02",
    )
    assert "Do not restore legacy capability acceptance" in rollback
    assert "Do not expect a killed process to reappear" in rollback

    unknowns = _section(text, "What remains unproven until cutover")
    for unknown in ("process identity", "credential file", "network", "phone", "real container"):
        assert unknown in unknowns.lower()

    record = _step(text, "S09")
    assert "gh issue comment 203" in record
    assert "cutover evidence" in record and "newly discovered follow-up" in record
    assert "Do not open a new issue" in record
    assert "already described in #202 or #203" in record

    exercise = _section(text, "Authoring exercise record")
    production_only = re.findall(r"^- (S\d{2}|G\d{2}): production-only — (.+)$", exercise, re.MULTILINE)
    assert production_only
    assert {item for item, _ in production_only} >= {"S03", "S04", "S05", "G07", "G09"}
    assert all(reason.strip() for _, reason in production_only)


INVALIDATING_MUTATIONS = {
    "enforced-mode-cutover-runbook.1.1": ("S05_CHECK_EXEC='tmux -L panopticon list-sessions'", "S05_CHECK_EXEC='curl $PANOPTICON_SERVICE_URL/tasks'"),
    "enforced-mode-cutover-runbook.1.2": ("## S09 —", "## S08 — duplicate"),
    "enforced-mode-cutover-runbook.1.3": ("STEP_BLOCKS_HAVE_DISTINCT_ACTION_CHECK=1", "STEP_BLOCKS_HAVE_DISTINCT_ACTION_CHECK=0"),
    "enforced-mode-cutover-runbook.1.4": ("### G11 —", "### G10 — duplicate"),
    "enforced-mode-cutover-runbook.1.5": ("EVIDENCE_CLASSIFICATION_PARTITION=complete", "EVIDENCE_CLASSIFICATION_PARTITION=incomplete"),
    "enforced-mode-cutover-runbook.1.6": ("VALIDATOR_REPORTS_GOVERNING_REQUIREMENT=1", "VALIDATOR_REPORTS_GOVERNING_REQUIREMENT=0"),
    "enforced-mode-cutover-runbook.2.1": ("git merge-base --is-ancestor", "git merge-base"),
    "enforced-mode-cutover-runbook.2.2": ("creation, respawn, and resume are frozen", "new tasks may still start"),
    "enforced-mode-cutover-runbook.2.3": ("INVENTORY_IDENTITY_FIELDS='pid start_time'", "INVENTORY_IDENTITY_FIELDS='pid'"),
    "enforced-mode-cutover-runbook.2.4": ("docker stop", "docker restart"),
    "enforced-mode-cutover-runbook.2.5": ("DRAIN_EXPECTED_EMPTY=1", "DRAIN_EXPECTED_EMPTY=0"),
    "enforced-mode-cutover-runbook.2.6": ("never a gate", "the enforcement gate"),
    "enforced-mode-cutover-runbook.2.7": ("Do not add a `pt1` compatibility window", "Temporarily accept `pt1`"),
    "enforced-mode-cutover-runbook.2.8": ("Do not re-mint or replace credentials inside running containers", "Replace live credentials"),
    "enforced-mode-cutover-runbook.2.9": ("Do not revert or weaken PR #163", "Revert PR #163"),
    "enforced-mode-cutover-runbook.3.1": ("kill-session -t runner", "leave the runner session alive"),
    "enforced-mode-cutover-runbook.3.2": ("new PID/start-time pair differs", "new PID may equal the old PID"),
    "enforced-mode-cutover-runbook.3.3": ("SURVIVOR_DISPOSITION_REQUIRED=1", "SURVIVOR_DISPOSITION_REQUIRED=0"),
    "enforced-mode-cutover-runbook.3.4": ("freshly launched CLI proves nothing about a survivor", "a fresh CLI proves the survivor works"),
    "enforced-mode-cutover-runbook.3.5": ("PANOPTICON_SERVICE_AUTH_MODE=enforced", "PANOPTICON_SERVICE_AUTH_MODE=permissive"),
    "enforced-mode-cutover-runbook.3.6": ("mode 0600", "mode 0644"),
    "enforced-mode-cutover-runbook.3.7": ("tmux -L panopticon set-environment -g PANOPTICON_BROWSER_ORIGINS", "export PANOPTICON_BROWSER_ORIGINS"),
    "enforced-mode-cutover-runbook.3.8": ("AUTH_FILE_ENV_ASSIGNMENT=filename-only", "AUTH_FILE_ENV_ASSIGNMENT=missing"),
    "enforced-mode-cutover-runbook.3.9": ("scheme, host, and port only", "include the board path"),
    "enforced-mode-cutover-runbook.3.10": ("distinct nonempty `read` array", "reuse the write token as read"),
    "enforced-mode-cutover-runbook.4.1": ("G01_EXPECTED_STATUS=200", "G01_EXPECTED_STATUS=401"),
    "enforced-mode-cutover-runbook.4.2": ("G02_EXPECTED_STATUS=401", "G02_EXPECTED_STATUS=200"),
    "enforced-mode-cutover-runbook.4.3": ("G03_REQUIRE_SAME_RUNNER=1", "G03_REQUIRE_SAME_RUNNER=0"),
    "enforced-mode-cutover-runbook.4.4": ("G04_FORBID_STATUS='401 403'", "G04_FORBID_STATUS=''"),
    "enforced-mode-cutover-runbook.4.5": ("G05_EXPECTED_STATUS=401", "G05_EXPECTED_STATUS=200"),
    "enforced-mode-cutover-runbook.4.6": ("G06_CHECK_PREFLIGHT=1", "G06_CHECK_PREFLIGHT=0"),
    "enforced-mode-cutover-runbook.4.7": ("G07_COMPARE_NAMED_TASK=1", "G07_COMPARE_NAMED_TASK=0"),
    "enforced-mode-cutover-runbook.4.8": ("G08_EXPECTED_CONTAINERS=0", "G08_EXPECTED_CONTAINERS=1"),
    "enforced-mode-cutover-runbook.4.9": ("G09_CAPABILITY_PREFIX='ptc1.'", "G09_CAPABILITY_PREFIX='pt1.'"),
    "enforced-mode-cutover-runbook.4.10": ("G10_REQUIRE_PID_AND_START=1", "G10_REQUIRE_PID_AND_START=0"),
    "enforced-mode-cutover-runbook.4.11": ("G11_REQUIRE_ALL_SURVIVORS=1", "G11_REQUIRE_ALL_SURVIVORS=0"),
    "enforced-mode-cutover-runbook.4.12": ("G06_PREFLIGHT_ECHO_ORIGIN=1", "G06_PREFLIGHT_ECHO_ORIGIN=0"),
    "enforced-mode-cutover-runbook.4.13": ("G06_ACTUAL_ECHO_ORIGIN=1", "G06_ACTUAL_ECHO_ORIGIN=0"),
    "enforced-mode-cutover-runbook.4.14": ("G06_FORBID_CREDENTIALS_BOTH=1", "G06_FORBID_CREDENTIALS_BOTH=0"),
    "enforced-mode-cutover-runbook.4.15": ("G09_MIN_KEEPALIVES=1", "G09_MIN_KEEPALIVES=0"),
    "enforced-mode-cutover-runbook.4.16": ("G09_CAPABILITY_SOURCE='mounted-container-credential'", "G09_CAPABILITY_SOURCE='generated-value'"),
    "enforced-mode-cutover-runbook.4.17": ("G09_REAL_CONTAINER=1", "G09_REAL_CONTAINER=0"),
    "enforced-mode-cutover-runbook.4.18": ("G09_FRESH_SPAWN=1", "G09_FRESH_SPAWN=0"),
    "enforced-mode-cutover-runbook.5.3": ("Claude evidence level: unit", "Claude evidence level: missing"),
    "enforced-mode-cutover-runbook.5.4": ("CANARY_BEFORE_BULK=1", "CANARY_BEFORE_BULK=0"),
    "enforced-mode-cutover-runbook.5.5": ("REQUIRE_TASK_DISPOSITION=1", "REQUIRE_TASK_DISPOSITION=0"),
    "enforced-mode-cutover-runbook.5.8": ("Codex evidence level: unit", "Codex evidence level: missing"),
    "enforced-mode-cutover-runbook.6.1": ("ROLLBACK_TRIGGER_CANARY_FAILURE=1", "ROLLBACK_TRIGGER_CANARY_FAILURE=0"),
    "enforced-mode-cutover-runbook.6.2": ("ROLLBACK_KEEP_CONTAINERS_STOPPED=1", "ROLLBACK_KEEP_CONTAINERS_STOPPED=0"),
    "enforced-mode-cutover-runbook.6.3": ("ROLLBACK_ALLOW_LEGACY=0", "ROLLBACK_ALLOW_LEGACY=1"),
    "enforced-mode-cutover-runbook.6.4": ("production process identity remains unproven", "production process identity is proven"),
    "enforced-mode-cutover-runbook.6.5": ("APPEND_CUTOVER_EVIDENCE_TO_203=1", "APPEND_CUTOVER_EVIDENCE_TO_203=0"),
    "enforced-mode-cutover-runbook.6.6": ("PRODUCTION_ONLY_REASON_COVERAGE=complete", "PRODUCTION_ONLY_REASON_COVERAGE=incomplete"),
    "enforced-mode-cutover-runbook.6.7": ("NEW_FOLLOWUP_ISSUE=203", "NEW_FOLLOWUP_ISSUE=204"),
    "enforced-mode-cutover-runbook.6.8": ("OPEN_DUPLICATE_ISSUES=0", "OPEN_DUPLICATE_ISSUES=1"),
    "enforced-mode-cutover-runbook.6.9": ("ROLLBACK_EXPECT_KILLED_PROCESS=0", "ROLLBACK_EXPECT_KILLED_PROCESS=1"),
    "enforced-mode-cutover-runbook.6.10": ("ROLLBACK_KEEP_CLIENTS_STOPPED_UNTIL_RESTORED=1", "ROLLBACK_KEEP_CLIENTS_STOPPED_UNTIL_RESTORED=0"),
}

# Each tuple is an almost-conforming document that preserves the surrounding step or gate while
# violating one semantic boundary.  These complement the primary mutation above; in particular,
# they prevent a parser from accepting self-declared metadata that contradicts the executable
# command or another instruction elsewhere in the runbook.
ADDITIONAL_INVALIDATING_MUTATIONS = {
    "enforced-mode-cutover-runbook.1.1": (
        ("S05_ACTION_EXEC='tmux -L panopticon kill-session -t service'", "S05_ACTION_EXEC='curl $PANOPTICON_SERVICE_URL/tasks'"),
    ),
    "enforced-mode-cutover-runbook.1.2": (
        ("S00_EFFECT=verify-prerequisite", "S00_EFFECT=bulk-respawn"),
        ("S01_EFFECT=quiesce", "S01_EFFECT=canary-verification"),
        ("S02_EFFECT=inventory", "S02_EFFECT=dashboard-restart"),
        ("S03_EFFECT=container-drain", "S03_EFFECT=runner-restart"),
        ("S04_EFFECT=long-lived-client-restart", "S04_EFFECT=container-drain"),
        ("S05_EFFECT=enforced-service-restart", "S05_EFFECT=quiesce"),
        ("S06_EFFECT=post-restart-gates", "S06_EFFECT=bulk-respawn"),
        ("S07_EFFECT=canary-verification", "S07_EFFECT=inventory"),
        ("S08_EFFECT=bulk-respawn", "S08_EFFECT=prerequisite-verification"),
        ("S09_EFFECT=evidence-recording", "S09_EFFECT=container-drain"),
    ),
    "enforced-mode-cutover-runbook.1.3": (
        ("ACTION_EXECUTABLE=1", "ACTION_EXECUTABLE=0"),
        ("CHECK_EXECUTABLE=1", "CHECK_EXECUTABLE=0"),
        ("CHECK_INDEPENDENT=1", "CHECK_INDEPENDENT=0"),
        ("EXPECTED_FROM=check", "EXPECTED_FROM=action"),
        ("FAILURE_ON=unexpected-check-result", "FAILURE_ON=operator-discretion"),
        ("S06_ACTION_EXEC='run-gates G01 G02 G03 G04 G05 G06'", "S06_ACTION_EXEC=''"),
        ("S06_CHECK_EXEC='test all-gates-passed'", "S06_CHECK_EXEC=''"),
        ("S06_CHECK_EXEC='test all-gates-passed'", "S06_CHECK_EXEC='run-gates G01 G02 G03 G04 G05 G06'"),
        ("S06_EXPECTED='all six gates pass'", "S06_EXPECTED='one gate may fail'"),
        ("S06_FAILURE='STOP and roll back'", "S06_FAILURE='Investigate; STOP later'"),
        ("S06_FAILURE='STOP and roll back'", "S06_FAILURE='STOPPED'"),
    ),
    "enforced-mode-cutover-runbook.1.5": (
        ("S03_EVIDENCE_STATUS=cutover-only", "S03_EVIDENCE_STATUS=authoring"),
        ("S06_EVIDENCE_STATUS=authoring", "S06_EVIDENCE_STATUS=cutover-only"),
        ("S04_EVIDENCE_STATUS=cutover-only", "S04_EVIDENCE_STATUS=authoring"),
        ("S05_EVIDENCE_STATUS=cutover-only", "S05_EVIDENCE_STATUS=authoring"),
        ("S07_EVIDENCE_STATUS=cutover-only", "S07_EVIDENCE_STATUS=authoring"),
        ("S08_EVIDENCE_STATUS=cutover-only", "S08_EVIDENCE_STATUS=authoring"),
    ),
    "enforced-mode-cutover-runbook.1.4": (
        ("G01–G07: PR #163 deployment gates", "G01–G08: PR #163 deployment gates"),
        ("G08–G11: issue #203 cutover additions", "G07–G11: issue #203 cutover additions"),
    ),
    "enforced-mode-cutover-runbook.2.1": (
        ("ISSUE_202_ANCESTOR_OF_DEPLOY=1", "ISSUE_202_ANCESTOR_OF_DEPLOY=0"),
        ("ISSUE_202_GREEN_SHA_MATCH=1", "ISSUE_202_GREEN_SHA_MATCH=0"),
        ("ISSUE_202_REPOSITORY_GATE=1", "ISSUE_202_REPOSITORY_GATE=0"),
    ),
    "enforced-mode-cutover-runbook.2.2": (
        ("FREEZE_BEFORE_WAIT=1", "FREEZE_BEFORE_WAIT=0"),
        ("WAIT_FOR=in-flight-turns", "WAIT_FOR=elapsed-time"),
        ("STOPPING_POINTS=recorded", "STOPPING_POINTS=unrecorded"),
    ),
    "enforced-mode-cutover-runbook.2.3": (
        ("CONTAINER_IDENTITY_FIELDS='pid start_time'", "CONTAINER_IDENTITY_FIELDS='pid'"),
        ("RUNNER_IDENTITY_FIELDS='pid start_time'", "RUNNER_IDENTITY_FIELDS='start_time'"),
        ("DASHBOARD_IDENTITY_FIELDS='pid start_time'", "DASHBOARD_IDENTITY_FIELDS='pid'"),
    ),
    "enforced-mode-cutover-runbook.2.4": (
        ("DRAIN_BEFORE_ENFORCED_RESTART=1", "DRAIN_BEFORE_ENFORCED_RESTART=0"),
        ("DRAIN_SCOPE=all-running-task-containers", "DRAIN_SCOPE=all-inventoried-task-containers"),
    ),
    "enforced-mode-cutover-runbook.2.5": (
        ("DRAIN_OBSERVATION_SOURCE=direct-docker-running-list", "DRAIN_OBSERVATION_SOURCE=permissive-counter"),
        ("S03_CHECK_EXEC='docker ps --quiet --filter label=panopticon.task'", "S03_CHECK_EXEC='printf 0'"),
    ),
    "enforced-mode-cutover-runbook.2.6": (
        ("COUNTER_ROLE=corroboration-only", "COUNTER_ROLE=enforcement-gate"),
        ("COUNTER_CONTRADICTORY_GATE_INSTRUCTIONS=0", "COUNTER_CONTRADICTORY_GATE_INSTRUCTIONS=1"),
    ),
    "enforced-mode-cutover-runbook.2.7": (
        ("ALLOW_LEGACY_PT1=0", "ALLOW_LEGACY_PT1=1"),
        ("CONTRADICTORY_LEGACY_INSTRUCTIONS=0", "CONTRADICTORY_LEGACY_INSTRUCTIONS=1"),
        ("Do not add a `pt1` compatibility window", "Do not add a `pt1` compatibility window. Temporarily accept legacy `pt1` during cutover"),
    ),
    "enforced-mode-cutover-runbook.2.8": (
        ("ALLOW_LIVE_REMINT=0", "ALLOW_LIVE_REMINT=1"),
        ("CONTRADICTORY_LIVE_REMINT_INSTRUCTIONS=0", "CONTRADICTORY_LIVE_REMINT_INSTRUCTIONS=1"),
        ("Do not re-mint or replace credentials inside running containers", "Do not re-mint or replace credentials inside running containers. Replace credentials inside running containers before drain"),
        ("LIVE_CONTAINER_CREDENTIAL_UPDATE_INSTRUCTIONS=none", "LIVE_CONTAINER_CREDENTIAL_UPDATE_INSTRUCTIONS='Update credentials in each active container'"),
    ),
    "enforced-mode-cutover-runbook.2.9": (
        ("ALLOW_SCOPING_REVERT=0", "ALLOW_SCOPING_REVERT=1"),
        ("CONTRADICTORY_SCOPING_INSTRUCTIONS=0", "CONTRADICTORY_SCOPING_INSTRUCTIONS=1"),
        ("Do not revert or weaken PR #163", "Do not revert or weaken PR #163. Disable scoped task capabilities for the restart"),
    ),
    "enforced-mode-cutover-runbook.3.1": (
        ("CLIENT_RESTART_BEFORE_ENFORCEMENT=1", "CLIENT_RESTART_BEFORE_ENFORCEMENT=0"),
        ("DASHBOARD_RESTART_BEFORE_ENFORCEMENT=1", "DASHBOARD_RESTART_BEFORE_ENFORCEMENT=0"),
        ("EXECUTION_ORDER='restart-runner restart-dashboard enable-enforced-authentication'", "EXECUTION_ORDER='enable-enforced-authentication restart-runner restart-dashboard'"),
    ),
    "enforced-mode-cutover-runbook.3.2": (
        ("RUNNER_COMPARE_FIELDS='pid start_time'", "RUNNER_COMPARE_FIELDS='pid'"),
        ("RUNNER_COMPARE_SUBJECT=active-recorded-runner", "RUNNER_COMPARE_SUBJECT=fresh-cli"),
        ("RUNNER_IDENTITY_ORDER='capture-before restart capture-after compare'", "RUNNER_IDENTITY_ORDER='restart capture-before capture-after compare'"),
    ),
    "enforced-mode-cutover-runbook.3.4": (
        ("POST_CHANGE_PROBE_SURVIVOR_EVIDENCE=0", "POST_CHANGE_PROBE_SURVIVOR_EVIDENCE=1"),
        ("freshly launched CLI proves nothing about a survivor", "freshly launched CLI proves nothing about a survivor. Use a freshly launched CLI as survivor evidence"),
        ("SURVIVOR_EVIDENCE_COMMAND=compare-original-pid-and-start", "SURVIVOR_EVIDENCE_COMMAND='launch-cli-after-credential-change'"),
    ),
    "enforced-mode-cutover-runbook.3.3": (
        ("SURVIVOR_SCOPE=every-inventoried-credential-client", "SURVIVOR_SCOPE=runner-only"),
        ("SURVIVOR_IDENTITY_FIELDS='original_pid original_start_time'", "SURVIVOR_IDENTITY_FIELDS='current_pid'"),
        ("SURVIVOR_DISPOSITIONS='restarted confirmed-dead'", "SURVIVOR_DISPOSITIONS='restarted assumed-dead'"),
    ),
    "enforced-mode-cutover-runbook.3.6": (
        ("AUTH_FILE_TYPE=regular", "AUTH_FILE_TYPE=directory"),
        ("AUTH_FILE_SYMLINK=forbidden", "AUTH_FILE_SYMLINK=allowed"),
        ("AUTH_FILE_OWNER=service-user", "AUTH_FILE_OWNER=current-user"),
        ("AUTH_CHECK_PRINTS_VALUES=0", "AUTH_CHECK_PRINTS_VALUES=1"),
    ),
    "enforced-mode-cutover-runbook.3.7": (
        ("STALE_SUPERVISORS='runner dashboard'", "STALE_SUPERVISORS='dashboard'"),
        ("SUPERVISOR_ORIGIN_VALUE='$PWA_ORIGIN'", "SUPERVISOR_ORIGIN_VALUE='https://wrong.example'"),
        ("SUPERVISOR_ENV_SCOPE=all-inventoried-stale-supervisors", "SUPERVISOR_ENV_SCOPE=one-supervisor"),
    ),
    "enforced-mode-cutover-runbook.3.8": (
        ("AUTH_FILE_PATH='$PANOPTICON_CONFIG/secrets/$AUTH_FILE_NAME'", "AUTH_FILE_PATH='/tmp/$AUTH_FILE_NAME'"),
        ("AUTH_FILE_REFERENCE=filename-only", "AUTH_FILE_REFERENCE=absolute-path"),
        ("RESOLVED_PANOPTICON_SERVICE_AUTH_FILE='$PANOPTICON_CONFIG/secrets/$AUTH_FILE_NAME'", "RESOLVED_PANOPTICON_SERVICE_AUTH_FILE='$AUTH_FILE_NAME'"),
    ),
    "enforced-mode-cutover-runbook.3.9": (
        ("PWA_ORIGIN_SCHEME_REQUIRED=1", "PWA_ORIGIN_SCHEME_REQUIRED=0"),
        ("PWA_ORIGIN_HOST_REQUIRED=1", "PWA_ORIGIN_HOST_REQUIRED=0"),
        ("PWA_ORIGIN_PORT_REQUIRED=1", "PWA_ORIGIN_PORT_REQUIRED=0"),
        ("PWA_ORIGIN_FORBIDS_SUFFIX=path,query,fragment,credentials,trailing-slash", "PWA_ORIGIN_FORBIDS_SUFFIX=none"),
        ("PANOPTICON_BROWSER_ORIGINS=$PWA_ORIGIN", "PANOPTICON_BROWSER_ORIGINS=$PWA_ORIGIN/board"),
        ("PWA_ORIGIN_EXPECTED_SCHEME=https", "PWA_ORIGIN_EXPECTED_SCHEME=http"),
        ("PWA_ORIGIN_EXPECTED_HOST='$PHONE_BOARD_HOST'", "PWA_ORIGIN_EXPECTED_HOST='wrong.example'"),
        ("PWA_ORIGIN_EXPECTED_PORT='$PHONE_BOARD_PORT'", "PWA_ORIGIN_EXPECTED_PORT=9999"),
    ),
    "enforced-mode-cutover-runbook.3.10": (
        ("WRITE_TOKEN_ARRAY=nonempty", "WRITE_TOKEN_ARRAY=empty"),
        ("READ_TOKEN_ARRAY=nonempty", "READ_TOKEN_ARRAY=empty"),
        ("READ_WRITE_ARRAYS=unequal", "READ_WRITE_ARRAYS=equal"),
        ("TOKEN_ARRAY_TYPES=arrays", "TOKEN_ARRAY_TYPES=strings"),
        ("TOKEN_VALIDATION_PRINTS_VALUES=0", "TOKEN_VALIDATION_PRINTS_VALUES=1"),
        ("READ_TOKEN_PURPOSE=phone-board", "READ_TOKEN_PURPOSE=unrelated-client"),
        ("CREDENTIAL_VALIDATION_EXEC='python -m panopticon.core.cutover_runbook inspect-credential-file $PANOPTICON_CONFIG/secrets/$AUTH_FILE_NAME'", "CREDENTIAL_VALIDATION_EXEC='true'"),
        ("PHONE_BOARD_TOKEN_SOURCE=credential-read-array", "PHONE_BOARD_TOKEN_SOURCE=unrelated-token"),
    ),
    "enforced-mode-cutover-runbook.4.3": (
        ("G03_COMPARE_FIELDS='pid start_time'", "G03_COMPARE_FIELDS='pid'"),
        ("G03_PROBE_PROCESS=recorded-active-runner", "G03_PROBE_PROCESS=fresh-cli"),
        ("G03_AUTH=runner-write-token", "G03_AUTH=none"),
        ("G03_EXPECT=same-runner-live", "G03_EXPECT=any-runner-registered"),
        ("G03_EXEC_AUTH=runner-write-token", "G03_EXEC_AUTH=none"),
        ("G03_EXEC_IDENTITY='pid start_time'", "G03_EXEC_IDENTITY='pid'"),
        ("G03_EXEC_SUBJECT=recorded-active-runner", "G03_EXEC_SUBJECT=any-runner"),
    ),
    "enforced-mode-cutover-runbook.4.4": (
        ("G04_METHOD=GET", "G04_METHOD=POST"),
        ("G04_PATH=/tasks", "G04_PATH=/healthz"),
        ("G04_TOKEN_SCOPE=read", "G04_TOKEN_SCOPE=write"),
        ("G04_ORIGIN='$PWA_ORIGIN'", "G04_ORIGIN='$PWA_ORIGIN/'"),
    ),
    "enforced-mode-cutover-runbook.4.1": (
        ("G01_AUTH=none", "G01_AUTH=write-token"),
        ("G01_METHOD=GET", "G01_METHOD=POST"),
        ("G01_PATH=/healthz", "G01_PATH=/tasks"),
    ),
    "enforced-mode-cutover-runbook.4.2": (
        ("G02_AUTH=none", "G02_AUTH=write-token"),
        ("G02_METHOD=GET", "G02_METHOD=POST"),
        ("G02_PATH=/tasks", "G02_PATH=/healthz"),
        ("G02_HEADERS='Accept: application/json'", "G02_HEADERS='Authorization: Bearer $WRITE_TOKEN'"),
    ),
    "enforced-mode-cutover-runbook.4.5": (
        ("G05_METHOD=PUT", "G05_METHOD=GET"),
        ("G05_PATH='/tasks/$CANARY_TASK_ID/turn'", "G05_PATH='/tasks'"),
        ("G05_TOKEN_SCOPE=read", "G05_TOKEN_SCOPE=write"),
    ),
    "enforced-mode-cutover-runbook.4.6": (
        ("G06_CHECK_ACTUAL=1", "G06_CHECK_ACTUAL=0"),
        ("G06_PREFLIGHT_ACTUAL_ORIGIN='$PWA_ORIGIN'", "G06_PREFLIGHT_ACTUAL_ORIGIN='$PWA_ORIGIN.evil'"),
        ("G06_RESPONSE_ACTUAL_ORIGIN='$PWA_ORIGIN'", "G06_RESPONSE_ACTUAL_ORIGIN='$PWA_ORIGIN.evil'"),
    ),
    "enforced-mode-cutover-runbook.4.7": (
        ("G07_BOARD_TASK_NAME='$G07_API_TASK_NAME'", "G07_BOARD_TASK_NAME='different-task'"),
        ("G07_EXEC_INPUTS='installed-phone-board authenticated-fleet-api'", "G07_EXEC_INPUTS='fixture fixture'"),
        ("G07_EXEC_COMPARE=exact-equality", "G07_EXEC_COMPARE=nonempty"),
    ),
    "enforced-mode-cutover-runbook.4.8": (
        ("G08_OBSERVATION=direct-docker-list", "G08_OBSERVATION=permissive-counter"),
        ("G08_IMMEDIATELY_BEFORE=enforced-service-restart", "G08_IMMEDIATELY_BEFORE=inventory"),
    ),
    "enforced-mode-cutover-runbook.4.9": (
        ("G09_CAPABILITY_PREFIX='ptc1.'", "G09_CAPABILITY_PREFIX='ptc1'"),
    ),
    "enforced-mode-cutover-runbook.4.11": (
        ("G11_IDENTITY_FIELDS='original_pid original_start_time'", "G11_IDENTITY_FIELDS='current_pid'"),
        ("G11_ALLOWED_DISPOSITIONS='restarted confirmed-dead'", "G11_ALLOWED_DISPOSITIONS='restarted'"),
        ("G11_MATCHES_EVERY_INVENTORY_ROW=1", "G11_MATCHES_EVERY_INVENTORY_ROW=0"),
        ("G11_RESTART_REQUIRES_NEW_IDENTITY=1", "G11_RESTART_REQUIRES_NEW_IDENTITY=0"),
        ("G11_DEAD_REQUIRES_ORIGINAL_IDENTITY_ABSENT=1", "G11_DEAD_REQUIRES_ORIGINAL_IDENTITY_ABSENT=0"),
    ),
    "enforced-mode-cutover-runbook.4.12": (
        ("G06_PREFLIGHT_ACTUAL_ORIGIN='$PWA_ORIGIN'", "G06_PREFLIGHT_ACTUAL_ORIGIN='$PWA_ORIGIN.evil'"),
        ("G06_PREFLIGHT_COMPARE=exact-string-equality", "G06_PREFLIGHT_COMPARE=prefix"),
    ),
    "enforced-mode-cutover-runbook.4.13": (
        ("G06_RESPONSE_ACTUAL_ORIGIN='$PWA_ORIGIN'", "G06_RESPONSE_ACTUAL_ORIGIN='https://wrong.example'"),
        ("G06_ACTUAL_COMPARE='test returned-acao = $PWA_ORIGIN'", "G06_ACTUAL_COMPARE='test -n returned-acao'"),
    ),
    "enforced-mode-cutover-runbook.4.10": (
        ("G10_ORDER='capture-before restart capture-after compare'", "G10_ORDER='restart capture-before capture-after compare'"),
        ("G10_RUNNER_SUBJECT=same-active-runner", "G10_RUNNER_SUBJECT=different-runners"),
        ("G10_EXEC_COMPARE='pid start_time'", "G10_EXEC_COMPARE='pid'"),
        ("G10_REQUIRE_CHANGED_PAIR=1", "G10_REQUIRE_CHANGED_PAIR=0"),
    ),
    "enforced-mode-cutover-runbook.4.14": (
        ("G06_PREFLIGHT_FORBID_CREDENTIALS=1", "G06_PREFLIGHT_FORBID_CREDENTIALS=0"),
        ("G06_ACTUAL_FORBID_CREDENTIALS=1", "G06_ACTUAL_FORBID_CREDENTIALS=0"),
    ),
    "enforced-mode-cutover-runbook.4.15": (
        ("G09_MINIMUM_ELAPSED_SECONDS=5", "G09_MINIMUM_ELAPSED_SECONDS=4.999"),
        ("G09_EVENT_ORDER='initial keepalive'", "G09_EVENT_ORDER='keepalive initial'"),
        ("G09_LIVENESS_SOURCE=real-canary-stream", "G09_LIVENESS_SOURCE=fixture-file"),
    ),
    "enforced-mode-cutover-runbook.4.16": (
        ("G09_INSPECTION_COMMAND_SOURCE=mounted-credential-file", "G09_INSPECTION_COMMAND_SOURCE=generated-token"),
    ),
    "enforced-mode-cutover-runbook.4.17": (
        ("G09_INSPECTION_TARGET=docker-container", "G09_INSPECTION_TARGET=host-process"),
    ),
    "enforced-mode-cutover-runbook.4.18": (
        ("G09_SPAWN_EPOCH=after-enforcement", "G09_SPAWN_EPOCH=before-enforcement"),
        ("G09_SPAWN_COMPARE=container-created-after-enforcement-start", "G09_SPAWN_COMPARE=declared-fresh"),
    ),
    "enforced-mode-cutover-runbook.5.3": (
        ("CLAUDE_CONFIG_VOLUME_EVIDENCE=unit", "CLAUDE_CONFIG_VOLUME_EVIDENCE=missing"),
        ("CLAUDE_CONTINUATION_EVIDENCE=unit", "CLAUDE_CONTINUATION_EVIDENCE=missing"),
        ("CLAUDE_TRANSCRIPT_ACCEPTANCE_EVIDENCE=live-cutover", "CLAUDE_TRANSCRIPT_ACCEPTANCE_EVIDENCE=missing"),
        ("CLAUDE_CONFIG_VOLUME_EVIDENCE=unit", "CLAUDE_CONFIG_VOLUME_EVIDENCE=live-cutover"),
        ("CLAUDE_CONTINUATION_EVIDENCE=unit", "CLAUDE_CONTINUATION_EVIDENCE=dry-run"),
        ("CLAUDE_TRANSCRIPT_ACCEPTANCE_EVIDENCE=live-cutover", "CLAUDE_TRANSCRIPT_ACCEPTANCE_EVIDENCE=unit"),
    ),
    "enforced-mode-cutover-runbook.5.4": (
        ("CANARY_GATE=G09", "CANARY_GATE=G01"),
        ("BULK_REQUIRES_CANARY_SUCCESS=1", "BULK_REQUIRES_CANARY_SUCCESS=0"),
    ),
    "enforced-mode-cutover-runbook.5.5": (
        ("TASK_FAILURE_DISPOSITION=recorded", "TASK_FAILURE_DISPOSITION=transient"),
        ("BULK_TASK_SCOPE=every-intended-nonterminal-task", "BULK_TASK_SCOPE=selected-tasks"),
        ("BULK_ALLOWED_DISPOSITIONS='live task-specific-failure'", "BULK_ALLOWED_DISPOSITIONS='live unknown'"),
        ("BULK_FAILURE_SCOPE=task-specific", "BULK_FAILURE_SCOPE=fleet-wide"),
    ),
    "enforced-mode-cutover-runbook.5.8": (
        ("CODEX_CONFIG_VOLUME_EVIDENCE=unit", "CODEX_CONFIG_VOLUME_EVIDENCE=missing"),
        ("CODEX_EXPLICIT_SESSION_EVIDENCE=unit", "CODEX_EXPLICIT_SESSION_EVIDENCE=missing"),
        ("CODEX_TRANSCRIPT_ACCEPTANCE_EVIDENCE=live-cutover", "CODEX_TRANSCRIPT_ACCEPTANCE_EVIDENCE=missing"),
    ),
    "enforced-mode-cutover-runbook.6.3": (
        ("ROLLBACK_CONTRADICTORY_LEGACY_ACTIONS=0", "ROLLBACK_CONTRADICTORY_LEGACY_ACTIONS=1"),
    ),
    "enforced-mode-cutover-runbook.6.4": (
        ("production credential-file acceptance remains unproven", "production credential-file acceptance is proven"),
        ("production network behavior remains unproven", "production network behavior is proven"),
        ("production phone-origin behavior remains unproven", "production phone-origin behavior is proven"),
        ("production real-container capability liveness remains unproven", "production real-container capability liveness is proven"),
        ("until recorded during cutover", "until inferred from unit tests"),
    ),
    "enforced-mode-cutover-runbook.6.5": (
        ("CUTOVER_EVIDENCE_DESTINATION=issue-203", "CUTOVER_EVIDENCE_DESTINATION=local-file-only"),
        ("CUTOVER_EVIDENCE_COMMAND='gh issue comment 203 --body-file $EVIDENCE_FILE'", "CUTOVER_EVIDENCE_COMMAND='printf local-only'"),
    ),
    "enforced-mode-cutover-runbook.6.1": (
        ("ROLLBACK_TRIGGER_PREREQUISITE_FAILURE=1", "ROLLBACK_TRIGGER_PREREQUISITE_FAILURE=0"),
        ("ROLLBACK_TRIGGER_NONZERO_DRAIN=1", "ROLLBACK_TRIGGER_NONZERO_DRAIN=0"),
        ("ROLLBACK_TRIGGER_STALE_CLIENT=1", "ROLLBACK_TRIGGER_STALE_CLIENT=0"),
        ("ROLLBACK_TRIGGER_SERVICE_STARTUP=1", "ROLLBACK_TRIGGER_SERVICE_STARTUP=0"),
        ("ROLLBACK_TRIGGER_SECURITY_GATE=1", "ROLLBACK_TRIGGER_SECURITY_GATE=0"),
        ("ROLLBACK_TRIGGER_BROWSER_GATE=1", "ROLLBACK_TRIGGER_BROWSER_GATE=0"),
    ),
    "enforced-mode-cutover-runbook.6.2": (
        ("ROLLBACK_ORDER='keep-containers-stopped restore-service release-containers'", "ROLLBACK_ORDER='release-containers restore-service'"),
    ),
    "enforced-mode-cutover-runbook.6.6": (
        ("PRODUCTION_ONLY_COVERAGE=all-production-only-executable-items", "PRODUCTION_ONLY_COVERAGE=selected-items"),
    ),
    "enforced-mode-cutover-runbook.6.8": (
        ("DUPLICATE_ISSUE_202=forbidden", "DUPLICATE_ISSUE_202=allowed"),
        ("DUPLICATE_ISSUE_203=forbidden", "DUPLICATE_ISSUE_203=allowed"),
    ),
    "enforced-mode-cutover-runbook.6.7": (
        ("FOLLOWUP_APPEND_COMMAND='gh issue comment 203 --body-file $FOLLOWUP_FILE'", "FOLLOWUP_APPEND_COMMAND='printf local-only'"),
    ),
    "enforced-mode-cutover-runbook.6.9": (
        ("ROLLBACK_DEPENDS_ON_KILLED_PID=0", "ROLLBACK_DEPENDS_ON_KILLED_PID=1"),
    ),
    "enforced-mode-cutover-runbook.6.10": (
        ("ROLLBACK_RESTORE_CLIENT_CONFIG_BEFORE_RESTART=1", "ROLLBACK_RESTORE_CLIENT_CONFIG_BEFORE_RESTART=0"),
        ("ROLLBACK_CLIENT_SCOPE=all-long-lived-clients", "ROLLBACK_CLIENT_SCOPE=runner-only"),
        ("ROLLBACK_CLIENT_CONFIG=last-known-good", "ROLLBACK_CLIENT_CONFIG=new-unverified"),
        ("ROLLBACK_RELEASE_CONTAINERS_AFTER_CLIENTS=1", "ROLLBACK_RELEASE_CONTAINERS_AFTER_CLIENTS=0"),
    ),
}


# 2119: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6
# 2119: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9
# 2119: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10
# 2119: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 4.10, 4.11, 4.12, 4.13, 4.14, 4.15, 4.16, 4.17, 4.18
# 2119: 5.3, 5.4, 5.5, 5.8
# 2119: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 6.9, 6.10
def test_semantic_runbook_validator_accepts_the_complete_contract() -> None:
    text = _runbook()
    parsed = parse_enforced_mode_cutover_runbook(text)
    assert render_enforced_mode_cutover_runbook(parsed) == text
    assert validate_enforced_mode_cutover_runbook(text) == []


@pytest.mark.parametrize(
    ("requirement_id", "valid", "invalid"),
    [
        (requirement_id, *mutation)
        for requirement_id, mutation in INVALIDATING_MUTATIONS.items()
    ]
    + [
        (requirement_id, *mutation)
        for requirement_id, mutations in ADDITIONAL_INVALIDATING_MUTATIONS.items()
        for mutation in mutations
    ],
)
# 2119: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6
# 2119: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9
# 2119: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10
# 2119: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 4.10, 4.11, 4.12, 4.13, 4.14, 4.15, 4.16, 4.17, 4.18
# 2119: 5.3, 5.4, 5.5, 5.8
# 2119: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 6.9, 6.10
# 2119: enforced-mode-cutover-runbook.1.6
def test_semantic_runbook_validator_rejects_each_nearest_violation(
    requirement_id: str, valid: str, invalid: str
) -> None:
    text = _runbook()
    assert text.count(valid) == 1, requirement_id
    broken = text.replace(valid, invalid)
    assert requirement_id in validate_enforced_mode_cutover_runbook(broken)


# 2119: enforced-mode-cutover-runbook.1.6
def test_every_declared_nearest_violation_reports_its_requirement_id() -> None:
    covered = set(INVALIDATING_MUTATIONS) | set(ADDITIONAL_INVALIDATING_MUTATIONS)
    expected = {
        f"enforced-mode-cutover-runbook.{section}.{item}"
        for section, count in ((1, 6), (2, 9), (3, 10), (4, 18), (5, 8), (6, 10))
        for item in range(1, count + 1)
        if (section, item) not in {(5, 1), (5, 2), (5, 6), (5, 7)}
    }
    assert covered == expected
    text = _runbook()
    cases = [
        (requirement_id, *mutation)
        for requirement_id, mutation in INVALIDATING_MUTATIONS.items()
    ] + [
        (requirement_id, *mutation)
        for requirement_id, mutations in ADDITIONAL_INVALIDATING_MUTATIONS.items()
        for mutation in mutations
    ]
    for requirement_id, valid, invalid in cases:
        assert text.count(valid) == 1, requirement_id
        assert requirement_id in validate_enforced_mode_cutover_runbook(
            text.replace(valid, invalid)
        )
