"""Exact audit gate for the complete built-in workflow image-layer set."""

from __future__ import annotations

import hashlib
from pathlib import Path

from panopticon.workflows.discovery import discover_workflows


# 2119: REQ-022.2
def test_every_shipped_workflow_layer_matches_audited_content(tmp_path: Path) -> None:
    workflows = discover_workflows(_home_workflows=tmp_path / "no-home-workflows")
    empty_layer = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    codex_only_layer = "c3003ca3c1eef8f565f29c456cabb568235862e500e3706b7dfbbb90786d67ae"
    # Exact audited layer bytes: an altered or newly shipped layer cannot evade this gate by
    # spelling, downloading, or renaming the gh executable differently.
    expected = {
        "github-peer-reviewed": empty_layer,
        "github-self-reviewed": empty_layer,
        "local-git-self-reviewed": empty_layer,
        "orchestrator": empty_layer,
        "review": empty_layer,
        "setup-repo": empty_layer,
        "2119-auto-spec": codex_only_layer,
        "2119-auto-sol": codex_only_layer,
        "2119-human-spec": codex_only_layer,
        "spike": empty_layer,
    }
    actual = {
        name: hashlib.sha256(workflow.image_layer().encode()).hexdigest()
        for name, workflow in workflows.items()
    }
    assert actual == expected
