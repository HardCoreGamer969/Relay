"""The seam.

``call_model`` is THE abstraction every later part of Relay is built on: it
resolves a model by role, selects that role's *provider* (OpenRouter, DeepSeek,
...), calls it through the shared OpenAI-compatible client, times the call,
records telemetry (tokens + the real per-generation cost, extracted per provider),
and returns the text. Keep it clean.

Provider selection is per role and defaults to OpenRouter, so the historical
single-provider behavior is unchanged: an OpenRouter role still asks for and reads
OpenRouter's returned ``cost`` exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from relay.catalog import get_catalog
from relay.client import build_client
from relay.config import ModelConfig, load_models
from relay.providers import DEFAULT_PROVIDER
from relay.telemetry import CallRecord, Ledger, timer


@dataclass
class ModelResult:
    """The text a model returned, plus the telemetry recorded for the call."""

    text: str
    record: CallRecord


def _get(obj: Any, key: str) -> Any:
    """Read ``key`` from an attribute-style, pydantic-extra, or dict-style object.

    Providers surface extra usage fields differently: the openai SDK's pydantic v2
    usage models put unknown fields (OpenRouter's ``cost``, DeepSeek's
    ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``) in ``model_extra``,
    not as first-class attributes. Try attribute, then ``model_extra``, then dict.
    """
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


def _extract_tokens(usage: Any) -> tuple[int, int]:
    prompt = _get(usage, "prompt_tokens")
    completion = _get(usage, "completion_tokens")
    return int(prompt or 0), int(completion or 0)


def _read_cost_field(usage: Any) -> Any:
    """Find OpenRouter's ``cost`` in the usage block, trying real shapes in turn.

    OpenRouter returns ``cost`` as an *extra* field. The ``openai`` SDK models
    are pydantic v2 with ``extra="allow"``, so the value is surfaced through
    ``model_extra`` and not necessarily as a first-class attribute. Try, in
    order: a direct attribute, then ``model_extra``, then dict-style access.
    """
    if usage is None:
        return None
    # 1. direct attribute (if the SDK promoted it to a first-class field)
    cost = getattr(usage, "cost", None)
    if cost is not None:
        return cost
    # 2. pydantic v2 extra fields (openai SDK usage models allow extras)
    model_extra = getattr(usage, "model_extra", None)
    if isinstance(model_extra, dict):
        cost = model_extra.get("cost")
        if cost is not None:
            return cost
    # 3. plain mapping
    if isinstance(usage, dict):
        return usage.get("cost")
    return None


def _openrouter_cost(usage: Any) -> float | None:
    """Read OpenRouter's real per-generation cost from the usage block.

    Guarded so pricing can never crash a run: any missing/odd value → None.
    """
    cost = _read_cost_field(usage)
    if cost is None:
        return None
    try:
        return float(cost)
    except (TypeError, ValueError):
        return None


def _token(usage: Any, key: str) -> int:
    """Read an integer token count from the usage block (0 when absent/odd)."""
    try:
        return int(_get(usage, key) or 0)
    except (TypeError, ValueError):
        return 0


def _catalog_cost(usage: Any, *, provider: str, model: str) -> float | None:
    """Price a call from the catalog using the provider's cache hit/miss split.

    DeepSeek's usage carries ``prompt_cache_hit_tokens`` /
    ``prompt_cache_miss_tokens`` plus completion tokens, and the documented cost is

        cost = hit  * cache_read_price
             + miss * input_price
             + completion * output_price

    Catalog prices are per-1M tokens, so each is divided by 1e6. A naive
    ``total_input * input_price`` is wrong by up to ~50x once caching kicks in --
    and it WILL, because Relay's loop reuses long shared prefixes (system prompt,
    plan context) call after call, exactly what DeepSeek caches. So read the split
    and apply the two input rates.

    If the catalog entry lacks ``cache_read``, fall back to pricing all input at the
    input (miss) rate -- never zero. Likewise, if no split is present at all, price
    the whole ``prompt_tokens`` at the input rate. Returns ``None`` (unknown) only
    when the catalog has no usable price for the model.
    """
    if usage is None:
        return None
    cost = get_catalog().cost(provider, model)
    if cost is None or cost.input is None or cost.output is None:
        return None  # genuinely unpriced -> unknown (shown as "-"), not a wrong 0

    hit = _token(usage, "prompt_cache_hit_tokens")
    miss = _token(usage, "prompt_cache_miss_tokens")
    completion = _token(usage, "completion_tokens")
    if hit == 0 and miss == 0:
        # No cache split surfaced; price all prompt tokens at the input rate.
        miss = _token(usage, "prompt_tokens")

    input_rate = cost.input / 1_000_000.0
    output_rate = cost.output / 1_000_000.0
    # cache_read absent -> treat cache hits at the input rate (never zero).
    cache_rate = (cost.cache_read / 1_000_000.0) if cost.cache_read is not None else input_rate

    return hit * cache_rate + miss * input_rate + completion * output_rate


def _extract_cost(usage: Any, *, provider: str, model: str) -> float | None:
    """Per-provider cost extraction. Never raises -- pricing can't crash a run.

    OpenRouter returns the real per-generation ``cost`` in the usage block, so we
    read it directly (unchanged). Every other provider is priced from the catalog
    via the cache hit/miss split (:func:`_catalog_cost`).
    """
    if provider == DEFAULT_PROVIDER:
        return _openrouter_cost(usage)
    return _catalog_cost(usage, provider=provider, model=model)


def call_model(
    role: str,
    messages: list[dict[str, str]],
    *,
    models: ModelConfig | None = None,
    ledger: Ledger | None = None,
    client: Any | None = None,
    **kwargs: Any,
) -> ModelResult:
    """Call the model bound to ``role`` through OpenRouter and record telemetry.

    Args:
        role: ``"brain"`` or ``"hands"``.
        messages: OpenAI-style chat messages.
        models: Role→(provider, model) config (defaults to :func:`load_models`).
        ledger: If given, the resulting :class:`CallRecord` is appended to it.
        client: Pre-built client (tests inject a fake). When ``None``, a client is
            built for the role's provider profile. An explicitly-passed client is
            used as-is, but the provider still drives request/cost handling.
        **kwargs: Forwarded to ``chat.completions.create`` (e.g. temperature).
    """
    models = models or load_models()
    model = models.for_role(role)
    provider = models.provider_for_role(role)
    client = client or build_client(provider)

    extra_body = dict(kwargs.pop("extra_body", {}) or {})
    if provider == DEFAULT_PROVIDER:
        # OpenRouter: ask for the actual per-generation cost in ``usage``, while
        # preserving any caller-provided extra_body / usage options. (Unchanged.)
        extra_body["usage"] = {"include": True, **dict(extra_body.get("usage", {}) or {})}

    with timer() as elapsed:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            extra_body=extra_body,
            **kwargs,
        )

    text = response.choices[0].message.content or ""
    usage = getattr(response, "usage", None)
    prompt_tokens, completion_tokens = _extract_tokens(usage)

    record = CallRecord(
        role=role,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_s=elapsed.seconds,
        cost_usd=_extract_cost(usage, provider=provider, model=model),
    )
    if ledger is not None:
        ledger.add(record)
    return ModelResult(text=text, record=record)
