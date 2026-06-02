"""Role → model mapping.

Relay resolves models by *role* (``brain`` = planner, ``hands`` = executor),
never by hard-coding a model into logic. Any OpenRouter model slug works, and
swapping a model is always a config/env change — never a code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# The two roles Relay knows about.
ROLES = ("brain", "hands")

# These defaults are MEANT TO BE OVERRIDDEN via RELAY_BRAIN_MODEL /
# RELAY_HANDS_MODEL; they exist only so the CLI is runnable out of the box once
# an API key is supplied. They must be *currently available* OpenRouter slugs --
# a stronger model for the brain (planner, called rarely) and a cheap one for the
# hands (executor, called per step). (The old anthropic/claude-3.7-sonnet default
# now 404s on OpenRouter, so it was replaced.)
DEFAULT_BRAIN_MODEL = "anthropic/claude-sonnet-4.5"
DEFAULT_HANDS_MODEL = "anthropic/claude-3.5-haiku"


@dataclass(frozen=True)
class ModelConfig:
    """Immutable mapping of role → OpenRouter model slug."""

    brain: str
    hands: str

    def for_role(self, role: str) -> str:
        """Resolve the model slug for ``role``, raising on unknown roles."""
        mapping = {"brain": self.brain, "hands": self.hands}
        if role not in mapping:
            raise ValueError(
                f"Unknown role {role!r}. Valid roles: {', '.join(ROLES)}."
            )
        return mapping[role]


def load_models() -> ModelConfig:
    """Build a :class:`ModelConfig` from the environment.

    Loads ``.env`` (via python-dotenv) and resolves each role from
    ``RELAY_BRAIN_MODEL`` / ``RELAY_HANDS_MODEL``, falling back to the defaults.
    """
    load_dotenv()
    return ModelConfig(
        brain=os.environ.get("RELAY_BRAIN_MODEL", DEFAULT_BRAIN_MODEL),
        hands=os.environ.get("RELAY_HANDS_MODEL", DEFAULT_HANDS_MODEL),
    )
