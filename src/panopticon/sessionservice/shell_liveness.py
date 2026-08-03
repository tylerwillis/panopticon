"""Detached liveness owner for host shell tasks.

The helper owns its HTTP connection and observes the tmux session directly.  Forced tmux teardown
therefore closes liveness without signalling a PID that may have been recycled by another process.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import threading
import time
from pathlib import Path

import httpx

from panopticon.client import TaskServiceClient


def _session_exists(socket: str, session: str) -> bool:
    command = ["tmux", *(["-L", socket] if socket else []), "has-session", "-t", session]
    return subprocess.run(command, capture_output=True, check=False).returncode == 0


def hold_shell_liveness(args: argparse.Namespace) -> None:
    snapshot = Path(args.snapshot) if args.snapshot else None

    def watch_session() -> None:
        while _session_exists(args.socket, args.session):
            time.sleep(0.1)
        if snapshot is not None:
            snapshot.unlink(missing_ok=True)
        os._exit(0)

    threading.Thread(target=watch_session, daemon=True).start()
    with httpx.Client(base_url=args.service_url, trust_env=False) as http:
        client = TaskServiceClient(http)
        while _session_exists(args.socket, args.session):
            try:
                for _ in client.live(
                    args.task_id, container_id=args.session, runner_id=args.runner_id
                ):
                    pass
            except httpx.HTTPError:
                time.sleep(0.25)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service-url", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--runner-id", required=True)
    parser.add_argument("--socket", default="")
    parser.add_argument("--session", required=True)
    parser.add_argument("--snapshot", default="")
    hold_shell_liveness(parser.parse_args())


if __name__ == "__main__":  # pragma: no cover - module entry point
    main()
