"""Composable task images (ADR 0005): a task's image = **base → harness → workflow → repo**.

The base is minimal and general; a **harness** contributes its agent CLI (M3 — empty for claude,
whose CLI still ships in the base); a workflow contributes any workflow-specific additions; a
repo contributes its toolchain/setup. We compose them by writing a Dockerfile
that `FROM`s the base and appends the layers, tag it `panopticon-<harness>-<workflow>-<repo>`,
and `docker build` it behind the injectable command-runner (so it's unit-testable without a
daemon). LLM-free. The runner builds the composed image, then spawns the task on it.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import logging
import tempfile
from collections.abc import Sequence
from pathlib import Path

from panopticon.sessionservice.local_runner import DEFAULT_IMAGE, CommandRunner, _subprocess_run

_log = logging.getLogger(__name__)

_BASE_FINGERPRINT_LABEL = "io.panopticon.base-fingerprint"


def image_tag(harness: str, workflow: str, repo_id: str) -> str:
    """The composed image's tag for a (harness, workflow, repo) triple (ADR 0005 naming)."""
    return f"panopticon-{harness}-{workflow}-{repo_id}"


def compose_dockerfile(base: str, layers: Sequence[str]) -> str:
    """A Dockerfile that starts from ``base`` and appends each non-empty layer fragment."""
    body = "\n\n".join(layer.strip() for layer in layers if layer.strip())
    return f"FROM {base}\n" + (f"\n{body}\n" if body else "")


def _base_fingerprint() -> str:
    """Fingerprint every packaged input that defines the base image.

    The package version matters because the production build installs that exact release. The
    Dockerfile and entrypoint are the only packaged files copied into the base build context.
    """
    import panopticon
    import panopticon.docker as _docker_pkg

    digest = hashlib.sha256()
    digest.update(panopticon.__version__.encode())
    for name in ("Dockerfile", "entrypoint.sh"):
        digest.update(b"\0")
        digest.update((importlib.resources.files(_docker_pkg) / name).read_bytes())
    return digest.hexdigest()


class ImageBuilder:
    """Builds composed task images on the local Docker daemon (one host)."""

    def __init__(self, *, base: str = DEFAULT_IMAGE, run: CommandRunner = _subprocess_run) -> None:
        self._base = base
        self._run = run

    def build(
        self,
        harness: str,
        workflow: str,
        repo_id: str,
        layers: Sequence[str],
        *,
        verbose: bool = False,
    ) -> str:
        """Compose base → ``layers`` and `docker build` it; return the image tag.

        ``verbose`` streams docker build output to the caller's stdout/stderr (visible in the
        runner's tmux session) instead of capturing it."""
        tag = image_tag(harness, workflow, repo_id)
        dockerfile = compose_dockerfile(self._base, layers)
        with tempfile.TemporaryDirectory() as context:
            (Path(context) / "Dockerfile").write_text(dockerfile)
            self._run(["docker", "build", "--tag", tag, context], verbose=verbose)
        return tag

    def build_base(self, *, verbose: bool = False) -> None:
        """Build the base image unconditionally from the bundled Dockerfile."""
        import panopticon
        import panopticon.docker as _docker_pkg

        fingerprint = _base_fingerprint()
        dockerfile_ref = importlib.resources.files(_docker_pkg) / "Dockerfile"
        with importlib.resources.as_file(dockerfile_ref) as dockerfile_path:
            self._run(
                [
                    "docker",
                    "build",
                    "--tag",
                    self._base,
                    "--label",
                    f"{_BASE_FINGERPRINT_LABEL}={fingerprint}",
                    "--build-arg",
                    f"PANOPTICON_VERSION={panopticon.__version__}",
                    "--file",
                    str(dockerfile_path),
                    str(dockerfile_path.parent),
                ],
                verbose=verbose,
            )

    def build_base_if_missing(self, *, verbose: bool = False) -> bool:
        """Build the bundled base image when its tag is absent or its definition is stale.

        The image carries a content/version fingerprint label. An old image with the same static
        tag has no matching label, so package upgrades rebuild it before any task is spawned.
        Returns ``True`` if a build was triggered, ``False`` when the current image is reusable.
        """
        fingerprint = _base_fingerprint()
        current = self._run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                f'{{{{ index .Config.Labels "{_BASE_FINGERPRINT_LABEL}" }}}}',
                self._base,
            ],
            check=False,
        ).strip()
        if current == fingerprint:
            return False
        _log.warning("base image %r is missing or stale — building automatically", self._base)
        self.build_base(verbose=verbose)
        return True
