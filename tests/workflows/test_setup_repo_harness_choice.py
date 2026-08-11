"""Regression tests for setup-repo's explicit harness-choice boundary."""

from __future__ import annotations

import importlib.resources
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from panopticon.workflows import SetupRepo

_LIB = (importlib.resources.files("panopticon.workflows") / "setup_repo_lib.sh").read_text()
_TASK_LIB = (importlib.resources.files("panopticon.sessionservice") / "task_lib.sh").read_text()
_FULL_SCRIPT = SetupRepo().shell_script()


def _sh(body: str) -> str:
    result = subprocess.run(
        ["sh", "-c", f"{_LIB}\n{body}"],
        capture_output=True,
        text=True,
        check=True,
        stdin=subprocess.DEVNULL,
    )
    return result.stdout


# 2119-spec: explicit-onboarding-harness-choice
# 2119: 4.1
@pytest.mark.parametrize("raw_default", ["null", '""'])
def test_load_repo_auth_context_rejects_missing_default_harness(raw_default: str) -> None:
    body = f"""
PANOPTICON_PYTHON={shlex.quote(sys.executable)}
PANOPTICON_SERVICE_URL=http://service
PANOPTICON_TASK_ID=task
_panopticon_curl() {{
    case "$*" in
        *'/tasks/task'*) printf '%s' '{{"repo_id":"repo"}}' ;;
        *'/repos/repo'*) printf '%s' '{{"default_harness":{raw_default},"credential_dir":""}}' ;;
    esac
}}
if load_repo_auth_context; then
    printf 'unexpected:%s\n' "$default_harness"
else
    printf 'rejected:%s\n' "$default_harness"
fi
"""

    assert _sh(body) == "rejected:\n"


# 2119: 4.2
@pytest.mark.parametrize(
    "repo_response",
    [
        '{"default_harness":null,"credential_dir":""}',
        '{"default_harness":"","credential_dir":""}',
        '{"credential_dir":""}',
        "{",
        None,
    ],
)
def test_setup_repo_stops_before_auth_when_default_harness_is_missing(
    tmp_path: Path, repo_response: str | None
) -> None:
    env_file = tmp_path / "repo.env"
    env_file.write_text("")
    instrumented_script = _FULL_SCRIPT.replace(
        "dispatch_harness_auth() {\n",
        "dispatch_harness_auth() {\n    printf 'AUTH_DISPATCHED:%s\\n' \"$1\"\n",
        1,
    )
    assert instrumented_script != _FULL_SCRIPT
    repo_action = (
        "return 22" if repo_response is None else f"printf '%s' {shlex.quote(repo_response)}"
    )
    shell = f"""
curl() {{
    case "$*" in
        *'/tasks/task'*) printf '%s' '{{"repo_id":"repo"}}' ;;
        *'/repos/repo'*) {repo_action} ;;
    esac
}}
{_TASK_LIB}
{instrumented_script}
"""
    completed = subprocess.run(
        ["sh", "-c", shell],
        input="sk-ant-api-test\n\n",
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "PANOPTICON_PYTHON": sys.executable,
            "PANOPTICON_SERVICE_URL": "http://service",
            "PANOPTICON_TASK_ID": "task",
            "PANOPTICON_ENV_FILE": str(env_file),
            "PANOPTICON_GIT_URL": "file:///repo",
            "PANOPTICON_REPO_NAME": "repo",
        },
    )

    assert completed.returncode != 0
    output = completed.stdout + completed.stderr
    assert "Choose a default harness for this repo before running setup-repo." in output
    assert "To set up:" not in output
    assert "AUTH_DISPATCHED:" not in output
