"""Counterexample for registered-harness zero-observation diagnostics."""

from __future__ import annotations

import pytest
from credential_guard_helpers import assert_minimum_subjects


def test_zero_observation_failure_names_discovered_harnesses() -> None:
    # 2119: REQ-044.7.1
    harnesses = ["claude", "codex", "outfitter", "pi"]
    with pytest.raises(AssertionError) as exc:
        assert_minimum_subjects([], 1, f"registered harnesses {harnesses!r}")
    for harness in harnesses:
        assert harness in str(exc.value)
