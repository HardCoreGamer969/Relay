"""Network-free tests for cost envelope contracts (relay/envelope.py + resolvers)."""

from __future__ import annotations

from relay.config import (
    DEFAULT_ENVELOPE_SCOPE,
    DEFAULT_ENVELOPE_WARN,
    resolve_envelope_scope,
    resolve_envelope_warn,
)
from relay.envelope import CostEnvelope, brain_cost_since, format_warn_pct
from relay.telemetry import CallRecord, Ledger


def test_envelope_scope_defaults_to_all(monkeypatch):
    monkeypatch.delenv("RELAY_ENVELOPE_SCOPE", raising=False)
    assert resolve_envelope_scope(config={}) == "all"
    assert DEFAULT_ENVELOPE_SCOPE == "all"


def test_envelope_scope_override_beats_env(monkeypatch):
    monkeypatch.setenv("RELAY_ENVELOPE_SCOPE", "all")
    assert resolve_envelope_scope(override="execution", config={}) == "execution"


def test_envelope_scope_env_beats_config(monkeypatch):
    monkeypatch.setenv("RELAY_ENVELOPE_SCOPE", "execution")
    assert resolve_envelope_scope(config={"envelope_scope": "all"}) == "execution"


def test_envelope_scope_invalid_falls_through(monkeypatch):
    monkeypatch.setenv("RELAY_ENVELOPE_SCOPE", "banana")
    assert resolve_envelope_scope(config={"envelope_scope": "execution"}) == "execution"


def test_envelope_warn_defaults(monkeypatch):
    monkeypatch.delenv("RELAY_ENVELOPE_WARN", raising=False)
    assert resolve_envelope_warn(config={}) == DEFAULT_ENVELOPE_WARN
    assert DEFAULT_ENVELOPE_WARN == (0.50, 0.80, 0.90, 0.99)


def test_envelope_warn_parses_percents_and_fractions(monkeypatch):
    monkeypatch.delenv("RELAY_ENVELOPE_WARN", raising=False)
    assert resolve_envelope_warn(override="50,80%,0.9,99", config={}) == (
        0.50,
        0.80,
        0.90,
        0.99,
    )


def test_envelope_warn_override_beats_env(monkeypatch):
    monkeypatch.setenv("RELAY_ENVELOPE_WARN", "0.5")
    assert resolve_envelope_warn(override=[0.25, 0.75], config={}) == (0.25, 0.75)


def test_preflight_states_unbounded_cost_explicitly():
    text = CostEnvelope(max_steps=50).preflight_text()
    assert "cost: unbounded" in text
    assert "steps ≤ 50" in text
    assert "50%/80%/90%/99%" in format_warn_pct(DEFAULT_ENVELOPE_WARN)


def test_preflight_includes_scope_when_cost_set():
    text = CostEnvelope(max_cost=0.4, max_steps=12, scope="execution").preflight_text()
    assert "cost ≤ $0.4000" in text
    assert "scope=execution" in text


def test_chargeable_cost_execution_excludes_baseline():
    ledger = Ledger()
    ledger.add(CallRecord("brain", "m", 1, 1, 0.0, 0.10))
    env = CostEnvelope(max_cost=1.0, scope="execution")
    env.mark_execution_start(ledger)
    ledger.add(CallRecord("hands", "m", 1, 1, 0.0, 0.05))
    assert abs(env.chargeable_cost(ledger) - 0.05) < 1e-9
    assert not env.hit_cost_limit(ledger)
    ledger.add(CallRecord("hands", "m", 1, 1, 0.0, 1.0))
    assert env.hit_cost_limit(ledger)


def test_chargeable_cost_all_includes_planning():
    ledger = Ledger()
    ledger.add(CallRecord("brain", "m", 1, 1, 0.0, 0.10))
    env = CostEnvelope(max_cost=0.15, scope="all")
    env.mark_execution_start(ledger)  # no-op for scope=all
    assert env.chargeable_cost(ledger) == 0.10
    ledger.add(CallRecord("hands", "m", 1, 1, 0.0, 0.06))
    assert env.hit_cost_limit(ledger)


def test_warnings_fire_once_per_threshold_dimension():
    ledger = Ledger()
    env = CostEnvelope(
        max_cost=1.0,
        max_steps=10,
        warn_thresholds=(0.5, 0.8),
        scope="all",
    )
    ledger.add(CallRecord("brain", "m", 1, 1, 0.0, 0.5))
    first = env.drain_warnings(ledger=ledger, steps_used=5)
    assert {w["dimension"] for w in first} == {"cost", "steps"}
    assert all(w["threshold"] == 0.5 for w in first)
    # Same levels again — no re-fire.
    assert env.drain_warnings(ledger=ledger, steps_used=5) == []
    ledger.add(CallRecord("brain", "m", 1, 1, 0.0, 0.3))
    second = env.drain_warnings(ledger=ledger, steps_used=8)
    assert len(second) == 2
    assert {w["threshold"] for w in second} == {0.8}


def test_brain_cost_since_sums_only_brain():
    ledger = Ledger()
    ledger.add(CallRecord("hands", "m", 1, 1, 0.0, 9.0))
    start = len(ledger.records)
    ledger.add(CallRecord("brain", "m", 1, 1, 0.0, 0.02))
    ledger.add(CallRecord("hands", "m", 1, 1, 0.0, 0.01))
    ledger.add(CallRecord("brain", "m", 1, 1, 0.0, 0.03))
    assert brain_cost_since(ledger, start) == 0.05


def test_receipt_includes_wasted_and_outcome():
    ledger = Ledger()
    ledger.add(CallRecord("brain", "m", 10, 5, 0.1, 0.002))
    ledger.add(CallRecord("hands", "m", 20, 5, 0.2, 0.001))
    env = CostEnvelope(max_cost=1.0, scope="all")
    env.wasted_brain_usd = 0.0015
    env.completed_steps = 2
    lines = env.receipt_lines(ledger, status="completed")
    joined = "\n".join(lines)
    assert "wasted brain" in joined
    assert "$0.0015" in joined
    assert "envelope outcome: within" in joined
    assert "$/completed-step" in joined
