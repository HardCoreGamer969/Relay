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


# How a provider's models are chosen:
# - "manual": the user supplies a slug; we do NOT enumerate (aggregators like
#   OpenRouter expose thousands of slugs -- the right UX is type-a-slug, validated).
# - "list": enumerate live via the provider's ``/models`` endpoint (direct
#   providers like DeepSeek). Listing live self-corrects against deprecations.
DISCOVERY_MANUAL = "manual"
DISCOVERY_LIST = "list"


@dataclass(frozen=True)
class ProviderProfile:
    """A provider as a thin profile over the shared OpenAI-compatible client."""

    id: str
    base_url: str
    key_env: str
    discovery: str = DISCOVERY_MANUAL


# The built-in registry. NOTE DeepSeek's base URL includes ``/v1`` (its docs), and
# DeepSeek is OpenAI-compatible so it needs no special client -- just this entry.
# OpenRouter is a manual aggregator (type-a-slug); DeepSeek is a direct provider
# that lists its models live.
OPENROUTER = ProviderProfile(
    "openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", DISCOVERY_MANUAL
)
DEEPSEEK = ProviderProfile(
    "deepseek", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", DISCOVERY_LIST
)

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


def list_models(provider: str | ProviderProfile, *, client=None) -> list[str]:
    """Return the model ids a provider offers, by its ``discovery`` mode.

    - ``manual`` providers (OpenRouter): return ``[]`` -- the caller should prompt
      for a slug instead (check ``profile.discovery``). No enumeration.
    - ``list`` providers (DeepSeek): enumerate live via the OpenAI-compatible
      ``/models`` endpoint (``GET {base_url}/models``, bearer key). Listing live
      self-corrects against deprecations -- a retired id simply won't appear.

    ``client`` may be injected (tests pass a fake with ``.models.list()``); when
    ``None`` a real client is built for the provider. Only model ids are returned
    -- cross-reference the catalog for pricing/context at display time.
    """
    profile = resolve_provider(provider)
    if profile.discovery != DISCOVERY_LIST:
        return []
    if client is None:
        # Lazy import avoids a providers <-> client import cycle.
        from relay.client import build_client

        client = build_client(profile.id)
    raw = client.models.list()
    data = getattr(raw, "data", None)
    if data is None:
        data = raw if isinstance(raw, (list, tuple)) else []
    ids: list[str] = []
    for entry in data:
        mid = getattr(entry, "id", None)
        if mid is None and isinstance(entry, dict):
            mid = entry.get("id")
        if mid:
            ids.append(str(mid))
    return ids


def validate_model(provider: str | ProviderProfile, model: str, *, client=None) -> tuple[bool, str]:
    """Validate a ``(provider, model)`` LIVE before it is saved. Never raises.

    Returns ``(ok, note)``. For a ``list`` provider (DeepSeek): the id must appear
    in the live ``/models`` list. For a ``manual`` provider (OpenRouter): a minimal
    ``max_tokens=1`` probe -- so a typo'd slug is rejected at entry, not at the
    first (paid) run. ``client`` may be injected (tests); else one is built.
    """
    try:
        profile = resolve_provider(provider)
    except ValueError as exc:
        return False, str(exc)
    if client is None:
        from relay.client import build_client

        try:
            client = build_client(profile.id)
        except Exception as exc:  # noqa: BLE001 -- missing key etc.: a clear note
            return False, str(exc).splitlines()[0]
    if profile.discovery == DISCOVERY_LIST:
        try:
            ids = list_models(profile.id, client=client)
        except Exception as exc:  # noqa: BLE001
            return False, f"could not list {profile.id} models: {str(exc).splitlines()[0]}"
        if ids and model not in ids:
            return False, f"{model!r} is not in {profile.id}'s live model list"
        return True, "in live model list"
    # manual provider: a live preflight probe.
    try:
        client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": "ping"}], max_tokens=1
        )
        return True, "resolved"
    except Exception as exc:  # noqa: BLE001 -- classify any failure as the note
        text = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        return False, text[:120]
