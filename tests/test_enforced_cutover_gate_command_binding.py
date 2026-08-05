from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from panopticon.core.cutover_runbook import (
    RUNBOOK_PATH,
    parse_enforced_mode_cutover_runbook,
    validate_enforced_mode_cutover_runbook,
)

ALLOWED_COMMANDS = {
    ":",
    "awk",
    "basename",
    "break",
    "cat",
    "cmp",
    "curl",
    "date",
    "docker",
    "env",
    "exec",
    "export",
    "find",
    "gh",
    "git",
    "grep",
    "kill",
    "mktemp",
    "ps",
    "printf",
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
        lexer = shlex.shlex(raw, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        tokens = list(lexer)
        expect_command = True
        command_wrapper = False
        skip_redirection_target = False
        for token in tokens:
            if skip_redirection_target:
                skip_redirection_target = False
                continue
            if token in {"&&", "||", "|", ";"}:
                expect_command = True
                command_wrapper = False
                continue
            if token in {"then", "do"}:
                expect_command = True
                continue
            if token in {"done", "fi", "in", "}", "{"}:
                continue
            if token in {">", ">>", "<"}:
                skip_redirection_target = True
                continue
            if expect_command and token in {"if", "while", "until", "!"}:
                continue
            if expect_command and token == "for":
                expect_command = False
                continue
            if expect_command and "=" in token and not token.startswith(("test=", "[=")):
                continue
            if expect_command:
                heads.add(token)
                expect_command = token in {"exec", "env"}
                command_wrapper = expect_command
                continue
            if command_wrapper and "=" not in token:
                heads.add(token)
                command_wrapper = token in {"exec", "env"}
                expect_command = command_wrapper
        heads.update(re.findall(r"\$\(\s*([A-Za-z_:][A-Za-z0-9_:-]*)", raw))
        heads.update(re.findall(r"`\s*([A-Za-z_:][A-Za-z0-9_:-]*)", raw))
        for token in tokens:
            if token.startswith(("until ", "if ", "exec ")):
                heads.update(_command_heads(token))
    return heads


def _repository_entry_points(shell: str) -> set[str]:
    entry_points: set[str] = set()
    pattern = re.compile(
        r"\buv\s+(?:(?:--directory\s+(?:\"[^\"]+\"|\S+))\s+)?run\s+"
        r"(?P<command>[A-Za-z0-9_.-]+)(?:\s+-m\s+(?P<module>[A-Za-z0-9_.]+))?"
    )
    for match in pattern.finditer(shell):
        command, module = match.group("command", "module")
        entry_points.add(module if command == "python" and module else command)
    return entry_points


# 2119: enforced-mode-cutover-runbook.7.1
def test_every_runbook_command_is_standard_or_repository_installed() -> None:
    plan = parse_enforced_mode_cutover_runbook(RUNBOOK_PATH.read_text())
    shells = [shell for item in (*plan.steps, *plan.gates) for shell in (item.action, item.check)]
    commands = {command for shell in shells for command in _command_heads(shell)}
    assert commands <= ALLOWED_COMMANDS
    assert _command_heads("if undefined-helper; then allowed && chained-helper; fi") == {
        "undefined-helper",
        "allowed",
        "chained-helper",
    }
    assert "undefined-helper" in _command_heads("VALUE=`undefined-helper`")
    entry_points = {module for shell in shells for module in _repository_entry_points(shell)}
    assert entry_points == {
        "panopticon",
        "panopticon.core.cutover_runbook",
        "panopticon.sessionservice.host",
        "panopticon.taskservice",
        "python",
    }
    assert _repository_entry_points("uv run python -m panopticon.undefined") == {
        "panopticon.undefined"
    }
    assert _repository_entry_points("uv run undefined-helper") == {"undefined-helper"}
    assert _repository_entry_points("uv run --quiet undefined-helper") == {"--quiet"}


# 2119: enforced-mode-cutover-runbook.7.1
@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("until curl", "until undefined-helper"),
        (
            "exec env PANOPTICON_CONFIG=",
            "exec undefined-helper PANOPTICON_CONFIG=",
        ),
    ],
)
def test_undefined_helpers_inside_tmux_shell_payloads_are_detected(old: str, new: str) -> None:
    action = parse_enforced_mode_cutover_runbook(RUNBOOK_PATH.read_text()).steps[4].action
    assert old in action
    mutated = action.replace(old, new, 1)
    assert "undefined-helper" in _command_heads(mutated)
    assert not _command_heads(mutated) <= ALLOWED_COMMANDS


# 2119: enforced-mode-cutover-runbook.4.5
@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("--request PUT", "--request POST"),
        ("Authorization: Bearer $READ_TOKEN", "Authorization: Bearer $WRITE_TOKEN"),
        (
            '"$SERVICE_URL/tasks/$CANARY_TASK_ID/turn"',
            '"$SERVICE_URL/tasks/$CANARY_TASK_ID/blocked"',
        ),
        (')" = 401', ')" = 403'),
        (" --request PUT", "\nprintf '%s\\n' '--request PUT'"),
    ],
)
def test_g05_binds_every_read_token_rejection_conjunct_to_one_check(old: str, new: str) -> None:
    text = RUNBOOK_PATH.read_text()
    action = parse_enforced_mode_cutover_runbook(text).gates[4].action
    assert old in action
    mutated = text.replace(action, action.replace(old, new, 1), 1)
    assert "enforced-mode-cutover-runbook.4.5" in validate_enforced_mode_cutover_runbook(mutated)


# 2119: enforced-mode-cutover-runbook.4.5
def test_g05_executes_one_read_token_put_and_requires_generic_401(tmp_path: Path) -> None:
    action = parse_enforced_mode_cutover_runbook(RUNBOOK_PATH.read_text()).gates[4].action
    curl = tmp_path / "curl"
    arguments = tmp_path / "curl-arguments"
    curl.write_text(
        "#!/bin/sh\nprintf 'CALL\\0' >> \"$CUTOVER_CURL_ARGS\"\n"
        'printf \'%s\\0\' "$@" >> "$CUTOVER_CURL_ARGS"\n'
        "printf '%s' \"$CUTOVER_CURL_STATUS\"\n"
    )
    curl.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": str(tmp_path),
        "CUTOVER_CURL_ARGS": str(arguments),
        "CUTOVER_CURL_STATUS": "401",
        "READ_TOKEN": "read-only-token",
        "SERVICE_URL": "https://service.example",
        "CANARY_TASK_ID": "canary-task",
    }

    accepted = subprocess.run(["/bin/bash", "-c", action], env=environment, check=False)
    assert accepted.returncode == 0
    accepted_parts = arguments.read_bytes().rstrip(b"\0").decode().split("\0")
    assert accepted_parts.count("CALL") == 1
    assert accepted_parts[0] == "CALL"
    argv = accepted_parts[1:]
    assert argv[argv.index("--request") + 1] == "PUT"
    assert "Authorization: Bearer read-only-token" in argv
    assert argv[-1] == "https://service.example/tasks/canary-task/turn"

    arguments.unlink()
    environment["CUTOVER_CURL_STATUS"] = "403"
    rejected = subprocess.run(["/bin/bash", "-c", action], env=environment, check=False)
    assert rejected.returncode != 0
    rejected_parts = arguments.read_bytes().rstrip(b"\0").decode().split("\0")
    assert rejected_parts.count("CALL") == 1
    assert rejected_parts[0] == "CALL"
