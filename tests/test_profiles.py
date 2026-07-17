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


def test_env_assumption_level_beats_default_profile(monkeypatch, tmp_path):
    """relay run must honor RELAY_ASSUMPTION_LEVEL over the default profile dial."""
    import os
    from relay.config import resolve_assumption_level
    from relay.profiles import resolve_profile

    monkeypatch.delenv("RELAY_PROFILE", raising=False)
    monkeypatch.setenv("RELAY_ASSUMPTION_LEVEL", "5")
    active = resolve_profile(None, root=tmp_path, config={})
    dial_override = None
    if dial_override is None and not os.environ.get("RELAY_ASSUMPTION_LEVEL"):
        dial_override = active.assumption_level
    dial = resolve_assumption_level(override=dial_override)
    assert dial == "5"
    assert active.assumption_level == "3"  # contractor default still 3
