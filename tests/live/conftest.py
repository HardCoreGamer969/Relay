"""Fixtures for the LIVE test tier — the opposite of the network-free suite.

These tests call a *real* model through a *real* provider, so they:

- are marked ``live`` (see each module's ``pytestmark``) and are deselected by
  default via ``addopts = -m 'not live'`` in ``pyproject.toml``; CI's live-smoke
  job re-selects them with ``-m live``;
- **skip cleanly** when no provider key is present (so forks / local runs without
  a key are green, never red);
- default to DeepSeek's cheapest model (``deepseek-v4-flash`` — confirmed current;
  the legacy ``deepseek-chat`` alias is deprecated 2026-07-24), overridable via
  ``RELAY_LIVE_MODEL``;
- share a session-wide **hard cost ceiling** (``RELAY_LIVE_BUDGET_USD``, default
  $0.50) so a misbehaving run can never quietly burn money.

The root ``tests/conftest.py`` autouse fixture still isolates the on-disk config
dir, but it does NOT clear ``DEEPSEEK_API_KEY`` / ``OPENROUTER_API_KEY``, so the
live key resolves normally and pricing is fetched from the real catalog.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session")
def live_models():
    """A :class:`~relay.config.ModelConfig` bound to a cheap live model.

    Prefers DeepSeek (cheapest, with a known default slug). Falls back to
    OpenRouter only when an explicit ``RELAY_LIVE_MODEL`` is given (OpenRouter
    slugs are not guessed). Skips the whole live tier when no usable key exists.
    """
    from relay.config import ModelConfig

    model = os.environ.get("RELAY_LIVE_MODEL")
    if os.environ.get("DEEPSEEK_API_KEY"):
        provider = "deepseek"
        model = model or "deepseek-v4-flash"
    elif os.environ.get("OPENROUTER_API_KEY"):
        provider = "openrouter"
        if not model:
            pytest.skip(
                "OPENROUTER_API_KEY is set but RELAY_LIVE_MODEL is not; "
                "set RELAY_LIVE_MODEL to a cheap OpenRouter slug to run live tests."
            )
    else:
        pytest.skip(
            "live tests need a provider key: set DEEPSEEK_API_KEY (preferred) "
            "or OPENROUTER_API_KEY + RELAY_LIVE_MODEL."
        )

    # Same cheap model for both roles by default: the point of these tests is the
    # brain<->hands relay and the safety rails, not a premium-model bake-off.
    return ModelConfig(
        brain=model, hands=model, brain_provider=provider, hands_provider=provider
    )


class _Budget:
    """A cumulative spend guard shared across the whole live session."""

    def __init__(self, cap_usd: float) -> None:
        self.cap_usd = cap_usd
        self.spent_usd = 0.0

    def charge(self, cost_usd: float | None) -> None:
        self.spent_usd += cost_usd or 0.0
        assert self.spent_usd < self.cap_usd, (
            f"live test budget exceeded: spent ${self.spent_usd:.4f} "
            f"of the ${self.cap_usd:.2f} cap (raise RELAY_LIVE_BUDGET_USD if intended)."
        )


@pytest.fixture(scope="session")
def live_budget() -> _Budget:
    cap = float(os.environ.get("RELAY_LIVE_BUDGET_USD", "0.50"))
    return _Budget(cap)
