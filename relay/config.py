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
import re
from dataclasses import dataclass

from dotenv import find_dotenv, load_dotenv

import relay.store as store
from relay.providers import DEFAULT_PROVIDER, known_providers

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


# --- the global step ceiling (v0.0.21) --------------------------------------

# The autonomous loop's one comprehensible top-level safety net: the total number
# of executor model-calls a run may spend. 50 is generous -- comfortably above any
# healthy run (real successful runs use far fewer) but well below the grind a
# stuck step could otherwise reach -- so it should never fire in normal use, only
# backstop a runaway. It is raisable (or disable-able) so a genuinely large
# project is never walled off.
DEFAULT_MAX_TOTAL_STEPS = 50
# Values that mean "no ceiling" (unbounded): the user opting OUT of the safety net.
_CEILING_DISABLE = frozenset({"0", "off", "none", "no", "false", "disable", "disabled", "unbounded"})
_UNSET = object()  # "this source had nothing usable" -- fall through to the next


def _parse_ceiling(value: object) -> object:
    """One ceiling source -> a positive ``int``, ``None`` (disabled), or ``_UNSET``.

    ``_UNSET`` means absent or unparseable, so resolution falls through to the
    next source rather than crashing on a typo.
    """
    if value is None or isinstance(value, bool):  # bool is an int subclass -- reject it
        return _UNSET
    if isinstance(value, int):
        return value if value > 0 else (None if value == 0 else _UNSET)
    text = str(value).strip().lower()
    if not text:
        return _UNSET
    if text in _CEILING_DISABLE:
        return None
    try:
        n = int(text)
    except ValueError:
        return _UNSET
    return n if n > 0 else (None if n == 0 else _UNSET)


def resolve_max_total_steps(
    override: object = None, config: dict | None = None
) -> int | None:
    """Resolve the global executor-step ceiling: **override > env > config > default**.

    ``override`` is the per-run CLI flag (``--max-total-steps``); the env knob is
    ``RELAY_MAX_TOTAL_STEPS``; ``config.json``'s top-level ``max_total_steps`` is
    the next rung; the built-in default is :data:`DEFAULT_MAX_TOTAL_STEPS` (50).
    Returns a positive ceiling, or ``None`` for "unbounded" when explicitly
    disabled (``0`` / ``off`` / ``none``). Invalid values fall through to the next
    source. Precedence mirrors the rest of Relay (env wins over saved config).
    """
    for candidate in (override, os.environ.get("RELAY_MAX_TOTAL_STEPS")):
        parsed = _parse_ceiling(candidate)
        if parsed is not _UNSET:
            return parsed  # type: ignore[return-value]
    config = config if config is not None else store.load_config()
    cfg_val = config.get("max_total_steps") if isinstance(config, dict) else None
    parsed = _parse_ceiling(cfg_val)
    if parsed is not _UNSET:
        return parsed  # type: ignore[return-value]
    return DEFAULT_MAX_TOTAL_STEPS


def assumption_summary(level: str) -> str:
    """A short, plain-language description of what the dial does at ``level``.

    DERIVED from the real directive (:data:`_ASSUMPTION_DIRECTIVES` via
    :func:`assumption_directive`) -- the single source of truth -- so it can never
    drift from the dial's actual behavior. Each directive reads
    ``ASSUMPTION DIAL = <level> (<label>): <clause>; ...``; this returns
    ``"<label> -- <first clause>"`` (e.g. ``"balanced -- assume technical/decidable
    details"``), grounded in the same text the brain is actually given.
    """
    directive = assumption_directive(level)
    head, _, rest = directive.partition("): ")
    label = head[head.rfind("(") + 1:] if "(" in head else ""
    # The first clause: up to the first sentence/clause boundary in the directive.
    match = re.search(r"\. |; | -- ", rest)
    first = (rest[:match.start()] if match else rest).strip().rstrip(".")
    return f"{label} -- {first}" if label else first

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


_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def _env_bool(name: str) -> bool:
    """Read a boolean env flag (``1`` / ``true`` / ``yes`` / ``on`` → True)."""
    return str(os.environ.get(name, "")).strip().lower() in _TRUE


def _env_name(role: str, field: str) -> str:
    """The env var for a role field, e.g. (``brain``, ``model``) -> ``RELAY_BRAIN_MODEL``."""
    return f"RELAY_{role.upper()}_{field.upper()}"


def _config_role_field(config: dict, role: str, field: str):
    """The value of ``config["roles"][role][field]`` or ``None`` (defensive)."""
    roles = config.get("roles") if isinstance(config, dict) else None
    role_cfg = roles.get(role) if isinstance(roles, dict) else None
    if isinstance(role_cfg, dict):
        value = role_cfg.get(field)
        if value is not None:
            return value
    return None


def _default_for(role: str, field: str):
    if field == "provider":
        return DEFAULT_PROVIDER
    if field == "thinking":
        return False
    return DEFAULT_BRAIN_MODEL if role == "brain" else DEFAULT_HANDS_MODEL


def resolve_role_field(role: str, field: str, config: dict | None = None):
    """Resolve one role field with provenance: **env > config.json > default**.

    Returns ``(value, source)`` where ``source`` is ``"env"`` / ``"config"`` /
    ``"default"``. ``field`` is ``"provider"`` / ``"model"`` / ``"thinking"``.
    A user's ``RELAY_*`` env var always wins, so the historical workflow is intact;
    config.json is the next rung; the built-in default is last.
    """
    config = config if config is not None else store.load_config()
    raw_env = os.environ.get(_env_name(role, field))

    if field == "thinking":
        if raw_env is not None and raw_env.strip() != "":
            return (raw_env.strip().lower() in _TRUE), "env"
        cfg = _config_role_field(config, role, field)
        if cfg is not None:
            return bool(cfg), "config"
        return False, "default"

    # provider / model: a non-empty env var wins.
    if raw_env:
        return raw_env, "env"
    cfg = _config_role_field(config, role, field)
    if cfg is not None:
        return str(cfg), "config"
    return _default_for(role, field), "default"


def env_override_for(role: str, field: str = "model", config: dict | None = None) -> str | None:
    """The ``RELAY_*`` env var NAME silently shadowing a role's saved selection, or None.

    Returns the variable name (e.g. ``RELAY_BRAIN_MODEL``) when ``field`` resolves
    from the **environment** AND ``config.json`` holds a value for it -- i.e. a
    saved selection the env var is overriding. This only *reports* the shadow so a
    surface (the TUI) can give honest feedback after a save; it never alters the
    env > config > default resolution (which is intentional and unchanged).
    """
    config = config if config is not None else store.load_config()
    _, source = resolve_role_field(role, field, config)
    if source != "env":
        return None
    if _config_role_field(config, role, field) is None:
        return None
    return _env_name(role, field)


def default_config() -> dict:
    """A fresh, fully-populated v1 config skeleton (for seeding ``config.json``).

    Mirrors the documented shape, including the reserved picker sockets
    (``preferences.cost_bias`` / ``recommendations_source``) -- round-tripped but
    inert (no logic reads them yet).
    """
    return {
        "version": store.CONFIG_VERSION,
        "providers": {pid: {"enabled": True} for pid in known_providers()},
        "roles": {
            "brain": {
                "provider": DEFAULT_PROVIDER, "model": DEFAULT_BRAIN_MODEL, "thinking": False,
            },
            "hands": {
                "provider": DEFAULT_PROVIDER, "model": DEFAULT_HANDS_MODEL, "thinking": False,
            },
        },
        "preferences": {"cost_bias": "balanced"},   # reserved picker socket (inert)
        "recommendations_source": "bundled",        # reserved picker socket (inert)
    }


def describe_resolution() -> dict:
    """Resolved config for ``relay config show`` (and tests).

    Returns per-role ``{field: (value, source)}`` and per-provider key presence.
    **Never** includes a key value -- only whether one is available (env or
    auth.json). Importing the key check lazily keeps all secret access in
    :mod:`relay.secrets`.
    """
    from relay.providers import resolve_provider
    from relay.secrets import resolve_key

    config = store.load_config()
    roles = {
        role: {field: resolve_role_field(role, field, config) for field in ("provider", "model", "thinking")}
        for role in ROLES
    }
    providers = {}
    for pid in known_providers():
        profile = resolve_provider(pid)
        providers[pid] = {"key_present": resolve_key(pid, profile.key_env) is not None}
    return {"roles": roles, "providers": providers}


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
    """Build a :class:`ModelConfig`, resolving each role **env > config.json > default**.

    Loads a project ``.env`` from the current working directory (:func:`load_env`),
    then resolves per role (:func:`resolve_role_field`): the model
    (``RELAY_BRAIN_MODEL`` / ``RELAY_HANDS_MODEL``), provider (``RELAY_BRAIN_PROVIDER``
    / ``RELAY_HANDS_PROVIDER``, default ``openrouter``), and thinking toggle
    (``RELAY_BRAIN_THINKING`` / ``RELAY_HANDS_THINKING``, default off). A user's env
    var still wins over the global ``config.json``, which wins over the built-in
    default -- so the historical env/.env workflow is unchanged, and an absent
    config.json falls through exactly as before.
    """
    load_env()
    config = store.load_config()

    def value(role: str, field: str):
        return resolve_role_field(role, field, config)[0]

    return ModelConfig(
        brain=value("brain", "model"),
        hands=value("hands", "model"),
        brain_provider=value("brain", "provider"),
        hands_provider=value("hands", "provider"),
        brain_thinking=value("brain", "thinking"),
        hands_thinking=value("hands", "thinking"),
    )
