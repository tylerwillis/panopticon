"""Hermetic process environment for the test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_operator_auth_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests opt into service auth explicitly; never consume the operator's live configuration."""
    monkeypatch.delenv("PANOPTICON_SERVICE_AUTH_FILE", raising=False)
    monkeypatch.delenv("PANOPTICON_SERVICE_AUTH_MODE", raising=False)
    monkeypatch.delenv("PANOPTICON_CONFIG", raising=False)
