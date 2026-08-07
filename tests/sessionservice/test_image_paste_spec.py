"""REQ-053 contract tests for the attached-session image-paste bridge."""

# ruff: noqa: B023

from __future__ import annotations

import subprocess

from panopticon.sessionservice.image_paste import (
    DARWIN_PNG_SCRIPT,
    MAX_IMAGE_BYTES,
    CapturedImage,
    capture_clipboard_image,
    container_image_path,
    image_paste_binding,
    main,
    paste_clipboard_image,
    staging_script,
)
from panopticon.sessionservice.tmux_defaults import defaults_argv, server_default_config_text


def _which(present: set[str]):
    return lambda tool: f"/usr/bin/{tool}" if tool in present else None


# 2119: REQ-053.1.1
def test_binding_routes_container_task_ctrl_v_to_its_originating_pane() -> None:
    binding = image_paste_binding("python -m panopticon.sessionservice.image_paste")
    assert binding == (
        "bind-key -T root C-v run-shell -b "
        "'python -m panopticon.sessionservice.image_paste #{session_name} #{pane_id}'"
    )
    assert binding in server_default_config_text(clipboard=None)
    assert defaults_argv(None) == []

    calls: list[tuple[list[str], bytes | None]] = []

    def run(argv: list[str], *, input: bytes | None = None, **_kwargs: object):
        calls.append((argv, input))
        return subprocess.CompletedProcess(argv, 0, stdout=b"panopticon-task-123\n", stderr=b"")

    exit_code = main(
        ["panopticon-task-123", "%7"],
        run=run,
        capture=lambda: CapturedImage(b"png", "png"),
        token=lambda: "fixed",
    )
    assert exit_code == 0
    assert calls[0][0] == [
        "docker",
        "ps",
        "--filter",
        "name=^panopticon-task-123$",
        "--format",
        "{{.Names}}",
    ]
    assert calls[1][0][:6] == [
        "docker",
        "exec",
        "--interactive",
        "--user",
        "panopticon",
        "panopticon-task-123",
    ]
    assert calls[-1][0][-2:] == ["-t", "%7"]
    assert not any(argv[-1:] == ["C-v"] for argv, _ in calls)


# 2119: REQ-053.1.1
def test_success_never_forwards_ctrl_v_after_invoking_the_bridge() -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object):
        calls.append(argv)
        if argv[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(argv, 0, stdout=b"panopticon-task-123\n", stderr=b"")
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    assert (
        main(
            ["panopticon-task-123", "%7"],
            run=run,
            capture=lambda: CapturedImage(b"png", "png"),
            token=lambda: "fixed",
        )
        == 0
    )
    assert [argv[0:2] for argv in calls] == [
        ["docker", "ps"],
        ["docker", "exec"],
        ["tmux", "load-buffer"],
        ["tmux", "paste-buffer"],
    ]
    assert [argv for argv in calls if argv[:2] == ["tmux", "send-keys"]] == []


# 2119: REQ-053.1.2
def test_binding_forwards_ctrl_v_outside_a_running_task_container() -> None:
    for session in ("dashboard", "service", "panopticon-stopped-task"):
        calls: list[list[str]] = []

        def run(argv: list[str], **_kwargs: object):
            calls.append(argv)
            running = session != "panopticon-stopped-task" and argv[0] == "docker"
            return subprocess.CompletedProcess(
                argv,
                0 if running else 1,
                stdout=f"{session}\n".encode() if running else b"",
                stderr=b"",
            )

        assert main([session, "%9"], run=run) == 0
        assert calls[-1] == ["tmux", "send-keys", "-t", "%9", "C-v"]
        assert sum(argv[-1:] == ["C-v"] for argv in calls) == 1
        assert not any(argv[:2] == ["docker", "exec"] for argv in calls)


# 2119: REQ-053.2.1
def test_darwin_capture_uses_the_native_pasteboard_png_type() -> None:
    calls: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...], **_kwargs: object):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="«data PNGf706e672d6279746573»\n".encode(),
            stderr=b"",
        )

    captured = capture_clipboard_image(
        platform="darwin", environ={}, which=_which({"osascript"}), run=run
    )
    assert captured == CapturedImage(b"png-bytes", "png")
    assert calls == [("/usr/bin/osascript", "-e", DARWIN_PNG_SCRIPT)]
    assert DARWIN_PNG_SCRIPT == "get the clipboard as «class PNGf»"


# 2119: REQ-053.2.2
def test_linux_capture_prefers_wayland_png() -> None:
    calls: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...], **_kwargs: object):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=b"png-bytes", stderr=b"")

    captured = capture_clipboard_image(
        platform="linux",
        environ={"WAYLAND_DISPLAY": "wayland-0"},
        which=_which({"wl-paste", "xclip"}),
        run=run,
    )
    assert captured == CapturedImage(b"png-bytes", "png")
    assert calls == [("/usr/bin/wl-paste", "--type", "image/png")]


# 2119: REQ-053.2.3
def test_linux_capture_falls_back_to_x11_png() -> None:
    calls: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...], **_kwargs: object):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=b"png-bytes", stderr=b"")

    captured = capture_clipboard_image(
        platform="linux",
        environ={"DISPLAY": ":0"},
        which=_which({"xclip"}),
        run=run,
    )
    assert captured == CapturedImage(b"png-bytes", "png")
    assert calls == [
        (
            "/usr/bin/xclip",
            "-selection",
            "clipboard",
            "-target",
            "image/png",
            "-out",
        )
    ]


# 2119: REQ-053.2.4
def test_empty_and_oversize_images_are_rejected_before_container_io() -> None:
    calls: list[tuple[list[str], bytes | None]] = []

    def run(argv: list[str], *, input: bytes | None = None, **_kwargs: object):
        calls.append((argv, input))
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    assert MAX_IMAGE_BYTES == 20 * 1024 * 1024
    for data in (b"", b"x" * (MAX_IMAGE_BYTES + 1)):
        result = paste_clipboard_image(
            "panopticon-task",
            "%1",
            capture=lambda data=data: CapturedImage(data, "png"),
            run=run,
            token=lambda: "fixed",
        )
        assert not result.ok
    assert not any(argv[:2] == ["docker", "exec"] for argv, _ in calls)

    accepted = paste_clipboard_image(
        "panopticon-task",
        "%1",
        capture=lambda: CapturedImage(b"x" * MAX_IMAGE_BYTES, "png"),
        run=run,
        token=lambda: "boundary",
    )
    assert accepted.ok
    assert any(argv[:2] == ["docker", "exec"] for argv, _ in calls)


# 2119: REQ-053.3.1
# 2119: REQ-053.3.2
# 2119: REQ-053.4.1
def test_success_stages_unique_private_files_then_exactly_bracket_pastes_the_path() -> None:
    calls: list[tuple[list[str], bytes | None]] = []

    def run(argv: list[str], *, input: bytes | None = None, **_kwargs: object):
        calls.append((argv, input))
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    png = b"\x89PNG\r\n\x1a\ncontent"
    tokens = iter(("first", "second"))
    for _ in range(2):
        result = paste_clipboard_image(
            "panopticon-task",
            "%7",
            capture=lambda: CapturedImage(png, "png"),
            run=run,
            token=lambda: next(tokens),
        )
        assert result.ok
    stages = [call for call in calls if call[0][:2] == ["docker", "exec"]]
    assert len(stages) == 2
    assert [argv for argv, _ in stages] == [
        [
            "docker",
            "exec",
            "--interactive",
            "--user",
            "panopticon",
            "panopticon-task",
            "sh",
            "-c",
            f"umask 077; exec dd of=/tmp/panopticon-clipboard-{token}.png status=none",
        ]
        for token in ("first", "second")
    ]
    assert [data for _, data in stages] == [png, png]
    assert all(data == png for argv, data in calls if argv[:2] == ["docker", "exec"])
    assert all(data != png for argv, data in calls if argv[:2] != ["docker", "exec"])
    loads = [call for call in calls if "load-buffer" in call[0]]
    pastes = [call for call in calls if "paste-buffer" in call[0]]
    assert loads == [
        (
            ["tmux", "load-buffer", "-b", f"panopticon-image-{token}", "-"],
            f"/tmp/panopticon-clipboard-{token}.png".encode(),
        )
        for token in ("first", "second")
    ]
    assert pastes == [
        (
            [
                "tmux",
                "paste-buffer",
                "-p",
                "-d",
                "-b",
                f"panopticon-image-{token}",
                "-t",
                "%7",
            ],
            None,
        )
        for token in ("first", "second")
    ]
    assert [argv[0] for argv, _ in calls] == [
        "docker",
        "tmux",
        "tmux",
        "docker",
        "tmux",
        "tmux",
    ]
    assert not any("Enter" in argv or "send-keys" in argv for argv, _ in calls)


# 2119: REQ-053.3.1
def test_default_paths_are_unique_and_staging_script_creates_mode_0600_file(tmp_path) -> None:
    paths = {container_image_path("png") for _ in range(100)}
    assert len(paths) == 100
    assert all(path.startswith("/tmp/panopticon-clipboard-") for path in paths)
    assert all(path.endswith(".png") for path in paths)

    destination = tmp_path / "captured.png"
    completed = subprocess.run(
        ["sh", "-c", staging_script(str(destination))],
        input=b"image bytes",
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0
    assert destination.read_bytes() == b"image bytes"
    assert destination.stat().st_mode & 0o777 == 0o600


# 2119: REQ-053.5.1
def test_each_failure_reports_workaround_without_pasting_a_path() -> None:
    for failure in ("capture", "empty", "oversize", "staging", "delivery"):
        calls: list[list[str]] = []

        def capture() -> CapturedImage:
            if failure == "capture":
                raise RuntimeError("clipboard unavailable")
            if failure == "empty":
                return CapturedImage(b"", "png")
            if failure == "oversize":
                return CapturedImage(b"x" * (MAX_IMAGE_BYTES + 1), "png")
            return CapturedImage(b"valid", "png")

        def run(argv: list[str], **_kwargs: object):
            calls.append(argv)
            fails = (failure == "staging" and argv[:2] == ["docker", "exec"]) or (
                failure == "delivery" and "paste-buffer" in argv
            )
            return subprocess.CompletedProcess(
                argv, 1 if fails else 0, stdout=b"", stderr=b"failed" if fails else b""
            )

        result = paste_clipboard_image(
            "panopticon-task",
            "%3",
            capture=capture,
            run=run,
            token=lambda: "fixed",
        )
        assert not result.ok
        paste_indexes = [i for i, argv in enumerate(calls) if "paste-buffer" in argv]
        if failure in {"capture", "empty", "oversize", "staging"}:
            assert not any("paste-buffer" in argv for argv in calls)
        else:
            assert paste_indexes == [len(calls) - 2]
        display_index = next(i for i, argv in enumerate(calls) if "display-message" in argv)
        assert not any(
            "load-buffer" in argv or "paste-buffer" in argv for argv in calls[display_index + 1 :]
        )
        message_argv = next(argv for argv in calls if "display-message" in argv)
        assert message_argv == [
            "tmux",
            "display-message",
            "-t",
            "%3",
            "Image paste is unavailable; save the image under the task workspace and paste its path",
        ]
