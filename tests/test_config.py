"""Network-free tests for the assumption dial (relay/config.py)."""

from __future__ import annotations

from relay.config import (
    ASSUMPTION_LEVELS,
    DEFAULT_ASSUMPTION_LEVEL,
    DEFAULT_MAX_TOTAL_STEPS,
    assumption_directive,
    assumption_summary,
    resolve_assumption_level,
    resolve_max_total_steps,
)


def test_assumption_summary_is_grounded_in_the_directive():
    """Each summary is DERIVED from the real directive, not a hardcoded guess: it's
    non-empty for every level, short (a phrase), and its words appear in the
    directive it summarizes (so it can't drift from the dial's actual behavior)."""
    for level in ASSUMPTION_LEVELS:
        summary = assumption_summary(level)
        directive = assumption_directive(level).lower()
        assert summary, f"{level} has no summary"
        assert len(summary) < 120, f"{level} summary is a paragraph, not a phrase"
        # Every word of the derived summary comes from the directive text.
        for word in summary.lower().replace("--", " ").split():
            assert word in directive, f"{level}: {word!r} is not in the real directive"


def test_assumption_summary_distinguishes_loose_from_strict():
    # Grounded in the real low-vs-high posture (assume freely .. assume almost nothing).
    assert "assume almost everything" in assumption_summary("1")
    assert "assume almost nothing" in assumption_summary("5")


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


# --- v0.0.21: the global step ceiling (env > config > default 50) ------------


def test_max_total_steps_defaults_to_50(monkeypatch):
    monkeypatch.delenv("RELAY_MAX_TOTAL_STEPS", raising=False)
    assert resolve_max_total_steps(config={}) == 50
    assert DEFAULT_MAX_TOTAL_STEPS == 50


def test_max_total_steps_env_beats_config(monkeypatch):
    monkeypatch.setenv("RELAY_MAX_TOTAL_STEPS", "200")
    assert resolve_max_total_steps(config={"max_total_steps": 99}) == 200


def test_max_total_steps_config_beats_default(monkeypatch):
    monkeypatch.delenv("RELAY_MAX_TOTAL_STEPS", raising=False)
    assert resolve_max_total_steps(config={"max_total_steps": 120}) == 120


def test_max_total_steps_override_beats_env(monkeypatch):
    monkeypatch.setenv("RELAY_MAX_TOTAL_STEPS", "200")
    assert resolve_max_total_steps(override=30, config={}) == 30


def test_max_total_steps_can_be_disabled(monkeypatch):
    monkeypatch.delenv("RELAY_MAX_TOTAL_STEPS", raising=False)
    assert resolve_max_total_steps(override=0, config={}) is None       # 0 -> unbounded
    assert resolve_max_total_steps(override="off", config={}) is None
    monkeypatch.setenv("RELAY_MAX_TOTAL_STEPS", "none")
    assert resolve_max_total_steps(config={"max_total_steps": 5}) is None


def test_max_total_steps_invalid_falls_through(monkeypatch):
    monkeypatch.setenv("RELAY_MAX_TOTAL_STEPS", "abc")  # unparseable -> next source
    assert resolve_max_total_steps(config={"max_total_steps": 77}) == 77
    monkeypatch.setenv("RELAY_MAX_TOTAL_STEPS", "-5")   # negative -> next source
    assert resolve_max_total_steps(config={}) == 50
