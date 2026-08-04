"""Host-local bearer credentials for the task-service control plane."""

from __future__ import annotations

import base64
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


TASK_CAPABILITY_PREFIX = "ptc1"
TASK_CAPABILITY_PROFILE = "self"
TASK_CAPABILITY_DOMAIN = b"panopticon-task-capability-v1\0"


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def derive_task_capability(write_token: str, task_id: str) -> str:
    """Derive the deterministic, task-bound control-plane capability for ``task_id``."""
    subject = task_id.encode("utf-8")
    message = TASK_CAPABILITY_DOMAIN + subject + b"\0" + TASK_CAPABILITY_PROFILE.encode()
    mac = hmac.new(write_token.encode(), message, hashlib.sha256).digest()
    return f"{TASK_CAPABILITY_PREFIX}.{_base64url(subject)}.{TASK_CAPABILITY_PROFILE}.{_base64url(mac)}"


def decode_task_capability(token: str, write_tokens: tuple[str, ...]) -> str | None:
    """Return the authenticated subject, or ``None`` for every malformed/forged token."""
    try:
        version, encoded_subject, profile, encoded_mac = token.split(".")
        if version != TASK_CAPABILITY_PREFIX or profile != TASK_CAPABILITY_PROFILE:
            return None
        if not encoded_subject or "=" in encoded_subject or not encoded_mac or "=" in encoded_mac:
            return None
        subject_bytes = base64.urlsafe_b64decode(
            encoded_subject + "=" * (-len(encoded_subject) % 4)
        )
        mac = base64.urlsafe_b64decode(encoded_mac + "=" * (-len(encoded_mac) % 4))
        subject = subject_bytes.decode("utf-8")
        if _base64url(subject_bytes) != encoded_subject or len(mac) != hashlib.sha256().digest_size:
            return None
    except (ValueError, UnicodeDecodeError):
        return None
    return (
        subject
        if any(
            hmac.compare_digest(token, derive_task_capability(root, subject))
            for root in write_tokens
        )
        else None
    )


@dataclass(frozen=True)
class AuthPrincipal:
    privilege: Literal["read", "write"]
    task_id: str | None = None


def scoped_task_token(master: str, task_id: str) -> str:
    """Compatibility name for the canonical profile-bound task capability."""

    return derive_task_capability(master, task_id)


def authenticate_token(tokens: AuthTokens, presented: str) -> AuthPrincipal | None:
    value = presented.encode()
    if any(hmac.compare_digest(value, token.encode()) for token in tokens.write):
        return AuthPrincipal("write")
    if any(hmac.compare_digest(value, token.encode()) for token in tokens.read):
        return AuthPrincipal("read")
    task_id = decode_task_capability(presented, tokens.write)
    return AuthPrincipal("write", task_id) if task_id is not None else None


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
        if not isinstance(raw, dict) or not set(raw) <= {"read", "write"} or "write" not in raw:
            raise _credential_error()
        read, write = raw.get("read", []), raw["write"]
        if not isinstance(read, list) or not isinstance(write, list) or not write:
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
    if not values:
        raise ValueError(f"authentication credential has no configured {privilege} token")
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


def snapshot_task_capability(
    reference: str,
    task_id: str,
    *,
    directory: str | Path | None = None,
    secrets_dir: str | Path | None = None,
    prefix: str = "panopticon-service-auth-",
) -> Path:
    """Create a private runtime snapshot containing only ``task_id``'s capability."""
    tokens = load_tokens(reference, secrets_dir=secrets_dir)
    capability = derive_task_capability(tokens.write[-1], task_id)
    fd, raw_path = tempfile.mkstemp(prefix=prefix, suffix=".json", dir=directory)
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"task": capability}, handle)
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
        contents = _read_regular_file(path)
        try:
            runtime = json.loads(contents)
        except json.JSONDecodeError as exc:
            raise _credential_error() from exc
        if isinstance(runtime, dict) and set(runtime) == {"task"}:
            task_token = runtime["task"]
            if privilege != "write" or not isinstance(task_token, str) or not task_token:
                raise _credential_error()
            return task_token
        tokens = _parse_tokens(contents, allow_runtime_snapshot=True)
        values = tokens.read if privilege == "read" else tokens.write
        if not values:
            raise _credential_error()
        return values[-1]
    return load_client_token(reference, privilege=privilege)
