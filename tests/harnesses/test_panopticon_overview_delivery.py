"""The shared panopticon orientation reaches each system-prompt harness surface."""

from __future__ import annotations

import tomllib
from pathlib import Path

from panopticon.harnesses import LaunchContext
from panopticon.harnesses.claude import ClaudeHarness, write_workflow_overview
from panopticon.harnesses.codex import render_config
from panopticon.harnesses.pi import PiHarness
from panopticon.harnesses.pi import write_workflow_overview as write_pi_overview
from panopticon.workflows import GithubPeerReviewed

PANOPTICON_NORM_MARKERS = (
    "## Working in panopticon",
    "inside a panopticon task container",
    "Publish each reviewable document as a task artifact when you produce it",
    "without waiting for the user to ask",
    "**The task external URL**",
    "**Responsibilities**",
    "**`advance` and `drop`**",
)


def _assert_panopticon_norms(delivered: str) -> None:
    normalized = " ".join(delivered.split())
    for marker in PANOPTICON_NORM_MARKERS:
        assert marker in normalized


# 2119: REQ-041.7.1
def test_claude_delivers_panopticon_orientation_in_the_system_prompt(tmp_path: Path) -> None:
    overview = GithubPeerReviewed().overview()
    write_workflow_overview(tmp_path / ".claude", overview)

    argv = ClaudeHarness().argv(LaunchContext(home=tmp_path, cwd=Path("/workspace")))
    index = argv.index("--append-system-prompt")
    assert argv[index + 1] == overview
    _assert_panopticon_norms(argv[index + 1])


# 2119: REQ-041.8.1
def test_codex_delivers_panopticon_orientation_as_developer_instructions() -> None:
    overview = GithubPeerReviewed().overview()
    config = tomllib.loads(render_config("http://svc:8000", overview, Path("/workspace")))

    assert config["developer_instructions"] == overview
    _assert_panopticon_norms(config["developer_instructions"])


# 2119: REQ-041.9.1
def test_pi_delivers_panopticon_orientation_in_the_system_prompt(tmp_path: Path) -> None:
    overview = GithubPeerReviewed().overview()
    write_pi_overview(tmp_path / ".pi", overview)

    argv = PiHarness().argv(LaunchContext(home=tmp_path, cwd=Path("/workspace")))
    index = argv.index("--append-system-prompt")
    assert argv[index + 1] == overview
    _assert_panopticon_norms(argv[index + 1])
