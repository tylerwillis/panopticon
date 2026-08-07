"""Structural parsing and offline checks for the enforced-mode cutover runbook."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

RUNBOOK_PATH = Path(__file__).parents[3] / "docs" / "runbooks" / "enforced-mode-cutover.md"


@dataclass(frozen=True)
class CredentialMetadata:
    write_count: int
    read_count: int


@dataclass(frozen=True)
class ProcedureItem:
    item_id: str
    title: str
    action: str
    check: str
    expected: str
    failure_action: str
    evidence_status: str


@dataclass(frozen=True)
class CutoverPlan:
    steps: tuple[ProcedureItem, ...]
    gates: tuple[ProcedureItem, ...]


def assert_fresh_container_id(container_id: str, inventory_path: str | Path) -> None:
    """Reject a container ID that existed in the pre-enforcement inventory."""

    candidate = container_id.strip()
    if not candidate:
        raise ValueError("canary container ID is empty")
    existing = {
        line.strip() for line in Path(inventory_path).read_text().splitlines() if line.strip()
    }
    if candidate in existing:
        raise ValueError("canary container predates enforcement")


def assert_runner_set(path: str | Path, expected_runner_id: str | None = None) -> None:
    """Require the complete live-runner registry to be empty or one expected identity."""

    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError("runner registry is invalid")
    runner_ids = [item.get("id") for item in payload]
    expected = [] if expected_runner_id is None else [expected_runner_id]
    if runner_ids != expected:
        raise ValueError("runner registry does not match the controlled set")


def process_start(pid: int, *, run: Any = subprocess.run) -> str | None:
    """Read one process start identity without exposing its environment."""

    completed = run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    value = completed.stdout.strip()
    return value or None


def assert_process_replaced(pid: int, original_start: str, *, run: Any = subprocess.run) -> None:
    """Require an original PID/start-time pair to be absent or reused by another process."""

    if process_start(pid, run=run) == original_start.strip():
        raise ValueError("original process identity is still alive")


def assert_runner_process(
    pid: int,
    recorded_start: str,
    command_path: str | Path,
    config_path: str,
    auth_file: str,
    runner_id: str,
    *,
    run: Any = subprocess.run,
) -> None:
    """Bind the live pane process identity to its exact exec-time runner ID."""

    if process_start(pid, run=run) != recorded_start.strip():
        raise ValueError("runner process identity changed")
    tokens = shlex.split(Path(command_path).read_text().strip())
    required_environment = {
        f"PANOPTICON_CONFIG={config_path}",
        f"PANOPTICON_SERVICE_AUTH_FILE={auth_file}",
        "PANOPTICON_SERVICE_AUTH_MODE=enforced",
        f"PANOPTICON_RUNNER_ID={runner_id}",
    }
    expected_argv = ["uv", "run", "python", "-m", "panopticon.sessionservice.host"]
    if (
        tokens[:2] != ["exec", "env"]
        or not required_environment.issubset(tokens[2:])
        or tokens[-len(expected_argv) :] != expected_argv
    ):
        raise ValueError("runner start command is not exec-bound to the expected identity")


def assert_dashboard_process(
    pid: int,
    recorded_start: str,
    command_path: str | Path,
    config_path: str,
    auth_file: str,
    browser_origin: str,
    service_url: str,
    *,
    run: Any = subprocess.run,
) -> None:
    """Bind the live pane process identity to the enforced dashboard command."""

    if process_start(pid, run=run) != recorded_start.strip():
        raise ValueError("dashboard process identity changed")
    tokens = shlex.split(Path(command_path).read_text().strip())
    try:
        exec_index = tokens.index("exec")
    except ValueError as error:
        raise ValueError(
            "dashboard start command is not exec-bound to enforced configuration"
        ) from error
    required_environment = {
        f"PANOPTICON_CONFIG={config_path}",
        f"PANOPTICON_SERVICE_AUTH_FILE={auth_file}",
        "PANOPTICON_SERVICE_AUTH_MODE=enforced",
        f"PANOPTICON_BROWSER_ORIGINS={browser_origin}",
    }
    expected_argv = ["uv", "run", "panopticon", "--service-url", service_url, "dashboard"]
    if (
        tokens[exec_index : exec_index + 2] != ["exec", "env"]
        or not required_environment.issubset(tokens[exec_index + 2 :])
        or tokens[-len(expected_argv) :] != expected_argv
    ):
        raise ValueError("dashboard start command is not exec-bound to enforced configuration")


def assert_container_started_after(container_started: str, enforcement_started: str) -> None:
    """Require Docker's start timestamp to be strictly after enforced service launch began."""

    def parse(value: str) -> dt.datetime:
        return dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))

    if parse(container_started) <= parse(enforcement_started):
        raise ValueError("canary container did not start after enforcement began")


_STEP = re.compile(
    r"^## (?P<id>S\d{2}) — (?P<title>[^\n]+)\n(?P<body>.*?)(?=^## S\d{2} — |^## Rollback|\Z)",
    re.MULTILINE | re.DOTALL,
)
_GATE = re.compile(
    r"^### (?P<id>G\d{2}) — (?P<title>[^\n]+)\n(?P<body>.*?)(?=^### G\d{2} — |^## |\Z)",
    re.MULTILINE | re.DOTALL,
)


def _section(body: str, heading: str, *, level: int) -> str:
    marker = "#" * level
    match = re.search(
        rf"^{marker} {re.escape(heading)}\n(?P<body>.*?)(?=^{marker} |^{'#' * (level - 1)} |\Z)",
        body,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise ValueError(f"missing {heading} section")
    return cast(str, match.group("body")).strip()


def _shell(section: str) -> str:
    matches = cast(list[str], re.findall(r"```(?:sh|bash)\n(.*?)```", section, re.DOTALL))
    if len(matches) != 1 or not matches[0].strip():
        raise ValueError("expected exactly one nonempty shell block")
    return matches[0].strip()


def _parse_item(match: re.Match[str], *, level: int) -> ProcedureItem:
    body = match.group("body")
    return ProcedureItem(
        item_id=match.group("id"),
        title=match.group("title"),
        action=_shell(_section(body, "Action" if level == 3 else "Command", level=level)),
        check=_shell(_section(body, "Check", level=level)),
        expected=_section(body, "Expected", level=level),
        failure_action=_section(body, "Failure action", level=level),
        evidence_status=_section(body, "Evidence status", level=level),
    )


def parse_enforced_mode_cutover_runbook(text: str) -> CutoverPlan:
    """Parse the actual Markdown procedure and its shell blocks."""

    if not text.strip():
        raise ValueError("runbook is empty")
    steps = tuple(_parse_item(match, level=3) for match in _STEP.finditer(text))
    gates = tuple(_parse_item(match, level=4) for match in _GATE.finditer(text))
    return CutoverPlan(steps=steps, gates=gates)


def validate_enforced_mode_cutover_runbook(text: str) -> list[str]:
    """Return specific structural requirement IDs, never a byte-equality checksum."""

    violations: list[str] = []
    try:
        plan = parse_enforced_mode_cutover_runbook(text)
    except ValueError:
        return ["enforced-mode-cutover-runbook.1.3"]
    if tuple(item.item_id for item in plan.steps) != tuple(f"S{i:02d}" for i in range(10)):
        violations.append("enforced-mode-cutover-runbook.1.2")
    if tuple(item.item_id for item in plan.gates) != tuple(f"G{i:02d}" for i in range(1, 12)):
        violations.append("enforced-mode-cutover-runbook.1.4")
    if any(not item.evidence_status for item in (*plan.steps, *plan.gates)):
        violations.append("enforced-mode-cutover-runbook.1.5")
    executable = "\n".join(
        shell for item in (*plan.steps, *plan.gates) for shell in (item.action, item.check)
    )
    rejected = text.split("## Rejected strategies", 1)[-1]
    weakens_capabilities = (
        "Do not restore legacy\ncapability acceptance" not in text
        or "Do not add a `pt1` compatibility window" not in rejected
        or "Do not revert or weaken PR #163" not in rejected
        or "pt1.task" in executable
        or re.search(
            r"(?im)^(?!do not\b).*\b(?:restore|accept|enable)\b.*\blegacy\b.*\bcapabilit",
            text,
        )
        is not None
        or re.search(
            r"(?im)^(?:[-*]\s*)?(?:revert|weaken|remove|disable|bypass)\b.*"
            r"(?:\bscoped\b.*\bcapabilit|PR\s*#163)",
            text,
        )
        is not None
    )
    if weakens_capabilities:
        violations.append("enforced-mode-cutover-runbook.2.9")
    normalized_prose = re.sub(r"\s+", " ", text)
    contradicts_single_stage = (
        re.search(
            r"(?i)\ba permissive(?:-mode)? restart\s+(?!does not\b).*?"
            r"(?:accepts legacy Bearer|provides a no-drain stage)",
            normalized_prose,
        )
        is not None
        or re.search(
            r"(?i)\blegacy Bearer liveness\b[^.]{0,120}\b(?:succeeds|works|passes)\b"
            r"[^.]{0,120}\bpermissive mode\b",
            normalized_prose,
        )
        is not None
        or re.search(
            r"(?i)\bpermissive(?: mode|-mode restart)\b[^.]{0,200}"
            r"\b(?:enables|offers|provides)\b[^.]{0,40}"
            r"\b(?:zero-downtime|no-drain)\b",
            normalized_prose,
        )
        is not None
    )
    if (
        "A permissive-mode restart does not avoid this drain" not in text
        or "fallback accepts only requests\nwithout an Authorization header" not in text
        or "legacy container sends its `pt1` token as a Bearer\ncredential" not in text
        or "classifies `/tasks/<id>/live` as mutating" not in text
        or "and returns 401" not in text
        or "turn this one-stage cutover into two\ndrains with no availability benefit" not in text
        or contradicts_single_stage
    ):
        violations.append("enforced-mode-cutover-runbook.2.10")
    s01 = next((item for item in plan.steps if item.item_id == "S01"), None)
    s07 = next((item for item in plan.steps if item.item_id == "S07"), None)
    g05 = next((item for item in plan.gates if item.item_id == "G05"), None)
    g09 = next((item for item in plan.gates if item.item_id == "G09"), None)
    g05_required = (
        'test "$(curl ',
        "--request PUT",
        "Authorization: Bearer $READ_TOKEN",
        '"$SERVICE_URL/tasks/$CANARY_TASK_ID/turn"',
        ')" = 401',
    )
    if (
        g05 is None
        or len(g05.action.splitlines()) != 1
        or not all(fragment in g05.action for fragment in g05_required)
        or [g05.action.index(fragment) for fragment in g05_required]
        != sorted(g05.action.index(fragment) for fragment in g05_required)
    ):
        violations.append("enforced-mode-cutover-runbook.4.5")
    capability_read = (
        'test "$(docker exec "$CANARY_CONTAINER_ID" python -c \'import json; '
        'print(json.load(open("/run/secrets/panopticon-service-auth"))["task"][:5])\')" = ptc1.'
    )
    if (
        g09 is None
        or "docker inspect --format '{{.Id}} {{.State.Pid}} {{.State.StartedAt}}' \"$CANARY_CONTAINER_ID\""
        not in g09.action
        or capability_read not in g09.action
    ):
        violations.append("enforced-mode-cutover-runbook.4.17")
    if (
        s01 is None
        or s07 is None
        or g09 is None
        or "docker ps --all --quiet --no-trunc --filter label=panopticon.task" not in s01.action
        or 'assert-fresh-container "$CANARY_CONTAINER_ID" "$EVIDENCE_DIR/S03-all-container-ids-before-enforcement.txt"'
        not in s07.check
        or 'assert-fresh-container "$CANARY_CONTAINER_ID" "$EVIDENCE_DIR/S03-all-container-ids-before-enforcement.txt"'
        not in g09.check
        or 'assert-container-started-after "$CANARY_CONTAINER_STARTED" "$ENFORCEMENT_STARTED_AT"'
        not in s07.check
        or 'assert-container-started-after "$CANARY_CONTAINER_STARTED" "$ENFORCEMENT_STARTED_AT"'
        not in g09.check
        or capability_read not in g09.action
        or "G09-target-at-capability.txt" not in g09.action
        or 'cmp "$EVIDENCE_DIR/G09-target-at-capability.txt" "$EVIDENCE_DIR/S07-container-after-keepalive.txt"'
        not in g09.check
    ):
        violations.append("enforced-mode-cutover-runbook.4.18")
    s00 = next((item for item in plan.steps if item.item_id == "S00"), None)
    s04 = next((item for item in plan.steps if item.item_id == "S04"), None)
    g08 = next((item for item in plan.gates if item.item_id == "G08"), None)
    g10 = next((item for item in plan.gates if item.item_id == "G10"), None)
    g03 = next((item for item in plan.gates if item.item_id == "G03"), None)
    g11 = next((item for item in plan.gates if item.item_id == "G11"), None)
    if (
        s04 is None
        or g08 is None
        or s04.check.splitlines()[-3:]
        != [
            'docker ps --quiet --filter label=panopticon.task | tee "$EVIDENCE_DIR/G08-running.txt"',
            'test ! -s "$EVIDENCE_DIR/G08-running.txt"',
            "printf 'G08: PASS\\n' >> \"$EVIDENCE_DIR/gates.txt\"",
        ]
        or g08.action.splitlines()
        != [
            'docker ps --quiet --filter label=panopticon.task | tee "$EVIDENCE_DIR/G08-running.txt"'
        ]
        or g08.check.splitlines()
        != [
            'test ! -s "$EVIDENCE_DIR/G08-running.txt"',
            "printf 'G08: PASS\\n' >> \"$EVIDENCE_DIR/gates.txt\"",
        ]
    ):
        violations.append("enforced-mode-cutover-runbook.4.8")
    if (
        g10 is None
        or 'test "$NEW_RUNNER_PID" != "$OLD_RUNNER_PID"' not in g10.action
        or 'cmp --silent "$EVIDENCE_DIR/S01-client-identities-before.txt"' not in g10.action
        or 'test "$(ps -o lstart= -p "$NEW_RUNNER_PID" | sed \'s/^ *//\')" = "$NEW_RUNNER_START"'
        not in g10.check
        or '!= "$OLD_RUNNER_START"' not in g10.check
        or 'assert-process-replaced "$OLD_RUNNER_PID" "$OLD_RUNNER_START"' not in g10.check
    ):
        violations.append("enforced-mode-cutover-runbook.4.10")
    if (
        g11 is None
        or 'test -n "$OLD_RUNNER_START"' not in g11.action
        or 'test -n "$OLD_DASHBOARD_START"' not in g11.action
        or 'assert-process-replaced "$OLD_RUNNER_PID" "$OLD_RUNNER_START"' not in g11.check
        or 'assert-process-replaced "$OLD_DASHBOARD_PID" "$OLD_DASHBOARD_START"' not in g11.check
        or 'assert-dashboard-process "$NEW_DASHBOARD_PID" "$NEW_DASHBOARD_START"' not in g11.check
    ):
        violations.append("enforced-mode-cutover-runbook.4.11")
    if (
        s00 is None
        or s01 is None
        or s04 is None
        or g03 is None
        or g11 is None
        or 'assert-runner-set "$EVIDENCE_DIR/S00-runners.json" "$OLD_RUNNER_ID"' not in s00.check
        or 'assert-runner-set "$EVIDENCE_DIR/S01-runners-immediately-before-service-stop.json"'
        not in s01.action
        or "pane_start_command" not in s04.action
        or "S04-dashboard-start-command.txt" not in s04.action
        or 'assert-runner-set "$EVIDENCE_DIR/G03-runners.json" "$NEW_RUNNER_ID"' not in g03.check
        or "assert-runner-process" not in g03.check
        or "assert-dashboard-process" not in g11.check
        or 'assert-process-replaced "$OLD_RUNNER_PID" "$OLD_RUNNER_START"' not in g11.check
        or 'assert-process-replaced "$OLD_DASHBOARD_PID" "$OLD_DASHBOARD_START"' not in g11.check
    ):
        violations.extend(
            ["enforced-mode-cutover-runbook.3.3", "enforced-mode-cutover-runbook.4.3"]
        )
    return violations


def inspect_cutover_credential_file(
    path: str | Path, *, service_uid: int | None = None
) -> CredentialMetadata:
    """Validate credential metadata without writing credential values to stdout or stderr."""

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
        payload: Any = json.loads(credential.read_text())
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


def _main() -> int:
    parser = argparse.ArgumentParser(description="Offline enforced-mode cutover checks")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate-runbook")
    validate_parser.add_argument("path", type=Path)
    inspect_parser = subparsers.add_parser("inspect-credential-file")
    inspect_parser.add_argument("path", type=Path)
    fresh_parser = subparsers.add_parser("assert-fresh-container")
    fresh_parser.add_argument("container_id")
    fresh_parser.add_argument("inventory_path", type=Path)
    runner_parser = subparsers.add_parser("assert-runner-set")
    runner_parser.add_argument("path", type=Path)
    runner_parser.add_argument("expected_runner_id", nargs="?")
    replaced_parser = subparsers.add_parser("assert-process-replaced")
    replaced_parser.add_argument("pid", type=int)
    replaced_parser.add_argument("original_start")
    process_parser = subparsers.add_parser("assert-runner-process")
    process_parser.add_argument("pid", type=int)
    process_parser.add_argument("recorded_start")
    process_parser.add_argument("command_path", type=Path)
    process_parser.add_argument("config_path")
    process_parser.add_argument("auth_file")
    process_parser.add_argument("runner_id")
    dashboard_parser = subparsers.add_parser("assert-dashboard-process")
    dashboard_parser.add_argument("pid", type=int)
    dashboard_parser.add_argument("recorded_start")
    dashboard_parser.add_argument("command_path", type=Path)
    dashboard_parser.add_argument("config_path")
    dashboard_parser.add_argument("auth_file")
    dashboard_parser.add_argument("browser_origin")
    dashboard_parser.add_argument("service_url")
    started_parser = subparsers.add_parser("assert-container-started-after")
    started_parser.add_argument("container_started")
    started_parser.add_argument("enforcement_started")
    arguments = parser.parse_args()
    if arguments.command == "validate-runbook":
        violations = validate_enforced_mode_cutover_runbook(arguments.path.read_text())
        if violations:
            for violation in violations:
                print(violation, file=sys.stderr)
            return 1
    elif arguments.command == "inspect-credential-file":
        inspect_cutover_credential_file(arguments.path)
    elif arguments.command == "assert-fresh-container":
        assert_fresh_container_id(arguments.container_id, arguments.inventory_path)
    elif arguments.command == "assert-runner-set":
        assert_runner_set(arguments.path, arguments.expected_runner_id)
    elif arguments.command == "assert-process-replaced":
        assert_process_replaced(arguments.pid, arguments.original_start)
    elif arguments.command == "assert-runner-process":
        assert_runner_process(
            arguments.pid,
            arguments.recorded_start,
            arguments.command_path,
            arguments.config_path,
            arguments.auth_file,
            arguments.runner_id,
        )
    elif arguments.command == "assert-dashboard-process":
        assert_dashboard_process(
            arguments.pid,
            arguments.recorded_start,
            arguments.command_path,
            arguments.config_path,
            arguments.auth_file,
            arguments.browser_origin,
            arguments.service_url,
        )
    elif arguments.command == "assert-container-started-after":
        assert_container_started_after(arguments.container_started, arguments.enforcement_started)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
