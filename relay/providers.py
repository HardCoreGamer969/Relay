"""Provider profiles: the seam that makes Relay genuinely multi-provider.

Relay reaches every model through an OpenAI-compatible API. A *provider* is just
a thin profile -- ``{id, base_url, key_env}`` -- not bespoke code. This is exactly
OpenCode's pattern: every OpenAI-compatible provider is one small entry over one
shared client. Adding a new provider is a one-line :class:`ProviderProfile` in the
registry below, never a new client implementation.

The client (``relay/client.py``) builds an ``openai.OpenAI`` pointed at a profile's
``base_url`` with the key from its ``key_env``; ``call_model`` selects the profile
**per role** (see ``RELAY_BRAIN_PROVIDER`` / ``RELAY_HANDS_PROVIDER`` in
``relay/config.py``), defaulting to OpenRouter so all existing behavior is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

# The default provider. Roles resolve to OpenRouter unless told otherwise, so the
# whole pre-multi-provider world is byte-for-byte unchanged.
DEFAULT_PROVIDER = "openrouter"


@dataclass(frozen=True)
class ProviderProfile:
    """A provider as a thin profile over the shared OpenAI-compatible client."""

    id: str
    base_url: str
    key_env: str


# The built-in registry. NOTE DeepSeek's base URL includes ``/v1`` (its docs), and
# DeepSeek is OpenAI-compatible so it needs no special client -- just this entry.
OPENROUTER = ProviderProfile("openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY")
DEEPSEEK = ProviderProfile("deepseek", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY")

_REGISTRY: dict[str, ProviderProfile] = {p.id: p for p in (OPENROUTER, DEEPSEEK)}


def resolve_provider(provider: str | ProviderProfile | None) -> ProviderProfile:
    """Resolve a provider id (or a profile) to a :class:`ProviderProfile`.

    ``None`` / empty resolves to the default (OpenRouter). Unknown ids raise a
    clear error -- a typo'd provider should fail loudly, not silently mis-route.
    """
    if isinstance(provider, ProviderProfile):
        return provider
    pid = (provider or DEFAULT_PROVIDER).strip().lower()
    profile = _REGISTRY.get(pid)
    if profile is None:
        known = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"Unknown provider {provider!r}. Known providers: {known}.")
    return profile


def known_providers() -> tuple[str, ...]:
    """The registered provider ids (e.g. for help text / the future picker)."""
    return tuple(sorted(_REGISTRY))
