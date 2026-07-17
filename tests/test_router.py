"""Tests for the model router (B4)."""

from __future__ import annotations

from types import SimpleNamespace

from relay.config import ModelConfig
from relay.envelope import CostEnvelope
from relay.router import (
    BUMP_FREEZE_FRACTION,
    DEFAULT_ROUTE,
    ROUTE_MODELS,
    ModelRouter,
    apply_route,
    get_route,
    next_brain_tier,
    resolve_route,
)


def test_builtin_routes():
    for name in ("economy", "balanced", "premium"):
        r = get_route(name)
        assert r is not None
        assert r.brain and r.hands


def test_resolve_override_beats_env(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_ROUTE", "premium")
    assert resolve_route("economy", root=tmp_path, config={}).name == "economy"


def test_resolve_default(monkeypatch, tmp_path):
    monkeypatch.delenv("RELAY_ROUTE", raising=False)
    assert resolve_route(None, root=tmp_path, config={}).name == DEFAULT_ROUTE


def test_explicit_env_beats_router(monkeypatch):
    monkeypatch.setenv("RELAY_BRAIN_MODEL", "custom/brain-x")
    monkeypatch.delenv("RELAY_HANDS_MODEL", raising=False)
    from relay.config import DEFAULT_BRAIN_MODEL, DEFAULT_HANDS_MODEL

    base = ModelConfig(brain=DEFAULT_BRAIN_MODEL, hands=DEFAULT_HANDS_MODEL)
    route = get_route("economy")
    cfg, changes = apply_route(base, route, config={})
    assert cfg.brain == "custom/brain-x"
    assert cfg.hands == route.hands
    assert all(c.role != "brain" or c.reason != "default" for c in changes)


def test_router_sets_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("RELAY_BRAIN_MODEL", raising=False)
    monkeypatch.delenv("RELAY_HANDS_MODEL", raising=False)
    from relay.config import DEFAULT_BRAIN_MODEL, DEFAULT_HANDS_MODEL

    base = ModelConfig(brain=DEFAULT_BRAIN_MODEL, hands=DEFAULT_HANDS_MODEL)
    route = get_route("economy")
    cfg, changes = apply_route(base, route, config={})
    assert cfg.brain == route.brain
    assert cfg.hands == route.hands
    assert any(c.reason == "default" for c in changes)


def test_router_preserves_non_default_base(monkeypatch):
    monkeypatch.delenv("RELAY_BRAIN_MODEL", raising=False)
    monkeypatch.delenv("RELAY_HANDS_MODEL", raising=False)
    base = ModelConfig(brain="vendor/brain", hands="vendor/hands")
    route = get_route("economy")
    cfg, changes = apply_route(base, route, config={})
    assert cfg.brain == "vendor/brain"
    assert cfg.hands == "vendor/hands"
    assert changes == []


def test_replan_bump_one_tier(monkeypatch):
    monkeypatch.delenv("RELAY_BRAIN_MODEL", raising=False)
    monkeypatch.delenv("RELAY_HANDS_MODEL", raising=False)
    router = ModelRouter(route=get_route("economy"), brain_explicit=False)
    models = ModelConfig(brain=ROUTE_MODELS["economy"]["brain"], hands="h")
    bumped, change = router.models_for_replan(models)
    assert change is not None
    assert change.reason == "replan_bump"
    assert bumped.brain == ROUTE_MODELS["balanced"]["brain"]
    assert models.brain == ROUTE_MODELS["economy"]["brain"]  # call-scoped


def test_bump_frozen_at_80pct(monkeypatch):
    monkeypatch.delenv("RELAY_BRAIN_MODEL", raising=False)
    router = ModelRouter(route=get_route("economy"), brain_explicit=False)
    models = ModelConfig(brain=ROUTE_MODELS["economy"]["brain"], hands="h")
    envelope = CostEnvelope(max_cost=10.0)
    ledger = SimpleNamespace(total_cost=lambda: 8.0)  # 80%
    assert float(ledger.total_cost()) >= 10.0 * BUMP_FREEZE_FRACTION
    out, change = router.models_for_replan(models, envelope=envelope, ledger=ledger)
    assert out.brain == models.brain
    assert change is not None
    assert change.reason == "bump_frozen"


def test_explicit_brain_never_bumps(monkeypatch):
    monkeypatch.setenv("RELAY_BRAIN_MODEL", "custom/pinned")
    router = ModelRouter.from_resolve("economy", config={})
    assert router.brain_explicit
    models = ModelConfig(brain="custom/pinned", hands="h")
    out, change = router.models_for_replan(models)
    assert out.brain == "custom/pinned"
    assert change is not None
    assert change.reason == "explicit_override"


def test_next_brain_tier():
    eco = ROUTE_MODELS["economy"]["brain"]
    bal = ROUTE_MODELS["balanced"]["brain"]
    prem = ROUTE_MODELS["premium"]["brain"]
    assert next_brain_tier(eco) == bal
    assert next_brain_tier(bal) == prem
    assert next_brain_tier(prem) is None
