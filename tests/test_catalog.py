"""Network-free tests for the model catalog (relay/catalog.py).

Every test points ``RELAY_MODELS_URL`` at a local fixture file or monkeypatches
``_fetch_raw`` -- no socket is ever opened. The autouse ``_isolate_catalog``
fixture (conftest) isolates the disk cache to a tmp dir and resets the memo.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import relay.catalog as catalog
from relay.catalog import (
    Catalog,
    Cost,
    load_catalog,
    parse_catalog,
)


# --- parsing ----------------------------------------------------------------


def test_parse_and_lookup_cost_capabilities_limit(catalog_fixture, monkeypatch):
    monkeypatch.setenv("RELAY_MODELS_URL", catalog_fixture)
    cat = load_catalog()

    # cost lookup (per-1M prices straight off the fixture)
    cost = cat.cost("deepseek", "deepseek-v4-pro")
    assert cost is not None
    assert cost.input == pytest.approx(0.56)
    assert cost.output == pytest.approx(1.68)
    assert cost.cache_read == pytest.approx(0.07)

    # capability + limit lookup
    assert cat.supports_reasoning("deepseek", "deepseek-v4-pro") is True
    assert cat.context_limit("deepseek", "deepseek-v4-pro") == 131072

    model = cat.model("openrouter", "anthropic/claude-sonnet-4.5")
    assert model is not None and model.tool_call is True
    assert cat.context_limit("openrouter", "anthropic/claude-sonnet-4.5") == 200000

    # list-models seam for the (next-milestone) picker
    ids = {m.id for m in cat.models_for("deepseek")}
    assert ids == {"deepseek-v4-pro", "deepseek-v4-flash"}


def test_lookup_misses_return_none_not_zero(catalog_fixture, monkeypatch):
    monkeypatch.setenv("RELAY_MODELS_URL", catalog_fixture)
    cat = load_catalog()
    assert cat.cost("deepseek", "no-such-model") is None
    assert cat.model("nope", "nope") is None
    assert cat.context_limit("deepseek", "no-such-model") is None
    assert cat.supports_reasoning("deepseek", "no-such-model") is False


def test_defensive_parsing_tolerates_unknown_and_missing_fields():
    raw = {
        "good": {
            "id": "good",
            "name": "Good",
            "env": ["GOOD_KEY"],
            "api": "https://example.com",
            "future_top_level_field": 123,  # unknown -> ignored
            "models": {
                "m1": {
                    "id": "m1",
                    # no name/reasoning/tool_call/temperature/limit -> tolerated
                    "cost": {"input": 1.0},  # output/cache_* missing -> None
                    "brand_new_field": True,  # unknown -> ignored
                },
                "m2": "this is not an object",  # one bad model -> skipped, rest survive
            },
        },
        "bad_provider": "not an object",  # skipped, doesn't poison the catalog
    }
    providers = parse_catalog(raw)
    assert set(providers) == {"good"}  # bad provider dropped, good kept
    good = providers["good"]
    assert set(good.models) == {"m1"}  # bad model dropped, good kept
    m1 = good.models["m1"]
    assert m1.cost.input == pytest.approx(1.0)
    assert m1.cost.output is None and m1.cost.cache_read is None
    assert m1.reasoning is False  # missing optional defaulted, no crash


def test_cache_read_can_be_absent(catalog_fixture, monkeypatch):
    monkeypatch.setenv("RELAY_MODELS_URL", catalog_fixture)
    cat = load_catalog()
    flash = cat.cost("deepseek", "deepseek-v4-flash")
    assert flash is not None
    assert flash.input == pytest.approx(0.28)
    assert flash.cache_read is None  # fixture omits it -- caller must tolerate


# --- the fallback chain -----------------------------------------------------


def _write_disk_cache(endpoint: str, data: dict, *, age_s: float) -> None:
    """Write a cache envelope for ``endpoint`` with a chosen age."""
    path = catalog._cache_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {"source": endpoint, "fetched_at": time.time() - age_s, "data": data}
    path.write_text(json.dumps(envelope), encoding="utf-8")


def test_fresh_cache_is_used_without_fetching(monkeypatch):
    endpoint = catalog._endpoint(catalog.DEFAULT_SOURCE)
    _write_disk_cache(endpoint, {"deepseek": {"id": "deepseek", "models": {}}}, age_s=1)

    def _boom(_endpoint):
        raise AssertionError("must not fetch when a fresh cache exists")

    monkeypatch.setattr(catalog, "_fetch_raw", _boom)
    cat = load_catalog()
    assert cat.status == "cache"
    assert "deepseek" in cat.providers


def test_network_fetch_populates_and_caches(catalog_fixture, monkeypatch):
    monkeypatch.setenv("RELAY_MODELS_URL", catalog_fixture)
    cat = load_catalog()
    assert cat.status == "fetch"  # no cache yet -> reads the fixture ("network")
    assert cat.cost("deepseek", "deepseek-v4-pro") is not None
    # ...and it wrote the disk cache, so a second load is served fresh from cache.
    again = load_catalog()
    assert again.status == "cache"


def test_network_failure_falls_back_to_stale_cache(monkeypatch):
    endpoint = catalog._endpoint(catalog.DEFAULT_SOURCE)
    _write_disk_cache(
        endpoint,
        {"deepseek": {"id": "deepseek", "models": {}}},
        age_s=catalog.CACHE_TTL_S * 10,  # deliberately stale
    )

    def _down(_endpoint):
        raise OSError("network down")

    monkeypatch.setattr(catalog, "_fetch_raw", _down)
    cat = load_catalog()
    assert cat.status == "stale"  # better stale than nothing
    assert "deepseek" in cat.providers


def test_nothing_available_uses_bundled_fallback_with_nonzero_pricing(monkeypatch):
    # No cache (isolated tmp dir is empty) and fetch disabled -> bundled.
    monkeypatch.setenv("RELAY_DISABLE_MODELS_FETCH", "1")
    cat = load_catalog()
    assert cat.status == "bundled"

    # The whole point of the bundled rung: DeepSeek + OpenRouter cost is never zero.
    ds = cat.cost("deepseek", "deepseek-v4-pro")
    assert ds is not None and ds.input and ds.input > 0 and ds.output and ds.output > 0
    assert ds.cache_read and ds.cache_read > 0
    orr = cat.cost("openrouter", "anthropic/claude-sonnet-4.5")
    assert orr is not None and orr.input and orr.input > 0


def test_disable_fetch_still_prefers_fresh_cache(monkeypatch):
    endpoint = catalog._endpoint(catalog.DEFAULT_SOURCE)
    _write_disk_cache(endpoint, {"deepseek": {"id": "deepseek", "models": {}}}, age_s=1)
    monkeypatch.setenv("RELAY_DISABLE_MODELS_FETCH", "1")
    cat = load_catalog()
    assert cat.status == "cache"


def test_cache_from_a_different_source_is_ignored(catalog_fixture, monkeypatch):
    # A fresh cache stamped with a DIFFERENT source must not be served.
    _write_disk_cache("https://somewhere-else.example/api.json",
                      {"x": {"id": "x", "models": {}}}, age_s=1)
    monkeypatch.setenv("RELAY_MODELS_URL", catalog_fixture)
    cat = load_catalog()
    assert cat.status == "fetch"  # the mismatched cache was ignored; fixture read
    assert "deepseek" in cat.providers


def test_load_never_raises_on_garbage_source(monkeypatch, tmp_path):
    bad = tmp_path / "not.json"
    bad.write_text("{ this is not valid json", encoding="utf-8")
    monkeypatch.setenv("RELAY_MODELS_URL", str(bad))
    cat = load_catalog()
    # Parse failed -> fell through to bundled rather than crashing.
    assert cat.status == "bundled"
    assert cat.cost("deepseek", "deepseek-v4-pro") is not None


# --- http fetch headers -----------------------------------------------------


def test_http_fetch_sets_a_non_default_user_agent(monkeypatch):
    # models.dev (behind a CDN) 403s the default Python-urllib agent, so the HTTP
    # fetch must identify itself -- otherwise every real run silently falls back.
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"x": {"id": "x", "models": {}}}'

    def fake_urlopen(request, timeout=None):
        captured["ua"] = request.get_header("User-agent")
        return _Resp()

    monkeypatch.setattr(catalog.urllib.request, "urlopen", fake_urlopen)
    catalog._fetch_raw("https://models.dev/api.json")
    ua = (captured["ua"] or "").lower()
    assert "relay" in ua
    assert "python-urllib" not in ua


# --- endpoint resolution ----------------------------------------------------


def test_endpoint_appends_api_json_to_base():
    assert catalog._endpoint("https://models.dev") == "https://models.dev/api.json"
    assert catalog._endpoint("https://models.dev/") == "https://models.dev/api.json"


def test_endpoint_uses_direct_json_path_as_is():
    assert catalog._endpoint("/tmp/fixtures/api.json") == "/tmp/fixtures/api.json"


# --- in-process memo --------------------------------------------------------


def test_get_catalog_memoizes(catalog_fixture, monkeypatch):
    monkeypatch.setenv("RELAY_MODELS_URL", catalog_fixture)
    calls = {"n": 0}
    real = catalog._fetch_raw

    def _counting(endpoint):
        calls["n"] += 1
        return real(endpoint)

    monkeypatch.setattr(catalog, "_fetch_raw", _counting)
    first = catalog.get_catalog()
    second = catalog.get_catalog()
    assert first is second  # served from the memo the second time
    assert calls["n"] == 1
