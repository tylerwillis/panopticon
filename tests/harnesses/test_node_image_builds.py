"""Real-daemon acceptance coverage for the Node-based harness image layers.

These tests deliberately build the generated layers and execute Node in the result. Rendering
assertions alone cannot detect a decompressor missing from ``panopticon-base``.
"""

from __future__ import annotations

import importlib.resources
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

import panopticon.docker as _docker_pkg
from panopticon.harnesses.outfitter import NODE_VERSION as OUTFITTER_NODE_VERSION
from panopticon.harnesses.outfitter import OutfitterHarness
from panopticon.harnesses.pi import NODE_VERSION as PI_NODE_VERSION
from panopticon.harnesses.pi import PiHarness
from panopticon.sessionservice.images import ImageBuilder, _base_fingerprint

_BASE_IMAGE = "panopticon-node-harness-acceptance-base:latest"


def _docker_running() -> bool:
    return bool(
        shutil.which("docker")
        and subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    )


@pytest.fixture(scope="module")
def node_harness_base(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Build the current base from a local wheel under an acceptance-only tag."""
    work = tmp_path_factory.mktemp("node-harness-base")
    wheel_out = work / "wheels"
    wheel_out.mkdir()
    repo_root = Path(__file__).parent.parent.parent
    subprocess.run(
        ["uv", "build", "--wheel", f"--out-dir={wheel_out}"],
        check=True,
        capture_output=True,
        cwd=repo_root,
    )
    (wheel,) = list(wheel_out.glob("*.whl"))

    context = work / "context"
    context.mkdir()
    dockerfile_ref = importlib.resources.files(_docker_pkg) / "Dockerfile"
    entrypoint_ref = importlib.resources.files(_docker_pkg) / "entrypoint.sh"
    with (
        importlib.resources.as_file(dockerfile_ref) as dockerfile_path,
        importlib.resources.as_file(entrypoint_ref) as entrypoint_path,
    ):
        shutil.copy(dockerfile_path, context / "Dockerfile")
        shutil.copy(entrypoint_path, context / "entrypoint.sh")
    shutil.copy(wheel, context / wheel.name)
    subprocess.run(
        [
            "docker",
            "build",
            "--tag",
            _BASE_IMAGE,
            "--build-arg",
            f"PANOPTICON_WHEEL={wheel.name}",
            "--build-arg",
            f"PANOPTICON_BASE_FINGERPRINT={_base_fingerprint()}",
            str(context),
        ],
        check=True,
    )
    try:
        yield _BASE_IMAGE
    finally:
        subprocess.run(["docker", "image", "rm", "--force", _BASE_IMAGE], capture_output=True)


@pytest.mark.parametrize(
    ("harness_name", "layer", "node_version"),
    [
        pytest.param(
            "pi",
            PiHarness().image_layer(),
            PI_NODE_VERSION,
            id="pi",
        ),
        pytest.param(
            "outfitter",
            OutfitterHarness().image_layer(),
            OUTFITTER_NODE_VERSION,
            id="outfitter",
        ),
    ],
)
# 2119: build-pi-outfitter-with-gzip.1.1
# 2119: build-pi-outfitter-with-gzip.1.2
# 2119: build-pi-outfitter-with-gzip.2.1
@pytest.mark.skipif(not _docker_running(), reason="needs a working docker daemon")
def test_node_harness_image_builds_and_runs_pinned_node(
    harness_name: str,
    layer: str,
    node_version: str,
    node_harness_base: str,
) -> None:
    """Build each real harness layer and prove its installed Node executable is usable."""
    builder = ImageBuilder(base=node_harness_base)
    tag = builder.build(
        harness_name,
        "gzip-acceptance",
        "itest",
        [layer],
        verbose=True,
    )
    try:
        completed = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "node", tag, "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert completed.stdout.strip() == f"v{node_version}"
        tools = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "sh",
                tag,
                "-c",
                "command -v gzip >/dev/null && ! command -v xz >/dev/null",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert tools.stdout == "" and tools.stderr == ""
    finally:
        subprocess.run(["docker", "image", "rm", "--force", tag], capture_output=True)
