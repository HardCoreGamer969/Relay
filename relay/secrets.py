"""The secrets store (the SECRET half): credentials in ``auth.json``.

Modeled on OpenCode's ``auth.json``: a SEPARATE file from ``config.json``, written
``0o600`` (owner read/write only), keyed by provider, holding a **credential
record** (``{"type": "api", "key": "..."}``) rather than a bare string -- so an
OAuth-type record can land later without a schema change.

ALL secret handling lives in this one module on purpose: a key is never written
to ``config.json``, never logged, never printed, never put in a run record or
telemetry. ``get/set/remove/all`` are keyed by provider; :func:`resolve_key`
implements the **env-key > auth.json** precedence so the existing
``OPENROUTER_API_KEY`` / ``DEEPSEEK_API_KEY`` workflow keeps winning untouched.

``RELAY_AUTH_CONTENT`` (like OpenCode's ``OPENCODE_AUTH_CONTENT``) injects the
whole auth file as JSON for CI/tests -- read-only; writes always go to the file.
"""

from __future__ import annotations

import json
import os
import warnings
from pathlib import Path

from relay.store import config_dir

# The env var that injects the entire auth file as JSON (read-only; CI/tests).
AUTH_CONTENT_ENV = "RELAY_AUTH_CONTENT"

# Owner read/write only. POSIX honors this; on Windows it is a best-effort no-op
# (the file still lives in the per-user profile dir).
_AUTH_MODE = 0o600


def auth_path() -> Path:
    return config_dir() / "auth.json"


def _read_auth() -> dict:
    """Read the auth map. ``RELAY_AUTH_CONTENT`` wins; else the file; else ``{}``.

    Never raises -- a missing or corrupt source is simply "no credentials".
    """
    injected = os.environ.get(AUTH_CONTENT_ENV)
    if injected is not None:
        return _parse_auth(injected, source=AUTH_CONTENT_ENV)
    try:
        text = auth_path().read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    return _parse_auth(text, source=str(auth_path()))


def _parse_auth(text: str, *, source: str) -> dict:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        warnings.warn(f"ignoring corrupt auth at {source} (invalid JSON)", stacklevel=2)
        return {}
    return data if isinstance(data, dict) else {}


def _write_auth(auth: dict) -> Path:
    """Atomically write ``auth.json`` with ``0o600`` perms. Writes go to the FILE
    (never the env override)."""
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = auth_path()
    tmp = path.with_name(path.name + ".tmp")
    # Create the temp file with restrictive perms from the start (POSIX), so the
    # secret is never briefly world-readable between write and chmod.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _AUTH_MODE)
    try:
        os.write(fd, json.dumps(auth, indent=2, ensure_ascii=False).encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)
    try:
        os.chmod(path, _AUTH_MODE)  # belt-and-suspenders (and after replace)
    except OSError:
        pass  # Windows / unusual filesystems: best-effort
    return path


def get_key(provider: str) -> str | None:
    """The stored API key for ``provider`` from auth.json (NOT env). ``None`` if absent."""
    record = _read_auth().get(provider)
    if isinstance(record, dict):
        key = record.get("key")
        return key if isinstance(key, str) and key else None
    return None


def set_key(provider: str, key: str, *, type: str = "api") -> Path:
    """Store ``key`` for ``provider`` as a credential record, ``0o600``. Returns the path."""
    auth = _read_file_auth()  # writes build on the FILE state, not the env override
    auth[provider] = {"type": type, "key": key}
    return _write_auth(auth)


def remove_key(provider: str) -> Path:
    """Remove ``provider``'s credential record (no-op if absent). Returns the path."""
    auth = _read_file_auth()
    auth.pop(provider, None)
    return _write_auth(auth)


def all_providers() -> list[str]:
    """Providers that currently have a stored credential (sorted)."""
    return sorted(k for k, v in _read_auth().items() if isinstance(v, dict))


def _read_file_auth() -> dict:
    """Read the on-disk auth map only (ignores the env override) -- the basis a
    write mutates, so a CI injection can't get accidentally persisted."""
    try:
        text = auth_path().read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    return _parse_auth(text, source=str(auth_path()))


def resolve_key(provider: str, key_env: str) -> str | None:
    """The key Relay will use for ``provider``: **env var > auth.json** > ``None``.

    Preserves the historical env-key workflow as highest precedence; the stored
    key is the fallback for a user who set it in-app instead of the shell.
    """
    env = os.environ.get(key_env)
    if env:
        return env
    return get_key(provider)
