"""Host-local bearer credentials for the task-service control plane."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from panopticon.core.dirs import _secrets_dir

AuthMode = Literal["disabled", "permissive", "enforced"]


@dataclass(frozen=True)
class AuthTokens:
    read: tuple[str, ...]
    write: tuple[str, ...]


def _credential_error() -> ValueError:
    return ValueError("authentication credential file is invalid or unavailable")


def credential_path(reference: str, *, secrets_dir: str | Path | None = None) -> Path:
    root = Path(secrets_dir) if secrets_dir is not None else _secrets_dir()
    root = root.resolve()
    candidate = Path(reference)
    if candidate.is_absolute():
        raise _credential_error()
    resolved = (root / candidate).resolve()
    if resolved.parent != root or candidate.name != reference:
        raise _credential_error()
    return resolved


def _parse_tokens(contents: str) -> AuthTokens:
    try:
        raw = json.loads(contents)
        if not isinstance(raw, dict) or set(raw) != {"read", "write"}:
            raise _credential_error()
        read, write = raw["read"], raw["write"]
        if not all(isinstance(values, list) and values for values in (read, write)):
            raise _credential_error()
        if not all(isinstance(token, str) and token for token in [*read, *write]):
            raise _credential_error()
        if len(set(read)) != len(read) or len(set(write)) != len(write) or set(read) & set(write):
            raise _credential_error()
        return AuthTokens(tuple(read), tuple(write))
    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        raise _credential_error() from exc


def load_tokens(reference: str, *, secrets_dir: str | Path | None = None) -> AuthTokens:
    try:
        return _parse_tokens(credential_path(reference, secrets_dir=secrets_dir).read_text())
    except OSError as exc:
        raise _credential_error() from exc


def load_client_token(
    reference: str, *, privilege: Literal["read", "write"], secrets_dir: str | Path | None = None
) -> str:
    tokens = load_tokens(reference, secrets_dir=secrets_dir)
    values = tokens.read if privilege == "read" else tokens.write
    return values[0]


def environment_token(*, privilege: Literal["read", "write"] = "write") -> str | None:
    reference = os.environ.get("PANOPTICON_SERVICE_AUTH_FILE")
    if not reference:
        return None
    path = Path(reference)
    if path.is_absolute():
        try:
            tokens = _parse_tokens(path.read_text())
        except OSError as exc:
            raise _credential_error() from exc
        return (tokens.read if privilege == "read" else tokens.write)[0]
    return load_client_token(reference, privilege=privilege)
