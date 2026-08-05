from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from panopticon.core.cutover_runbook import (
    RUNBOOK_PATH,
    inspect_cutover_credential_file,
    parse_enforced_mode_cutover_runbook,
    validate_enforced_mode_cutover_runbook,
)

ROOT = Path(__file__).parents[1]


def _command_heads(shell: str) -> set[str]:
    heads: set[str] = set()
    in_python = False
    for raw in shell.splitlines():
        line = raw.strip()
        if in_python:
            in_python = line != "PY"
            continue
        if "<<'PY'" in line:
            in_python = True
        line = line.removeprefix("!").strip()
        if not line or line in {"do", "done", "then", "fi", "}", "PY"}:
            continue
        if line.startswith(
            ("do ", "for ", "while ", "until ", "if ", "or ", "import ", "from ", "assert ")
        ):
            continue
        if line.startswith(("{", "}", "(", "terminal =", "tasks =", "headers =", "name, value =")):
            continue
        tokens = shlex.split(line, comments=True)
        if tokens and tokens[0] not in {"or"} and "=" not in tokens[0]:
            heads.add(tokens[0])
        heads.update(re.findall(r"(?:\||\$\()\s*([A-Za-z_:][A-Za-z0-9_:-]*)", line))
    return heads


def _text() -> str:
    return RUNBOOK_PATH.read_text()


def _item(item_id: str) -> str:
    text = _text()
    level = "##" if item_id.startswith("S") else "###"
    next_heading = "## S" if item_id.startswith("S") else "### G"
    match = re.search(
        rf"^{level} {item_id} — .*?\n(?P<body>.*?)(?=^{next_heading}|^## (?:Rollback|The eleven gates|Resume evidence)|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None
    return match.group(0)


# 2119: enforced-mode-cutover-runbook.1.1, enforced-mode-cutover-runbook.1.2
# 2119: enforced-mode-cutover-runbook.1.3, enforced-mode-cutover-runbook.1.4
# 2119: enforced-mode-cutover-runbook.1.5, enforced-mode-cutover-runbook.1.6
# 2119: enforced-mode-cutover-runbook.6.6, enforced-mode-cutover-runbook.7.1
def test_parser_reads_actual_operator_shell_blocks_and_every_block_parses() -> None:
    text = _text()
    plan = parse_enforced_mode_cutover_runbook(text)
    assert [item.item_id for item in plan.steps] == [f"S{i:02d}" for i in range(10)]
    assert [item.item_id for item in plan.gates] == [f"G{i:02d}" for i in range(1, 12)]
    assert [item.title for item in plan.steps] == [
        "Prepare evidence, credentials, and prerequisite",
        "Inventory identities, freeze new work, and wait for stopping points",
        "Reconcile every long-lived client and capture the drain set",
        "Stop every task container and prove an empty fleet",
        "Start replacement runner and waiting dashboard with fresh identities",
        "Start the task service with exported enforced configuration",
        "Run gates G01 through G07",
        "Release and verify exactly one real canary",
        "Release old-runner claims for controlled bulk respawn",
        "Append evidence and follow-ups to issue #203",
    ]
    assert plan.steps[5].action in text
    assert "PANOPTICON_SERVICE_AUTH_MODE='$PANOPTICON_SERVICE_AUTH_MODE'" in plan.steps[5].action
    assert plan.gates[8].action in text
    for item in (*plan.steps, *plan.gates):
        assert item.action != item.check
        assert item.expected
        assert item.failure_action.startswith(("STOP", "ROLL BACK"))
        assert item.evidence_status.startswith(("Production-only:", "Authoring-tested:"))
        assert item.evidence_status.split(":", 1)[1].strip()
        for shell in (item.action, item.check):
            result = subprocess.run(["bash", "-n"], input=shell, text=True, capture_output=True)
            assert result.returncode == 0, f"{item.item_id}: {result.stderr}"
    forbidden = (
        "panopticon-admin",
        "panopticon-cutover",
        "run-gate",
        "run-gates",
        "assert-http",
        "assert-canary",
        "assert-cors",
        "assert-named-task",
    )
    assert not any(command in text for command in forbidden)
    allowed_heads = {
        ":",
        "awk",
        "basename",
        "cat",
        "cmp",
        "curl",
        "date",
        "docker",
        "env",
        "export",
        "find",
        "gh",
        "git",
        "grep",
        "kill",
        "mktemp",
        "printf",
        "ps",
        "read",
        "sed",
        "set",
        "sleep",
        "sort",
        "tee",
        "test",
        "tmux",
        "uv",
        "wc",
    }
    for item in (*plan.steps, *plan.gates):
        assert _command_heads(item.action) <= allowed_heads
        assert _command_heads(item.check) <= allowed_heads
    assert {item.evidence_status.split(":", 1)[0] for item in (*plan.steps, *plan.gates)} == {
        "Authoring-tested",
        "Production-only",
    }
    assert [item.evidence_status.startswith("Authoring-tested:") for item in plan.steps] == [
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        True,
    ]
    assert all(item.evidence_status.startswith("Production-only:") for item in plan.gates)
    expected_anchors = {
        "S00": "ancestor",
        "S01": "PID and start time",
        "S02": "confirmed-dead",
        "S03": "empty",
        "S04": "PID/start-time pairs differ",
        "S05": "inherits enforced mode",
        "S06": "G01 through G07",
        "S07": "ptc1.",
        "S08": "dependency-clear nonterminal task is live",
        "S09": "Issue #203 contains",
        "G01": "returns 200",
        "G02": "returns 401",
        "G03": "recorded new PID/start-time pair",
        "G04": "authorized and echoes",
        "G05": "returns 403",
        "G06": "Both responses echo",
        "G07": "same chosen task name",
        "G08": "empty immediately before S05",
        "G09": "freshly spawned container",
        "G10": "PID/start-time pairs differ",
        "G11": "confirmed dead by original identity",
    }
    for item in (*plan.steps, *plan.gates):
        assert expected_anchors[item.item_id] in item.expected
    assert "G01–G07 are PR #163's original seven" in text
    assert "G08–G11 are issue #203's four" in text


# 2119: enforced-mode-cutover-runbook.7.1
def test_every_repository_entry_point_used_by_the_runbook_resolves() -> None:
    # Host programs such as docker and tmux are deployment prerequisites and are
    # deliberately absent from some test containers.  Repository-owned commands,
    # however, can and must be resolved here instead of being asserted by name.
    for argv in (
        ["uv", "run", "panopticon", "--help"],
        ["uv", "run", "python", "-m", "panopticon.core.cutover_runbook", "--help"],
        ["uv", "run", "python", "-m", "panopticon.taskservice", "--help"],
        ["uv", "run", "python", "-m", "panopticon.sessionservice.host", "--help"],
    ):
        assert subprocess.run(argv, cwd=ROOT, capture_output=True).returncode == 0


# 2119: enforced-mode-cutover-runbook.1.6
def test_validator_is_structural_and_does_not_reject_unrelated_valid_edits() -> None:
    text = _text()
    assert validate_enforced_mode_cutover_runbook(text) == []
    assert validate_enforced_mode_cutover_runbook(text.replace("one-way", "one way")) == []
    missing_step = re.sub(r"^## S09 —.*?(?=^## Rollback)", "", text, flags=re.MULTILINE | re.DOTALL)
    assert validate_enforced_mode_cutover_runbook(missing_step) == [
        "enforced-mode-cutover-runbook.1.2"
    ]
    missing_shell = text.replace("```sh\nset -uo pipefail", "set -uo pipefail", 1)
    assert validate_enforced_mode_cutover_runbook(missing_shell) == [
        "enforced-mode-cutover-runbook.1.3"
    ]
    missing_gate_shell = text.replace(
        '```sh\ntest "$(curl --silent --output /dev/null --write-out \'%{http_code}\' "$SERVICE_URL/healthz")" = 200',
        'test "$(curl --silent --output /dev/null --write-out \'%{http_code}\' "$SERVICE_URL/healthz")" = 200',
        1,
    )
    assert validate_enforced_mode_cutover_runbook(missing_gate_shell) == [
        "enforced-mode-cutover-runbook.1.3"
    ]


# 2119: enforced-mode-cutover-runbook.2.1, enforced-mode-cutover-runbook.2.2
# 2119: enforced-mode-cutover-runbook.2.3, enforced-mode-cutover-runbook.2.4
# 2119: enforced-mode-cutover-runbook.2.5, enforced-mode-cutover-runbook.2.6
# 2119: enforced-mode-cutover-runbook.2.7, enforced-mode-cutover-runbook.2.8
# 2119: enforced-mode-cutover-runbook.2.9
def test_prerequisite_quiescence_inventory_and_direct_drain_are_executable() -> None:
    s00, s01, s02, s03 = (_item(f"S{i:02d}") for i in range(4))
    assert "merge-base --is-ancestor" in s00 and "gh run list" in s00
    assert '--commit "$ISSUE_202_COMMIT" --workflow ci.yml --status success' in s00
    assert "--jq 'length == 1' | grep --fixed-strings true" in s00
    assert "closing commit is an ancestor" in s00 and "repository CI is green" in s00
    assert s00.index("merge-base --is-ancestor") < s01.index("kill-session -t runner")
    assert (
        "list-panes" in s01
        and 'ps -o pid= -o lstart= -p "$OLD_RUNNER_PID,$OLD_DASHBOARD_PID"' in s01
    )
    assert "docker inspect --format '{{.Id}} {{.Name}} {{.State.StartedAt}}'" in s01
    assert "S01-all-container-ids-before.txt" in s01
    assert 'OLD_RUNNER_START="$(ps -o lstart=' in s01
    assert 'OLD_DASHBOARD_START="$(ps -o lstart=' in s01
    assert s01.index("kill-session -t runner") < s01.index("while :; do")
    assert 'turn"] == "user"' in s01 and "kill-session -t service" in s01
    assert "confirmed-dead disposition" in s02
    assert "docker stop" in s03
    assert 'test -z "$TASK_CONTAINERS" || docker stop $TASK_CONTAINERS' in s03
    assert "docker ps --quiet --filter label=panopticon.task" in s03
    assert _text().index("## S03") < _text().index("## S05")
    assert 'test ! -s "$EVIDENCE_DIR/S03-running-after.txt"' in s03
    assert (
        'docker ps --quiet --filter label=panopticon.task | tee "$EVIDENCE_DIR/S03-running-after.txt"'
        in s03
    )
    assert "permissive counter is never a gate" in s03
    assert "permissive request\ncounter is only weak corroboration" in _text()
    rejected = _text().split("## Rejected strategies", 1)[1]
    assert "Do not add a `pt1` compatibility window" in rejected
    assert "Do not replace credentials inside running containers" in rejected
    assert "Do not revert or weaken PR #163" in rejected
    executable = "\n".join(
        shell
        for item in (
            *parse_enforced_mode_cutover_runbook(_text()).steps,
            *parse_enforced_mode_cutover_runbook(_text()).gates,
        )
        for shell in (item.action, item.check)
    )
    assert "pt1.task" not in executable
    assert "permissive" not in executable
    assert "replace credentials inside" not in executable
    assert "revert" not in executable


# 2119: enforced-mode-cutover-runbook.3.1, enforced-mode-cutover-runbook.3.2
# 2119: enforced-mode-cutover-runbook.3.3, enforced-mode-cutover-runbook.3.4
# 2119: enforced-mode-cutover-runbook.3.5, enforced-mode-cutover-runbook.3.6
# 2119: enforced-mode-cutover-runbook.3.7, enforced-mode-cutover-runbook.3.8
# 2119: enforced-mode-cutover-runbook.3.9, enforced-mode-cutover-runbook.3.10
# 2119: enforced-mode-cutover-runbook.7.2, enforced-mode-cutover-runbook.7.3
def test_clients_are_replaced_before_exported_enforced_service_launch() -> None:
    text = _text()
    s04, s05 = _item("S04"), _item("S05")
    assert text.index("## S04") < text.index("## S05")
    assert "new-session -d -s runner" in s04
    assert "new-session -d -s dashboard" in s04
    assert "NEW_RUNNER_ID" in s04 and 'test "$NEW_RUNNER_ID" != "$OLD_RUNNER_ID"' in _item("S00")
    for process in ("RUNNER", "DASHBOARD"):
        assert f"NEW_{process}_PID" in s04 and f"OLD_{process}_PID" in s04
        assert f"NEW_{process}_START" in s04
    assert "S01-client-identities-before.txt" in s04
    assert "freshly launched CLI proves nothing" in _item("S02")
    for name in (
        "PANOPTICON_SERVICE_AUTH_MODE",
        "PANOPTICON_SERVICE_AUTH_FILE",
        "PANOPTICON_BROWSER_ORIGINS",
    ):
        assert f"export {name}=" in s05
        assert f"{name}='${name}'" in s05
    assert "export PANOPTICON_SERVICE_AUTH_MODE=enforced" in s05
    assert "inspect-credential-file" in _item("S00")
    assert 'test "$AUTH_FILE_NAME" = "$(basename "$AUTH_FILE_NAME")"' in _item("S00")
    assert 'assert value == f"{parsed.scheme}://{authority}"' in _item("S00")
    assert "set-environment -g PANOPTICON_BROWSER_ORIGINS" in s04
    assert 'set-environment -g PANOPTICON_BROWSER_ORIGINS "$PWA_ORIGIN"' in s04
    assert 'ps -o pid= -o lstart= -p "$NEW_RUNNER_PID,$NEW_DASHBOARD_PID"' in s04
    assert "PANOPTICON_RUNNER_ID='$NEW_RUNNER_ID'" in s04
    assert 'test ! -s "$EVIDENCE_DIR/G08-running.txt"' in s04
    assert "scheme-host-port" in s05
    assert 'environment_token(privilege="read")' in _item("S06")


# 2119: enforced-mode-cutover-runbook.4.1, enforced-mode-cutover-runbook.4.2
# 2119: enforced-mode-cutover-runbook.4.3, enforced-mode-cutover-runbook.4.4
# 2119: enforced-mode-cutover-runbook.4.5, enforced-mode-cutover-runbook.4.6
# 2119: enforced-mode-cutover-runbook.4.7, enforced-mode-cutover-runbook.4.8
# 2119: enforced-mode-cutover-runbook.4.9, enforced-mode-cutover-runbook.4.10
# 2119: enforced-mode-cutover-runbook.4.11, enforced-mode-cutover-runbook.4.12
# 2119: enforced-mode-cutover-runbook.4.13, enforced-mode-cutover-runbook.4.14
# 2119: enforced-mode-cutover-runbook.4.15, enforced-mode-cutover-runbook.4.16
# 2119: enforced-mode-cutover-runbook.4.17, enforced-mode-cutover-runbook.4.18
def test_all_eleven_gates_assert_the_real_boundary() -> None:
    gates = {f"G{i:02d}": _item(f"G{i:02d}") for i in range(1, 12)}
    assert '--write-out \'%{http_code}\' "$SERVICE_URL/healthz")" = 200' in gates["G01"]
    assert "Authorization:" not in gates["G01"]
    assert '--write-out \'%{http_code}\' "$SERVICE_URL/tasks")" = 401' in gates["G02"]
    assert "Authorization:" not in gates["G02"]
    assert "/runners/$NEW_RUNNER_ID" in gates["G03"] and "NEW_RUNNER_PID" in gates["G03"]
    assert "Authorization: Bearer $WRITE_TOKEN" in gates["G03"]
    assert 'ps -o lstart= -p "$NEW_RUNNER_PID"' in gates["G03"]
    assert (
        "Authorization: Bearer $READ_TOKEN" in gates["G04"]
        and "Origin: $PWA_ORIGIN" in gates["G04"]
    )
    assert "!= 401" in gates["G04"] and "!= 403" in gates["G04"]
    assert '"$EVIDENCE_DIR/G04-status.txt")" = 200' in gates["G04"]
    assert '"$SERVICE_URL/tasks"' in gates["G04"]
    assert "--request PUT" in gates["G05"] and "= 403" in gates["G05"]
    assert "Authorization: Bearer $READ_TOKEN" in gates["G05"]
    assert '"$SERVICE_URL/tasks/$CANARY_TASK_ID/turn"' in gates["G05"]
    assert "--request OPTIONS" in gates["G06"] and "G06-actual.txt" in gates["G06"]
    assert 'headers.get("access-control-allow-origin") == [origin]' in gates["G06"]
    assert '"access-control-allow-credentials" not in headers' in gates["G06"]
    assert '"Origin: $PWA_ORIGIN"' in gates["G06"]
    assert "assert open(phone_path).read().strip() == name" in gates["G07"]
    assert 'name in {task.get("name"), task.get("slug")}' in gates["G07"]
    assert "Authorization: Bearer $READ_TOKEN" in gates["G07"]
    assert "docker ps --quiet" in gates["G08"] and "test ! -s" in gates["G08"]
    assert 'tee "$EVIDENCE_DIR/G08-running.txt"' in gates["G08"]
    assert 'test ! -s "$EVIDENCE_DIR/G08-running.txt"' in gates["G08"]
    assert "/run/secrets/panopticon-service-auth" in gates["G09"] and "ptc1." in gates["G09"]
    assert '" = ptc1.' in gates["G09"]
    assert 'container_status"] == "live"' in gates["G09"] and "keepalive" in gates["G09"]
    assert 'cmp "$EVIDENCE_DIR/S07-container-initial.txt"' in gates["G09"]
    assert "NEW_RUNNER_PID" in gates["G10"] and "OLD_RUNNER_PID" in gates["G10"]
    assert "NEW_RUNNER_START" in gates["G10"] and "OLD_RUNNER_START" in gates["G10"]
    assert 'test "$NEW_RUNNER_PID" != "$OLD_RUNNER_PID"' in gates["G10"]
    assert "cmp --silent" in gates["G10"]
    assert "OLD_DASHBOARD_PID" in gates["G11"] and "NEW_DASHBOARD_PID" in gates["G11"]
    assert "NEW_DASHBOARD_START" in gates["G11"]
    assert "OLD_RUNNER_START" in gates["G11"] and "OLD_DASHBOARD_START" in gates["G11"]
    assert 'ps -o lstart= -p "$OLD_RUNNER_PID"' in gates["G11"]
    assert 'docker exec "$CANARY_CONTAINER_ID"' in gates["G09"]
    assert 'docker exec "$CANARY_CONTAINER"' not in gates["G09"]


# 2119: enforced-mode-cutover-runbook.5.3, enforced-mode-cutover-runbook.5.4
# 2119: enforced-mode-cutover-runbook.5.5, enforced-mode-cutover-runbook.5.8
# 2119: enforced-mode-cutover-runbook.7.3, enforced-mode-cutover-runbook.7.4
def test_resume_evidence_canary_release_and_bulk_reclaim_are_ordered() -> None:
    text = _text()
    assert text.index("DELETE") < text.index("/runners/$OLD_RUNNER_ID/reclaim")
    assert "only the canary task claim" in _item("S07")
    assert "five-second liveness keepalive interval" in _item("S07")
    assert "recorded task-specific failed\ndisposition" in _item("S08")
    assert 'task["container_status"] == "live"' in _item("S08")
    assert 'task["container_status"] == "failed" and task["lifecycle_detail"]' in _item("S08")
    assert _item("S07").count("--request DELETE") == 1
    assert "/tasks/$CANARY_TASK_ID/claim" in _item("S07")
    assert "/runners/$OLD_RUNNER_ID/reclaim" not in _item("S07")
    assert "/runners/$OLD_RUNNER_ID/reclaim" in _item("S08")
    assert "ptc1." in _item("S07") and 'container_status"] == "live"' in _item("S07")
    assert "CANARY_CONTAINER_ID" in _item("S07")
    assert "S01-all-container-ids-before.txt" in _item("S07")
    before_canary = "\n".join(_item(f"S{i:02d}") for i in range(7))
    assert "/claim" not in before_canary
    resume = text.split("## Resume evidence", 1)[1].split("## What remains", 1)[0]
    claude_row = next(line for line in resume.splitlines() if line.startswith("| Claude |"))
    codex_row = next(line for line in resume.splitlines() if line.startswith("| Codex |"))
    assert (
        "Unit:" in claude_row and "`--continue`" in claude_row and "Production-only:" in claude_row
    )
    assert (
        "Unit:" in codex_row
        and "explicit interactive session ID" in codex_row
        and "Production-only:" in codex_row
    )


# 2119: enforced-mode-cutover-runbook.6.1, enforced-mode-cutover-runbook.6.2
# 2119: enforced-mode-cutover-runbook.6.3, enforced-mode-cutover-runbook.6.4
# 2119: enforced-mode-cutover-runbook.6.5, enforced-mode-cutover-runbook.6.7
# 2119: enforced-mode-cutover-runbook.6.8, enforced-mode-cutover-runbook.6.9
# 2119: enforced-mode-cutover-runbook.6.10
def test_rollback_unknowns_and_issue_record_are_explicit() -> None:
    text = _text()
    rollback = text.split("## Rollback", 1)[1].split("## The eleven gates", 1)[0]
    assert rollback.startswith(
        "\n\nTrigger rollback on prerequisite failure, nonzero drain, stale-client evidence, service startup,\nsecurity/browser gate, or canary failure."
    )
    for trigger in (
        "prerequisite",
        "nonzero drain",
        "stale-client",
        "service startup",
        "security/browser",
        "canary",
    ):
        assert trigger in rollback
    assert rollback.index("Keep every task container stopped") < rollback.index(
        "restore the last-known-good"
    )
    assert "Do not restore legacy\ncapability acceptance" in rollback
    assert "do not expect a killed PID" in rollback
    assert rollback.index("restore the last-known-good service environment") < rollback.index(
        "restart\nthe clients"
    )
    unknowns = text.split("## What remains unproven until cutover", 1)[1]
    for fact in (
        "process identity",
        "credential-file",
        "network",
        "installed-phone",
        "real-container",
        "liveness",
    ):
        assert fact in unknowns
    assert "remain unproven until their production evidence files are\nrecorded" in unknowns
    assert "gh issue comment 203" in _item("S09")
    assert 'cat "$EVIDENCE_DIR/gates.txt"' in _item("S09")
    assert 'cat "$EVIDENCE_DIR/followups.md"' in _item("S09")
    assert "Do not\nopen a new issue for anything already described in #202 or #203" in _item("S09")
    executable = "\n".join(
        shell
        for item in (
            *parse_enforced_mode_cutover_runbook(text).steps,
            *parse_enforced_mode_cutover_runbook(text).gates,
        )
        for shell in (item.action, item.check)
    )
    assert "gh issue create" not in executable
    assert "docker start" not in rollback and "docker run" not in rollback


# 2119: enforced-mode-cutover-runbook.3.6, enforced-mode-cutover-runbook.3.10
@pytest.mark.parametrize(
    "payload",
    [
        {"write": [], "read": ["r"]},
        {"write": ["w"], "read": []},
        {"write": "w", "read": ["r"]},
        {"write": ["w"], "read": "r"},
        {"write": ["same"], "read": ["same"]},
        {"write": ["w", "shared"], "read": ["shared", "r"]},
    ],
)
def test_credential_payload_rejections_reach_json_validation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], payload: object
) -> None:
    path = tmp_path / "service-auth.json"
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    with pytest.raises(ValueError, match="credential metadata is invalid"):
        inspect_cutover_credential_file(path)
    assert capsys.readouterr() == ("", "")


# 2119: enforced-mode-cutover-runbook.3.6, enforced-mode-cutover-runbook.3.10
def test_credential_file_acceptance_and_metadata_rejections_are_distinct(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "service-auth.json"
    path.write_text(json.dumps({"write": ["secret-write"], "read": ["secret-read"]}))
    path.chmod(0o600)
    result = inspect_cutover_credential_file(path, service_uid=path.stat().st_uid)
    assert (result.write_count, result.read_count) == (1, 1)
    assert capsys.readouterr() == ("", "")
    path.chmod(0o640)
    with pytest.raises(ValueError, match="credential metadata is unsafe"):
        inspect_cutover_credential_file(path, service_uid=path.stat().st_uid)
    path.chmod(0o600)
    with pytest.raises(ValueError, match="credential metadata is unsafe"):
        inspect_cutover_credential_file(path, service_uid=path.stat().st_uid + 1)
    link = tmp_path / "service-auth-link.json"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="credential metadata is unsafe"):
        inspect_cutover_credential_file(link, service_uid=path.stat().st_uid)
    directory = tmp_path / "credential-directory"
    directory.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="credential metadata is unsafe"):
        inspect_cutover_credential_file(directory, service_uid=directory.stat().st_uid)
    assert capsys.readouterr() == ("", "")
