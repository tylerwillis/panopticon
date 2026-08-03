from argparse import Namespace
from pathlib import Path

import httpx
import pytest

from panopticon.sessionservice import shell_liveness


def test_permanent_auth_rejection_stops_and_removes_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text("secret")
    monkeypatch.setattr(shell_liveness.threading.Thread, "start", lambda self: None)
    monkeypatch.setattr(shell_liveness, "_session_exists", lambda socket, session: True)

    def rejected(self, task_id, *, container_id, runner_id):
        request = httpx.Request("GET", "http://service/tasks/t/live")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("rejected", request=request, response=response)
        yield

    monkeypatch.setattr(shell_liveness.TaskServiceClient, "live", rejected)
    shell_liveness.hold_shell_liveness(
        Namespace(
            snapshot=str(snapshot),
            socket="panopticon",
            session="panopticon-t",
            service_url="http://service",
            task_id="t",
            runner_id="r",
        )
    )
    assert not snapshot.exists()
