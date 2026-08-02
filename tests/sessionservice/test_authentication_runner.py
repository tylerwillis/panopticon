"""Task-service credential boundary at the local container runner."""

from __future__ import annotations

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


@pytest.mark.parametrize("existing_kind", ["missing", "directory"])
def test_spawn_rejects_missing_service_credential_before_docker(
    tmp_path: Path, existing_kind: str
) -> None:
    # 2119: REQ-034.28.1
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    credential = secrets / "task-service-auth.json"
    if existing_kind == "directory":
        credential.mkdir()
    rec = _Recorder()
    runner = LocalRunner(
        "http://svc:8000",
        auth_file=credential.name,
        secrets_dir=secrets,
        run=rec,
    )

    with pytest.raises(ValueError, match="must be an existing regular file"):
        runner.spawn("t1")

    assert rec.calls == []
    if existing_kind == "missing":
        assert not credential.exists()
    else:
        assert credential.is_dir()
