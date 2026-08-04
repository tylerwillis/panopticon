from __future__ import annotations

from pathlib import Path

import pytest

from panopticon.core.cutover_runbook import (
    assert_complete_inventory,
    cors_response_passes,
    counter_is_corroboration_only,
    capability_inspection_reads_mount,
    origins_match_exactly,
    parse_enforced_mode_cutover_runbook,
    repository_gate_run_matches,
    runner_liveness_probe_passes,
    validate_enforced_mode_cutover_runbook,
)

ROOT = Path(__file__).parents[1]
RUNBOOK = ROOT / "docs" / "runbooks" / "enforced-mode-cutover.md"


# 2119-spec: enforced-mode-cutover-runbook


# 2119: enforced-mode-cutover-runbook.2.1
def test_issue_202_green_run_must_be_the_repository_gate_for_the_closing_sha() -> None:
    assert repository_gate_run_matches(
        closing_sha="abc123", run_sha="abc123", workflow="ci", repository_gate="ci"
    )
    assert not repository_gate_run_matches(
        closing_sha="abc123", run_sha="abc123", workflow="docs", repository_gate="ci"
    )
    assert not repository_gate_run_matches(
        closing_sha="abc123", run_sha="def456", workflow="ci", repository_gate="ci"
    )


# 2119: enforced-mode-cutover-runbook.2.3
def test_inventory_completeness_covers_arbitrary_discovered_credential_processes() -> None:
    discovered = {
        ("container", "c1"),
        ("runner", "r1"),
        ("dashboard", "d1"),
        ("credential-bearing-long-lived-process", "future-supervisor"),
    }
    assert_complete_inventory(discovered, discovered.copy())
    with pytest.raises(ValueError, match="inventory incomplete"):
        assert_complete_inventory(
            discovered,
            discovered - {("credential-bearing-long-lived-process", "future-supervisor")},
        )


# 2119: enforced-mode-cutover-runbook.2.6
@pytest.mark.parametrize("contradiction", ["strong", "primary", "decisive", "gate"])
def test_permissive_counter_rejects_every_noncorroborating_role(contradiction: str) -> None:
    plan = parse_enforced_mode_cutover_runbook(RUNBOOK.read_text())
    assert plan.permissive_counter_labels == frozenset({"weak", "corroboration-only"})
    assert counter_is_corroboration_only(["weak", "corroboration-only"])
    assert not counter_is_corroboration_only(
        ["weak", "corroboration-only", contradiction]
    )


# 2119: enforced-mode-cutover-runbook.4.14
@pytest.mark.parametrize("response_kind", ["preflight", "actual"])
def test_g06_rejects_credentials_true_in_either_response(response_kind: str) -> None:
    safe = {"Access-Control-Allow-Origin": "https://phone.example:443"}
    unsafe = {**safe, "Access-Control-Allow-Credentials": "true"}
    assert cors_response_passes(response_kind, safe, "https://phone.example:443")
    assert not cors_response_passes(response_kind, unsafe, "https://phone.example:443")


# 2119: enforced-mode-cutover-runbook.4.16
def test_g09_capability_inspection_rejects_every_nonmounted_source() -> None:
    mount_path = "/run/panopticon/task-capability"
    assert capability_inspection_reads_mount(
        command=("docker", "exec", "canary", "read-file", mount_path),
        required_mount_path=mount_path,
    )
    for command in (
        ("generate-token",),
        ("docker", "exec", "canary", "read-env", "TASK_CAPABILITY"),
        ("read-file", "/tmp/fixture-token"),
        ("docker", "cp", mount_path, "/tmp/copied-token"),
        ("docker", "exec", "canary", "test", "-e", mount_path),
    ):
        assert not capability_inspection_reads_mount(
            command=command,
            required_mount_path=mount_path,
        )


# 2119: enforced-mode-cutover-runbook.4.3
def test_g03_requires_authenticated_liveness_for_the_recorded_runner_identity() -> None:
    recorded = (321, "2026-08-04T10:00:00Z")
    assert runner_liveness_probe_passes(
        authenticated=True,
        recorded_identity=recorded,
        observed_identity=recorded,
        live=True,
    )
    assert not runner_liveness_probe_passes(
        authenticated=False,
        recorded_identity=recorded,
        observed_identity=recorded,
        live=True,
    )
    assert not runner_liveness_probe_passes(
        authenticated=True,
        recorded_identity=recorded,
        observed_identity=(322, "2026-08-04T10:01:00Z"),
        live=True,
    )
    assert not runner_liveness_probe_passes(
        authenticated=True,
        recorded_identity=recorded,
        observed_identity=recorded,
        live=False,
    )


# 2119: enforced-mode-cutover-runbook.4.4
def test_g04_origin_comparison_rejects_near_miss_scheme_host_and_port() -> None:
    expected = "https://phone.example:443"
    assert origins_match_exactly(expected, expected)
    for near_miss in (
        "http://phone.example:443",
        "https://other.example:443",
        "https://phone.example:444",
        "https://phone.example",
        "https://phone.example:443/",
    ):
        assert not origins_match_exactly(expected, near_miss)


# 2119: enforced-mode-cutover-runbook.5.8
def test_codex_resume_claims_have_their_exact_honest_evidence_levels() -> None:
    text = RUNBOOK.read_text()
    plan = parse_enforced_mode_cutover_runbook(text)
    assert {
        name: claim.level for name, claim in plan.resume_evidence["codex"].claims.items()
    } == {
        "configuration-volume-persistence": "unit",
        "explicit-session-selection": "unit",
        "real-cli-transcript-acceptance": "live-cutover",
    }
    for valid, wrong in (
        ("CODEX_CONFIG_VOLUME_EVIDENCE=unit", "CODEX_CONFIG_VOLUME_EVIDENCE=live-cutover"),
        ("CODEX_EXPLICIT_SESSION_EVIDENCE=unit", "CODEX_EXPLICIT_SESSION_EVIDENCE=dry-run"),
        (
            "CODEX_TRANSCRIPT_ACCEPTANCE_EVIDENCE=live-cutover",
            "CODEX_TRANSCRIPT_ACCEPTANCE_EVIDENCE=unit",
        ),
    ):
        assert text.count(valid) == 1
        assert "enforced-mode-cutover-runbook.5.8" in validate_enforced_mode_cutover_runbook(
            text.replace(valid, wrong)
        )
