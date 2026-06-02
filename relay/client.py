"""The OpenRouter client factory.

This is the ONLY module in Relay that touches the OpenAI SDK directly.
OpenRouter exposes an OpenAI-compatible API, so we point the ``openai`` SDK at
OpenRouter's base URL and treat OpenRouter as the single, model-agnostic
backend for the whole project. Everything else goes through ``call_model``.
"""

from __future__ import annotations

import os

from openai import OpenAI

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def build_client(api_key: str | None = None) -> OpenAI:
    """Build an OpenRouter-backed OpenAI client.

    The API key is read from the ``OPENROUTER_API_KEY`` environment variable
    unless one is passed explicitly. Raises a clear error when no key is found
    so callers can point the user at ``.env.example``.
    """
    key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    return OpenAI(base_url=OPENROUTER_BASE_URL, api_key=key)
