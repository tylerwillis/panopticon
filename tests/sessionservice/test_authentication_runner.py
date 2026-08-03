"""Task-service credential boundary at the local container runner."""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from pathlib import Path

import pytest

from panopticon.sessionservice.local_runner import LocalRunner


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        interactive: bool = False,
        verbose: bool = False,
    ) -> str:
        del check, interactive, verbose
        self.calls.append(list(args))
        return ""


@pytest.mark.parametrize("existing_kind", ["missing", "directory", "fifo", "malformed"])
def test_spawn_rejects_missing_service_credential_before_docker(
    tmp_path: Path, existing_kind: str
) -> None:
    # 2119: REQ-035.28.1
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    credential = secrets / "task-service-auth.json"
    original_stat = None
    if existing_kind == "directory":
        credential.mkdir()
        (credential / "sentinel").write_text("unchanged")
        original_stat = credential.stat()
    elif existing_kind == "fifo":
        os.mkfifo(credential)
        original_stat = credential.stat()
    elif existing_kind == "malformed":
        credential.write_text('{"read": [], "write": ["token"]}')
        credential.chmod(0o600)
    rec = _Recorder()
    runner = LocalRunner(
        "http://svc:8000",
        auth_file=credential.name,
        secrets_dir=secrets,
        run=rec,
    )

    with pytest.raises(
        ValueError, match="authentication credential file is invalid or unavailable"
    ):
        runner.spawn("t1")

    assert rec.calls == []
    if existing_kind == "missing":
        assert not credential.exists()
    elif existing_kind == "directory":
        assert credential.is_dir()
        assert credential.stat() == original_stat
        assert {path.name: path.read_text() for path in credential.iterdir()} == {
            "sentinel": "unchanged"
        }
    elif existing_kind == "fifo":
        assert credential.stat() == original_stat
        assert stat.S_ISFIFO(credential.stat().st_mode)
    else:
        assert credential.is_file()
