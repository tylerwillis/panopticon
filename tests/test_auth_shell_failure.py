"""Shell auth extraction fails closed before invoking curl."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path


def test_shell_helper_does_not_call_curl_when_credential_parsing_fails(tmp_path: Path) -> None:
    called = tmp_path / "curl-called"
    credential = tmp_path / "invalid.json"
    credential.write_text("not-json")
    task_lib = Path("src/panopticon/sessionservice/task_lib.sh").read_text()
    shell = f"""
curl() {{ touch {shlex.quote(str(called))}; }}
{task_lib}
_panopticon_curl --silent http://service
"""

    completed = subprocess.run(
        ["sh", "-c", shell],
        env={
            **os.environ,
            "PANOPTICON_SERVICE_AUTH_FILE": str(credential),
        },
        check=False,
    )

    assert completed.returncode != 0
    assert not called.exists()
