"""Shared test fixtures.

The model catalog (``relay/catalog.py``) reads env (``RELAY_MODELS_URL`` /
``RELAY_DISABLE_MODELS_FETCH`` / ``RELAY_CACHE_DIR``), an on-disk cache, and an
in-process memo. To keep the whole suite **network-free and order-independent**,
this autouse fixture isolates all three for every test:

- the disk cache is pointed at a fresh per-test tmp dir (never the user's real
  cache, never polluted by a prior test);
- the catalog env overrides start cleared, so a test sees only what it sets;
- the in-process memo is reset around every test.

Tests that exercise the catalog opt in explicitly (set ``RELAY_MODELS_URL`` to the
fixture, or monkeypatch the fetch); tests that don't touch the catalog are
unaffected. The path to the catalog fixture is exposed as ``catalog_fixture``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolate_catalog(monkeypatch, tmp_path):
    from relay import catalog

    catalog.reset_catalog_cache()
    monkeypatch.setenv("RELAY_CACHE_DIR", str(tmp_path / "relay-cache"))
    monkeypatch.delenv("RELAY_MODELS_URL", raising=False)
    monkeypatch.delenv("RELAY_DISABLE_MODELS_FETCH", raising=False)
    yield
    catalog.reset_catalog_cache()


@pytest.fixture
def catalog_fixture() -> str:
    """Absolute path to the local ``api.json`` catalog fixture."""
    return str(FIXTURES / "catalog_api.json")
