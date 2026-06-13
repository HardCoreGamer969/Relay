"""Network-free tests for brain context-window resolution (relay/context.py).

A fake client supplies OpenRouter metadata; local probes are monkeypatched so no
socket is ever opened.
"""

from __future__ import annotations

from types import SimpleNamespace

import relay.context as ctx
from relay.context import DEFAULT_CONTEXT_WINDOW, resolve_context_window


class FakeModelsClient:
    """Stand-in whose .models.list() yields OpenRouter-shaped model entries."""

    def __init__(self, models):
        self._models = models
        self.chat = SimpleNamespace()  # unused here

    @property
    def models(self):
        return SimpleNamespace(list=lambda: list(self._models))


def _model(model_id, context_length=None, top_provider_len=None):
    top = SimpleNamespace(context_length=top_provider_len) if top_provider_len else None
    return SimpleNamespace(id=model_id, context_length=context_length, top_provider=top)


def test_override_param_beats_env(monkeypatch):
    monkeypatch.setenv("RELAY_BRAIN_CONTEXT", "5000")
    window, source = resolve_context_window("anthropic/whatever", override=64000)
    assert (window, source) == (64000, "override")


def test_env_override_used_when_no_param(monkeypatch):
    monkeypatch.setenv("RELAY_BRAIN_CONTEXT", "32000")
    window, source = resolve_context_window("anthropic/whatever")
    assert (window, source) == (32000, "override")


def test_bad_env_override_falls_through(monkeypatch):
    monkeypatch.setenv("RELAY_BRAIN_CONTEXT", "not-a-number")
    client = FakeModelsClient([_model("anthropic/x", context_length=111000)])
    window, source = resolve_context_window("anthropic/x", client=client)
    assert (window, source) == (111000, "openrouter")  # bad env ignored, metadata wins


def test_openrouter_metadata_read(monkeypatch):
    monkeypatch.delenv("RELAY_BRAIN_CONTEXT", raising=False)
    client = FakeModelsClient([_model("anthropic/claude-sonnet-4.5", context_length=200000)])
    window, source = resolve_context_window("anthropic/claude-sonnet-4.5", client=client)
    assert (window, source) == (200000, "openrouter")


def test_openrouter_metadata_via_top_provider(monkeypatch):
    monkeypatch.delenv("RELAY_BRAIN_CONTEXT", raising=False)
    client = FakeModelsClient([_model("x/y", context_length=None, top_provider_len=128000)])
    window, source = resolve_context_window("x/y", client=client)
    assert (window, source) == (128000, "openrouter")


def test_openrouter_metadata_caches_on_client(monkeypatch):
    monkeypatch.delenv("RELAY_BRAIN_CONTEXT", raising=False)
    calls = {"n": 0}

    class CountingClient:
        def __init__(self):
            self.chat = SimpleNamespace()

        @property
        def models(self):
            def _list():
                calls["n"] += 1
                return [_model("a/b", context_length=64000)]

            return SimpleNamespace(list=_list)

    client = CountingClient()
    resolve_context_window("a/b", client=client)
    resolve_context_window("a/b", client=client)
    assert calls["n"] == 1  # listed once, cached on the client


def test_unknown_model_falls_to_default(monkeypatch):
    monkeypatch.delenv("RELAY_BRAIN_CONTEXT", raising=False)
    client = FakeModelsClient([])  # model not present
    window, source = resolve_context_window("anthropic/unknown", client=client)
    assert (window, source) == (DEFAULT_CONTEXT_WINDOW, "default")


def test_no_client_no_info_yields_default(monkeypatch):
    monkeypatch.delenv("RELAY_BRAIN_CONTEXT", raising=False)
    window, source = resolve_context_window("anthropic/x", client=None)
    assert (window, source) == (DEFAULT_CONTEXT_WINDOW, "default")


class _FakeCatalog:
    """Minimal catalog stand-in exposing only context_limit (what resolve uses)."""

    def __init__(self, limits):
        self._limits = limits  # {(provider, model): int}

    def context_limit(self, provider, model):
        return self._limits.get((provider, model))


def test_catalog_limit_beats_probe(monkeypatch):
    monkeypatch.delenv("RELAY_BRAIN_CONTEXT", raising=False)
    # The probe would say 64000, but the catalog (better source) says 131072.
    client = FakeModelsClient([_model("deepseek-v4-pro", context_length=64000)])
    catalog = _FakeCatalog({("deepseek", "deepseek-v4-pro"): 131072})
    window, source = resolve_context_window(
        "deepseek-v4-pro", provider="deepseek", client=client, catalog=catalog
    )
    assert (window, source) == (131072, "catalog")


def test_catalog_miss_falls_through_to_probe(monkeypatch):
    monkeypatch.delenv("RELAY_BRAIN_CONTEXT", raising=False)
    client = FakeModelsClient([_model("anthropic/x", context_length=200000)])
    catalog = _FakeCatalog({})  # catalog knows nothing about this model
    window, source = resolve_context_window(
        "anthropic/x", provider="openrouter", client=client, catalog=catalog
    )
    assert (window, source) == (200000, "openrouter")  # fell through to the probe


def test_override_still_beats_catalog(monkeypatch):
    monkeypatch.delenv("RELAY_BRAIN_CONTEXT", raising=False)
    catalog = _FakeCatalog({("deepseek", "deepseek-v4-pro"): 131072})
    window, source = resolve_context_window(
        "deepseek-v4-pro", provider="deepseek", override=99999, catalog=catalog
    )
    assert (window, source) == (99999, "override")


def test_no_catalog_keeps_existing_probe_behavior(monkeypatch):
    # Passing neither provider nor catalog must be byte-for-byte the old path.
    monkeypatch.delenv("RELAY_BRAIN_CONTEXT", raising=False)
    client = FakeModelsClient([_model("anthropic/x", context_length=200000)])
    window, source = resolve_context_window("anthropic/x", client=client)
    assert (window, source) == (200000, "openrouter")


def test_local_probe_success(monkeypatch):
    monkeypatch.delenv("RELAY_BRAIN_CONTEXT", raising=False)
    monkeypatch.setattr(ctx, "probe_local_context", lambda model: (8192, "ollama"))
    window, source = resolve_context_window("ollama/llama3")
    assert (window, source) == (8192, "local:ollama")


def test_local_probe_failure_falls_through_to_default(monkeypatch):
    monkeypatch.delenv("RELAY_BRAIN_CONTEXT", raising=False)
    monkeypatch.setattr(ctx, "probe_local_context", lambda model: None)
    window, source = resolve_context_window("ollama/llama3")
    assert (window, source) == (DEFAULT_CONTEXT_WINDOW, "default")


def test_resolution_never_raises_even_if_client_blows_up(monkeypatch):
    monkeypatch.delenv("RELAY_BRAIN_CONTEXT", raising=False)

    class ExplodingClient:
        def __init__(self):
            self.chat = SimpleNamespace()

        @property
        def models(self):
            def _list():
                raise RuntimeError("network down")

            return SimpleNamespace(list=_list)

    window, source = resolve_context_window("anthropic/x", client=ExplodingClient())
    assert (window, source) == (DEFAULT_CONTEXT_WINDOW, "default")  # guarded, no raise
