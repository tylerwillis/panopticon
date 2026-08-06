"""Host-side image clipboard bridge for Panopticon's attached task panes."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

MAX_IMAGE_BYTES = 20 * 1024 * 1024
DARWIN_PNG_SCRIPT = "get the clipboard as «class PNGf»"
FAILURE_MESSAGE = (
    "Image paste is unavailable; save the image under the task workspace and paste its path"
)

Run = Callable[..., subprocess.CompletedProcess[bytes]]


@dataclass(frozen=True)
class CapturedImage:
    data: bytes
    extension: str


@dataclass(frozen=True)
class PasteResult:
    ok: bool


def _run(
    command: Sequence[str],
    *,
    input: bytes | None = None,
    capture_output: bool = False,
    check: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, input=input, capture_output=capture_output, check=check)


def capture_clipboard_image(
    *,
    platform: str = sys.platform,
    environ: Mapping[str, str] = os.environ,
    which: Callable[[str], str | None] = shutil.which,
    run: Run = _run,
) -> CapturedImage:
    """Read PNG bytes from the host's native clipboard facility."""

    if platform == "darwin":
        executable = which("osascript")
        if not executable:
            raise RuntimeError("osascript is unavailable")
        completed = run((executable, "-e", DARWIN_PNG_SCRIPT), capture_output=True)
        if completed.returncode:
            raise RuntimeError("pasteboard capture failed")
        raw_output = completed.stdout
        output = (
            raw_output.decode(errors="replace") if isinstance(raw_output, bytes) else raw_output
        )
        match = re.fullmatch(r"\s*«data PNGf([0-9A-Fa-f]*)»\s*", output)
        if not match:
            raise RuntimeError("pasteboard did not return PNG data")
        return CapturedImage(bytes.fromhex(match.group(1)), "png")

    if platform == "linux" and environ.get("WAYLAND_DISPLAY") and (executable := which("wl-paste")):
        command: tuple[str, ...] = (executable, "--type", "image/png")
    elif platform == "linux" and (executable := which("xclip")):
        command = (
            executable,
            "-selection",
            "clipboard",
            "-target",
            "image/png",
            "-out",
        )
    else:
        raise RuntimeError("no supported image clipboard tool is available")
    completed = run(command, capture_output=True)
    if completed.returncode:
        raise RuntimeError("clipboard capture failed")
    data = completed.stdout
    if isinstance(data, str):
        data = data.encode()
    return CapturedImage(data, "png")


def container_image_path(extension: str, *, token: Callable[[], str] | None = None) -> str:
    identifier = (token or (lambda: uuid.uuid4().hex))()
    return f"/tmp/panopticon-clipboard-{identifier}.{extension}"


def staging_script(path: str) -> str:
    return f"umask 077; exec dd of={shlex.quote(path)} status=none"


def image_paste_binding(command: str) -> str:
    return f"bind-key -T root C-v run-shell -b '{command} #{{session_name}} #{{pane_id}}'"


def _failure(pane: str, run: Run) -> PasteResult:
    run(["tmux", "display-message", "-t", pane, FAILURE_MESSAGE], check=False)
    return PasteResult(ok=False)


def paste_clipboard_image(
    session: str,
    pane: str,
    *,
    capture: Callable[[], CapturedImage] = capture_clipboard_image,
    run: Run = _run,
    token: Callable[[], str] | None = None,
) -> PasteResult:
    """Capture, privately stage, and bracket-paste one container-local image path."""

    try:
        image = capture()
        if not image.data or len(image.data) > MAX_IMAGE_BYTES:
            return _failure(pane, run)
        identifier = (token or (lambda: uuid.uuid4().hex))()
        path = container_image_path(image.extension, token=lambda: identifier)
        staged = run(
            [
                "docker",
                "exec",
                "--interactive",
                "--user",
                "panopticon",
                session,
                "sh",
                "-c",
                staging_script(path),
            ],
            input=image.data,
            capture_output=True,
        )
        if staged.returncode:
            return _failure(pane, run)
        buffer = f"panopticon-image-{identifier}"
        loaded = run(
            ["tmux", "load-buffer", "-b", buffer, "-"],
            input=path.encode(),
            capture_output=True,
        )
        if loaded.returncode:
            return _failure(pane, run)
        pasted = run(
            ["tmux", "paste-buffer", "-p", "-d", "-b", buffer, "-t", pane],
            capture_output=True,
        )
        if pasted.returncode:
            return _failure(pane, run)
        return PasteResult(ok=True)
    except (OSError, RuntimeError, ValueError):
        return _failure(pane, run)


def _forward_ctrl_v(pane: str, run: Run) -> int:
    run(["tmux", "send-keys", "-t", pane, "C-v"], check=False)
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    run: Run = _run,
    capture: Callable[[], CapturedImage] = capture_clipboard_image,
    token: Callable[[], str] | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        return 2
    session, pane = args
    if not session.startswith("panopticon-"):
        return _forward_ctrl_v(pane, run)
    probe = run(
        [
            "docker",
            "ps",
            "--filter",
            f"name=^{session}$",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
    )
    output = probe.stdout
    if isinstance(output, bytes):
        names = output.decode(errors="replace").splitlines()
    else:
        names = str(output).splitlines()
    if probe.returncode or session not in names:
        return _forward_ctrl_v(pane, run)
    return (
        0 if paste_clipboard_image(session, pane, capture=capture, run=run, token=token).ok else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
