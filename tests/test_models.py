"""Network-free tests for the model seam.

The OpenRouter client's ``chat.completions.create`` is mocked — no real calls
are ever made. A fake response exposes ``.choices[0].message.content`` and a
``.usage`` block (tokens plus OpenRouter's returned cost).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from relay.config import ModelConfig
from relay.models import call_model
from relay.telemetry import Ledger


def make_response(content, prompt_tokens, completion_tokens, cost):
    """Build a fake OpenAI/OpenRouter-shaped response object."""
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cost=cost,
    )
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice], usage=usage)


class FakeCompletions:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeClient:
    """Stand-in for ``openai.OpenAI`` — never hits the network."""

    def __init__(self, response):
        self.chat = SimpleNamespace(completions=FakeCompletions(response))


def test_role_resolves_to_right_model_slug():
    cfg = ModelConfig(brain="vendor/brain-model", hands="vendor/hands-model")
    assert cfg.for_role("brain") == "vendor/brain-model"
    assert cfg.for_role("hands") == "vendor/hands-model"
    with pytest.raises(ValueError):
        cfg.for_role("unknown-role")


def test_single_call_records_one_entry_with_tokens_and_cost():
    cfg = ModelConfig(brain="vendor/brain-model", hands="vendor/hands-model")
    ledger = Ledger()
    client = FakeClient(make_response("one concrete step", 10, 5, 0.000123))

    result = call_model(
        "brain",
        [{"role": "user", "content": "hi"}],
        models=cfg,
        ledger=ledger,
        client=client,
    )

    # Returned text and a single recorded telemetry entry.
    assert result.text == "one concrete step"
    assert len(ledger.records) == 1

    rec = ledger.records[0]
    assert rec.role == "brain"
    assert rec.model == "vendor/brain-model"
    assert rec.prompt_tokens == 10
    assert rec.completion_tokens == 5
    assert rec.total_tokens == 15
    assert rec.cost_usd == 0.000123

    # The seam must request OpenRouter's per-generation cost.
    sent = client.chat.completions.calls[0]
    assert sent["model"] == "vendor/brain-model"
    assert sent["extra_body"]["usage"]["include"] is True


def test_brain_then_hands_yields_by_role_with_two_distinct_models():
    cfg = ModelConfig(brain="vendor/brain-model", hands="vendor/hands-model")
    ledger = Ledger()
    client = FakeClient(make_response("text", 3, 4, 0.0002))

    call_model("brain", [{"role": "user", "content": "a"}], models=cfg, ledger=ledger, client=client)
    call_model("hands", [{"role": "user", "content": "b"}], models=cfg, ledger=ledger, client=client)

    summary = ledger.by_role()
    assert set(summary) == {"brain", "hands"}
    assert summary["brain"].model == "vendor/brain-model"
    assert summary["hands"].model == "vendor/hands-model"

    models_used = {s.model for s in summary.values()}
    assert len(models_used) == 2, "brain and hands must be two distinct models"

    # Aggregated totals.
    assert ledger.total_cost() == pytest.approx(0.0004)
    assert summary["brain"].total_tokens == 7


def test_missing_cost_falls_back_to_none():
    cfg = ModelConfig(brain="vendor/brain-model", hands="vendor/hands-model")
    ledger = Ledger()
    client = FakeClient(make_response("step", 2, 1, None))

    call_model("brain", [{"role": "user", "content": "x"}], models=cfg, ledger=ledger, client=client)

    assert ledger.records[0].cost_usd is None
    assert ledger.total_cost() is None
