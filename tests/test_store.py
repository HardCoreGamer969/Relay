"""Network-free tests for the config + secrets backend (store.py / secrets.py).

The autouse conftest fixture points RELAY_CONFIG_DIR at a fresh tmp dir per test
and clears RELAY_AUTH_CONTENT, so these never touch the real user config and are
order-independent. Precedence, perms, defensiveness, and secret-non-leak are the
load-bearing properties.
"""

from __future__ import annotations

import json
import os

import pytest

import relay.secrets as secrets
import relay.store as store
from relay.config import (
    DEFAULT_BRAIN_MODEL,
    DEFAULT_HANDS_MODEL,
    default_config,
    describe_resolution,
    load_models,
    resolve_role_field,
)


# --- config.json round-trip + defensiveness ---------------------------------


def test_config_round_trips(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))
    cfg = default_config()
    cfg["roles"]["hands"]["provider"] = "deepseek"
    cfg["roles"]["hands"]["model"] = "deepseek-v4-flash"
    path = store.save_config(cfg)
    assert path == store.config_path()
    assert store.load_config() == cfg  # write -> read -> equal


def test_absent_config_is_empty_dict():
    # Fresh isolated dir: no file yet.
    assert store.load_config() == {}


def test_corrupt_config_is_harmless(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))
    store.config_dir().mkdir(parents=True, exist_ok=True)
    store.config_path().write_text("{ not valid json", encoding="utf-8")
    with pytest.warns(UserWarning):
        assert store.load_config() == {}  # warn + treat as absent, never crash


def test_default_config_has_reserved_sockets():
    cfg = default_config()
    assert cfg["version"] == store.CONFIG_VERSION
    # Reserved picker sockets are present + round-trippable, but inert.
    assert cfg["preferences"]["cost_bias"] == "balanced"
    assert cfg["recommendations_source"] == "bundled"
    assert set(cfg["roles"]) == {"brain", "hands"}


# --- model/provider/thinking precedence: env > config > default -------------


def test_default_when_nothing_set(monkeypatch):
    for var in ("RELAY_BRAIN_MODEL", "RELAY_BRAIN_PROVIDER", "RELAY_BRAIN_THINKING"):
        monkeypatch.delenv(var, raising=False)
    value, source = resolve_role_field("brain", "model", {})
    assert (value, source) == (DEFAULT_BRAIN_MODEL, "default")
    assert resolve_role_field("brain", "provider", {}) == ("openrouter", "default")
    assert resolve_role_field("brain", "thinking", {}) == (False, "default")


def test_config_beats_default(monkeypatch):
    for var in ("RELAY_HANDS_MODEL", "RELAY_HANDS_PROVIDER", "RELAY_HANDS_THINKING"):
        monkeypatch.delenv(var, raising=False)
    cfg = {"roles": {"hands": {"provider": "deepseek", "model": "deepseek-v4-flash", "thinking": True}}}
    assert resolve_role_field("hands", "provider", cfg) == ("deepseek", "config")
    assert resolve_role_field("hands", "model", cfg) == ("deepseek-v4-flash", "config")
    assert resolve_role_field("hands", "thinking", cfg) == (True, "config")


def test_env_beats_config(monkeypatch):
    monkeypatch.setenv("RELAY_HANDS_MODEL", "from-env")
    monkeypatch.setenv("RELAY_HANDS_PROVIDER", "openrouter")
    monkeypatch.setenv("RELAY_HANDS_THINKING", "1")
    cfg = {"roles": {"hands": {"provider": "deepseek", "model": "from-config", "thinking": False}}}
    assert resolve_role_field("hands", "model", cfg) == ("from-env", "env")
    assert resolve_role_field("hands", "provider", cfg) == ("openrouter", "env")
    assert resolve_role_field("hands", "thinking", cfg) == (True, "env")


def test_load_models_reads_config_when_env_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))
    # chdir away from the repo so load_env() finds no project .env to repopulate
    # the role vars -- this test is specifically the "env truly absent" case.
    monkeypatch.chdir(tmp_path)
    for var in ("RELAY_HANDS_MODEL", "RELAY_HANDS_PROVIDER", "RELAY_HANDS_THINKING",
                "RELAY_BRAIN_MODEL", "RELAY_BRAIN_PROVIDER", "RELAY_BRAIN_THINKING"):
        monkeypatch.delenv(var, raising=False)
    cfg = default_config()
    cfg["roles"]["hands"] = {"provider": "deepseek", "model": "deepseek-v4-pro", "thinking": True}
    store.save_config(cfg)

    models = load_models()
    assert models.provider_for_role("hands") == "deepseek"
    assert models.for_role("hands") == "deepseek-v4-pro"
    assert models.thinking_for_role("hands") is True
    # The env workflow is untouched: brain (no env, no config override) is default.
    assert models.provider_for_role("brain") == "openrouter"


def test_env_still_wins_in_load_models(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))
    cfg = default_config()
    cfg["roles"]["brain"]["model"] = "config-model"
    store.save_config(cfg)
    monkeypatch.setenv("RELAY_BRAIN_MODEL", "env-model")  # env overrides config
    assert load_models().for_role("brain") == "env-model"


# --- auth.json: 0o600, get/set/remove, precedence, override -----------------


def test_set_key_writes_record_and_round_trips(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))
    secrets.set_key("deepseek", "sk-secret-123")
    assert secrets.get_key("deepseek") == "sk-secret-123"
    # Stored as a credential RECORD (room for OAuth later), not a bare string.
    raw = json.loads(secrets.auth_path().read_text(encoding="utf-8"))
    assert raw["deepseek"] == {"type": "api", "key": "sk-secret-123"}


@pytest.mark.skipif(os.name != "posix", reason="POSIX file mode bits")
def test_auth_file_is_0600(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))
    secrets.set_key("openrouter", "sk-or-xyz")
    mode = secrets.auth_path().stat().st_mode & 0o777
    assert mode == 0o600


def test_remove_key(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))
    secrets.set_key("deepseek", "sk-x")
    secrets.remove_key("deepseek")
    assert secrets.get_key("deepseek") is None


def test_env_key_beats_auth_file(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))
    secrets.set_key("openrouter", "from-auth-file")
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    assert secrets.resolve_key("openrouter", "OPENROUTER_API_KEY") == "from-env"
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert secrets.resolve_key("openrouter", "OPENROUTER_API_KEY") == "from-auth-file"


def test_auth_content_env_override(monkeypatch):
    # env-key still wins over auth content, so clear it to exercise the override.
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv(
        "RELAY_AUTH_CONTENT",
        json.dumps({"deepseek": {"type": "api", "key": "ci-injected"}}),
    )
    assert secrets.get_key("deepseek") == "ci-injected"
    assert secrets.resolve_key("deepseek", "DEEPSEEK_API_KEY") == "ci-injected"


def test_corrupt_auth_is_harmless(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))
    secrets.config_dir().mkdir(parents=True, exist_ok=True)
    secrets.auth_path().write_text("not json", encoding="utf-8")
    with pytest.warns(UserWarning):
        assert secrets.get_key("deepseek") is None  # warn + treat as absent


def test_write_does_not_persist_env_override(monkeypatch, tmp_path):
    # A CI-injected auth must not be silently written to disk when set_key runs.
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv(
        "RELAY_AUTH_CONTENT", json.dumps({"openrouter": {"type": "api", "key": "ci"}})
    )
    secrets.set_key("deepseek", "disk-key")
    monkeypatch.delenv("RELAY_AUTH_CONTENT", raising=False)
    on_disk = json.loads(secrets.auth_path().read_text(encoding="utf-8"))
    assert "deepseek" in on_disk
    assert "openrouter" not in on_disk  # the env-injected provider never hit disk


# --- secrets never leak into config show output -----------------------------


def test_describe_resolution_reports_key_presence_not_value(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    secrets.set_key("deepseek", "sk-super-secret-value")

    report = describe_resolution()
    assert report["providers"]["deepseek"]["key_present"] is True
    assert report["providers"]["openrouter"]["key_present"] is False
    # The actual secret must NEVER appear anywhere in the resolution report.
    assert "sk-super-secret-value" not in json.dumps(report)
