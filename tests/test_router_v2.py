"""Tests for router v2: route contracts (E1) and layered features."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from relay.config import DEFAULT_BRAIN_MODEL, DEFAULT_HANDS_MODEL, ModelConfig
from relay.envelope import CostEnvelope
from relay.router import (
    BUMP_FREEZE_FRACTION,
    CALL_CLASSES,
    DEFAULT_ROUTE,
    ROUTE_MODELS,
    ModelRouter,
    RouteContract,
    apply_provider_suffix,
    apply_route,
    builtin_contract,
    estimate_counterfactual_cost,
    explain_spend,
    format_broker_line,
    load_route_contract,
    log_shadow_decision,
    model_for_call_class,
    parse_route_contract,
    provider_routing_extras,
    recommend_route,
    resolve_route,
    resolve_route_contract,
    save_route_contract,
)


# --- E1 Route contracts -------------------------------------------------------


def test_e1_builtin_contract_has_call_class():
    c = builtin_contract("balanced")
    assert c is not None
    assert c.schema_version == 2
    assert set(CALL_CLASSES) <= set(c.call_class)
    assert c.brain == ROUTE_MODELS["balanced"]["brain"]
    assert c.call_class["skeptic"] == c.hands


def test_e1_parse_legacy_name_only():
    c = parse_route_contract({"route": "economy"})
    assert c.name == "economy"
    assert c.brain == ROUTE_MODELS["economy"]["brain"]


def test_e1_parse_full_contract_and_pins():
    c = parse_route_contract(
        {
            "schema_version": 2,
            "route": "balanced",
            "brain": "vendor/custom-brain",
            "hands": "vendor/custom-hands",
            "call_class": {"skeptic": "economy", "hands_step": "hands"},
            "pins": {"brain": "vendor/pinned-brain"},
            "provider_sort": "floor",
            "max_price": 0.5,
            "unknown_future": True,
        }
    )
    assert c.brain == "vendor/custom-brain"
    assert c.pins["brain"] == "vendor/pinned-brain"
    assert c.provider_sort == "floor"
    assert c.max_price == 0.5
    assert "unknown_future" in c.unknown_keys
    # economy in call_class expands to economy brain slug
    assert c.call_class["skeptic"] == ROUTE_MODELS["economy"]["brain"]


def test_e1_save_load_roundtrip(tmp_path):
    c = builtin_contract("premium")
    assert c is not None
    path = save_route_contract(tmp_path, c)
    assert path.exists()
    loaded = load_route_contract(tmp_path)
    assert loaded is not None
    assert loaded.name == "premium"
    assert loaded.brain == c.brain
    assert loaded.call_class["plan"] == c.call_class["plan"]


def test_e1_precedence_cli_beats_repo(tmp_path, monkeypatch):
    save_route_contract(tmp_path, builtin_contract("premium"))  # type: ignore[arg-type]
    monkeypatch.setenv("RELAY_ROUTE", "economy")
    assert resolve_route_contract("balanced", root=tmp_path, config={}).name == "balanced"
    # Without CLI: repo wins over env
    monkeypatch.delenv("RELAY_ROUTE", raising=False)
    assert resolve_route_contract(None, root=tmp_path, config={}).name == "premium"


def test_e1_precedence_env_beats_config(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_ROUTE", "economy")
    assert resolve_route_contract(None, root=tmp_path, config={"route": "premium"}).name == "economy"


def test_e1_contract_pin_beats_route_default(monkeypatch):
    monkeypatch.delenv("RELAY_BRAIN_MODEL", raising=False)
    monkeypatch.delenv("RELAY_HANDS_MODEL", raising=False)
    contract = parse_route_contract(
        {
            "route": "balanced",
            "pins": {"brain": "pinned/brain", "hands": "pinned/hands"},
        }
    )
    base = ModelConfig(brain=DEFAULT_BRAIN_MODEL, hands=DEFAULT_HANDS_MODEL)
    cfg, changes = apply_route(base, contract, config={})
    assert cfg.brain == "pinned/brain"
    assert cfg.hands == "pinned/hands"
    assert any(ch.reason == "contract_pin" for ch in changes)


def test_e1_invalid_route_string_raises():
    with pytest.raises(ValueError):
        parse_route_contract("not-a-route")


def test_e1_resolve_route_profile_compat(tmp_path, monkeypatch):
    monkeypatch.delenv("RELAY_ROUTE", raising=False)
    save_route_contract(tmp_path, builtin_contract("economy"))  # type: ignore[arg-type]
    profile = resolve_route(None, root=tmp_path, config={})
    assert profile.name == "economy"
    assert profile.brain == ROUTE_MODELS["economy"]["brain"]


# --- E2 Call-class ------------------------------------------------------------


def test_e2_model_for_call_class_defaults():
    c = builtin_contract("balanced")
    assert c is not None
    assert model_for_call_class(c, "plan") == c.brain
    assert model_for_call_class(c, "hands_step") == c.hands
    assert model_for_call_class(c, "skeptic") == c.hands


def test_e2_models_for_purpose_emits_change(monkeypatch):
    monkeypatch.delenv("RELAY_BRAIN_MODEL", raising=False)
    monkeypatch.delenv("RELAY_HANDS_MODEL", raising=False)
    router = ModelRouter.from_resolve("balanced", config={})
    # Skeptic call-class remaps the brain slot to the cheap (hands) slug.
    models = ModelConfig(
        brain=ROUTE_MODELS["premium"]["brain"],
        hands=ROUTE_MODELS["premium"]["hands"],
    )
    out, change = router.models_for_purpose(models, "skeptic")
    assert change is not None
    assert change.reason == "call_class"
    assert change.purpose == "skeptic"
    assert change.role == "brain"
    assert out.brain == router.contract.hands
    assert out.brain != ROUTE_MODELS["premium"]["brain"]


# --- E3 Broker ----------------------------------------------------------------


def test_e3_broker_line_contains_route_and_freeze():
    router = ModelRouter.from_resolve("balanced", config={})
    router.bound_brain = router.contract.brain
    router.bound_hands = router.contract.hands
    line = format_broker_line(router, CostEnvelope(max_cost=1.0), None)
    assert "route=balanced" in line
    assert "freeze@80%" in line
    assert "remaining" in line


# --- E4 Counterfactual --------------------------------------------------------


def test_e4_counterfactual_with_fixed_tokens():
    records = [
        SimpleNamespace(
            role="brain",
            model="x",
            prompt_tokens=1_000_000,
            completion_tokens=0,
            cost_usd=1.0,
            purpose=None,
        ),
        SimpleNamespace(
            role="hands",
            model="y",
            prompt_tokens=1_000_000,
            completion_tokens=0,
            cost_usd=0.5,
            purpose=None,
        ),
    ]
    ledger = SimpleNamespace(
        records=records,
        total_cost=lambda: 1.5,
    )
    result = estimate_counterfactual_cost(ledger, baseline_route="premium", catalog=None)
    assert result["unknown"] is False
    assert result["counterfactual"] is not None
    assert result["counterfactual"] > 1.5  # premium rates higher than actual
    assert "saved" in result["lines"][0]
    assert "approx" in result["lines"][0]


# --- E5 Explain spend ---------------------------------------------------------


def test_e5_explain_spend_from_events_and_ledger():
    events = [
        SimpleNamespace(
            kind="route_change",
            message="route balanced: brain a→b (replan_bump)",
            payload={"purpose": "replan", "reason": "replan_bump"},
        )
    ]
    records = [
        SimpleNamespace(role="brain", cost_usd=0.1, purpose="replan"),
        SimpleNamespace(role="hands", cost_usd=0.05, purpose=None),
    ]
    ledger = SimpleNamespace(records=records, total_cost=lambda: 0.15)
    text = explain_spend(events, ledger)
    assert "## Spend" in text
    assert "replan" in text
    assert "TOTAL" in text


# --- E6 Cheap skeptic ---------------------------------------------------------


def test_e6_skeptic_uses_cheap_model_on_balanced(monkeypatch):
    monkeypatch.delenv("RELAY_BRAIN_MODEL", raising=False)
    monkeypatch.delenv("RELAY_HANDS_MODEL", raising=False)
    monkeypatch.delenv("RELAY_SKEPTIC_MODEL", raising=False)
    router = ModelRouter.from_resolve("balanced", config={})
    models = ModelConfig(
        brain=ROUTE_MODELS["balanced"]["brain"],
        hands=ROUTE_MODELS["balanced"]["hands"],
    )
    out, change = router.skeptic_models(models)
    assert out.brain == ROUTE_MODELS["balanced"]["hands"]
    assert out.brain != ROUTE_MODELS["premium"]["brain"]
    assert change is not None
    assert change.purpose == "skeptic"


def test_e6_skeptic_env_pin(monkeypatch):
    monkeypatch.delenv("RELAY_BRAIN_MODEL", raising=False)
    monkeypatch.setenv("RELAY_SKEPTIC_MODEL", "vendor/skeptic-x")
    router = ModelRouter.from_resolve("balanced", config={})
    models = ModelConfig(
        brain=ROUTE_MODELS["balanced"]["brain"],
        hands=ROUTE_MODELS["balanced"]["hands"],
    )
    out, _ = router.skeptic_models(models)
    assert out.brain == "vendor/skeptic-x"


# --- E7 Provider micro --------------------------------------------------------


def test_e7_provider_extras_floor():
    c = parse_route_contract({"route": "economy", "provider_sort": "floor", "max_price": 0.2})
    extras = provider_routing_extras(c, provider="openrouter")
    assert extras["provider"]["sort"] == "price"
    assert extras["provider"]["max_price"] == 0.2
    assert apply_provider_suffix("anthropic/claude-3.5-haiku", c) == "anthropic/claude-3.5-haiku:floor"
    assert provider_routing_extras(c, provider="deepseek") == {}
    assert apply_provider_suffix("x", c, provider="deepseek") == "x"


# --- E8 Phase-aware -----------------------------------------------------------


def test_e8_phase_overrides_models():
    c = parse_route_contract(
        {
            "route": "balanced",
            "phases": {
                "planning": {"brain": "plan/brain"},
                "execution": {"hands": "exec/hands"},
            },
        }
    )
    assert model_for_call_class(c, "plan", phase="planning") == "plan/brain"
    assert model_for_call_class(c, "hands_step", phase="execution") == "exec/hands"
    router = ModelRouter(
        route=c.as_profile(),
        contract=c,
    )
    change = router.set_phase("execution")
    assert change is not None
    assert change.reason == "phase"
    assert router.phase == "execution"


# --- E9 Fitness-gated hands ---------------------------------------------------


def test_e9_fitness_bump_and_decay(monkeypatch):
    monkeypatch.delenv("RELAY_HANDS_MODEL", raising=False)
    c = parse_route_contract(
        {
            "route": "economy",
            "hands_bump_on_parse_failures": 2,
            "hands_bump_steps": 1,
        }
    )
    router = ModelRouter(route=c.as_profile(), contract=c)
    models = ModelConfig(brain=c.brain, hands=c.hands)
    assert router.note_parse_failure(models) is None
    change = router.note_parse_failure(models)
    assert change is not None
    assert change.reason == "fitness_bump"
    assert router.fitness_hands == ROUTE_MODELS["premium"]["hands"]
    decay = router.note_hands_success()
    assert decay is not None
    assert decay.reason == "fitness_decay"
    assert router.fitness_hands is None


def test_e9_fitness_frozen_by_envelope(monkeypatch):
    monkeypatch.delenv("RELAY_HANDS_MODEL", raising=False)
    c = parse_route_contract({"route": "economy", "hands_bump_on_parse_failures": 1})
    router = ModelRouter(route=c.as_profile(), contract=c)
    router.bumps_frozen = True
    models = ModelConfig(brain=c.brain, hands=c.hands)
    change = router.note_parse_failure(models)
    assert change is not None
    assert change.reason == "bump_frozen"


# --- E10 Orchestra × router ---------------------------------------------------


def test_e10_orchestra_hands_use_hands_step_class(monkeypatch):
    monkeypatch.delenv("RELAY_HANDS_MODEL", raising=False)
    router = ModelRouter.from_resolve("balanced", config={})
    models = ModelConfig(
        brain=ROUTE_MODELS["balanced"]["brain"],
        hands=ROUTE_MODELS["premium"]["hands"],  # start hot
    )
    out, change = router.models_for_purpose(models, "hands_step", role="hands-2")
    assert out.hands == ROUTE_MODELS["balanced"]["hands"]
    assert change is not None
    assert change.purpose == "hands_step"


# --- E11 Repo-learned ---------------------------------------------------------


def test_e11_recommend_from_duel_fixture(tmp_path):
    duels = tmp_path / ".relay" / "duels"
    duels.mkdir(parents=True)
    (duels / "d1.json").write_text(
        json.dumps(
            {
                "pairings": [
                    {
                        "brain": ROUTE_MODELS["premium"]["brain"],
                        "hands": ROUTE_MODELS["premium"]["hands"],
                        "status": "completed",
                        "cost_usd": 2.0,
                    },
                    {
                        "brain": ROUTE_MODELS["economy"]["brain"],
                        "hands": ROUTE_MODELS["economy"]["hands"],
                        "status": "completed",
                        "cost_usd": 0.2,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    rec = recommend_route(tmp_path)
    assert rec["route"] == "economy"
    assert any("duel" in e for e in rec["evidence"])


# --- E12 Shadow ---------------------------------------------------------------


def test_e12_shadow_log_only(tmp_path, monkeypatch):
    monkeypatch.delenv("RELAY_BRAIN_MODEL", raising=False)
    router = ModelRouter.from_resolve(
        "balanced", root=tmp_path, config={}, shadow=True
    )
    models = ModelConfig(
        brain=ROUTE_MODELS["balanced"]["brain"],
        hands=ROUTE_MODELS["balanced"]["hands"],
    )
    router.models_for_purpose(models, "plan")
    path = tmp_path / ".relay" / "shadow.jsonl"
    assert path.exists()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert rows
    assert rows[0]["purpose"] == "plan"
    assert rows[0]["dual_call"] is False
    assert "shadow_model" in rows[0]


def test_e12_log_shadow_helper(tmp_path):
    log_shadow_decision(
        tmp_path,
        purpose="review",
        actual_model="a",
        shadow_model="b",
        route="balanced",
    )
    text = (tmp_path / ".relay" / "shadow.jsonl").read_text(encoding="utf-8")
    assert "review" in text
