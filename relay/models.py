"""The seam.

``call_model`` is THE abstraction every later part of Relay is built on: it
resolves a model by role, calls OpenRouter through the shared client, times the
call, records telemetry (tokens + OpenRouter's real per-generation cost), and
returns the text. Keep it clean.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from relay.client import build_client
from relay.config import ModelConfig, load_models
from relay.telemetry import CallRecord, Ledger, timer


@dataclass
class ModelResult:
    """The text a model returned, plus the telemetry recorded for the call."""

    text: str
    record: CallRecord


def _get(obj: Any, key: str) -> Any:
    """Read ``key`` from an attribute-style or dict-style object."""
    if obj is None:
        return None
    value = getattr(obj, key, None)
    if value is None and isinstance(obj, dict):
        value = obj.get(key)
    return value


def _extract_tokens(usage: Any) -> tuple[int, int]:
    prompt = _get(usage, "prompt_tokens")
    completion = _get(usage, "completion_tokens")
    return int(prompt or 0), int(completion or 0)


def _extract_cost(usage: Any) -> float | None:
    """Read OpenRouter's returned per-generation cost.

    Guarded so pricing can never crash a run: any missing/odd value → None.
    """
    cost = _get(usage, "cost")
    if cost is None:
        return None
    try:
        return float(cost)
    except (TypeError, ValueError):
        return None


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
        models: Role→model config (defaults to :func:`load_models`).
        ledger: If given, the resulting :class:`CallRecord` is appended to it.
        client: OpenRouter client (defaults to :func:`build_client`).
        **kwargs: Forwarded to ``chat.completions.create`` (e.g. temperature).
    """
    models = models or load_models()
    model = models.for_role(role)
    client = client or build_client()

    # Ask OpenRouter to include the actual per-generation cost in ``usage``,
    # while preserving any caller-provided extra_body / usage options.
    extra_body = dict(kwargs.pop("extra_body", {}) or {})
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
        cost_usd=_extract_cost(usage),
    )
    if ledger is not None:
        ledger.add(record)
    return ModelResult(text=text, record=record)
