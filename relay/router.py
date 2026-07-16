"""Model router: named spend policies across brain/hands roles.

Relay binds models by *route* (economy | balanced | premium), then may bump
the brain one tier on replan — until the cost envelope freezes bumps at 80%.
Explicit ``RELAY_BRAIN_MODEL`` / config role models always beat the router.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import relay.store as store
from relay.config import DEFAULT_BRAIN_MODEL, DEFAULT_HANDS_MODEL, ModelConfig, resolve_role_field

ROUTES = ("economy", "balanced", "premium")
DEFAULT_ROUTE = "balanced"

# Placeholder / catalog-friendly OpenRouter slugs. Override via route config or
# explicit RELAY_*_MODEL (which beats the router entirely).
ROUTE_MODELS: dict[str, dict[str, str]] = {
    "economy": {
        "brain": "anthropic/claude-3.5-haiku",
        "hands": "anthropic/claude-3.5-haiku",
    },
    "balanced": {
        "brain": "anthropic/claude-sonnet-4.5",
        "hands": "anthropic/claude-3.5-haiku",
    },
    "premium": {
        "brain": "anthropic/claude-opus-4",
        "hands": "anthropic/claude-sonnet-4.5",
    },
}

# Brain tier ladder for mid-run bumps (one step up on replan).
_BRAIN_TIERS = (
    ROUTE_MODELS["economy"]["brain"],
    ROUTE_MODELS["balanced"]["brain"],
    ROUTE_MODELS["premium"]["brain"],
)

BUMP_FREEZE_FRACTION = 0.80
EVENT_ROUTE_CHANGE = "route_change"


@dataclass(frozen=True)
class RouteProfile:
    name: str
    brain: str
    hands: str


@dataclass
class RouteChange:
    """One router decision (emitted for /why)."""

    reason: str  # default | replan_bump | bump_frozen | explicit_override
    role: str
    from_model: str
    to_model: str
    route: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "role": self.role,
            "from_model": self.from_model,
            "to_model": self.to_model,
            "route": self.route,
        }


def get_route(name: str) -> RouteProfile | None:
    key = str(name).strip().lower()
    models = ROUTE_MODELS.get(key)
    if models is None:
        return None
    return RouteProfile(name=key, brain=models["brain"], hands=models["hands"])


def resolve_route(
    override: str | None = None,
    *,
    root: str | Path | None = None,
    config: dict | None = None,
) -> RouteProfile:
    """Resolve route: CLI/override > repo ``.relay/route.json`` > env > config > default."""
    for candidate in (
        override,
        _repo_route_name(root) if root is not None else None,
        os.environ.get("RELAY_ROUTE"),
    ):
        if not candidate:
            continue
        profile = get_route(str(candidate))
        if profile is not None:
            return profile
    config = config if config is not None else store.load_config()
    if isinstance(config, dict):
        profile = get_route(str(config.get("route") or ""))
        if profile is not None:
            return profile
    return get_route(DEFAULT_ROUTE)  # type: ignore[return-value]


def _repo_route_name(root: str | Path | None) -> str | None:
    if root is None:
        return None
    path = Path(root) / ".relay" / "route.json"
    if not path.exists():
        return None
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        return data.get("route") or data.get("name")
    if isinstance(data, str):
        return data
    return None


def role_explicitly_set(role: str, config: dict | None = None) -> bool:
    """True when env or config.json sets this role's model (beats the router)."""
    _, source = resolve_role_field(role, "model", config)
    return source in ("env", "config")


def apply_route(
    base: ModelConfig,
    route: RouteProfile,
    *,
    config: dict | None = None,
) -> tuple[ModelConfig, list[RouteChange]]:
    """Bind route models under explicit env/config overrides.

    Precedence: env/config role model > non-default ``base`` slug (caller/test
    override) > route profile > built-in default.

    Returns the possibly-updated config plus route-change events for roles the
    router actually set.
    """
    config = config if config is not None else store.load_config()
    changes: list[RouteChange] = []
    _brain, brain_src = resolve_role_field("brain", "model", config)
    _hands, hands_src = resolve_role_field("hands", "model", config)

    new_brain = base.brain
    new_hands = base.hands

    if brain_src in ("env", "config"):
        new_brain = _brain  # explicit beats everything
    elif brain_src == "default" and base.brain == DEFAULT_BRAIN_MODEL:
        if base.brain != route.brain:
            changes.append(
                RouteChange(
                    reason="default",
                    role="brain",
                    from_model=base.brain,
                    to_model=route.brain,
                    route=route.name,
                )
            )
        new_brain = route.brain
    # else: preserve caller-provided non-default base.brain

    if hands_src in ("env", "config"):
        new_hands = _hands
    elif hands_src == "default" and base.hands == DEFAULT_HANDS_MODEL:
        if base.hands != route.hands:
            changes.append(
                RouteChange(
                    reason="default",
                    role="hands",
                    from_model=base.hands,
                    to_model=route.hands,
                    route=route.name,
                )
            )
        new_hands = route.hands

    return (
        replace(base, brain=new_brain, hands=new_hands),
        changes,
    )


def next_brain_tier(current: str) -> str | None:
    """One tier up on the brain ladder, or None if already at the top / unknown."""
    try:
        idx = _BRAIN_TIERS.index(current)
    except ValueError:
        # Not on the ladder: treat route name mapping — try to bump via route order.
        return None
    if idx + 1 >= len(_BRAIN_TIERS):
        return None
    return _BRAIN_TIERS[idx + 1]


@dataclass
class ModelRouter:
    """Per-run router state: route binding + optional one-call brain bumps."""

    route: RouteProfile
    brain_explicit: bool = False
    hands_explicit: bool = False
    bumps_frozen: bool = False
    last_bump: RouteChange | None = None

    @classmethod
    def from_resolve(
        cls,
        override: str | None = None,
        *,
        root: str | Path | None = None,
        config: dict | None = None,
    ) -> "ModelRouter":
        config = config if config is not None else store.load_config()
        route = resolve_route(override, root=root, config=config)
        return cls(
            route=route,
            brain_explicit=role_explicitly_set("brain", config),
            hands_explicit=role_explicitly_set("hands", config),
        )

    def bind(self, base: ModelConfig, *, config: dict | None = None) -> tuple[ModelConfig, list[RouteChange]]:
        """Apply static route defaults under explicit overrides."""
        return apply_route(base, self.route, config=config)

    def envelope_freeze(self, envelope: Any | None, ledger: Any | None) -> bool:
        """Freeze bumps at ≥80% of the cost envelope (when a ceiling exists)."""
        if envelope is None:
            return self.bumps_frozen
        max_cost = getattr(envelope, "max_cost", None)
        if max_cost is None or max_cost <= 0:
            return self.bumps_frozen
        chargeable = None
        if hasattr(envelope, "chargeable_cost"):
            chargeable = envelope.chargeable_cost(ledger)
        if chargeable is None:
            return self.bumps_frozen
        if float(chargeable) >= float(max_cost) * BUMP_FREEZE_FRACTION:
            self.bumps_frozen = True
        return self.bumps_frozen

    def models_for_replan(
        self,
        models: ModelConfig,
        *,
        envelope: Any | None = None,
        ledger: Any | None = None,
    ) -> tuple[ModelConfig, RouteChange | None]:
        """Optionally bump brain one tier for a single replan call.

        Explicit brain overrides never bump. Freezes at 80% cost envelope.
        Returns ``(models_for_call, change_or_none)``; bump is call-scoped
        (caller uses the returned config only for that call).
        """
        self.last_bump = None
        if self.brain_explicit:
            change = RouteChange(
                reason="explicit_override",
                role="brain",
                from_model=models.brain,
                to_model=models.brain,
                route=self.route.name,
            )
            return models, change

        if self.envelope_freeze(envelope, ledger):
            change = RouteChange(
                reason="bump_frozen",
                role="brain",
                from_model=models.brain,
                to_model=models.brain,
                route=self.route.name,
            )
            self.last_bump = change
            return models, change

        bumped = next_brain_tier(models.brain)
        if bumped is None:
            # Fall back: bump via route ladder (economy→balanced→premium brain).
            route_names = list(ROUTES)
            try:
                ridx = route_names.index(self.route.name)
            except ValueError:
                return models, None
            if ridx + 1 >= len(route_names):
                return models, None
            bumped = ROUTE_MODELS[route_names[ridx + 1]]["brain"]

        if bumped == models.brain:
            return models, None

        change = RouteChange(
            reason="replan_bump",
            role="brain",
            from_model=models.brain,
            to_model=bumped,
            route=self.route.name,
        )
        self.last_bump = change
        return replace(models, brain=bumped), change
