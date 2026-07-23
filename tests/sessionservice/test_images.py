"""Composable task images (ADR 0005): tag naming, Dockerfile composition, and the build
command — unit-tested without a real daemon (the command-runner is faked)."""

from __future__ import annotations

import importlib.resources
import re
from collections.abc import Sequence
from pathlib import Path

import panopticon.docker as _docker_pkg
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


# 2119: REQ-009.1
def test_base_image_installs_github_cli() -> None:
    assert re.search(
        r"(?m)^\s*&& apt-get install --yes --no-install-recommends .*\bgh\b.*$",
        _base_dockerfile(),
    )


# 2119: REQ-009.1
def test_documented_make_build_applies_the_base_fingerprint() -> None:
    makefile = (Path(__file__).parents[2] / "Makefile").read_text()
    assert "--build-arg PANOPTICON_BASE_FINGERPRINT=" in makefile
    assert "from panopticon.sessionservice.images import _base_fingerprint" in makefile
    assert 'LABEL io.panopticon.base-fingerprint="${PANOPTICON_BASE_FINGERPRINT}"' in (
        _base_dockerfile()
    )


# 2119: REQ-009.2
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


# 2119: REQ-009.1
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
