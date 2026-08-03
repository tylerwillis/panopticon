"""Platform-aware bind selection for integrated task-service startup."""

from __future__ import annotations

import ast
import inspect
import shlex
import textwrap
from pathlib import Path

import pytest

from panopticon.terminal import __main__ as cli


class _Completed:
    returncode = 1


def _service_command(
    monkeypatch: pytest.MonkeyPatch, *, platform: str, configured_host: str | None = None
) -> str:
    if configured_host is None:
        monkeypatch.delenv("PANOPTICON_HOST", raising=False)
    else:
        monkeypatch.setenv("PANOPTICON_HOST", configured_host)
    monkeypatch.setattr("sys.platform", platform)
    monkeypatch.setattr(
        "shutil.which", lambda tool: "/usr/bin/docker" if tool == "docker" else None
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("default-host selection probed Docker directly"),
    )
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> _Completed:
        # Host selection is static: _start_sessions may inspect the platform, but it has no reason
        # to run Docker. Rejecting every non-tmux command makes a runtime networking probe fail.
        assert args[0] == "tmux"
        calls.append(args)
        return _Completed()

    cli._start_sessions(run=fake_run)
    service = next(call for call in calls if "new-session" in call and "service" in call)
    return service[-1]


def _host_options(command: str) -> list[str]:
    launch = command.split(" 2>&1", maxsplit=1)[0]
    argv = shlex.split(launch)
    return [argv[index + 1] for index, item in enumerate(argv) if item == "--host"]


# 2119: REQ-035.29.1
# 2119: REQ-044.1.1
# 2119: REQ-044.2.1
@pytest.mark.parametrize(
    ("platform", "expected_host"),
    [("darwin", "127.0.0.1"), ("linux", "0.0.0.0"), ("win32", "0.0.0.0")],
)
@pytest.mark.parametrize("configured_host", [None, ""])
def test_integrated_service_uses_static_platform_default(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    expected_host: str,
    configured_host: str | None,
) -> None:
    command = _service_command(monkeypatch, platform=platform, configured_host=configured_host)
    assert _host_options(command) == [expected_host]


# 2119: REQ-044.4.1
def test_default_host_selector_is_a_pure_function_of_platform_identity() -> None:
    selector = cli._default_service_host
    assert list(inspect.signature(selector).parameters) == ["platform"]
    function = ast.parse(textwrap.dedent(inspect.getsource(selector))).body[0]
    assert isinstance(function, ast.FunctionDef)
    assert len(function.body) == 1
    statement = function.body[0]
    assert isinstance(statement, ast.Return)
    assert {type(node) for node in ast.walk(statement)} <= {
        ast.Return,
        ast.IfExp,
        ast.Compare,
        ast.Name,
        ast.Load,
        ast.Constant,
        ast.Eq,
    }
    loaded_names = {
        node.id
        for node in ast.walk(statement)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    assert loaded_names == {"platform"}


# 2119: REQ-044.3.1
@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
@pytest.mark.parametrize(
    "configured_host",
    [
        "100.101.102.103",
        "127.0.0.2",
        "::1",
        "2001:0DB8:0000:0000:0000:0000:0000:0001",
        "control.example.test",
        "Control.Example.TEST",
    ],
)
def test_integrated_service_honors_panopticon_host_on_every_platform(
    monkeypatch: pytest.MonkeyPatch, platform: str, configured_host: str
) -> None:
    command = _service_command(monkeypatch, platform=platform, configured_host=configured_host)
    assert _host_options(command) == [configured_host]


def test_integrated_service_shell_quotes_configured_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_host = "control.example.test; echo injected"
    command = _service_command(monkeypatch, platform="linux", configured_host=configured_host)
    assert _host_options(command) == [configured_host]


# 2119: REQ-044.5.1
def test_auth_docs_describe_one_coherent_platform_aware_bind_policy() -> None:
    auth_docs = (Path(__file__).parents[2] / "docs" / "auth.md").read_text()
    paragraph = next(
        block
        for block in auth_docs.split("\n\n")
        if block.startswith("The standalone task-service")
    )
    assert " ".join(paragraph.splitlines()) == (
        "The standalone task-service launcher defaults to `127.0.0.1`. The integrated "
        "`panopticon start` and `panopticon host` commands default to `127.0.0.1` on Darwin and "
        "`0.0.0.0` on Linux and Windows so native containers can reach the service. "
        "On native Linux this compatibility default intentionally listens on every host interface "
        "because bridge containers cannot reach host loopback; safe operation therefore depends "
        "on enforced task-service authentication plus independently encrypted and access-controlled "
        "transport. `PANOPTICON_HOST` overrides both launch paths when the operator selects another "
        "container-reachable intended interface. Bearer tokens travel over HTTP, so a broad bind "
        "is appropriate only where every reachable interface has those protections."
    )
