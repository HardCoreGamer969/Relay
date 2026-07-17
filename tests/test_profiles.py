"""Tests for assumption profiles (B1)."""

from __future__ import annotations

from relay.profiles import (
    DEFAULT_PROFILE,
    get_profile,
    resolve_profile,
    write_repo_profile,
)


def test_builtins_exist():
    for name in ("surgeon", "contractor", "intern", "chaos"):
        p = get_profile(name)
        assert p is not None
        assert p.assumption_level in ("1", "2", "3", "4", "5", "auto")


def test_resolve_override_beats_env(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_PROFILE", "chaos")
    write_repo_profile(tmp_path, "intern")
    assert resolve_profile("surgeon", root=tmp_path, config={}).name == "surgeon"


def test_resolve_repo_beats_env(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_PROFILE", "chaos")
    write_repo_profile(tmp_path, "intern")
    assert resolve_profile(None, root=tmp_path, config={}).name == "intern"


def test_resolve_env_beats_config(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_PROFILE", "chaos")
    assert resolve_profile(None, root=tmp_path, config={"profile": "surgeon"}).name == "chaos"


def test_unknown_falls_to_default(monkeypatch, tmp_path):
    monkeypatch.delenv("RELAY_PROFILE", raising=False)
    assert resolve_profile("nope", root=tmp_path, config={}).name == DEFAULT_PROFILE
