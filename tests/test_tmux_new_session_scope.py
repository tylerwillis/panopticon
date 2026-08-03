"""Exhaustive guard for the dedicated-socket tmux startup boundary."""

from __future__ import annotations

import ast
from pathlib import Path


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_string(node.left)
        right = _literal_string(node.right)
        if left is not None and right is not None:
            return left + right
    return None


# 2119: REQ-030.3.1
def test_every_production_new_session_uses_the_single_configured_constructor() -> None:
    source_root = Path(__file__).parents[1] / "src" / "panopticon"
    direct_uses = []
    for path in source_root.rglob("*.py"):
        if path.name == "tmux_defaults.py":
            continue
        tree = ast.parse(path.read_text())
        if any(_literal_string(node) == "new-session" for node in ast.walk(tree)):
            direct_uses.append(path.relative_to(source_root))

    assert direct_uses == []
