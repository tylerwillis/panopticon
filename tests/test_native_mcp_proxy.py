"""Consumer-level evidence for native MCP proxy isolation (no LLM call)."""

from __future__ import annotations

import http.server
import os
import shutil
import socketserver
import subprocess
import threading
from pathlib import Path

import pytest


class _CaptureHandler(http.server.BaseHTTPRequestHandler):
    hits: list[tuple[str, str | None]] = []

    def do_POST(self) -> None:
        type(self).hits.append((self.path, self.headers.get("Authorization")))
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


class _ProxyHandler(_CaptureHandler):
    hits: list[tuple[str, str | None]] = []


class _TargetHandler(_CaptureHandler):
    hits: list[tuple[str, str | None]] = []


@pytest.mark.skipif(not shutil.which("claude"), reason="needs the real Claude CLI")
# 2119: REQ-035.47
def test_real_claude_mcp_transport_bypasses_ambient_proxy(
    tmp_path: Path,
) -> None:
    """`claude mcp get` health-checks MCP without making an LLM request."""

    _ProxyHandler.hits = []
    _TargetHandler.hits = []
    servers: list[socketserver.TCPServer] = []
    for handler in (_ProxyHandler, _TargetHandler):
        server = socketserver.TCPServer(("127.0.0.1", 0), handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    proxy, target = servers
    token = "native-mcp-proxy-evidence"
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", os.defpath),
        "PANOPTICON_SERVICE_AUTH_TOKEN": token,
        "HTTP_PROXY": f"http://127.0.0.1:{proxy.server_address[1]}",
        "http_proxy": f"http://127.0.0.1:{proxy.server_address[1]}",
        "NO_PROXY": "127.0.0.1",
        "no_proxy": "127.0.0.1",
    }
    workdir = tmp_path / "work"
    workdir.mkdir()
    try:
        subprocess.run(
            [
                "claude",
                "mcp",
                "add",
                "--scope",
                "user",
                "--transport",
                "http",
                "panopticon",
                f"http://127.0.0.1:{target.server_address[1]}/mcp",
                "--header",
                "Authorization: Bearer ${PANOPTICON_SERVICE_AUTH_TOKEN}",
            ],
            cwd=workdir,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        subprocess.run(
            ["claude", "--dangerously-skip-permissions", "mcp", "get", "panopticon"],
            cwd=workdir,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()

    assert _ProxyHandler.hits == []
    assert _TargetHandler.hits == [("/mcp", f"Bearer {token}")]
