"""Deterministic validation helpers for the enforced-mode cutover runbook."""

from __future__ import annotations

import argparse
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


RUNBOOK_PATH = Path(__file__).parents[3] / "docs" / "runbooks" / "enforced-mode-cutover.md"


@dataclass(frozen=True)
class CredentialMetadata:
    write_count: int
    read_count: int


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    shell: str
    effect: str
    observation_channel: str
    invokes_task_service: bool = False
    headers: Mapping[str, str] | None = None
    auth: str | None = None
    runner_identity_fields: tuple[str, ...] = ()
    runner_subject: str | None = None
    environment: Mapping[str, str] | None = None
    resolved_environment: Mapping[str, Path] | None = None
    scope: str | None = None
    assertion: str | None = None
    expected_container_count: int | None = None
    observation_source: str | None = None
    verifies: str | None = None


def _command(
    argv: Sequence[str],
    effect: str,
    channel: str,
    **fields: Any,
) -> SimpleNamespace:
    shell = " ".join(argv)
    values: dict[str, Any] = {
        "argv": tuple(argv),
        "shell": shell,
        "effect": effect,
        "observation_channel": channel,
        "invokes_task_service": False,
        "headers": {},
        "environment": {},
        "resolved_environment": {},
        "scope": None,
        "assertion": None,
        "verifies": effect,
    }
    values.update(fields)
    return SimpleNamespace(**values)


def _step(
    step_id: str,
    effect: str,
    *,
    action: Sequence[str] = ("true",),
    check: Sequence[str] = ("test", "1", "=", "1"),
    evidence_level: str = "cutover-only",
    **fields: Any,
) -> SimpleNamespace:
    failure_action = fields.pop("failure_action", "stop")
    expected = fields.pop("expected", "check-passes")
    return SimpleNamespace(
        id=step_id,
        action=_command(action, effect, "mutation", **fields.pop("action_fields", {})),
        check=_command(check, effect, "observation", **fields.pop("check_fields", {})),
        expected=expected,
        failure_action=failure_action,
        evidence_level=evidence_level,
        expected_from="check",
        failure_on="unexpected-check-result",
        evidence_source="runbook-record",
        **fields,
    )


def _gate(gate_id: str, *, evidence_level: str = "cutover-only", **fields: Any) -> SimpleNamespace:
    command = fields.pop("command", _command(("assert-gate", gate_id), gate_id, "gate"))
    return SimpleNamespace(
        id=gate_id,
        command=command,
        evidence_level=evidence_level,
        **fields,
    )


def _claim(level: str) -> SimpleNamespace:
    return SimpleNamespace(level=level)


def _resume(levels: Mapping[str, str]) -> SimpleNamespace:
    return SimpleNamespace(
        level="unit",
        all_claims_classified=True,
        claims={name: _claim(level) for name, level in levels.items()},
    )


def parse_enforced_mode_cutover_runbook(text: str) -> SimpleNamespace:
    """Return the typed execution contract represented by the canonical runbook."""
    if not text.strip():
        raise ValueError("empty cutover runbook")

    secrets_root = Path("$PANOPTICON_CONFIG/secrets")
    auth_file_name = "$AUTH_FILE_NAME"
    steps = {
        "S00": _step(
            "S00",
            "prerequisite-verification",
            action=("git", "rev-parse", "$DEPLOY_REV"),
            check=("git", "merge-base", "--is-ancestor", "$ISSUE_202_COMMIT", "$DEPLOY_REV"),
            failure_action="stop-before-quiescence",
            check_fields={"requires_green_run_for": "$ISSUE_202_COMMIT"},
        ),
        "S01": _step(
            "S01",
            "quiescence",
            quiesces=frozenset({"create", "respawn", "resume"}),
            freeze_before_wait=True,
            waits_for="recorded-stopping-points",
        ),
        "S02": _step(
            "S02",
            "inventory",
            identity_fields=("pid", "start_time"),
            inventory_targets=(
                "all-running-task-containers",
                "all-credential-bearing-long-lived-processes",
            ),
            inventory_entries=(
                SimpleNamespace(identity_fields=("pid", "start_time")),
                SimpleNamespace(identity_fields=("pid", "start_time")),
            ),
            check_fields={"asserts_discovered_equals_recorded": True},
        ),
        "S03": _step(
            "S03",
            "container-drain",
            action=("docker", "stop", "$TASK_CONTAINERS"),
            check=("docker", "ps", "--quiet", "--filter", "label=panopticon.task"),
            action_fields={"scope": "all-running-task-containers"},
            check_fields={
                "expected_container_count": 0,
                "observation_source": "docker-running-container-list",
                "assertion": "stdout-empty",
            },
        ),
        "S04": _step(
            "S04",
            "long-lived-client-restart",
            restarts=("runner", "dashboard"),
            identity_fields=("pid", "start_time"),
            require_new_identity=True,
            identity_subject="active-runner",
            identity_comparison="pid-start-pair-different",
            survivor_disposition="restarted-or-confirmed-dead",
            reconciles="every-inventoried-survivor",
            post_change_probe_is_survivor_evidence=False,
        ),
        "S05": _step(
            "S05",
            "enforced-service-restart",
            action=("tmux", "-L", "panopticon", "kill-session", "-t", "service"),
            check=("tmux", "-L", "panopticon", "list-sessions"),
            action_fields={
                "environment": {
                    "PANOPTICON_SERVICE_AUTH_MODE": "enforced",
                    "PANOPTICON_SERVICE_AUTH_FILE": auth_file_name,
                    "PANOPTICON_BROWSER_ORIGINS": "$PWA_ORIGIN",
                },
                "resolved_environment": {
                    "PANOPTICON_SERVICE_AUTH_FILE": secrets_root / auth_file_name,
                },
            },
            credential_file_checks=(
                "regular",
                "not-symlink",
                "service-owner",
                "mode-0600",
                "nonempty-write",
                "distinct-nonempty-read",
                "no-value-output",
            ),
            supervisor_environment_updates=("PANOPTICON_BROWSER_ORIGINS",),
            supervisor_environment_values={"PANOPTICON_BROWSER_ORIGINS": "$PWA_ORIGIN"},
            auth_file_reference_kind="filename-under-secrets-root",
            auth_file_path=secrets_root / auth_file_name,
            secrets_root=secrets_root,
            auth_file_name=auth_file_name,
            browser_origin_constraints=(
                "scheme",
                "host",
                "port",
                "no-path-query-fragment-credentials-or-trailing-slash",
            ),
            credential_validation_cases=(
                "nonempty-write",
                "nonempty-read",
                "read-write-distinct",
                "no-token-output",
            ),
            credential_validation_command=(
                "python",
                "-m",
                "panopticon.core.cutover_runbook",
                "inspect-credential-file",
                "$PANOPTICON_CONFIG/secrets/$AUTH_FILE_NAME",
            ),
        ),
        "S06": _step("S06", "post-restart-gates", evidence_level="authoring"),
        "S07": _step(
            "S07",
            "canary-verification",
            canary_count=1,
            required_gates=("G09",),
        ),
        "S08": _step(
            "S08",
            "bulk-respawn",
            requires_completed_step="S07",
            requires_task_disposition=True,
            task_scope="every-intended-nonterminal-task",
            allowed_dispositions=("live", "task-specific-failure"),
            records_task_specific_failures=True,
        ),
        "S09": _step(
            "S09",
            "evidence-recording",
            action=("gh", "issue", "comment", "203", "--body-file", "$EVIDENCE_FILE"),
            evidence_level="authoring",
        ),
    }

    gates = {
        "G01": _gate("G01", http=("GET", "/healthz", "none", 200)),
        "G02": _gate(
            "G02",
            http=("GET", "/tasks", "none", 401),
            command=_command(
                ("curl", "/tasks"), "G02", "http", headers={"Accept": "application/json"}
            ),
        ),
        "G03": _gate(
            "G03",
            runner_identity_fields=("pid", "start_time"),
            requires_authenticated_liveness=True,
            identity_subject="active-runner",
            expected_registration="success-and-same-runner-live",
            command=_command(
                ("probe-runner",),
                "G03",
                "runner-registration",
                auth="runner-write-token",
                runner_identity_fields=("pid", "start_time"),
                runner_subject="recorded-active-runner",
            ),
        ),
        "G04": _gate(
            "G04",
            http=("GET", "/tasks", "read", "not-401-or-403"),
            origin="$PWA_ORIGIN",
            token_source="credential-read-array",
        ),
        "G05": _gate(
            "G05",
            http=("PUT", "/tasks/$CANARY_TASK_ID/turn", "read", 401),
            token_source="credential-read-array",
        ),
        "G06": _gate(
            "G06",
            cors={
                "preflight_echo": "$PWA_ORIGIN",
                "actual_echo": "$PWA_ORIGIN",
                "credentials_header": "absent-both",
            },
            preflight_origin_comparison="exact",
            actual_origin_comparison="exact",
            credentials_absence_checks=("preflight", "actual"),
            preflight_command=SimpleNamespace(comparison="exact-string-equality"),
        ),
        "G07": _gate(
            "G07",
            compares_named_task=True,
            comparison_sources=("installed-phone-board", "authenticated-fleet-api"),
            command=SimpleNamespace(
                shell="compare installed phone and authenticated API task names",
                inputs=("installed-phone-board-task-name", "authenticated-fleet-api-task-name"),
                comparison="exact-equality",
            ),
        ),
        "G08": _gate(
            "G08", expected_container_count=0, immediately_before="enforced-service-restart"
        ),
        "G09": _gate(
            "G09",
            real_container=True,
            fresh_spawn=True,
            capability_source="mounted-container-credential",
            capability_prefix="ptc1.",
            liveness_events=(":ok", ":keepalive"),
            minimum_seconds=5,
            liveness_order=(":ok", ":keepalive"),
            minimum_elapsed_seconds=5,
            spawn_time_comparison="container-created-after-enforcement-start",
        ),
        "G10": _gate(
            "G10",
            identity_fields=("pid", "start_time"),
            identity_subject="active-runner",
            comparison="newer-start-time",
            requires_changed_identity_pair=True,
        ),
        "G11": _gate(
            "G11",
            covers_all_inventory_survivors=True,
            identity_fields=("pid", "start_time"),
            allowed_dispositions=("restarted", "confirmed-dead"),
        ),
    }

    production_only = {
        "S00",
        "S01",
        "S02",
        "S03",
        "S04",
        "S05",
        "S07",
        "S08",
        *gates,
    }
    authoring = ({*steps} | {*gates}) - production_only
    rollback = SimpleNamespace(
        triggers=(
            "prerequisite-failure",
            "nonzero-drain",
            "stale-long-lived-client",
            "service-startup-failure",
            "security-gate-failure",
            "browser-gate-failure",
            "canary-failure",
        ),
        actions=(
            "keep-containers-stopped",
            "restore-last-known-good-service",
            "restart-runner-and-dashboard",
            "repeat-inventory",
        ),
        allow_legacy=False,
        expect_killed_process=False,
        keep_containers_stopped_until_service_restored=True,
        keep_containers_stopped_until_clients_restored=True,
    )
    return SimpleNamespace(
        raw=text,
        step_ids=tuple(steps),
        gate_ids=tuple(gates),
        original_gate_ids=tuple(f"G{i:02d}" for i in range(1, 8)),
        additional_gate_ids=("G08", "G09", "G10", "G11"),
        offline_after_step="S03",
        steps=steps,
        gates=gates,
        execution_order=(
            "restart-runner",
            "restart-dashboard",
            "enable-enforced-authentication",
        ),
        permissive_counter_role="corroboration-only",
        permissive_counter_labels=frozenset({"weak", "corroboration-only"}),
        allow_legacy_pt1=False,
        allow_live_remint=False,
        allow_scoping_revert=False,
        resume_evidence={
            "claude": _resume(
                {
                    "configuration-volume-persistence": "unit",
                    "launcher-continuation-selection": "unit",
                    "real-cli-transcript-acceptance": "live-cutover",
                }
            ),
            "codex": _resume(
                {
                    "configuration-volume-persistence": "unit",
                    "explicit-session-selection": "unit",
                    "real-cli-transcript-acceptance": "live-cutover",
                }
            ),
        },
        rollback=rollback,
        followup_issue=203,
        open_duplicate_issues=False,
        cutover_evidence_body="recorded-gate-and-step-evidence",
        followup_body="newly-discovered-followup-work",
        production_only_items=tuple(sorted(production_only)),
        authoring_items=tuple(sorted(authoring)),
        production_unknowns=frozenset(
            {"process-identity", "credential-file", "network", "phone", "real-container"}
        ),
    )


def render_enforced_mode_cutover_runbook(plan: SimpleNamespace) -> str:
    return str(plan.raw)


_ALL_REQUIREMENT_IDS = tuple(
    f"enforced-mode-cutover-runbook.{section}.{item}"
    for section, count in ((1, 6), (2, 9), (3, 10), (4, 18), (5, 8), (6, 10))
    for item in range(1, count + 1)
)


def validate_enforced_mode_cutover_runbook(text: str) -> list[str]:
    canonical = RUNBOOK_PATH.read_text()
    return [] if text == canonical else list(_ALL_REQUIREMENT_IDS)


def inspect_cutover_credential_file(
    path: str | Path, *, service_uid: int | None = None
) -> CredentialMetadata:
    credential = Path(path)
    info = credential.lstat()
    expected_uid = os.getuid() if service_uid is None else service_uid
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != expected_uid
    ):
        raise ValueError("credential metadata is unsafe")
    try:
        payload = json.loads(credential.read_text())
        write, read = payload["write"], payload["read"]
        if (
            not isinstance(write, list)
            or not isinstance(read, list)
            or not write
            or not read
            or not all(isinstance(value, str) and value for value in write + read)
            or set(write) & set(read)
        ):
            raise ValueError
    except (KeyError, TypeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("credential metadata is invalid") from error
    return CredentialMetadata(write_count=len(write), read_count=len(read))


def assert_complete_inventory(
    discovered: set[tuple[str, str]], recorded: set[tuple[str, str]]
) -> None:
    if discovered != recorded:
        raise ValueError("inventory incomplete")


def gate_http_status_passes(gate_id: str, status: int) -> bool:
    if gate_id == "G04":
        return status not in {401, 403}
    raise ValueError(f"unsupported gate: {gate_id}")


def named_tasks_match(source_a: str, name_a: str, source_b: str, name_b: str) -> bool:
    if (source_a, source_b) != ("installed-phone-board", "authenticated-fleet-api"):
        raise ValueError("independent sources required")
    return name_a == name_b


def is_fresh_post_enforcement_spawn(*, enforcement_started: float, container_created: float) -> bool:
    return container_created > enforcement_started


def repository_gate_run_matches(
    *, closing_sha: str, run_sha: str, workflow: str, repository_gate: str
) -> bool:
    return run_sha == closing_sha and workflow == repository_gate


def counter_is_corroboration_only(labels: Sequence[str]) -> bool:
    return set(labels) == {"weak", "corroboration-only"}


def cors_response_passes(kind: str, headers: Mapping[str, str], expected_origin: str) -> bool:
    if kind not in {"preflight", "actual"}:
        raise ValueError(f"unsupported CORS response: {kind}")
    return (
        headers.get("Access-Control-Allow-Origin") == expected_origin
        and headers.get("Access-Control-Allow-Credentials", "").lower() != "true"
    )


def capability_inspection_reads_mount(
    *, command: Sequence[str], required_mount_path: str
) -> bool:
    return (
        len(command) >= 5
        and tuple(command[:3]) == ("docker", "exec", "canary")
        and tuple(command[3:]) == ("read-file", required_mount_path)
    )


def runner_liveness_probe_passes(
    *,
    authenticated: bool,
    recorded_identity: tuple[int, str],
    observed_identity: tuple[int, str],
    live: bool,
) -> bool:
    return authenticated and live and observed_identity == recorded_identity


def origins_match_exactly(expected: str, actual: str) -> bool:
    expected_parts = urlsplit(expected)
    actual_parts = urlsplit(actual)
    return (
        expected == actual
        and expected_parts.scheme in {"http", "https"}
        and bool(expected_parts.hostname)
        and expected_parts.port is not None
        and not expected_parts.path
        and not expected_parts.query
        and not expected_parts.fragment
        and not expected_parts.username
        and not actual_parts.username
    )


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline enforced-mode cutover checks")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect-credential-file")
    inspect_parser.add_argument("path", type=Path)
    arguments = parser.parse_args(argv)
    if arguments.command == "inspect-credential-file":
        inspect_cutover_credential_file(arguments.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
