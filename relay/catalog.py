"""The model catalog: model metadata + pricing, fetched once and served locally.

Relay is model-agnostic, so it must learn a model's *cost*, *capabilities*, and
*context limit* from somewhere external rather than hard-coding them. This module
is that backbone, modeled on how OpenCode does it: a central catalog
(`https://models.dev/api.json` by default) is fetched, validated, cached to disk,
and served through a small lookup API.

**The fetch -> validate -> cache -> serve flow, with a fallback chain that a
network blip can never break (cost must never silently go to zero):**

1. fresh disk cache (within :data:`CACHE_TTL_S`) -> use it;
2. else fetch ``{source}/api.json`` -> validate, cache atomically, serve;
3. else stale disk cache -> use it (better stale than nothing);
4. else a small **bundled fallback** baked into this file (DeepSeek + OpenRouter
   pricing) -> so a model's cost is never silently zero.

The rung that was actually used is exposed as :attr:`Catalog.status`
(``"cache"`` / ``"fetch"`` / ``"stale"`` / ``"bundled"``) so a silent fallback is
visible (e.g. in ``relay doctor``).

**Power-user overrides (env only, no UI):**

- ``RELAY_MODELS_URL`` -- override the catalog source. May be a base URL (``/api.json``
  is appended) or a direct path/URL to an ``api.json`` (used by tests to point at a
  local fixture instead of the network).
- ``RELAY_DISABLE_MODELS_FETCH`` -- skip the network entirely (cache/bundled only).
- ``RELAY_CACHE_DIR`` -- where the on-disk cache lives (defaults to a user cache dir).

Parsing is **defensive**: unknown fields are ignored, missing optionals tolerated,
and one bad entry never poisons the rest -- the upstream catalog evolves and must
not break Relay on a new field.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# The default catalog source and the endpoint appended to it.
DEFAULT_SOURCE = "https://models.dev"
API_PATH = "/api.json"

# A fetched catalog is considered fresh for this window; past it, Relay re-fetches
# (falling back to the same cache if the network is down). Deliberately short.
CACHE_TTL_S = 3600  # 1 hour

# Network read timeout. Short -- a catalog fetch must never hang a run; it falls
# through to cache/bundled instead.
FETCH_TIMEOUT_S = 4.0


# --- schema -----------------------------------------------------------------


@dataclass(frozen=True)
class Cost:
    """Per-1M-token prices (USD) for a model. Any field may be ``None`` (unknown)."""

    input: float | None = None
    output: float | None = None
    cache_read: float | None = None
    cache_write: float | None = None


@dataclass(frozen=True)
class Limit:
    """Context / output token limits for a model."""

    context: int | None = None
    output: int | None = None


@dataclass(frozen=True)
class Model:
    """One model's metadata + pricing, as the picker (next milestone) will read it."""

    id: str
    name: str | None
    reasoning: bool
    tool_call: bool
    temperature: bool
    limit: Limit
    cost: Cost


@dataclass(frozen=True)
class Provider:
    """A provider entry: its id/name, key env-var names, base API URL, and models."""

    id: str
    name: str | None
    env: tuple[str, ...]
    api: str | None
    models: dict[str, Model] = field(default_factory=dict)


@dataclass(frozen=True)
class Catalog:
    """The served catalog plus the provenance of where it came from.

    ``status`` is the fallback rung actually used (``"fetch"`` / ``"cache"`` /
    ``"stale"`` / ``"bundled"``); ``source`` is the resolved endpoint.
    """

    providers: dict[str, Provider]
    status: str
    source: str

    def model(self, provider: str, model_id: str) -> Model | None:
        prov = self.providers.get(provider)
        if prov is None:
            return None
        return prov.models.get(model_id)

    def cost(self, provider: str, model_id: str) -> Cost | None:
        m = self.model(provider, model_id)
        return m.cost if m is not None else None

    def context_limit(self, provider: str, model_id: str) -> int | None:
        m = self.model(provider, model_id)
        if m is not None and m.limit is not None and m.limit.context:
            return m.limit.context
        return None

    def models_for(self, provider: str) -> list[Model]:
        """List a provider's models -- what the (next-milestone) picker consumes."""
        prov = self.providers.get(provider)
        return list(prov.models.values()) if prov is not None else []

    def supports_reasoning(self, provider: str, model_id: str) -> bool:
        """Whether a model advertises reasoning -- queryable for the picker."""
        m = self.model(provider, model_id)
        return bool(m.reasoning) if m is not None else False


# --- defensive parsing ------------------------------------------------------


def _f(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _i(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_cost(raw) -> Cost:
    if not isinstance(raw, dict):
        return Cost()
    # tiers[] and any unknown fields are intentionally ignored (parse-tolerant).
    return Cost(
        input=_f(raw.get("input")),
        output=_f(raw.get("output")),
        cache_read=_f(raw.get("cache_read")),
        cache_write=_f(raw.get("cache_write")),
    )


def _parse_limit(raw) -> Limit:
    if not isinstance(raw, dict):
        return Limit()
    return Limit(context=_i(raw.get("context")), output=_i(raw.get("output")))


def _parse_model(model_id: str, raw) -> Model:
    if not isinstance(raw, dict):
        raise ValueError("model entry is not an object")
    return Model(
        id=str(raw.get("id", model_id)),
        name=raw.get("name"),
        reasoning=bool(raw.get("reasoning", False)),
        tool_call=bool(raw.get("tool_call", False)),
        # ``temperature`` flags whether the param is honored; default True (most are).
        temperature=bool(raw.get("temperature", True)),
        limit=_parse_limit(raw.get("limit")),
        cost=_parse_cost(raw.get("cost")),
    )


def _parse_provider(provider_id: str, raw) -> Provider:
    if not isinstance(raw, dict):
        raise ValueError("provider entry is not an object")
    models: dict[str, Model] = {}
    raw_models = raw.get("models")
    if isinstance(raw_models, dict):
        for model_id, model_raw in raw_models.items():
            try:
                models[model_id] = _parse_model(model_id, model_raw)
            except Exception:  # noqa: BLE001 -- one bad model must not poison the rest
                continue
    raw_env = raw.get("env")
    env = tuple(str(e) for e in raw_env) if isinstance(raw_env, (list, tuple)) else ()
    return Provider(
        id=str(raw.get("id", provider_id)),
        name=raw.get("name"),
        env=env,
        api=raw.get("api"),
        models=models,
    )


def parse_catalog(raw) -> dict[str, Provider]:
    """Parse the ``{providerId: Provider}`` catalog object, tolerating bad entries."""
    providers: dict[str, Provider] = {}
    if not isinstance(raw, dict):
        return providers
    for provider_id, provider_raw in raw.items():
        try:
            providers[provider_id] = _parse_provider(provider_id, provider_raw)
        except Exception:  # noqa: BLE001 -- one bad provider must not poison the rest
            continue
    return providers


# --- the bundled fallback (rung 4) -----------------------------------------
#
# A SMALL, coarse safety net so cost is never silently zero when the network and
# every cache are unavailable. NOT a full catalog snapshot, and NOT authoritative
# pricing -- the live catalog (models.dev) always supersedes these once reachable.
# Prices are per-1M tokens; the relative shape (cache_read < input < output) is
# what matters here, not exact figures.
_BUNDLED_RAW: dict = {
    "openrouter": {
        "id": "openrouter",
        "name": "OpenRouter",
        "env": ["OPENROUTER_API_KEY"],
        "api": "https://openrouter.ai/api/v1",
        "models": {
            "anthropic/claude-sonnet-4.5": {
                "id": "anthropic/claude-sonnet-4.5",
                "name": "Claude Sonnet 4.5",
                "reasoning": True,
                "tool_call": True,
                "temperature": True,
                "limit": {"context": 200000, "output": 64000},
                "cost": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
            },
            "anthropic/claude-3.5-haiku": {
                "id": "anthropic/claude-3.5-haiku",
                "name": "Claude 3.5 Haiku",
                "reasoning": False,
                "tool_call": True,
                "temperature": True,
                "limit": {"context": 200000, "output": 8192},
                "cost": {"input": 0.8, "output": 4.0, "cache_read": 0.08},
            },
        },
    },
    "deepseek": {
        "id": "deepseek",
        "name": "DeepSeek",
        "env": ["DEEPSEEK_API_KEY"],
        "api": "https://api.deepseek.com",
        "models": {
            "deepseek-v4-flash": {
                "id": "deepseek-v4-flash",
                "name": "DeepSeek V4 Flash",
                "reasoning": True,
                "tool_call": True,
                "temperature": True,
                "limit": {"context": 128000, "output": 8192},
                "cost": {"input": 0.27, "output": 1.10, "cache_read": 0.07, "cache_write": 0.0},
            },
            "deepseek-v4-pro": {
                "id": "deepseek-v4-pro",
                "name": "DeepSeek V4 Pro",
                "reasoning": True,
                "tool_call": True,
                "temperature": True,
                "limit": {"context": 128000, "output": 8192},
                "cost": {"input": 0.55, "output": 2.19, "cache_read": 0.14, "cache_write": 0.0},
            },
        },
    },
}


# --- source / endpoint / cache plumbing ------------------------------------


def _source() -> str:
    return os.environ.get("RELAY_MODELS_URL") or DEFAULT_SOURCE


def _endpoint(source: str) -> str:
    """Resolve a source to the full ``api.json`` endpoint.

    A source ending in ``.json`` is used as-is (lets tests point straight at a
    fixture file); otherwise ``/api.json`` is appended.
    """
    src = (source or DEFAULT_SOURCE).strip()
    if src.lower().endswith(".json"):
        return src
    return src.rstrip("/") + API_PATH


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _cache_dir() -> Path:
    override = os.environ.get("RELAY_CACHE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "relay"


def _cache_file() -> Path:
    return _cache_dir() / "models.json"


def _fetch_raw(endpoint: str) -> str:
    """Read the raw catalog text from ``endpoint`` (http(s) URL or local file).

    Raises on any failure -- the caller turns that into a fallback, never a crash.
    """
    lowered = endpoint.lower()
    if lowered.startswith(("http://", "https://")):
        with urllib.request.urlopen(endpoint, timeout=FETCH_TIMEOUT_S) as response:  # noqa: S310
            return response.read().decode("utf-8")
    path = endpoint[len("file://"):] if lowered.startswith("file://") else endpoint
    return Path(path).read_text(encoding="utf-8")


def _read_cache(endpoint: str) -> tuple[dict, float] | None:
    """Read the disk cache envelope, but only if it matches ``endpoint``.

    Returns ``(data, fetched_at)`` or None. A cache from a *different* source is
    ignored (it would be meaningless to serve), as is any malformed file.
    """
    try:
        obj = json.loads(_cache_file().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 -- missing/corrupt cache is simply "no cache"
        return None
    if not isinstance(obj, dict) or obj.get("source") != endpoint:
        return None
    data = obj.get("data")
    fetched_at = _f(obj.get("fetched_at"))
    if not isinstance(data, dict) or fetched_at is None:
        return None
    return data, fetched_at


def _write_cache(endpoint: str, data: dict) -> None:
    """Atomically write the cache (temp file then rename). Never raises."""
    try:
        directory = _cache_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = _cache_file()
        tmp = path.with_name(path.name + ".tmp")
        envelope = {"source": endpoint, "fetched_at": time.time(), "data": data}
        tmp.write_text(json.dumps(envelope), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 -- caching is best-effort, must never crash
        pass


def _fresh(fetched_at: float) -> bool:
    return (time.time() - fetched_at) < CACHE_TTL_S


def _build(raw: dict, status: str, endpoint: str) -> Catalog:
    return Catalog(providers=parse_catalog(raw), status=status, source=endpoint)


# --- the loader (the fallback chain) ---------------------------------------


def load_catalog(*, source: str | None = None, force_refresh: bool = False) -> Catalog:
    """Resolve the catalog through the fallback chain. Never raises.

    ``source`` overrides the env/default source (mostly for tests). ``force_refresh``
    skips the fresh-cache rung and forces a network attempt.
    """
    endpoint = _endpoint(source if source is not None else _source())
    disable_fetch = _truthy(os.environ.get("RELAY_DISABLE_MODELS_FETCH"))
    cache = _read_cache(endpoint)

    # 1. fresh disk cache
    if cache is not None and not force_refresh and _fresh(cache[1]):
        return _build(cache[0], "cache", endpoint)

    # 2. network (or local-file) fetch
    if not disable_fetch:
        try:
            data = json.loads(_fetch_raw(endpoint))
            if isinstance(data, dict) and data:
                _write_cache(endpoint, data)
                return _build(data, "fetch", endpoint)
        except Exception:  # noqa: BLE001 -- a fetch failure falls through, never crashes
            pass

    # 3. stale disk cache (better stale than nothing)
    if cache is not None:
        return _build(cache[0], "stale", endpoint)

    # 4. bundled fallback (cost is never silently zero)
    return _build(_BUNDLED_RAW, "bundled", endpoint)


# --- in-process memo --------------------------------------------------------
#
# ``call_model`` may consult the catalog on every non-OpenRouter call, so the
# resolved catalog is memoized per (endpoint, disable-fetch) for the process.
# Keying on the resolved env means changing ``RELAY_MODELS_URL`` naturally
# invalidates it (important for tests); ``reset_catalog_cache`` is the explicit hook.
_MEMO: dict[tuple[str, bool], Catalog] = {}


def get_catalog() -> Catalog:
    """Return the process-cached catalog, loading it once per source/config."""
    key = (_endpoint(_source()), _truthy(os.environ.get("RELAY_DISABLE_MODELS_FETCH")))
    catalog = _MEMO.get(key)
    if catalog is None:
        catalog = load_catalog()
        _MEMO[key] = catalog
    return catalog


def reset_catalog_cache() -> None:
    """Clear the in-process memo (tests; or to force a re-resolve after env change)."""
    _MEMO.clear()
