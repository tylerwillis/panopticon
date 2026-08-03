"""Exhaustive guard for the dedicated-socket tmux startup boundary."""

from __future__ import annotations

from pathlib import Path


# 2119: REQ-030.3.1
def test_every_production_new_session_uses_the_single_configured_constructor() -> None:
    source_root = Path(__file__).parents[1] / "src" / "panopticon"
    direct_uses = [
        path.relative_to(source_root)
        for path in source_root.rglob("*.py")
        if '"new-session"' in path.read_text() and path.name != "tmux_defaults.py"
    ]

    assert direct_uses == []
