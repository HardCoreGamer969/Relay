"""The persistent user-config store (the NON-secret half).

Relay's config lives in two deliberately separate files in the global OS
user-config dir (``%LOCALAPPDATA%\\relay`` on Windows, ``~/.config/relay`` on
Linux/Mac -- resolved via ``platformdirs``):

- ``config.json`` (HERE) -- inspectable, non-secret selections + preferences
  (which provider/model/thinking per role, reserved picker sockets).
- ``auth.json`` (:mod:`relay.secrets`) -- credentials, written ``0o600``, never
  logged or printed.

The separation is the point: config is safe to print/inspect; secrets are not,
and keeping them in a different module makes "no secret ever touches config" an
auditable property (config.py / this module never import a key).

``RELAY_CONFIG_DIR`` overrides the directory (used by tests, and a power-user
escape hatch). Reads are defensive: a missing file is ``{}`` (fall through to env
/ defaults exactly as before), a corrupt file warns and is treated as absent --
never a crash. Writes are atomic (temp file then rename).
"""

from __future__ import annotations

import json
import os
import warnings
from pathlib import Path

import platformdirs

# Bumped only on a breaking config-schema change; round-tripped verbatim so an
# older Relay reading a newer file (or vice versa) degrades gracefully.
CONFIG_VERSION = 1

_APP_NAME = "relay"


def config_dir() -> Path:
    """The global user-config directory for Relay (``RELAY_CONFIG_DIR`` overrides).

    NOT project-local -- per-project overrides are a reserved future layer.
    """
    override = os.environ.get("RELAY_CONFIG_DIR")
    if override:
        return Path(override)
    return Path(platformdirs.user_config_dir(_APP_NAME, appauthor=False))


def config_path() -> Path:
    return config_dir() / "config.json"


def load_config() -> dict:
    """Read ``config.json`` as a dict. Never raises.

    Missing file -> ``{}`` (callers fall through to env/defaults, unchanged
    behavior). Corrupt/non-object file -> warn once and treat as absent (``{}``).
    Unknown fields are preserved on read so a round-trip doesn't drop them.
    """
    path = config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        warnings.warn(f"ignoring corrupt config at {path} (invalid JSON)", stacklevel=2)
        return {}
    if not isinstance(data, dict):
        warnings.warn(f"ignoring config at {path} (not a JSON object)", stacklevel=2)
        return {}
    return data


def save_config(config: dict) -> Path:
    """Atomically write ``config.json`` (temp file then rename). Returns the path.

    config.json is non-secret by contract -- callers must never put a key here.
    """
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = config_path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path
