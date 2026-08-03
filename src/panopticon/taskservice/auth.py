"""Host-local bearer credentials for the task-service control plane."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from panopticon.core.dirs import _secrets_dir

AuthMode = Literal["disabled", "permissive", "enforced"]
MIN_TOKEN_LENGTH = 12
_BEARER_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*\Z")
# Fixed values emitted by the generic 401 must never themselves be configurable credentials.
# Otherwise rejecting a request would disclose that credential in the response body/header.
_RESERVED_TOKENS = frozenset(
    {
        "application/json",
        "authentication",
        "content-length",
        "content-type",
        "www-authenticate",
    }
)


def _collides_with_failure_response(token: str) -> bool:
    """Whether the credential would occur in a fixed generic-401 wire value."""
    folded = token.casefold()
    return any(folded in value.casefold() for value in _RESERVED_TOKENS)


@dataclass(frozen=True)
class AuthTokens:
    read: tuple[str, ...]
    write: tuple[str, ...]


@dataclass(frozen=True)
class AuthPrincipal:
    privilege: Literal["read", "write"]
    task_id: str | None = None


def scoped_task_token(master: str, task_id: str) -> str:
    digest = hmac.new(master.encode(), f"task:{task_id}".encode(), hashlib.sha256).hexdigest()
    return f"pt1.task.{task_id}.{digest}"


def authenticate_token(tokens: AuthTokens, presented: str) -> AuthPrincipal | None:
    value = presented.encode()
    if any(hmac.compare_digest(value, token.encode()) for token in tokens.write):
        return AuthPrincipal("write")
    if any(hmac.compare_digest(value, token.encode()) for token in tokens.read):
        return AuthPrincipal("read")
    parts = presented.split(".", 3)
    if len(parts) == 4 and parts[:2] == ["pt1", "task"] and parts[2]:
        task_id = parts[2]
        if any(
            hmac.compare_digest(presented, scoped_task_token(master, task_id))
            for master in tokens.write
        ):
            return AuthPrincipal("write", task_id)
    return None


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


def _parse_tokens(contents: str, *, allow_runtime_snapshot: bool = False) -> AuthTokens:
    try:
        raw = json.loads(contents)
        if not isinstance(raw, dict) or set(raw) != {"read", "write"}:
            raise _credential_error()
        read, write = raw["read"], raw["write"]
        if not isinstance(read, list) or not isinstance(write, list) or not write:
            raise _credential_error()
        if not allow_runtime_snapshot and not read:
            raise _credential_error()
        if not all(
            isinstance(token, str)
            and len(token) >= MIN_TOKEN_LENGTH
            and _BEARER_TOKEN.fullmatch(token)
            and not _collides_with_failure_response(token)
            for token in [*read, *write]
        ):
            raise _credential_error()
        if len(set(read)) != len(read) or len(set(write)) != len(write) or set(read) & set(write):
            raise _credential_error()
        return AuthTokens(tuple(read), tuple(write))
    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        raise _credential_error() from exc


def _read_regular_file(path: Path) -> str:
    """Read only a regular file without blocking on a swapped-in FIFO or device."""
    fd = -1
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(fd)
        current_uid = getattr(os, "geteuid", lambda: opened.st_uid)()
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != current_uid
            or opened.st_mode & 0o077
        ):
            raise _credential_error()
        with os.fdopen(fd, encoding="utf-8") as handle:
            fd = -1  # ownership transferred to the file object
            return handle.read()
    except (OSError, UnicodeError) as exc:
        raise _credential_error() from exc
    finally:
        if fd >= 0:
            os.close(fd)


def load_tokens(reference: str, *, secrets_dir: str | Path | None = None) -> AuthTokens:
    return _parse_tokens(_read_regular_file(credential_path(reference, secrets_dir=secrets_dir)))


def load_client_token(
    reference: str, *, privilege: Literal["read", "write"], secrets_dir: str | Path | None = None
) -> str:
    tokens = load_tokens(reference, secrets_dir=secrets_dir)
    values = tokens.read if privilege == "read" else tokens.write
    return values[-1]


def snapshot_tokens(
    reference: str,
    *,
    directory: str | Path | None = None,
    secrets_dir: str | Path | None = None,
    prefix: str = "panopticon-service-auth-",
    task_id: str | None = None,
) -> Path:
    """Create a private regular-file snapshot for a process launch or bind mount."""
    tokens = load_tokens(reference, secrets_dir=secrets_dir)
    fd, raw_path = tempfile.mkstemp(prefix=prefix, suffix=".json", dir=directory)
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            token = scoped_task_token(tokens.write[-1], task_id) if task_id else tokens.write[-1]
            json.dump({"read": [], "write": [token]}, handle)
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def environment_token(*, privilege: Literal["read", "write"] = "write") -> str | None:
    reference = os.environ.get("PANOPTICON_SERVICE_AUTH_FILE")
    if not reference:
        return None
    path = Path(reference)
    if path.is_absolute():
        tokens = _parse_tokens(_read_regular_file(path), allow_runtime_snapshot=True)
        values = tokens.read if privilege == "read" else tokens.write
        if not values:
            raise _credential_error()
        return values[-1]
    return load_client_token(reference, privilege=privilege)
