"""The shared panopticon orientation reaches each system-prompt harness surface."""

from __future__ import annotations

import tomllib
from pathlib import Path

from panopticon.harnesses import LaunchContext
from panopticon.harnesses.claude import ClaudeHarness, write_workflow_overview
from panopticon.harnesses.codex import render_config
from panopticon.workflows import GithubPeerReviewed


# 2119: REQ-041.7.1
def test_claude_delivers_panopticon_orientation_in_the_system_prompt(tmp_path: Path) -> None:
    overview = GithubPeerReviewed().overview()
    write_workflow_overview(tmp_path / ".claude", overview)

    argv = ClaudeHarness().argv(
        LaunchContext(home=tmp_path, cwd=Path("/workspace"))
    )
    index = argv.index("--append-system-prompt")
    assert argv[index + 1] == overview
    assert "## Working in panopticon" in argv[index + 1]
    assert "when you produce" in argv[index + 1]


# 2119: REQ-041.8.1
def test_codex_delivers_panopticon_orientation_as_developer_instructions() -> None:
    overview = GithubPeerReviewed().overview()
    config = tomllib.loads(render_config("http://svc:8000", overview, Path("/workspace")))

    assert config["developer_instructions"] == overview
    assert "## Working in panopticon" in config["developer_instructions"]
    assert "when you produce" in config["developer_instructions"]
