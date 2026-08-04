"""Structural parsing and offline checks for the enforced-mode cutover runbook."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
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
    inspect_parser = subparsers.add_parser("inspect-credential-file")
    inspect_parser.add_argument("path", type=Path)
    arguments = parser.parse_args()
    if arguments.command == "inspect-credential-file":
        inspect_cutover_credential_file(arguments.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
