"""The long-lived container entrypoint: set slug → hold the liveness connection → reconnect.

Uses a fake client — no network, no Docker, no LLM. The fake's ``live`` stands in for the held
``/live`` stream: it yields a tick per server keepalive and records when the connection is opened
and closed, so we can assert the connection's lifetime *is* the liveness signal (no heartbeat)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from itertools import pairwise
from typing import Any

import httpx
import pytest

from panopticon.client import TaskServiceClient
from panopticon.container import entrypoint


class _FakeClient:
    """Records the entrypoint's calls; stands in for TaskServiceClient.

    ``drops`` makes the first N liveness connections fail mid-stream (a simulated network blip) so
    the reconnect path can be exercised; ``live`` is otherwise an endless keepalive stream the
    caller stops by closing."""

    def __init__(self, slug: str | None = None, *, drops: int = 0) -> None:
        self.slug = slug
        self.calls: list[str] = []
        self.live_connections = 0
        self.closed = 0
        self.containers: list[str] = []
        self._drops = drops

    def get_task(self, task_id: str) -> dict[str, Any]:
        return {"slug": self.slug}

    def set_slug(self, task_id: str, slug: str) -> dict[str, Any]:
        self.calls.append("set_slug")
        self.slug = slug
        return {"slug": slug}

    def live(
        self, task_id: str, *, container_id: str, runner_id: str | None = None
    ) -> Iterator[None]:
        self.calls.append("live")
        self.live_connections += 1
        self.containers.append(container_id)
        conn = self.live_connections

        def _gen() -> Iterator[None]:
            try:
                while True:
                    if conn <= self._drops:
                        raise httpx.ConnectError("simulated connection drop")
                    yield None
            finally:
                self.closed += 1
                self.calls.append("close")

        return _gen()


def _stop_after(n: int) -> Callable[[], bool]:
    """A ``running`` predicate true for ``n`` calls, then false."""
    seen = 0

    def running() -> bool:
        nonlocal seen
        seen += 1
        return seen <= n

    return running


def _raise_after(n: int) -> Callable[[], bool]:
    """A ``running`` predicate true for ``n`` calls, then raising (a non-clean stop)."""
    seen = 0

    def running() -> bool:
        nonlocal seen
        seen += 1
        if seen > n:
            raise RuntimeError("kill")
        return True

    return running


def _serve(client: _FakeClient, **kw: Any) -> None:
    entrypoint.serve(
        client,  # type: ignore[arg-type]
        "t1",
        container_id="c1",
        running=kw.pop("running", _stop_after(2)),
        sleep=kw.pop("sleep", lambda _s: None),
        **kw,
    )


def test_serve_holds_liveness_connection_and_closes_cleanly() -> None:
    client = _FakeClient()
    _serve(client)
    assert client.live_connections == 1  # one held connection for the container's life
    assert client.closed == 1  # a clean stop closes it (a clean deregister)
    assert client.calls[-1] == "close"


def test_serve_reconnects_after_a_dropped_connection() -> None:
    client = _FakeClient(drops=1)  # first connection drops underneath us
    naps: list[float] = []
    entrypoint.serve(
        client,  # type: ignore[arg-type]
        "t1",
        container_id="c1",
        running=_stop_after(3),
        reconnect_backoff=0.25,
        sleep=naps.append,
    )
    assert client.live_connections == 2  # dropped once, re-opened
    assert client.containers == ["c1", "c1"]  # the same container re-asserts liveness
    assert naps == [0.25]  # backed off once before reconnecting


@pytest.mark.parametrize("status", [401, 403])
def test_serve_stops_after_permanent_liveness_rejection(status: int) -> None:
    # 2119: REQ-035.36.1
    request = httpx.Request("GET", "http://service/tasks/t1/live")
    response = httpx.Response(status, request=request)

    attempts: list[int] = []
    naps: list[float] = []

    class _RejectedClient(_FakeClient):
        def live(self, *_args: object, **_kwargs: object) -> Iterator[None]:
            attempts.append(1)

            def rejected() -> Iterator[None]:
                raise httpx.HTTPStatusError("rejected", request=request, response=response)
                yield

            return rejected()

    with pytest.raises(RuntimeError, match="permanently rejected"):
        _serve(_RejectedClient(), running=lambda: True, sleep=naps.append)
    assert len(attempts) == 1
    assert naps == []


@pytest.mark.parametrize("status", [401, 403])
def test_real_client_serve_stops_after_permanent_liveness_rejection(status: int) -> None:
    # 2119: REQ-035.36.1
    attempts: list[httpx.Request] = []
    naps: list[float] = []

    def reject(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(status, json={"detail": "authentication required"})

    http = httpx.Client(base_url="http://service", transport=httpx.MockTransport(reject))
    with pytest.raises(RuntimeError, match="permanently rejected"):
        _serve(TaskServiceClient(http), running=lambda: True, sleep=naps.append)  # type: ignore[arg-type]
    assert len(attempts) == 1
    assert naps == []


def test_serve_sets_slug_when_unset_and_proposed() -> None:
    client = _FakeClient(slug=None)
    _serve(client, proposed_slug="fix-widget", running=_stop_after(1))
    assert client.slug == "fix-widget"
    assert "set_slug" in client.calls


def test_serve_leaves_existing_slug_alone() -> None:
    client = _FakeClient(slug="chosen")
    _serve(client, proposed_slug="other", running=_stop_after(1))
    assert client.slug == "chosen"
    assert "set_slug" not in client.calls


def test_serve_closes_connection_even_on_error() -> None:
    client = _FakeClient()
    with pytest.raises(RuntimeError):
        _serve(client, running=_raise_after(1))
    assert client.closed == 1  # the open connection was closed despite the error (finally ran)


def test_main_reads_env_and_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(drops=1)  # force a reconnect so the backoff env is exercised
    seen_url: list[str] = []
    naps: list[float] = []
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc:8000")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    monkeypatch.setenv("PANOPTICON_CONTAINER_ID", "panopticon-t1")
    monkeypatch.setenv("PANOPTICON_RUNNER_ID", "local")
    monkeypatch.setenv("PANOPTICON_RECONNECT_BACKOFF", "0.5")

    def factory(url: str) -> _FakeClient:
        seen_url.append(url)
        return client

    entrypoint.main(
        client_factory=factory,  # type: ignore[arg-type]
        running=_stop_after(3),
        sleep=naps.append,
    )
    assert seen_url == ["http://svc:8000"]
    assert client.live_connections == 2  # held, dropped, re-opened
    assert naps == [0.5]  # PANOPTICON_RECONNECT_BACKOFF threaded through


def test_main_treats_runner_pinned_empty_optional_values_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from panopticon.sessionservice.local_runner import LocalRunner

    calls: list[list[str]] = []

    def record(args: list[str], **_kwargs: object) -> str:
        calls.append(args)
        return "%1\n" if "display-message" in args else ""

    LocalRunner("http://svc:8000", run=record).spawn("t1")
    docker_run = next(call for call in calls if call[:2] == ["docker", "run"])
    emitted = {
        item.split("=", 1)[0]: item.split("=", 1)[1]
        for flag, item in pairwise(docker_run)
        if flag == "--env"
    }
    for name, value in emitted.items():
        monkeypatch.setenv(name, value)
    client = _FakeClient()

    entrypoint.main(
        client_factory=lambda _url: client,  # type: ignore[arg-type,return-value]
        running=lambda: False,
    )

    assert "set_slug" not in client.calls
