"""Knowing the brain model's context window, so memory can be budgeted to it.

Relay must know the brain's context window to budget plan memory against it. The
window is resolved in strict priority order, and discovery NEVER crashes a run
(a failed probe falls through to the next rung):

1. **Explicit override** -- a declared window (``RELAY_BRAIN_CONTEXT`` env or an
   ``override`` param) always wins.
2. **Auto-detect** --
   - OpenRouter models: read ``context_length`` from OpenRouter's models
     metadata (via the client; cached on the client for the process).
   - Local runtimes (best-effort): if the slug looks local, try the runtime's
     API (Ollama / LM Studio / llama.cpp), each guarded.
3. **Conservative default** -- if both fail, fall back to a small default and
   report the source as ``"default"`` so callers can note Relay is guessing.

``resolve_context_window`` returns ``(window, source)`` where source is one of
``"override"`` / ``"catalog"`` / ``"openrouter"`` / ``"local:<runtime>"`` /
``"default"`` so the provenance is visible and testable.

The model **catalog** (``relay/catalog.py``) is now a better source than the
OpenRouter-metadata probe -- it carries ``limit.context`` for every provider's
models -- so when a ``catalog`` (and ``provider``) is supplied it is consulted
*ahead* of the probe, with the probe kept as a fallback rung. Callers that pass
neither (the autonomous loop) keep the exact prior behavior.
"""

from __future__ import annotations

import json
import os
import urllib.request

# Small conservative default when nothing else is known. Deliberately low so we
# under-promise rather than overflow a real (possibly tiny) window.
DEFAULT_CONTEXT_WINDOW = 8192

# Slug shapes that indicate a local runtime rather than an OpenRouter model.
_LOCAL_PREFIXES = ("ollama/", "lmstudio/", "lm_studio/", "llamacpp/", "llama.cpp/", "local/")

# Default localhost endpoints for the local-runtime probes.
_OLLAMA_URL = "http://localhost:11434/api/show"
_LMSTUDIO_URL = "http://localhost:1234/v1/models"
_LLAMACPP_URL = "http://localhost:8080/props"
_PROBE_TIMEOUT_S = 1.5


def resolve_context_window(
    model: str,
    *,
    provider: str | None = None,
    client=None,
    override: int | None = None,
    catalog=None,
    env_override: str | None = "RELAY_BRAIN_CONTEXT",
) -> tuple[int, str]:
    """Resolve a model's context window and how it was determined.

    Returns ``(window, source)``. Never raises -- every discovery step is guarded
    and falls through to the conservative default.

    When ``catalog`` and ``provider`` are supplied, the catalog's ``limit.context``
    is consulted *ahead* of the OpenRouter probe (it's the better source); the probe
    remains a fallback rung. Omit them (the autonomous loop did pre-v0.0.29) for the
    prior override -> probe -> default behavior, unchanged.

    ``env_override`` names the environment variable consulted as the manual override
    (default ``RELAY_BRAIN_CONTEXT``). The HANDS pool resolves with
    ``env_override="RELAY_HANDS_CONTEXT"`` so it gets its OWN manual override and
    never silently inherits the brain's; pass ``None`` to skip the env rung entirely.
    """
    # 1. explicit override (param beats env; both are "override")
    declared = _coerce_int(override)
    if declared is not None:
        return (declared, "override")
    env_declared = _coerce_int(os.environ.get(env_override)) if env_override else None
    if env_declared is not None:
        return (env_declared, "override")

    # 2. catalog (better than the probe; only when one is supplied)
    if catalog is not None and provider:
        try:
            limit = catalog.context_limit(provider, model)
        except Exception:  # noqa: BLE001 -- catalog lookups must never crash a run
            limit = None
        if limit:
            return (int(limit), "catalog")

    # 3. auto-detect
    if _is_local_slug(model):
        probed = probe_local_context(model)
        if probed is not None:
            window, runtime = probed
            return (window, f"local:{runtime}")
    else:
        length = _openrouter_context_length(model, client)
        if length:
            return (length, "openrouter")

    # 4. conservative default
    return (DEFAULT_CONTEXT_WINDOW, "default")


# --- OpenRouter metadata ----------------------------------------------------


def _list_models(client) -> list:
    """List OpenRouter models once, cached on the client for the process."""
    cached = getattr(client, "_relay_models_cache", None)
    if cached is None:
        cached = list(client.models.list())
        try:
            setattr(client, "_relay_models_cache", cached)
        except Exception:  # noqa: BLE001 -- some clients may forbid setattr; skip caching
            pass
    return cached


def _openrouter_context_length(model: str, client) -> int | None:
    """Read a model's ``context_length`` from OpenRouter metadata, or None."""
    if client is None:
        return None
    try:
        for entry in _list_models(client):
            if _attr(entry, "id") == model:
                length = _attr(entry, "context_length")
                if length is None:
                    length = _attr(_attr(entry, "top_provider"), "context_length")
                return int(length) if length else None
    except Exception:  # noqa: BLE001 -- metadata fetch must never crash a run
        return None
    return None


# --- local runtime probes (best-effort, all guarded) -----------------------


def probe_local_context(model: str) -> tuple[int, str] | None:
    """Best-effort window probe for local runtimes. Returns ``(window, runtime)`` or None."""
    for runtime, probe in (
        ("ollama", _probe_ollama),
        ("lmstudio", _probe_lmstudio),
        ("llamacpp", _probe_llamacpp),
    ):
        try:
            window = probe(model)
        except Exception:  # noqa: BLE001 -- each runtime probe is allowed to fail
            window = None
        if window:
            return (window, runtime)
    return None


def _probe_ollama(model: str) -> int | None:
    name = _strip_local_prefix(model)
    data = _http_json(_OLLAMA_URL, payload={"name": name})
    info = data.get("model_info") if isinstance(data, dict) else None
    if isinstance(info, dict):
        # Ollama reports it as "<arch>.context_length" (e.g. "llama.context_length").
        for key, value in info.items():
            if key.endswith("context_length"):
                return _coerce_int(value)
    return _coerce_int((data or {}).get("context_length")) if isinstance(data, dict) else None


def _probe_lmstudio(model: str) -> int | None:
    data = _http_json(_LMSTUDIO_URL)
    items = data.get("data") if isinstance(data, dict) else None
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("loaded_context_length", "max_context_length", "context_length"):
                length = _coerce_int(item.get(key))
                if length:
                    return length
    return None


def _probe_llamacpp(model: str) -> int | None:
    data = _http_json(_LLAMACPP_URL)
    if not isinstance(data, dict):
        return None
    length = _coerce_int(data.get("n_ctx"))
    if length:
        return length
    settings = data.get("default_generation_settings")
    if isinstance(settings, dict):
        return _coerce_int(settings.get("n_ctx"))
    return None


# --- small utilities --------------------------------------------------------


def _is_local_slug(model: str) -> bool:
    slug = (model or "").lower()
    if not slug:
        return False
    if "/" not in slug:
        return True  # a bare name (no author/) is most likely a local model
    return slug.startswith(_LOCAL_PREFIXES) or "localhost" in slug or "127.0.0.1" in slug


def _strip_local_prefix(model: str) -> str:
    for prefix in _LOCAL_PREFIXES:
        if model.lower().startswith(prefix):
            return model[len(prefix):]
    return model


def _http_json(url: str, *, payload: dict | None = None) -> dict | None:
    """GET (or POST if ``payload``) JSON from a localhost URL. Raises on failure."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"} if data else {}
    )
    with urllib.request.urlopen(request, timeout=_PROBE_TIMEOUT_S) as response:  # noqa: S310 -- localhost only
        return json.loads(response.read().decode("utf-8"))


def _attr(obj, key: str):
    """Read ``key`` from an attribute-style, pydantic-extra, or dict-style object."""
    if obj is None:
        return None
    value = getattr(obj, key, None)
    if value is None:
        extra = getattr(obj, "model_extra", None)
        if isinstance(extra, dict):
            value = extra.get(key)
    if value is None and isinstance(obj, dict):
        value = obj.get(key)
    return value


def _coerce_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
