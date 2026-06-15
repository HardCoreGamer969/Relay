"""The OpenAI-compatible client factory.

This is the ONLY module in Relay that touches the OpenAI SDK directly. Every
provider Relay talks to (OpenRouter, DeepSeek, ...) exposes an OpenAI-compatible
API, so we point the ``openai`` SDK at a provider profile's base URL with its key
and treat that one SDK as the universal backend. Everything else goes through
``call_model``, which selects the provider profile per role.
"""

from __future__ import annotations

from openai import OpenAI

from relay.providers import ProviderProfile, resolve_provider
from relay.secrets import resolve_key

# Kept for backward compatibility: the OpenRouter base URL some callers/tests
# referenced directly. The canonical source is now the provider registry.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def build_client(
    provider: str | ProviderProfile = "openrouter", api_key: str | None = None
) -> OpenAI:
    """Build an OpenAI-compatible client for ``provider``.

    ``provider`` is a provider id (or a :class:`ProviderProfile`); it defaults to
    OpenRouter so ``build_client()`` behaves exactly as before. The API key is
    resolved **env var > auth.json** (an explicit ``api_key`` still wins over both),
    so the historical env-key workflow is unchanged but an in-app stored key works
    too. Raises a clear error naming the missing env-var when no key is found.
    """
    profile = resolve_provider(provider)
    key = api_key or resolve_key(profile.id, profile.key_env)
    if not key:
        raise RuntimeError(
            f"{profile.key_env} is not set (and no stored key for {profile.id}). "
            "Set the env var, copy .env.example to .env, or add a key via `relay config set-key`."
        )
    return OpenAI(base_url=profile.base_url, api_key=key)
