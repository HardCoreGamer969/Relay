"""Network-free tests for provider profiles + per-role provider selection.

Covers relay/providers.py (the registry), relay/client.py (provider-aware
``build_client``), relay/config.py (per-role provider/thinking), and the
provider routing in ``call_model`` (relay/models.py). No socket is opened: the
OpenAI client is constructed but never called, and ``call_model`` uses injected
or monkeypatched fakes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import relay.models as models_mod
from relay.client import build_client
from relay.config import ModelConfig, load_models
from relay.models import call_model
from relay.providers import (
    DEFAULT_PROVIDER,
    DEEPSEEK,
    OPENROUTER,
    ProviderProfile,
    known_providers,
    resolve_provider,
)
from relay.telemetry import Ledger


# --- the registry -----------------------------------------------------------


def test_resolve_known_providers():
    assert resolve_provider("openrouter") is OPENROUTER
    assert resolve_provider("deepseek") is DEEPSEEK
    # DeepSeek's base URL includes /v1 (its docs); OpenRouter's too.
    assert DEEPSEEK.base_url == "https://api.deepseek.com/v1"
    assert DEEPSEEK.key_env == "DEEPSEEK_API_KEY"
    assert OPENROUTER.base_url == "https://openrouter.ai/api/v1"


def test_resolve_default_and_passthrough():
    assert resolve_provider(None) is OPENROUTER  # default provider
    assert DEFAULT_PROVIDER == "openrouter"
    profile = ProviderProfile("x", "https://x/v1", "X_KEY")
    assert resolve_provider(profile) is profile  # a profile passes through
    assert "deepseek" in known_providers() and "openrouter" in known_providers()


def test_resolve_unknown_raises():
    with pytest.raises(ValueError):
        resolve_provider("not-a-provider")


# --- provider-aware build_client --------------------------------------------


def test_build_client_defaults_to_openrouter(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    client = build_client()  # zero-arg = OpenRouter, exactly as before
    assert str(client.base_url).startswith("https://openrouter.ai/api/v1")
    assert client.api_key == "or-key"


def test_build_client_deepseek_uses_its_base_url_and_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    client = build_client("deepseek")
    assert str(client.base_url).startswith("https://api.deepseek.com/v1")
    assert client.api_key == "ds-key"


def test_build_client_missing_key_names_the_env(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        build_client("deepseek")
    assert "DEEPSEEK_API_KEY" in str(excinfo.value)


# --- per-role config --------------------------------------------------------


def test_model_config_defaults_keep_openrouter():
    # The historical 2-arg construction still works and defaults to OpenRouter.
    cfg = ModelConfig(brain="vendor/brain", hands="vendor/hands")
    assert cfg.provider_for_role("brain") == "openrouter"
    assert cfg.provider_for_role("hands") == "openrouter"
    assert cfg.thinking_for_role("brain") is False
    assert cfg.for_role("brain") == "vendor/brain"


def test_model_config_per_role_provider_and_thinking():
    cfg = ModelConfig(
        brain="anthropic/claude-sonnet-4.5", hands="deepseek-v4-flash",
        brain_provider="openrouter", hands_provider="deepseek",
        hands_thinking=True,
    )
    assert cfg.provider_for_role("hands") == "deepseek"
    assert cfg.thinking_for_role("hands") is True
    assert cfg.thinking_for_role("brain") is False
    with pytest.raises(ValueError):
        cfg.provider_for_role("nope")


def test_load_models_reads_provider_and_thinking_env(monkeypatch):
    monkeypatch.setenv("RELAY_BRAIN_MODEL", "anthropic/claude-sonnet-4.5")
    monkeypatch.setenv("RELAY_HANDS_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("RELAY_HANDS_PROVIDER", "deepseek")
    monkeypatch.setenv("RELAY_HANDS_THINKING", "1")
    monkeypatch.delenv("RELAY_BRAIN_PROVIDER", raising=False)
    monkeypatch.delenv("RELAY_BRAIN_THINKING", raising=False)

    cfg = load_models()
    assert cfg.provider_for_role("brain") == "openrouter"  # unset -> default
    assert cfg.provider_for_role("hands") == "deepseek"
    assert cfg.thinking_for_role("hands") is True
    assert cfg.thinking_for_role("brain") is False


# --- call_model provider routing --------------------------------------------


class _CapturingCompletions:
    def __init__(self):
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        message = SimpleNamespace(content="ok")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


class _CapturingClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=_CapturingCompletions())


def test_call_model_builds_client_for_roles_provider(monkeypatch):
    """When no client is injected, call_model builds one for the role's provider."""
    seen = {}

    def fake_build_client(provider="openrouter", api_key=None):
        seen["provider"] = provider
        return _CapturingClient()

    monkeypatch.setattr(models_mod, "build_client", fake_build_client)
    cfg = ModelConfig(brain="x", hands="deepseek-v4-flash", hands_provider="deepseek")

    call_model("hands", [{"role": "user", "content": "hi"}], models=cfg, ledger=Ledger())
    assert seen["provider"] == "deepseek"


def test_openrouter_role_requests_usage_include():
    cfg = ModelConfig(brain="vendor/brain", hands="vendor/hands")  # both openrouter
    client = _CapturingClient()
    call_model("brain", [{"role": "user", "content": "hi"}], models=cfg, client=client)
    sent = client.chat.completions.calls[0]
    assert sent["extra_body"]["usage"]["include"] is True  # unchanged behavior


def test_deepseek_role_does_not_request_openrouter_usage():
    cfg = ModelConfig(brain="x", hands="deepseek-v4-flash", hands_provider="deepseek")
    client = _CapturingClient()
    call_model("hands", [{"role": "user", "content": "hi"}], models=cfg, client=client)
    sent = client.chat.completions.calls[0]
    # The OpenRouter-specific usage.include must NOT be sent to DeepSeek.
    assert "usage" not in (sent.get("extra_body") or {})
