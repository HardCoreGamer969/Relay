"""Network-free tests for the `relay config` CLI group.

Live validation / model-listing are monkeypatched so nothing hits the network;
the config/auth files are isolated to a tmp dir by the autouse conftest fixture.
The load-bearing property: a key is never printed by `config show`.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from relay import cli
import relay.secrets as secrets
import relay.store as store
from relay.cli import app

runner = CliRunner()


# --- config show ------------------------------------------------------------


def test_config_show_reports_key_presence_never_the_value(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    secrets.set_key("deepseek", "sk-do-not-print-me")

    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    assert "present" in result.output and "absent" in result.output
    # The secret value must NEVER appear in the output.
    assert "sk-do-not-print-me" not in result.output
    # Per-role resolution + provenance is shown.
    assert "brain" in result.output and "hands" in result.output


# --- set-role (live validation) ---------------------------------------------


def test_set_role_rejects_invalid_slug(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(cli, "_validate_role_model", lambda p, m: (False, "no such model"))

    result = runner.invoke(
        app, ["config", "set-role", "hands", "-p", "openrouter", "-m", "typo/nope"]
    )
    assert result.exit_code != 0
    assert "rejected" in result.output
    # Nothing was written.
    assert store.load_config() == {}


def test_set_role_accepts_valid_slug_and_writes_config(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(cli, "_validate_role_model", lambda p, m: (True, "resolved"))

    result = runner.invoke(
        app,
        ["config", "set-role", "hands", "-p", "deepseek", "-m", "deepseek-v4-flash", "--thinking"],
    )
    assert result.exit_code == 0
    cfg = store.load_config()
    assert cfg["roles"]["hands"] == {
        "provider": "deepseek", "model": "deepseek-v4-flash", "thinking": True,
    }
    # Reserved picker sockets are seeded (inert) when starting from scratch.
    assert cfg["preferences"]["cost_bias"] == "balanced"
    assert cfg["recommendations_source"] == "bundled"


def test_set_role_unknown_provider_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))
    result = runner.invoke(
        app, ["config", "set-role", "brain", "-p", "nope", "-m", "x"]
    )
    assert result.exit_code != 0
    assert store.load_config() == {}


# --- set-key (no echo) / remove-key -----------------------------------------


def test_set_key_stores_without_echo(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))
    # The key is supplied on stdin (hidden prompt); it must not appear in output.
    result = runner.invoke(app, ["config", "set-key", "deepseek"], input="sk-secret-typed\n")
    assert result.exit_code == 0
    assert "sk-secret-typed" not in result.output  # never echoed/printed
    raw = json.loads(secrets.auth_path().read_text(encoding="utf-8"))
    assert raw["deepseek"] == {"type": "api", "key": "sk-secret-typed"}


def test_set_key_empty_saves_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))
    result = runner.invoke(app, ["config", "set-key", "deepseek"], input="\n")
    assert result.exit_code != 0
    assert secrets.get_key("deepseek") is None


def test_remove_key(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))
    secrets.set_key("deepseek", "sk-x")
    result = runner.invoke(app, ["config", "remove-key", "deepseek"])
    assert result.exit_code == 0
    assert secrets.get_key("deepseek") is None


# --- list-models ------------------------------------------------------------


def test_list_models_manual_provider_says_manual():
    result = runner.invoke(app, ["config", "list-models", "openrouter"])
    assert result.exit_code == 0
    assert "manual slug entry" in result.output


def test_list_models_list_provider_lists_ids(monkeypatch):
    monkeypatch.setattr(
        cli, "list_models", lambda provider, **kw: ["deepseek-v4-flash", "deepseek-v4-pro"]
    )
    result = runner.invoke(app, ["config", "list-models", "deepseek"])
    assert result.exit_code == 0
    assert "deepseek-v4-flash" in result.output and "deepseek-v4-pro" in result.output
    # Deprecated ids the live endpoint omits aren't shown.
    assert "deepseek-chat" not in result.output


def test_config_group_is_registered():
    result = runner.invoke(app, ["config", "--help"])
    assert result.exit_code == 0
    for sub in ("show", "set-role", "set-key", "remove-key", "list-models"):
        assert sub in result.output
