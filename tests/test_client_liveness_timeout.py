"""Regression coverage for silent, half-open liveness response streams."""

from __future__ import annotations

import math
import socket
import threading
import time
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager

import httpx
import pytest

from panopticon import client as client_module
from panopticon.client import TaskServiceClient
from panopticon.core import liveness
from panopticon.taskservice import api


@contextmanager
def _silent_http_server() -> Iterator[str]:
    """Accept one request, send streaming headers, then remain silent until the client gives up."""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = int(listener.getsockname()[1])
    release = threading.Event()

    def serve() -> None:
        conn, _ = listener.accept()
        with conn:
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
            release.wait(timeout=2)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        release.set()
        listener.close()
        thread.join(timeout=2)


def _assert_silent_stream_times_out(
    open_stream: Callable[[TaskServiceClient], Generator[None, None, None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_timeout = 0.1
    monkeypatch.setattr(client_module, "LIVENESS_READ_TIMEOUT_SECONDS", read_timeout)
    with _silent_http_server() as base_url:
        client = TaskServiceClient(httpx.Client(base_url=base_url))
        stream = open_stream(client)
        started = time.monotonic()
        with pytest.raises(httpx.ReadTimeout):
            next(stream)
        assert time.monotonic() - started < 1.0


# 2119: REQ-039.1.1
def test_liveness_read_timeout_is_finite_and_exceeds_shared_keepalive() -> None:
    assert liveness.LIVENESS_READ_TIMEOUT_SECONDS > liveness.LIVENESS_KEEPALIVE_SECONDS
    assert math.isfinite(liveness.LIVENESS_READ_TIMEOUT_SECONDS)
    assert client_module.LIVENESS_READ_TIMEOUT_SECONDS is liveness.LIVENESS_READ_TIMEOUT_SECONDS
    assert api.LIVENESS_KEEPALIVE_SECONDS is liveness.LIVENESS_KEEPALIVE_SECONDS

    observed: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        observed.append(float(request.extensions["timeout"]["read"]))
        return httpx.Response(200, content=b"keepalive\n")

    client = TaskServiceClient(
        httpx.Client(base_url="http://service", transport=httpx.MockTransport(respond))
    )
    assert next(client.live("task-1", container_id="container-1")) is None
    assert next(client.live_runner("runner-1")) is None
    assert observed == [liveness.LIVENESS_READ_TIMEOUT_SECONDS] * 2


# 2119: REQ-039.2.1
def test_container_liveness_times_out_when_accepted_stream_is_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_silent_stream_times_out(
        lambda client: client.live("task-1", container_id="container-1", runner_id="runner-1"),
        monkeypatch,
    )


# 2119: REQ-039.2.2
def test_runner_liveness_times_out_when_accepted_stream_is_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_silent_stream_times_out(
        lambda client: client.live_runner("runner-1", host="worker.example.com"),
        monkeypatch,
    )
