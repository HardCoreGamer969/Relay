"""Network-free tests for provider-aware cost (the DeepSeek cache hit/miss split).

The catalog is loaded from a local fixture via ``RELAY_MODELS_URL`` (autouse
conftest isolation keeps it hermetic). A fake client returns a DeepSeek-shaped
usage block whose cache split lives in ``model_extra`` -- the real openai-SDK shape.

Fixture prices (per 1M tokens):
  deepseek-v4-pro    input 0.56  output 1.68  cache_read 0.07
  deepseek-v4-flash  input 0.28  output 0.42  (no cache_read)
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from relay.config import ModelConfig
from relay.models import call_model
from relay.telemetry import Ledger


class DeepSeekUsage:
    """Mimics DeepSeek's usage via the openai SDK: prompt/completion tokens are
    first-class, but the cache split is in pydantic's ``model_extra`` (extras)."""

    def __init__(self, hit, miss, completion):
        self.prompt_tokens = hit + miss
        self.completion_tokens = completion
        self.total_tokens = hit + miss + completion
        self.model_extra = {
            "prompt_cache_hit_tokens": hit,
            "prompt_cache_miss_tokens": miss,
        }


class _Client:
    def __init__(self, usage):
        message = SimpleNamespace(content="ok")
        response = SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)

        class _Completions:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return response

        self.chat = SimpleNamespace(completions=_Completions())


def _deepseek_cfg(model="deepseek-v4-pro"):
    return ModelConfig(brain="x", hands=model, hands_provider="deepseek")



def _use_fixture(monkeypatch, catalog_fixture: str) -> None:
    monkeypatch.delenv("RELAY_DISABLE_MODELS_FETCH", raising=False)
    monkeypatch.setenv("RELAY_MODELS_URL", catalog_fixture)


def test_deepseek_cost_uses_cache_hit_miss_split(catalog_fixture, monkeypatch):
    _use_fixture(monkeypatch, catalog_fixture)
    hit, miss, completion = 8000, 2000, 500
    client = _Client(DeepSeekUsage(hit, miss, completion))
    ledger = Ledger()

    result = call_model(
        "hands", [{"role": "user", "content": "hi"}],
        models=_deepseek_cfg(), ledger=ledger, client=client,
    )

    # Expected per the documented formula with fixture prices (per-token = per-1M/1e6).
    input_rate, output_rate, cache_rate = 0.56 / 1e6, 1.68 / 1e6, 0.07 / 1e6
    expected = hit * cache_rate + miss * input_rate + completion * output_rate
    assert result.record.cost_usd == pytest.approx(expected)
    assert ledger.total_cost() == pytest.approx(expected)
    assert result.record.cost_usd > 0  # never silently zero

    # The split must actually be used: a naive single-rate calc (all input at the
    # input rate) would be materially higher -- caching is the whole point.
    naive = (hit + miss) * input_rate + completion * output_rate
    assert naive > result.record.cost_usd
    assert result.record.cost_usd != pytest.approx(naive)


def test_deepseek_cost_falls_back_when_cache_read_absent(catalog_fixture, monkeypatch):
    # deepseek-v4-flash has no cache_read in the fixture -> hits priced at input rate.
    _use_fixture(monkeypatch, catalog_fixture)
    hit, miss, completion = 1000, 1000, 100
    client = _Client(DeepSeekUsage(hit, miss, completion))

    result = call_model(
        "hands", [{"role": "user", "content": "hi"}],
        models=_deepseek_cfg("deepseek-v4-flash"), client=client,
    )

    input_rate, output_rate = 0.28 / 1e6, 0.42 / 1e6
    expected = hit * input_rate + miss * input_rate + completion * output_rate
    assert result.record.cost_usd == pytest.approx(expected)
    assert result.record.cost_usd > 0  # cache_read missing must NOT zero cost


def test_deepseek_cost_no_split_prices_prompt_at_input_rate(catalog_fixture, monkeypatch):
    # A usage block with no cache split at all -> all prompt tokens at input rate.
    _use_fixture(monkeypatch, catalog_fixture)
    usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=200, total_tokens=1200)
    client = _Client(usage)

    result = call_model(
        "hands", [{"role": "user", "content": "hi"}],
        models=_deepseek_cfg(), client=client,
    )

    input_rate, output_rate = 0.56 / 1e6, 1.68 / 1e6
    expected = 1000 * input_rate + 200 * output_rate
    assert result.record.cost_usd == pytest.approx(expected)


def test_deepseek_cost_unknown_model_is_none_not_zero(catalog_fixture, monkeypatch):
    _use_fixture(monkeypatch, catalog_fixture)
    client = _Client(DeepSeekUsage(100, 100, 10))
    result = call_model(
        "hands", [{"role": "user", "content": "hi"}],
        models=_deepseek_cfg("deepseek-not-in-catalog"), client=client,
    )
    # No catalog price -> unknown (None), shown as "-". Distinct from a wrong 0.
    assert result.record.cost_usd is None


def test_openrouter_cost_path_unchanged(catalog_fixture, monkeypatch):
    """The OpenRouter cost path must not be touched by the DeepSeek work: it still
    reads the returned ``cost`` and never consults the catalog."""
    _use_fixture(monkeypatch, catalog_fixture)
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15, cost=0.000123)
    client = _Client(usage)
    cfg = ModelConfig(brain="vendor/brain", hands="vendor/hands")  # both openrouter
    result = call_model("brain", [{"role": "user", "content": "hi"}], models=cfg, client=client)
    assert result.record.cost_usd == pytest.approx(0.000123)
