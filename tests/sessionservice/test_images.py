"""Composable task images (ADR 0005): tag naming, Dockerfile composition, and the build
command — unit-tested without a real daemon (the command-runner is faked)."""

from __future__ import annotations

import importlib.resources
import os
import re
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

import panopticon.docker as _docker_pkg
import panopticon.sessionservice.images as _images
from panopticon.sessionservice.images import (
    ImageBuilder,
    _base_fingerprint,
    compose_dockerfile,
    image_tag,
)
from panopticon.workflows.discovery import discover_workflows


def _base_dockerfile() -> str:
    return (importlib.resources.files(_docker_pkg) / "Dockerfile").read_text()


def _source_installer() -> Path:
    return Path(str(importlib.resources.files(_docker_pkg) / "install-packaged-source.sh"))


def _build_args(command: list[str]) -> list[str]:
    return [command[index + 1] for index, item in enumerate(command) if item == "--build-arg"]


def _use_packaged_source(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Make package-resource discovery see an isolated installed-source tree."""
    real_files = importlib.resources.files

    def files(package: object) -> importlib.resources.abc.Traversable:
        if package is __import__("panopticon"):
            return root
        return real_files(package)

    monkeypatch.setattr(importlib.resources, "files", files)


def _clear_source_fingerprint_cache() -> None:
    """Simulate a fresh host process after installation of a new source revision."""
    clear = getattr(_images, "_clear_base_fingerprint_cache", None)
    if clear is not None:
        clear()


def _all_packaged_source_paths() -> tuple[str, ...]:
    """Enumerate the requirement's complete installed-source set independently of production."""
    package_root = Path(str(importlib.resources.files(__import__("panopticon"))))
    excluded = {"docker/Dockerfile", "docker/entrypoint.sh"}
    return tuple(
        path.relative_to(package_root).as_posix()
        for path in sorted(package_root.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
        and path.relative_to(package_root).as_posix() not in excluded
    )


def _docker_running() -> bool:
    return (
        shutil.which("docker") is not None
        and subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    )


# 2119: REQ-022.1
def test_base_image_installs_github_cli() -> None:
    assert re.search(
        r"(?m)^\s*&& apt-get install --yes --no-install-recommends .*\bgh\b.*$",
        _base_dockerfile(),
    )


# 2119: REQ-022.1
def test_documented_make_build_applies_the_base_fingerprint() -> None:
    makefile = (Path(__file__).parents[2] / "Makefile").read_text()
    assert "--build-arg PANOPTICON_BASE_FINGERPRINT=" in makefile
    assert "from panopticon.sessionservice.images import _base_fingerprint" in makefile
    assert 'LABEL io.panopticon.base-fingerprint="${PANOPTICON_BASE_FINGERPRINT}"' in (
        _base_dockerfile()
    )


# 2119: REQ-022.2
def test_panopticon_shipped_workflow_layers_do_not_reinstall_github_cli(tmp_path: Path) -> None:
    workflows = discover_workflows(_home_workflows=tmp_path / "no-home-workflows")
    offenders = [
        name for name, workflow in workflows.items() if re.search(r"\bgh\b", workflow.image_layer())
    ]
    assert offenders == []


def test_image_tag_names_by_harness_workflow_and_repo() -> None:
    assert (
        image_tag("claude", "github-peer-reviewed", "r1")
        == "panopticon-claude-github-peer-reviewed-r1"
    )


def test_compose_dockerfile_chains_base_then_layers() -> None:
    df = compose_dockerfile("panopticon-base", ["RUN install gh", "", "RUN deps"])
    assert df.startswith("FROM panopticon-base\n")
    assert "RUN install gh" in df and "RUN deps" in df


def test_compose_dockerfile_base_only_when_no_layers() -> None:
    assert compose_dockerfile("base", ["", "  "]) == "FROM base\n"


class _BuildRecorder:
    def __init__(self) -> None:
        self.cmd: list[str] = []
        self.dockerfile = ""

    def __call__(self, args: Sequence[str], *, check: bool = True, verbose: bool = False) -> str:
        self.cmd = list(args)
        self.dockerfile = (Path(args[-1]) / "Dockerfile").read_text()  # dir exists during the call
        return ""


def test_build_composes_and_runs_docker_build() -> None:
    rec = _BuildRecorder()
    tag = ImageBuilder(base="panopticon-base", run=rec).build(
        "codex", "github-peer-reviewed", "r1", ["RUN x"]
    )
    assert tag == "panopticon-codex-github-peer-reviewed-r1"
    assert rec.cmd[:4] == ["docker", "build", "--tag", "panopticon-codex-github-peer-reviewed-r1"]
    assert rec.dockerfile.startswith("FROM panopticon-base") and "RUN x" in rec.dockerfile


class _MultiRecorder:
    """Records all calls and returns canned responses in order (for multi-step sequences)."""

    def __init__(self, *responses: str, installed_purelib: Path | None = None) -> None:
        self._responses = list(responses)
        self._installed_purelib = installed_purelib
        self.calls: list[tuple[list[str], bool]] = []
        self.packaged_source: dict[str, bytes] = {}

    def __call__(self, args: Sequence[str], *, check: bool = True, verbose: bool = False) -> str:
        self.calls.append((list(args), check))
        source = Path(args[-1]) / "panopticon-source" / "panopticon"
        if args[:2] == ["docker", "build"] and source.is_dir():
            self.packaged_source = {
                path.relative_to(source).as_posix(): path.read_bytes()
                for path in source.rglob("*")
                if path.is_file()
            }
            dockerfile = (Path(args[-1]) / "Dockerfile").read_text()
            installer = Path(args[-1]) / "install-packaged-source.sh"
            if (
                self._installed_purelib is not None
                and "bash /ctx/install-packaged-source.sh" in dockerfile
            ):
                subprocess.run(
                    ["bash", str(installer)],
                    check=True,
                    env={
                        **os.environ,
                        "PANOPTICON_SOURCE_ROOT": str(source),
                        "PANOPTICON_PURELIB": str(self._installed_purelib),
                    },
                )
        return self._responses.pop(0) if self._responses else ""


def test_build_base_if_missing_skips_build_when_fingerprint_matches() -> None:
    rec = _MultiRecorder(_base_fingerprint())
    result = ImageBuilder(base="panopticon-base", run=rec).build_base_if_missing()
    assert result is False
    assert len(rec.calls) == 1  # only the fingerprint probe, no build
    assert rec.calls[0][0][:5] == [
        "docker",
        "image",
        "inspect",
        "--format",
        '{{ index .Config.Labels "io.panopticon.base-fingerprint" }}',
    ]
    assert rec.calls[0][0][-1] == "panopticon-base"
    assert rec.calls[0][1] is False  # check=False so a missing or stale image does not raise


# 2119: REQ-052.1
@pytest.mark.parametrize("relative_path", _all_packaged_source_paths())
def test_base_fingerprint_changes_with_any_packaged_source_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, relative_path: str
) -> None:
    source = tmp_path / "installed-panopticon"
    changed_file = source / relative_path
    changed_file.parent.mkdir(parents=True)
    changed_file.write_bytes(b"packaged-source\x00")
    _use_packaged_source(monkeypatch, source)

    _clear_source_fingerprint_cache()
    before = _base_fingerprint()
    changed_file.write_bytes(b"packaged-source\x01")
    _clear_source_fingerprint_cache()
    after = _base_fingerprint()

    assert after != before


# 2119: REQ-052.1
def test_base_fingerprint_changes_when_only_packaged_source_path_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "installed-panopticon"
    original = source / "first" / "resource-without-extension"
    renamed = source / "second" / "resource-without-extension"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"identical packaged bytes\n")
    _use_packaged_source(monkeypatch, source)

    _clear_source_fingerprint_cache()
    before = _base_fingerprint()
    renamed.parent.mkdir(parents=True)
    original.rename(renamed)
    _clear_source_fingerprint_cache()
    after = _base_fingerprint()

    assert after != before


# 2119: REQ-052.1
def test_base_fingerprint_ignores_packaged_source_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "installed-panopticon"
    packaged_file = source / "module.py"
    packaged_file.parent.mkdir(parents=True)
    packaged_file.write_bytes(b"identical packaged bytes\n")
    _use_packaged_source(monkeypatch, source)

    _clear_source_fingerprint_cache()
    before = _base_fingerprint()
    original = packaged_file.stat()
    replacement = source / "replacement"
    replacement.write_bytes(packaged_file.read_bytes())
    replacement.replace(packaged_file)
    os.utime(packaged_file, ns=(original.st_atime_ns, original.st_mtime_ns))
    _clear_source_fingerprint_cache()
    after = _base_fingerprint()

    assert packaged_file.stat().st_ino != original.st_ino
    assert after == before


# 2119: REQ-052.1
@pytest.mark.parametrize("byte_index", [0, 8, 15])
def test_base_fingerprint_changes_for_single_binary_byte_at_file_boundaries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, byte_index: int
) -> None:
    source = tmp_path / "installed-panopticon"
    packaged_file = source / "module.py"
    packaged_file.parent.mkdir(parents=True)
    original = bytearray(16)
    packaged_file.write_bytes(original)
    _use_packaged_source(monkeypatch, source)

    _clear_source_fingerprint_cache()
    before = _base_fingerprint()
    original[byte_index] = 1
    packaged_file.write_bytes(original)
    _clear_source_fingerprint_cache()

    assert _base_fingerprint() != before


# 2119: REQ-052.1
def test_base_fingerprint_changes_when_a_byte_is_appended(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "installed-panopticon"
    packaged_file = source / "module.py"
    packaged_file.parent.mkdir(parents=True)
    packaged_file.write_bytes(b"packaged source")
    _use_packaged_source(monkeypatch, source)

    _clear_source_fingerprint_cache()
    before = _base_fingerprint()
    packaged_file.write_bytes(b"packaged source\x00")
    _clear_source_fingerprint_cache()

    assert _base_fingerprint() != before


# 2119: REQ-052.1
def test_base_fingerprint_changes_for_every_file_of_a_multi_file_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pin the requirement's universal quantifier over a tree holding more than one file.

    Every other REQ-052.1 test builds a source tree containing exactly one file, so a digest
    that read only the first packaged file — or only the last, as a misplaced accumulator reset
    would — satisfies all of them while leaving the rest of the package uncovered. Changing any
    one file of a multi-file tree must move the fingerprint.
    """
    source = tmp_path / "installed-panopticon"
    relatives = ("alpha.py", "nested/beta.py", "nested/deeper/gamma.py", "zeta.py")
    for relative in relatives:
        packaged_file = source / relative
        packaged_file.parent.mkdir(parents=True, exist_ok=True)
        packaged_file.write_bytes(b"packaged source\n")
    _use_packaged_source(monkeypatch, source)

    ignored = []
    for relative in relatives:
        packaged_file = source / relative
        _clear_source_fingerprint_cache()
        before = _base_fingerprint()
        packaged_file.write_bytes(b"packaged source\x01")
        _clear_source_fingerprint_cache()
        if _base_fingerprint() == before:
            ignored.append(relative)
        packaged_file.write_bytes(b"packaged source\n")  # restore, so each file is tested alone

    assert not ignored, (
        f"fingerprint ignored changes to {ignored} of {len(relatives)} packaged source files"
    )


# 2119: REQ-052.2
def test_source_change_rebuilds_base_with_unchanged_version_and_docker_assets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "installed-panopticon"
    changed_file = source / "harnesses" / "pi.py"
    changed_file.parent.mkdir(parents=True)
    changed_file.write_text("old packaged source\n")
    _use_packaged_source(monkeypatch, source)

    _clear_source_fingerprint_cache()
    installed_image_fingerprint = _base_fingerprint()
    changed_file.write_text("merged fix\n")
    _clear_source_fingerprint_cache()
    installed_purelib = tmp_path / "image-purelib"
    stale_package = installed_purelib / "panopticon"
    stale_package.mkdir(parents=True)
    (stale_package / "harnesses").mkdir()
    (stale_package / "harnesses" / "pi.py").write_text("old packaged source\n")
    (stale_package / "removed.py").write_text("stale file\n")
    rec = _MultiRecorder(installed_image_fingerprint, installed_purelib=installed_purelib)

    rebuilt = ImageBuilder(base="panopticon-base", run=rec).build_base_if_missing()

    assert rebuilt is True
    assert rec.calls[1][0][:4] == ["docker", "build", "--tag", "panopticon-base"]
    assert rec.packaged_source["harnesses/pi.py"] == b"merged fix\n"
    assert (stale_package / "harnesses" / "pi.py").read_bytes() == b"merged fix\n"
    assert not (stale_package / "removed.py").exists()
    dockerfile = _base_dockerfile()
    install_command = "bash /ctx/install-packaged-source.sh"
    assert dockerfile.index(install_command) > dockerfile.index("pip install")
    assert "pip install" not in dockerfile[dockerfile.index(install_command) + 1 :]


# 2119: REQ-052.2
def test_source_installer_replaces_stale_installed_package(tmp_path: Path) -> None:
    source = tmp_path / "staged" / "panopticon"
    installed = tmp_path / "purelib" / "panopticon"
    source.mkdir(parents=True)
    installed.mkdir(parents=True)
    (source / "delivery_probe.py").write_bytes(b"revised packaged source\n")
    (installed / "delivery_probe.py").write_bytes(b"stale packaged source\n")
    (installed / "removed.py").write_bytes(b"must not survive\n")

    subprocess.run(
        ["bash", str(_source_installer())],
        check=True,
        env={
            **os.environ,
            "PANOPTICON_SOURCE_ROOT": str(source),
            "PANOPTICON_PURELIB": str(tmp_path / "purelib"),
        },
    )

    assert (installed / "delivery_probe.py").read_bytes() == b"revised packaged source\n"
    assert not (installed / "removed.py").exists()


# 2119: REQ-052.2
@pytest.mark.skipif(not _docker_running(), reason="needs a working docker daemon")
def test_rebuilt_base_image_executes_revised_packaged_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "installed-panopticon"
    source.mkdir()
    (source / "__init__.py").write_text("")
    probe = source / "delivery_probe.py"
    probe.write_text('VALUE = "stale source"\n')
    _use_packaged_source(monkeypatch, source)
    _clear_source_fingerprint_cache()
    image = f"panopticon-source-delivery-{os.getpid()}"

    def installed_value() -> str:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "python",
                image,
                "-c",
                "from panopticon.delivery_probe import VALUE; print(VALUE)",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    try:
        builder = ImageBuilder(base=image)
        builder.build_base()
        assert installed_value() == "stale source"

        probe.write_text('VALUE = "revised source reached image"\n')
        _clear_source_fingerprint_cache()
        assert builder.build_base_if_missing() is True
        assert installed_value() == "revised source reached image"
    finally:
        subprocess.run(["docker", "image", "rm", "--force", image], capture_output=True)


# 2119: REQ-052.3
def test_repeated_base_fingerprints_reuse_packaged_source_digest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "installed-panopticon"
    source.mkdir()
    (source / "module.py").write_text("packaged source\n")
    real_files = importlib.resources.files
    source_discoveries = 0

    def files(package: object) -> importlib.resources.abc.Traversable:
        nonlocal source_discoveries
        if package is __import__("panopticon"):
            source_discoveries += 1
            return source
        return real_files(package)

    monkeypatch.setattr(importlib.resources, "files", files)
    _clear_source_fingerprint_cache()

    first = _base_fingerprint()
    second = _base_fingerprint()

    assert second == first
    assert source_discoveries == 1


# 2119: REQ-022.1
def test_build_base_if_missing_rebuilds_when_fingerprint_is_stale() -> None:
    rec = _MultiRecorder("pre-gh-base-fingerprint")
    result = ImageBuilder(base="panopticon-base", run=rec).build_base_if_missing()
    assert result is True
    assert len(rec.calls) == 2
    assert rec.calls[1][0][:4] == ["docker", "build", "--tag", "panopticon-base"]
    assert f"PANOPTICON_BASE_FINGERPRINT={_base_fingerprint()}" in _build_args(rec.calls[1][0])


def test_build_base_if_missing_builds_when_inspect_returns_empty_string() -> None:
    rec = _MultiRecorder("")  # label inspect returns "" → image absent
    result = ImageBuilder(base="panopticon-base", run=rec).build_base_if_missing()
    assert result is True
    assert len(rec.calls) == 2
    build_cmd = rec.calls[1][0]
    # command structure: docker build --tag <img> --build-arg PANOPTICON_VERSION=<v> --file <path> <dir>
    assert build_cmd[:4] == ["docker", "build", "--tag", "panopticon-base"]
    assert "--build-arg" in build_cmd
    version_arg = build_cmd[build_cmd.index("--build-arg") + 1]
    assert version_arg.startswith("PANOPTICON_VERSION=")
    assert "--file" in build_cmd
    file_arg = build_cmd[build_cmd.index("--file") + 1]
    assert file_arg.endswith("Dockerfile")
    assert Path(file_arg).parent == Path(build_cmd[-1])
    assert "sessionservice/images.py" in rec.packaged_source
    assert rec.calls[1][1] is True  # check=True so a build failure propagates


def test_build_base_unconditional() -> None:
    rec = _MultiRecorder("")
    ImageBuilder(base="panopticon-base", run=rec).build_base(verbose=True)
    assert len(rec.calls) == 1  # no inspect probe — just the build
    build_cmd = rec.calls[0][0]
    assert build_cmd[:4] == ["docker", "build", "--tag", "panopticon-base"]
    assert "--build-arg" in build_cmd
    build_args = _build_args(build_cmd)
    assert any(arg.startswith("PANOPTICON_VERSION=") for arg in build_args)
    assert f"PANOPTICON_BASE_FINGERPRINT={_base_fingerprint()}" in build_args
    assert "--file" in build_cmd
    file_arg = build_cmd[build_cmd.index("--file") + 1]
    assert file_arg.endswith("Dockerfile")
    assert Path(file_arg).parent == Path(build_cmd[-1])
    assert "sessionservice/images.py" in rec.packaged_source
