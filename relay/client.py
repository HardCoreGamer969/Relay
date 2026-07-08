"""The OpenAI-compatible client factory.

This is the ONLY module in Relay that touches the OpenAI SDK directly. Every
provider Relay talks to (OpenRouter, DeepSeek, ...) exposes an OpenAI-compatible
API, so we point the ``openai`` SDK at a provider profile's base URL with its key
and treat that one SDK as the universal backend. Everything else goes through
``call_model``, which selects the provider profile per role.

v0.0.32: the client is built with ``max_retries=0`` so the SDK's own retry
loop doesn't double up with ours -- :mod:`relay.models` owns the retry policy
(exponential + jitter, honors ``Retry-After``, retries connection errors).
The default request timeout is also set explicitly so a hung provider can't
stall a step on the SDK's 600s default.
"""

from __future__ import annotations

import os

from openai import OpenAI

from relay.providers import ProviderProfile, resolve_provider
from relay.secrets import resolve_key

# Kept for backward compatibility: the OpenRouter base URL some callers/tests
# referenced directly. The canonical source is now the provider registry.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _default_request_timeout_s() -> float:
    """Per-request timeout, in seconds, for OpenAI SDK calls.

    Default 600s matches the openai SDK's historical default; a hung provider
    (no TCP response, no TLS handshake) would otherwise stall a step for
    10 minutes. ``RELAY_REQUEST_TIMEOUT_S`` lets the user raise/lower this
    without code changes. Returns a positive float; invalid values fall
    through to the default.
    """
    raw = os.environ.get("RELAY_REQUEST_TIMEOUT_S")
    if not raw:
        return 600.0
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return 600.0
    return n if n > 0 else 600.0


def build_client(
    provider: str | ProviderProfile = "openrouter", api_key: str | None = None
) -> OpenAI:
    """Build an OpenAI-compatible client for ``provider``.

    ``provider`` is a provider id (or a :class:`ProviderProfile`); it defaults to
    OpenRouter so ``build_client()`` behaves exactly as before. The API key is
    resolved **env var > auth.json** (an explicit ``api_key`` still wins over both),
    so the historical env-key workflow is unchanged but an in-app stored key works
    too. Raises a clear error naming the missing env-var when no key is found.

    The client is built with ``max_retries=0`` -- :mod:`relay.models` owns the
    retry policy (exponential + jitter, honors ``Retry-After``, retries
    connection errors) so the SDK's loop doesn't double up with ours and the
    user gets one clear retry contract.
    """
    profile = resolve_provider(provider)
    key = api_key or resolve_key(profile.id, profile.key_env)
    if not key:
        raise RuntimeError(
            f"{profile.key_env} is not set (and no stored key for {profile.id}). "
            "Set the env var, copy .env.example to .env, or add a key via `relay config set-key`."
        )
    return OpenAI(base_url=profile.base_url, api_key=key, max_retries=0)
