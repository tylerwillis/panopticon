"""Real-daemon acceptance coverage for the Node-based harness image layers.

These tests deliberately build the generated layers and execute Node in the result. Rendering
assertions alone cannot detect a decompressor missing from ``panopticon-base``.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from panopticon.harnesses.outfitter import NODE_VERSION as OUTFITTER_NODE_VERSION
from panopticon.harnesses.outfitter import OutfitterHarness
from panopticon.harnesses.pi import NODE_VERSION as PI_NODE_VERSION
from panopticon.harnesses.pi import PiHarness
from panopticon.sessionservice.images import ImageBuilder


def _docker_running() -> bool:
    return bool(
        shutil.which("docker")
        and subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    )


@pytest.mark.parametrize(
    ("harness_name", "layer", "node_version", "requirement"),
    [
        pytest.param(
            "pi",
            PiHarness().image_layer(),
            PI_NODE_VERSION,
            "build-pi-outfitter-with-gzip.2.1",
            id="pi",
        ),
        pytest.param(
            "outfitter",
            OutfitterHarness().image_layer(),
            OUTFITTER_NODE_VERSION,
            "build-pi-outfitter-with-gzip.2.2",
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
    requirement: str,
) -> None:
    """Build each real harness layer and prove its installed Node executable is usable."""
    del requirement  # IDs make parametrized failures self-describing and annotations auditable.
    builder = ImageBuilder()
    builder.build_base_if_missing(verbose=True)
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
