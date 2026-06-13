"""Role → model mapping, plus the assumption dial.

Relay resolves models by *role* (``brain`` = planner, ``hands`` = executor),
never by hard-coding a model into logic. Any OpenRouter model slug works, and
swapping a model is always a config/env change — never a code change.

The **assumption dial** (``RELAY_ASSUMPTION_LEVEL``) is a user-owned setting that
biases how much the brain assumes vs. asks — across BOTH the planning
conversation and the autonomous loop's ``answer_or_escalate``. It is a single
spine threaded everywhere the brain makes an assume-vs-ask decision.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import find_dotenv, load_dotenv

from relay.providers import DEFAULT_PROVIDER

# The two roles Relay knows about.
ROLES = ("brain", "hands")

# --- the assumption dial ----------------------------------------------------

# Valid dial values. "1".."5" are the *user* setting the assume-vs-ask threshold
# (1 = assume almost everything, 5 = follow the letter / ask freely); "auto"
# hands the threshold back to the brain (normal-mode coding-agent judgment).
# "auto" is its own mode, NOT a numeric midpoint.
ASSUMPTION_LEVELS = ("1", "2", "3", "4", "5", "auto")
DEFAULT_ASSUMPTION_LEVEL = "auto"


def resolve_assumption_level(override: str | int | None = None) -> str:
    """Resolve the assumption dial: ``override`` -> env -> default ``auto``.

    Returns a canonical value in :data:`ASSUMPTION_LEVELS`. Invalid values fall
    through to the next source (never crashes).
    """
    for candidate in (override, os.environ.get("RELAY_ASSUMPTION_LEVEL")):
        if candidate is None:
            continue
        value = str(candidate).strip().lower()
        if value in ASSUMPTION_LEVELS:
            return value
    return DEFAULT_ASSUMPTION_LEVEL


# Per-level directive embedded in every brain prompt that makes an assume-vs-ask
# decision. The literal "ASSUMPTION DIAL = <level>" marker is stable so callers
# (and tests) can verify the dial actually reached the prompt.
_ASSUMPTION_DIRECTIVES = {
    "1": (
        "ASSUMPTION DIAL = 1 (super loose): assume almost everything and act on "
        "intent. Ask/escalate almost nothing -- only a true blocking impossibility. "
        "The user has chosen 'build it, I'll react to the result', which deliberately "
        "trades pre-execution oversight for correction-after-seeing. Do NOT 'protect' "
        "them by asking anyway -- honor the setting."
    ),
    "2": (
        "ASSUMPTION DIAL = 2 (loose): assume most things and act on intent; ask only "
        "the rare high-stakes, genuinely-undetermined product decision."
    ),
    "3": (
        "ASSUMPTION DIAL = 3 (balanced): assume technical/decidable details; ask "
        "genuine product decisions that are consequential and undetermined."
    ),
    "4": (
        "ASSUMPTION DIAL = 4 (cautious): assume only clearly-decidable technical "
        "details; when a choice is consequential or even mildly undetermined, ask."
    ),
    "5": (
        "ASSUMPTION DIAL = 5 (exact letter): assume almost nothing; follow the "
        "instruction literally; ask whenever a choice is genuinely undetermined. "
        "STILL surface genuine impossibilities or contradictions (e.g. a request that "
        "conflicts with the project) and confirm rather than complying blindly."
    ),
    "auto": (
        "ASSUMPTION DIAL = auto (normal mode): you decide per-question whether to "
        "assume or ask, like a careful coding agent -- assume technical/decidable "
        "details, ask only genuine product decisions; lean to ask when truly unsure."
    ),
}


def assumption_directive(level: str) -> str:
    """The dial directive for ``level`` (defaults to ``auto`` for unknown values)."""
    return _ASSUMPTION_DIRECTIVES.get(level, _ASSUMPTION_DIRECTIVES["auto"])

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
    """Immutable per-role mapping of provider + model slug (+ thinking toggle).

    A role names BOTH its provider and its model id. The provider/thinking fields
    default so the historical ``ModelConfig(brain=..., hands=...)`` construction --
    and every caller of it -- keeps working unchanged: both roles default to
    OpenRouter with thinking off.
    """

    brain: str
    hands: str
    brain_provider: str = DEFAULT_PROVIDER
    hands_provider: str = DEFAULT_PROVIDER
    brain_thinking: bool = False
    hands_thinking: bool = False

    def for_role(self, role: str) -> str:
        """Resolve the model slug for ``role``, raising on unknown roles."""
        return self._pick(role, {"brain": self.brain, "hands": self.hands})

    def provider_for_role(self, role: str) -> str:
        """Resolve the provider id for ``role`` (e.g. ``"openrouter"`` / ``"deepseek"``)."""
        return self._pick(role, {"brain": self.brain_provider, "hands": self.hands_provider})

    def thinking_for_role(self, role: str) -> bool:
        """Whether thinking mode is enabled for ``role`` (default off)."""
        return self._pick(role, {"brain": self.brain_thinking, "hands": self.hands_thinking})

    @staticmethod
    def _pick(role: str, mapping: dict):
        if role not in mapping:
            raise ValueError(
                f"Unknown role {role!r}. Valid roles: {', '.join(ROLES)}."
            )
        return mapping[role]


def _env_bool(name: str) -> bool:
    """Read a boolean env flag (``1`` / ``true`` / ``yes`` / ``on`` → True)."""
    return str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on")


def load_env() -> str:
    """Load a ``.env`` from the CURRENT WORKING DIRECTORY (walking up), and return
    the path loaded (``""`` when none was found).

    This is the fix for the silent-config bug: a bare ``load_dotenv()`` (and
    ``find_dotenv()``) default to ``usecwd=False``, which resolves the search
    relative to the *caller module's file* -- under a global/editable install that
    is Relay's own install tree, NOT the user's project, so a project ``.env`` was
    silently ignored. ``usecwd=True`` keys the search off ``os.getcwd()`` (the
    directory the user ran ``relay`` in), giving the natural "nearest ``.env`` up
    the tree" behavior regardless of where Relay is installed.

    Precedence is conventional and deliberate: real process environment variables
    are NOT overridden (``override=False``), so a session var the user exports still
    wins over the file. An absent ``.env`` is harmless -- no error, no warning.
    """
    path = find_dotenv(usecwd=True)
    if path:
        load_dotenv(path)
    return path


def load_models() -> ModelConfig:
    """Build a :class:`ModelConfig` from the environment.

    Loads a project ``.env`` from the current working directory (:func:`load_env`,
    cwd-based so it works under any install location) and resolves, per role: the
    model from ``RELAY_BRAIN_MODEL`` / ``RELAY_HANDS_MODEL``, the provider from
    ``RELAY_BRAIN_PROVIDER`` / ``RELAY_HANDS_PROVIDER`` (default ``openrouter``),
    and the thinking toggle from ``RELAY_BRAIN_THINKING`` / ``RELAY_HANDS_THINKING``
    (default off). Provider/thinking defaults keep the OpenRouter behavior intact.
    """
    load_env()
    return ModelConfig(
        brain=os.environ.get("RELAY_BRAIN_MODEL", DEFAULT_BRAIN_MODEL),
        hands=os.environ.get("RELAY_HANDS_MODEL", DEFAULT_HANDS_MODEL),
        brain_provider=os.environ.get("RELAY_BRAIN_PROVIDER", DEFAULT_PROVIDER),
        hands_provider=os.environ.get("RELAY_HANDS_PROVIDER", DEFAULT_PROVIDER),
        brain_thinking=_env_bool("RELAY_BRAIN_THINKING"),
        hands_thinking=_env_bool("RELAY_HANDS_THINKING"),
    )
