"""Network-free tests for the assumption dial (relay/config.py)."""

from __future__ import annotations

from relay.config import (
    ASSUMPTION_LEVELS,
    DEFAULT_ASSUMPTION_LEVEL,
    assumption_directive,
    resolve_assumption_level,
)


def test_default_is_auto(monkeypatch):
    monkeypatch.delenv("RELAY_ASSUMPTION_LEVEL", raising=False)
    assert resolve_assumption_level() == "auto"
    assert DEFAULT_ASSUMPTION_LEVEL == "auto"


def test_env_sets_level(monkeypatch):
    monkeypatch.setenv("RELAY_ASSUMPTION_LEVEL", "5")
    assert resolve_assumption_level() == "5"


def test_override_beats_env(monkeypatch):
    monkeypatch.setenv("RELAY_ASSUMPTION_LEVEL", "5")
    assert resolve_assumption_level(override="1") == "1"


def test_int_override_accepted(monkeypatch):
    monkeypatch.delenv("RELAY_ASSUMPTION_LEVEL", raising=False)
    assert resolve_assumption_level(override=3) == "3"


def test_invalid_override_falls_through(monkeypatch):
    monkeypatch.setenv("RELAY_ASSUMPTION_LEVEL", "4")
    assert resolve_assumption_level(override="banana") == "4"  # bad override -> env


def test_invalid_everywhere_defaults_to_auto(monkeypatch):
    monkeypatch.setenv("RELAY_ASSUMPTION_LEVEL", "11")
    assert resolve_assumption_level(override="nope") == "auto"


def test_auto_is_distinct_from_numerics(monkeypatch):
    monkeypatch.delenv("RELAY_ASSUMPTION_LEVEL", raising=False)
    assert resolve_assumption_level(override="auto") == "auto"
    assert "auto" in ASSUMPTION_LEVELS
    assert "auto" not in ("1", "2", "3", "4", "5")  # not a numeric midpoint


def test_directive_carries_level_marker():
    for level in ("1", "2", "3", "4", "5", "auto"):
        directive = assumption_directive(level)
        assert f"ASSUMPTION DIAL = {level}" in directive
    # Unknown level degrades to the auto directive.
    assert "auto" in assumption_directive("nonsense")


def test_directive_low_vs_high_differ_in_posture():
    low = assumption_directive("1").lower()
    high = assumption_directive("5").lower()
    assert "assume almost everything" in low
    assert "assume almost nothing" in high
