"""Live: context-window handling holds up against a real model.

Two angles: (1) Relay resolves a real model's window to a sane number live, and
(2) forcing a tiny window doesn't blow up — memory budgeting keeps prompts inside
it and the run reaches a terminal state rather than raising a provider
'context length exceeded' error or melting into a parse-failure storm.
"""

from __future__ import annotations

import pytest

from relay.orchestrator import run_planned

pytestmark = pytest.mark.live


def test_context_window_resolves_for_the_live_model(live_models):
    # Resolve the window via the REAL catalog (the source `relay doctor` prefers).
    # The catalog rung only fires when a catalog is passed; without it this would
    # vacuously fall through to the hardcoded default for ANY string, proving
    # nothing. Asserting source != "default" makes this fail if DeepSeek's window
    # can no longer be resolved live — the regression worth catching.
    from relay.catalog import get_catalog
    from relay.context import resolve_context_window

    window, source = resolve_context_window(
        live_models.brain,
        provider=live_models.brain_provider,
        catalog=get_catalog(),
    )
    assert source in {"catalog", "openrouter"}, (
        f"window resolved only to {source!r} (fell through to the default) — "
        "the live model's context window could not actually be resolved"
    )
    assert isinstance(window, int) and window >= 8000, f"implausible window: {window!r}"


def test_tiny_context_window_does_not_crash(tmp_path, live_models, live_budget):
    # Force a very small brain window. Memory budgeting must keep prompts within
    # it: the run finishes or stops cleanly, never raises a context-overflow error.
    res = run_planned(
        "Create notes.txt containing three short lines describing the Relay project.",
        project_root=tmp_path,
        models=live_models,
        assumption_level="1",
        context_window=2000,  # deliberately tiny
        supervise=True,
        max_total_steps=8,
        max_escalations=1,
    )
    cost = res.ledger.total_cost()
    live_budget.charge(cost)

    # The contract here is "reached a terminal state without a provider/context
    # blow-up", and didn't degrade into a parse-failure storm.
    assert res.status, "no terminal status recorded"
    assert res.ledger.parse_failures < 5, (
        f"parse-failure storm under a tiny window: {res.ledger.parse_failures}"
    )
    assert (cost or 0.0) < 0.10, f"runaway cost under a tiny window: ${cost}"
