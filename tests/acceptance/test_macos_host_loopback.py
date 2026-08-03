"""macOS runtime acceptance for container-to-host loopback routing."""

from __future__ import annotations

import contextlib
import http.server
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator

import pytest

_IMAGE = "panopticon-base"


def _ready() -> bool:
    if sys.platform != "darwin":
        return False
    if shutil.which("docker") is None:
        return False
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        return False
    return (
        subprocess.run(["docker", "image", "inspect", _IMAGE], capture_output=True).returncode == 0
    )


@contextlib.contextmanager
def _loopback_server() -> Iterator[int]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.skipif(
    not _ready(),
    reason="needs Darwin with a working Docker-compatible runtime and built panopticon-base image",
)
def test_macos_container_reaches_loopback_host_through_internal_name() -> None:
    """Exercise Docker Desktop or OrbStack without detecting which runtime is active."""
    with _loopback_server() as port:
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--add-host",
                "host.docker.internal:host-gateway",
                _IMAGE,
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                f"http://host.docker.internal:{port}/",
            ],
            check=True,
            capture_output=True,
        )
