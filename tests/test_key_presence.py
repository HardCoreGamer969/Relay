"""Bug 2 (v0.0.19): key PRESENCE mirrors resolve_key (env var OR auth.json).

Regression-lock: the presence check must use the SAME precedence as the real key
*resolution* -- a key counts as "present" when it's resolvable from the env var OR
auth.json. Both surfaces share ``resolve_key``:

- ``describe_resolution`` (feeds ``config show`` / ``/config``), and
- the TUI first-run gate ``_has_usable_config``,

so an env-configured user (the developer's normal state) is never wrongly reported
"absent" nor pushed into first-run setup. (Already correct as of v0.0.18 -- this
pins it so the two can never diverge from ``resolve_key`` again.)
"""

from __future__ import annotations

import relay.secrets as secrets
from relay.config import ModelConfig, describe_resolution
from relay.tui import RelayTuiApp


def _no_keys(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


# --- describe_resolution: env OR auth.json => present -----------------------


def test_env_key_is_reported_present(monkeypatch):
    _no_keys(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env-or")  # env only, auth.json empty
    providers = describe_resolution()["providers"]
    assert providers["openrouter"]["key_present"] is True
    assert providers["deepseek"]["key_present"] is False


def test_auth_json_key_is_reported_present(monkeypatch):
    _no_keys(monkeypatch)
    secrets.set_key("deepseek", "sk-stored")  # auth.json only, no env
    providers = describe_resolution()["providers"]
    assert providers["deepseek"]["key_present"] is True
    assert providers["openrouter"]["key_present"] is False


def test_absent_when_neither_env_nor_auth(monkeypatch):
    _no_keys(monkeypatch)
    providers = describe_resolution()["providers"]
    assert providers["openrouter"]["key_present"] is False
    assert providers["deepseek"]["key_present"] is False


# --- _has_usable_config: an env-key user is NOT sent to first-run setup ------


def test_has_usable_config_true_with_env_key(tmp_path, monkeypatch):
    _no_keys(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env-or")  # both roles -> openrouter
    app = RelayTuiApp(root=str(tmp_path), models=ModelConfig(brain="a/b", hands="c/d"))
    assert app._has_usable_config() is True


def test_has_usable_config_false_without_any_key(tmp_path, monkeypatch):
    _no_keys(monkeypatch)
    app = RelayTuiApp(root=str(tmp_path), models=ModelConfig(brain="a/b", hands="c/d"))
    assert app._has_usable_config() is False
