"""Composable task images (ADR 0005): tag naming, Dockerfile composition, and the build
command — unit-tested without a real daemon (the command-runner is faked)."""

from __future__ import annotations

import importlib.resources
import re
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

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[list[str], bool]] = []

    def __call__(self, args: Sequence[str], *, check: bool = True, verbose: bool = False) -> str:
        self.calls.append((list(args), check))
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


# 2119: REQ-050.1
@pytest.mark.parametrize("relative_path", _all_packaged_source_paths())
def test_base_fingerprint_changes_with_any_packaged_source_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, relative_path: str
) -> None:
    source = tmp_path / "installed-panopticon"
    changed_file = source / relative_path
    changed_file.parent.mkdir(parents=True)
    changed_file.write_text("first revision\n")
    _use_packaged_source(monkeypatch, source)

    _clear_source_fingerprint_cache()
    before = _base_fingerprint()
    changed_file.write_text("second revision\n")
    _clear_source_fingerprint_cache()
    after = _base_fingerprint()

    assert after != before


# 2119: REQ-050.1
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


# 2119: REQ-050.2
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
    rec = _MultiRecorder(installed_image_fingerprint)

    rebuilt = ImageBuilder(base="panopticon-base", run=rec).build_base_if_missing()

    assert rebuilt is True
    assert rec.calls[1][0][:4] == ["docker", "build", "--tag", "panopticon-base"]


# 2119: REQ-050.3
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
    assert Path(build_cmd[-1]).name == "docker"  # context = parent dir of Dockerfile
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
    assert Path(build_cmd[-1]).name == "docker"  # context = parent dir of Dockerfile
