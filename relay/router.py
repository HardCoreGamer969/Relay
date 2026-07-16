"""Model router: named spend policies across brain/hands roles (v1 + v2).

Relay binds models by *route* (economy | balanced | premium), then may bump
the brain one tier on replan — until the cost envelope freezes bumps at 80%.
Explicit ``RELAY_BRAIN_MODEL`` / config role models always beat the router.

Router v2 adds **route contracts** (``.relay/route.json`` schema v2): call-class
maps, phases, provider micro-hints, fitness-gated hands, shadow logging, and
spend explain helpers. Explicit env/config pins still win.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import relay.store as store
from relay.config import DEFAULT_BRAIN_MODEL, DEFAULT_HANDS_MODEL, ModelConfig, resolve_role_field

ROUTES = ("economy", "balanced", "premium")
DEFAULT_ROUTE = "balanced"

CALL_CLASSES = (
    "plan",
    "replan",
    "review",
    "answer",
    "skeptic",
    "hands_step",
    "compact",
)
PHASES = ("planning", "execution", "review")
PROVIDER_SORTS = ("default", "floor", "nitro")

CONTRACT_SCHEMA_VERSION = 2

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
_HANDS_TIERS = (
    ROUTE_MODELS["economy"]["hands"],
    ROUTE_MODELS["balanced"]["hands"],
    ROUTE_MODELS["premium"]["hands"],
)

BUMP_FREEZE_FRACTION = 0.80
EVENT_ROUTE_CHANGE = "route_change"

# Default call-class → role mapping (which role owns the call).
_CLASS_ROLE: dict[str, str] = {
    "plan": "brain",
    "replan": "brain",
    "review": "brain",
    "answer": "brain",
    "skeptic": "brain",
    "hands_step": "hands",
    "compact": "brain",
}


@dataclass(frozen=True)
class RouteProfile:
    """v1 profile: named brain/hands pair (still the public bind surface)."""

    name: str
    brain: str
    hands: str


@dataclass(frozen=True)
class RouteContract:
    """v2 policy artifact: route name + optional call-class / phase / micro prefs."""

    name: str
    brain: str
    hands: str
    schema_version: int = CONTRACT_SCHEMA_VERSION
    call_class: dict[str, str] = field(default_factory=dict)
    phases: dict[str, dict[str, str]] = field(default_factory=dict)
    provider_sort: str = "default"
    max_price: float | None = None
    bump_freeze_fraction: float = BUMP_FREEZE_FRACTION
    hands_bump_on_parse_failures: int = 3
    hands_bump_steps: int = 2
    pins: dict[str, str] = field(default_factory=dict)
    shadow: dict[str, Any] = field(default_factory=dict)
    orchestra_hands_class: str = "hands_step"
    unknown_keys: tuple[str, ...] = ()

    def as_profile(self) -> RouteProfile:
        return RouteProfile(name=self.name, brain=self.brain, hands=self.hands)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "route": self.name,
            "brain": self.brain,
            "hands": self.hands,
            "call_class": dict(self.call_class),
            "phases": {k: dict(v) for k, v in self.phases.items()},
            "provider_sort": self.provider_sort,
            "bump_freeze_fraction": self.bump_freeze_fraction,
            "hands_bump_on_parse_failures": self.hands_bump_on_parse_failures,
            "hands_bump_steps": self.hands_bump_steps,
            "pins": dict(self.pins),
            "shadow": dict(self.shadow),
            "orchestra_hands_class": self.orchestra_hands_class,
        }
        if self.max_price is not None:
            data["max_price"] = self.max_price
        return data


@dataclass
class RouteChange:
    """One router decision (emitted for /why)."""

    reason: str  # default | replan_bump | bump_frozen | explicit_override | call_class | ...
    role: str
    from_model: str
    to_model: str
    route: str
    purpose: str | None = None
    phase: str | None = None
    provider_hint: str | None = None

    def as_payload(self) -> dict[str, Any]:
        payload = {
            "reason": self.reason,
            "role": self.role,
            "from_model": self.from_model,
            "to_model": self.to_model,
            "route": self.route,
        }
        if self.purpose:
            payload["purpose"] = self.purpose
        if self.phase:
            payload["phase"] = self.phase
        if self.provider_hint:
            payload["provider_hint"] = self.provider_hint
        return payload


def _default_call_class_map(name: str, brain: str, hands: str) -> dict[str, str]:
    """Built-in purpose → model map for a named route."""
    # Cheap classes always prefer hands slug; hot thought prefers brain.
    return {
        "plan": brain,
        "replan": brain,
        "review": brain,
        "answer": brain,
        "skeptic": hands,  # cheap critic by default
        "hands_step": hands,
        "compact": hands,
    }


def get_route(name: str) -> RouteProfile | None:
    key = str(name).strip().lower()
    models = ROUTE_MODELS.get(key)
    if models is None:
        return None
    return RouteProfile(name=key, brain=models["brain"], hands=models["hands"])


def builtin_contract(name: str) -> RouteContract | None:
    profile = get_route(name)
    if profile is None:
        return None
    return RouteContract(
        name=profile.name,
        brain=profile.brain,
        hands=profile.hands,
        call_class=_default_call_class_map(profile.name, profile.brain, profile.hands),
    )


def route_contract_path(root: str | Path) -> Path:
    return Path(root) / ".relay" / "route.json"


def parse_route_contract(data: Any, *, fallback_name: str | None = None) -> RouteContract:
    """Parse a route contract dict (or legacy name-only file). Raises ValueError."""
    if isinstance(data, str):
        name = data.strip().lower()
        contract = builtin_contract(name)
        if contract is None:
            raise ValueError(f"unknown route: {data!r}")
        return contract
    if not isinstance(data, dict):
        raise ValueError("route contract must be an object or route name string")

    known = {
        "schema_version",
        "route",
        "name",
        "brain",
        "hands",
        "call_class",
        "phases",
        "provider_sort",
        "max_price",
        "bump_freeze_fraction",
        "hands_bump_on_parse_failures",
        "hands_bump_steps",
        "pins",
        "shadow",
        "orchestra_hands_class",
    }
    unknown = tuple(sorted(k for k in data if k not in known))

    name = str(data.get("route") or data.get("name") or fallback_name or "").strip().lower()
    base = builtin_contract(name) if name in ROUTES else None
    if base is None and not name:
        name = DEFAULT_ROUTE
        base = builtin_contract(DEFAULT_ROUTE)
    if base is None:
        # Custom named contract must supply brain/hands (or we fall back to balanced).
        base = builtin_contract(DEFAULT_ROUTE)
        assert base is not None
        if not name:
            name = DEFAULT_ROUTE

    brain = str(data.get("brain") or base.brain)
    hands = str(data.get("hands") or base.hands)
    call_class = dict(base.call_class)
    raw_cc = data.get("call_class")
    if isinstance(raw_cc, dict):
        for k, v in raw_cc.items():
            key = str(k).strip().lower()
            if key in CALL_CLASSES and v:
                call_class[key] = _resolve_class_slug(str(v), brain=brain, hands=hands)

    phases: dict[str, dict[str, str]] = {}
    raw_phases = data.get("phases")
    if isinstance(raw_phases, dict):
        for phase, body in raw_phases.items():
            pkey = str(phase).strip().lower()
            if pkey not in PHASES or not isinstance(body, dict):
                continue
            entry: dict[str, str] = {}
            for rk in ("brain", "hands", "route"):
                if body.get(rk):
                    entry[rk] = str(body[rk]).strip()
            if entry:
                phases[pkey] = entry

    provider_sort = str(data.get("provider_sort") or "default").strip().lower()
    if provider_sort not in PROVIDER_SORTS:
        provider_sort = "default"

    max_price = data.get("max_price")
    try:
        max_price_f = float(max_price) if max_price is not None else None
    except (TypeError, ValueError):
        max_price_f = None

    freeze = data.get("bump_freeze_fraction", BUMP_FREEZE_FRACTION)
    try:
        freeze_f = float(freeze)
    except (TypeError, ValueError):
        freeze_f = BUMP_FREEZE_FRACTION

    try:
        parse_n = int(data.get("hands_bump_on_parse_failures", 3))
    except (TypeError, ValueError):
        parse_n = 3
    try:
        bump_steps = int(data.get("hands_bump_steps", 2))
    except (TypeError, ValueError):
        bump_steps = 2

    pins: dict[str, str] = {}
    raw_pins = data.get("pins")
    if isinstance(raw_pins, dict):
        for k, v in raw_pins.items():
            if v:
                pins[str(k).strip().lower()] = str(v).strip()

    shadow = dict(data["shadow"]) if isinstance(data.get("shadow"), dict) else {}
    orch = str(data.get("orchestra_hands_class") or "hands_step").strip().lower()
    if orch not in CALL_CLASSES:
        orch = "hands_step"

    try:
        schema_version = int(data.get("schema_version", CONTRACT_SCHEMA_VERSION))
    except (TypeError, ValueError):
        schema_version = CONTRACT_SCHEMA_VERSION

    return RouteContract(
        name=name or base.name,
        brain=brain,
        hands=hands,
        schema_version=schema_version,
        call_class=call_class,
        phases=phases,
        provider_sort=provider_sort,
        max_price=max_price_f,
        bump_freeze_fraction=freeze_f,
        hands_bump_on_parse_failures=max(1, parse_n),
        hands_bump_steps=max(1, bump_steps),
        pins=pins,
        shadow=shadow,
        orchestra_hands_class=orch,
        unknown_keys=unknown,
    )


def _resolve_class_slug(value: str, *, brain: str, hands: str) -> str:
    """Map a call-class value: route tier name, role alias, or raw slug."""
    key = value.strip().lower()
    if key in ROUTES:
        # Ambiguous role — prefer brain for hot, but caller already chose; use route brain.
        return ROUTE_MODELS[key]["brain"]
    if key == "brain":
        return brain
    if key == "hands":
        return hands
    if key in ("economy_hands", "cheap"):
        return ROUTE_MODELS["economy"]["hands"]
    if key in ("premium_brain", "hot"):
        return ROUTE_MODELS["premium"]["brain"]
    return value.strip()


def load_route_contract(root: str | Path | None) -> RouteContract | None:
    """Load ``.relay/route.json`` if present; None when missing."""
    if root is None:
        return None
    path = route_contract_path(root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return parse_route_contract(data)
    except ValueError:
        return None


def save_route_contract(root: str | Path, contract: RouteContract) -> Path:
    """Write a route contract to ``.relay/route.json`` (creates ``.relay/``)."""
    path = route_contract_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path


def resolve_route(
    override: str | None = None,
    *,
    root: str | Path | None = None,
    config: dict | None = None,
) -> RouteProfile:
    """Resolve route profile: CLI/override > repo contract > env > config > default."""
    contract = resolve_route_contract(override, root=root, config=config)
    return contract.as_profile()


def resolve_route_contract(
    override: str | None = None,
    *,
    root: str | Path | None = None,
    config: dict | None = None,
) -> RouteContract:
    """Full contract resolution with the same precedence as :func:`resolve_route`."""
    # 1. Explicit CLI / override name
    if override:
        built = builtin_contract(str(override))
        if built is not None:
            # Merge repo file overlays (call_class etc.) when override is only a name?
            # Spec: CLI beats repo for the *route name*; keep override as pure builtin.
            return built

    # 2. Repo contract
    repo = load_route_contract(root)
    if repo is not None:
        return repo

    # 3. Env
    env = os.environ.get("RELAY_ROUTE")
    if env:
        built = builtin_contract(str(env))
        if built is not None:
            return built

    # 4. Config
    config = config if config is not None else store.load_config()
    if isinstance(config, dict):
        built = builtin_contract(str(config.get("route") or ""))
        if built is not None:
            return built

    return builtin_contract(DEFAULT_ROUTE)  # type: ignore[return-value]


def _repo_route_name(root: str | Path | None) -> str | None:
    """Backward-compatible helper used by older tests/callers."""
    contract = load_route_contract(root)
    return contract.name if contract is not None else None


def role_explicitly_set(role: str, config: dict | None = None) -> bool:
    """True when env or config.json sets this role's model (beats the router)."""
    _, source = resolve_role_field(role, "model", config)
    return source in ("env", "config")


def apply_route(
    base: ModelConfig,
    route: RouteProfile | RouteContract,
    *,
    config: dict | None = None,
) -> tuple[ModelConfig, list[RouteChange]]:
    """Bind route models under explicit env/config overrides.

    Precedence: env/config role model > contract pins > non-default ``base`` slug
    (caller/test override) > route profile > built-in default.

    Returns the possibly-updated config plus route-change events for roles the
    router actually set.
    """
    config = config if config is not None else store.load_config()
    if isinstance(route, RouteContract):
        profile = route.as_profile()
        pins = route.pins
    else:
        profile = route
        pins = {}

    changes: list[RouteChange] = []
    _brain, brain_src = resolve_role_field("brain", "model", config)
    _hands, hands_src = resolve_role_field("hands", "model", config)

    new_brain = base.brain
    new_hands = base.hands

    if brain_src in ("env", "config"):
        new_brain = _brain  # explicit beats everything
    elif "brain" in pins:
        pin = pins["brain"]
        if base.brain != pin:
            changes.append(
                RouteChange(
                    reason="contract_pin",
                    role="brain",
                    from_model=base.brain,
                    to_model=pin,
                    route=profile.name,
                )
            )
        new_brain = pin
    elif brain_src == "default" and base.brain == DEFAULT_BRAIN_MODEL:
        if base.brain != profile.brain:
            changes.append(
                RouteChange(
                    reason="default",
                    role="brain",
                    from_model=base.brain,
                    to_model=profile.brain,
                    route=profile.name,
                )
            )
        new_brain = profile.brain
    # else: preserve caller-provided non-default base.brain

    if hands_src in ("env", "config"):
        new_hands = _hands
    elif "hands" in pins:
        pin = pins["hands"]
        if base.hands != pin:
            changes.append(
                RouteChange(
                    reason="contract_pin",
                    role="hands",
                    from_model=base.hands,
                    to_model=pin,
                    route=profile.name,
                )
            )
        new_hands = pin
    elif hands_src == "default" and base.hands == DEFAULT_HANDS_MODEL:
        if base.hands != profile.hands:
            changes.append(
                RouteChange(
                    reason="default",
                    role="hands",
                    from_model=base.hands,
                    to_model=profile.hands,
                    route=profile.name,
                )
            )
        new_hands = profile.hands

    return (
        replace(base, brain=new_brain, hands=new_hands),
        changes,
    )


def next_brain_tier(current: str) -> str | None:
    """One tier up on the brain ladder, or None if already at the top / unknown."""
    try:
        idx = _BRAIN_TIERS.index(current)
    except ValueError:
        return None
    if idx + 1 >= len(_BRAIN_TIERS):
        return None
    return _BRAIN_TIERS[idx + 1]


def next_hands_tier(current: str) -> str | None:
    """One tier up on the hands ladder (skip duplicate slugs across routes)."""
    try:
        idx = _HANDS_TIERS.index(current)
    except ValueError:
        # Not on ladder: bump toward premium hands.
        for slug in _HANDS_TIERS:
            if slug != current:
                return slug
        return None
    for j in range(idx + 1, len(_HANDS_TIERS)):
        if _HANDS_TIERS[j] != current:
            return _HANDS_TIERS[j]
    return None


def model_for_call_class(
    contract: RouteContract,
    purpose: str,
    *,
    phase: str | None = None,
    models: ModelConfig | None = None,
) -> str:
    """Resolve the model slug for a harness-tagged purpose (call-class)."""
    purpose = (purpose or "").strip().lower() or "plan"
    role = _CLASS_ROLE.get(purpose, "brain")

    # Phase overlay: may remap brain/hands or switch named route for this phase.
    phase_brain = None
    phase_hands = None
    if phase and phase in contract.phases:
        body = contract.phases[phase]
        if body.get("route") and body["route"] in ROUTES:
            alt = ROUTE_MODELS[body["route"]]
            phase_brain, phase_hands = alt["brain"], alt["hands"]
        if body.get("brain"):
            phase_brain = body["brain"]
        if body.get("hands"):
            phase_hands = body["hands"]

    if purpose in contract.pins:
        return contract.pins[purpose]

    # Phase role overrides beat the static call-class map (state-machine bind).
    if role == "hands" and phase_hands:
        return phase_hands
    if role == "brain" and phase_brain:
        return phase_brain

    if purpose in contract.call_class:
        slug = contract.call_class[purpose]
        # Allow tier names inside call_class that weren't expanded.
        return _resolve_class_slug(
            slug,
            brain=phase_brain or contract.brain,
            hands=phase_hands or contract.hands,
        )

    if role == "hands":
        if models is not None:
            return models.hands
        return contract.hands
    if models is not None:
        return models.brain
    return contract.brain


def provider_routing_extras(
    contract: RouteContract,
    *,
    provider: str = "openrouter",
    purpose: str | None = None,
) -> dict[str, Any]:
    """Build OpenRouter provider micro-routing extras (no-op for other providers).

    Does not change the model slug except optional ``:floor`` suffix is applied
    by :func:`apply_provider_suffix` separately when sort is floor.
    """
    if provider != "openrouter":
        return {}
    sort = contract.provider_sort
    if sort == "default":
        extras: dict[str, Any] = {}
    elif sort == "floor":
        extras = {"provider": {"sort": "price"}}
        if contract.max_price is not None:
            extras["provider"]["max_price"] = contract.max_price
    elif sort == "nitro":
        extras = {"provider": {"sort": "throughput"}}
    else:
        extras = {}
    return extras


def apply_provider_suffix(model: str, contract: RouteContract, *, provider: str = "openrouter") -> str:
    """Append ``:floor`` when contract asks for floor sort on OpenRouter."""
    if provider != "openrouter" or contract.provider_sort != "floor":
        return model
    if model.endswith(":floor") or model.endswith(":nitro"):
        return model
    return f"{model}:floor"


def format_broker_line(
    router: "ModelRouter",
    envelope: Any | None = None,
    ledger: Any | None = None,
    *,
    orchestra_workers: int = 1,
) -> str:
    """Spend-broker status line for CLI preflight / TUI."""
    c = router.contract
    parts = [f"route={c.name}"]
    parts.append(f"brain={router.bound_brain or c.brain}")
    parts.append(f"hands={router.bound_hands or c.hands}")
    if router.last_bump is not None:
        parts.append(
            f"last={router.last_bump.from_model}→{router.last_bump.to_model}"
            f"({router.last_bump.reason})"
        )
    freeze_pct = int(round(c.bump_freeze_fraction * 100))
    parts.append(f"freeze@{freeze_pct}%{'*' if router.bumps_frozen else ''}")
    if envelope is not None and getattr(envelope, "max_cost", None) is not None:
        remaining = None
        if hasattr(envelope, "remaining_cost"):
            remaining = envelope.remaining_cost(ledger)
        from relay.envelope import format_usd

        parts.append(f"remaining {format_usd(remaining)}")
    if orchestra_workers > 1:
        parts.append(f"orchestra={orchestra_workers}×{c.orchestra_hands_class}")
    if c.provider_sort != "default":
        parts.append(f"provider={c.provider_sort}")
    return " · ".join(parts)


def estimate_counterfactual_cost(
    ledger: Any,
    *,
    baseline_route: str = "premium",
    catalog: Any | None = None,
    provider: str = "openrouter",
) -> dict[str, Any]:
    """Estimate what the same token counts would cost on ``baseline_route``.

    Uses catalog $/1M rates × actual prompt/completion tokens. No model calls.
    Returns dict with actual, counterfactual, saved, approx flag, or unknown.
    """
    from relay.envelope import format_usd

    actual = ledger.total_cost() if ledger is not None else None
    route = get_route(baseline_route)
    if route is None or ledger is None:
        return {
            "actual": actual,
            "counterfactual": None,
            "saved": None,
            "baseline": baseline_route,
            "approx": True,
            "unknown": True,
            "lines": [
                f"actual {format_usd(actual)} · {baseline_route}-counterfactual unknown"
            ],
        }

    if catalog is None:
        try:
            from relay.catalog import get_catalog

            catalog = get_catalog()
        except Exception:  # noqa: BLE001 — pricing must never crash a receipt
            catalog = None

    # Fixed fallback rates (USD per 1M tokens) when catalog misses — labeled approx.
    _FALLBACK = {
        ROUTE_MODELS["economy"]["brain"]: (0.80, 4.00),
        ROUTE_MODELS["economy"]["hands"]: (0.80, 4.00),
        ROUTE_MODELS["balanced"]["brain"]: (3.00, 15.00),
        ROUTE_MODELS["balanced"]["hands"]: (0.80, 4.00),
        ROUTE_MODELS["premium"]["brain"]: (15.00, 75.00),
        ROUTE_MODELS["premium"]["hands"]: (3.00, 15.00),
    }

    def _rates(slug: str) -> tuple[float, float] | None:
        if catalog is not None:
            cost = catalog.cost(provider, slug)
            if cost is not None and cost.input is not None and cost.output is not None:
                return float(cost.input), float(cost.output)
        return _FALLBACK.get(slug)

    cf_total = 0.0
    priced = 0
    for record in getattr(ledger, "records", []) or []:
        role = getattr(record, "role", "brain")
        # Orchestra hands-N → hands baseline
        target = route.hands if str(role).startswith("hands") else route.brain
        rates = _rates(target)
        if rates is None:
            continue
        pin, pout = rates
        pt = int(getattr(record, "prompt_tokens", 0) or 0)
        ct = int(getattr(record, "completion_tokens", 0) or 0)
        cf_total += (pt * pin + ct * pout) / 1_000_000.0
        priced += 1

    if priced == 0:
        return {
            "actual": actual,
            "counterfactual": None,
            "saved": None,
            "baseline": baseline_route,
            "approx": True,
            "unknown": True,
            "lines": [
                f"actual {format_usd(actual)} · {baseline_route}-counterfactual unknown"
            ],
        }

    saved = None if actual is None else max(0.0, cf_total - float(actual))
    line = (
        f"actual {format_usd(actual)} · {baseline_route}-counterfactual "
        f"{format_usd(cf_total)} · saved ~{format_usd(saved)} (approx)"
    )
    return {
        "actual": actual,
        "counterfactual": cf_total,
        "saved": saved,
        "baseline": baseline_route,
        "approx": True,
        "unknown": False,
        "lines": [line],
    }


def explain_spend(
    events: Iterable[Any],
    ledger: Any | None = None,
    *,
    counterfactual: dict[str, Any] | None = None,
) -> str:
    """Markdown spend timeline from route_change events + ledger (zero tokens)."""
    lines = ["## Spend", ""]
    route_lines: list[str] = []
    for raw in events or []:
        if hasattr(raw, "kind"):
            kind = raw.kind
            message = raw.message
            payload = getattr(raw, "payload", {}) or {}
        elif isinstance(raw, dict):
            kind = raw.get("kind", "")
            message = raw.get("message", "")
            payload = raw.get("payload") or {}
        else:
            continue
        if kind != EVENT_ROUTE_CHANGE:
            continue
        purpose = payload.get("purpose") or ""
        extra = f" purpose={purpose}" if purpose else ""
        route_lines.append(f"- {message}{extra}")

    if route_lines:
        lines.append("### Route decisions")
        lines.extend(route_lines)
        lines.append("")
    else:
        lines.append("(no route_change events)")
        lines.append("")

    if ledger is not None:
        lines.append("### By role / purpose")
        by_purpose: dict[str, float] = {}
        for record in ledger.records:
            key = getattr(record, "purpose", None) or record.role
            if record.cost_usd is not None:
                by_purpose[key] = by_purpose.get(key, 0.0) + float(record.cost_usd)
        if by_purpose:
            for key, amount in sorted(by_purpose.items()):
                lines.append(f"- {key}: ${amount:.6f}")
        else:
            lines.append("- (no priced calls)")
        total = ledger.total_cost()
        lines.append(f"- TOTAL: {'unknown' if total is None else f'${total:.6f}'}")
        lines.append("")

    if counterfactual and counterfactual.get("lines"):
        lines.append("### Counterfactual")
        for line in counterfactual["lines"]:
            lines.append(f"- {line}")
        lines.append("")

    lines.append("_Deterministic from ledger + route_change — no new model tokens._")
    return "\n".join(lines)


def recommend_route(
    root: str | Path,
    *,
    runlog_path: Path | None = None,
) -> dict[str, Any]:
    """Suggest a route from duel scorecards, else most-used successful runlog route."""
    root = Path(root)
    evidence: list[str] = []
    duels_dir = root / ".relay" / "duels"
    best_route: str | None = None
    best_cost: float | None = None

    if duels_dir.is_dir():
        for path in sorted(duels_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for row in data.get("pairings") or []:
                status = str(row.get("status") or "")
                if status not in ("completed", "done"):
                    continue
                cost = row.get("cost_usd")
                brain = str(row.get("brain") or "")
                hands = str(row.get("hands") or "")
                guessed = _guess_route_from_pair(brain, hands)
                if guessed is None or cost is None:
                    continue
                try:
                    cost_f = float(cost)
                except (TypeError, ValueError):
                    continue
                evidence.append(
                    f"duel {path.name}: {guessed} cost=${cost_f:.4f} ({brain}/{hands})"
                )
                if best_cost is None or cost_f < best_cost:
                    best_cost = cost_f
                    best_route = guessed

    if best_route is None:
        # Fallback: runlog mode field / envelope notes — count successful routes.
        try:
            from relay.runlog import default_log_path, load_records

            path = runlog_path or default_log_path(root)
            records = load_records(path) if path.exists() else []
        except Exception:  # noqa: BLE001
            records = []
        counts: dict[str, int] = {}
        for rec in records:
            status = getattr(rec, "status", None) or (rec.get("status") if isinstance(rec, dict) else "")
            if status not in ("completed", "done"):
                continue
            # Prefer explicit route on record if present
            route_name = None
            if isinstance(rec, dict):
                route_name = rec.get("route")
                env = rec.get("envelope") or {}
                if not route_name and isinstance(env, dict):
                    route_name = env.get("route")
            else:
                route_name = getattr(rec, "route", None)
            if not route_name:
                continue
            key = str(route_name).strip().lower()
            if key not in ROUTES:
                continue
            counts[key] = counts.get(key, 0) + 1
            evidence.append(f"runlog: {key} success")
        if counts:
            best_route = max(counts, key=counts.get)  # type: ignore[arg-type]

    if best_route is None:
        best_route = DEFAULT_ROUTE
        evidence.append("fallback: default balanced (no duel/runlog evidence)")

    return {
        "route": best_route,
        "evidence": evidence,
        "cost": best_cost,
    }


def _guess_route_from_pair(brain: str, hands: str) -> str | None:
    for name, models in ROUTE_MODELS.items():
        if models["brain"] == brain and models["hands"] == hands:
            return name
    for name, models in ROUTE_MODELS.items():
        if models["brain"] == brain:
            return name
    return None


def shadow_log_path(root: str | Path) -> Path:
    return Path(root) / ".relay" / "shadow.jsonl"


def log_shadow_decision(
    root: str | Path,
    *,
    purpose: str,
    actual_model: str,
    shadow_model: str,
    route: str,
    dual_call: bool = False,
) -> None:
    """Append a shadow routing decision (model choice only by default)."""
    path = shadow_log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "purpose": purpose,
        "actual_model": actual_model,
        "shadow_model": shadow_model,
        "route": route,
        "dual_call": dual_call,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def resolve_shadow_enabled(override: bool | None = None) -> bool:
    if override is not None:
        return bool(override)
    raw = os.environ.get("RELAY_SHADOW_ROUTE", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


@dataclass
class ModelRouter:
    """Per-run router state: route binding + optional one-call brain bumps."""

    route: RouteProfile
    contract: RouteContract | None = None
    brain_explicit: bool = False
    hands_explicit: bool = False
    bumps_frozen: bool = False
    last_bump: RouteChange | None = None
    phase: str = "planning"
    bound_brain: str | None = None
    bound_hands: str | None = None
    # Fitness-gated hands (E9)
    parse_failures: int = 0
    hands_bump_remaining: int = 0
    fitness_hands: str | None = None
    # Shadow (E12)
    shadow_enabled: bool = False
    shadow_root: str | Path | None = None

    def __post_init__(self) -> None:
        if self.contract is None:
            built = builtin_contract(self.route.name)
            self.contract = built or RouteContract(
                name=self.route.name,
                brain=self.route.brain,
                hands=self.route.hands,
                call_class=_default_call_class_map(
                    self.route.name, self.route.brain, self.route.hands
                ),
            )

    @property
    def _contract(self) -> RouteContract:
        assert self.contract is not None
        return self.contract

    @classmethod
    def from_resolve(
        cls,
        override: str | None = None,
        *,
        root: str | Path | None = None,
        config: dict | None = None,
        shadow: bool | None = None,
    ) -> "ModelRouter":
        config = config if config is not None else store.load_config()
        contract = resolve_route_contract(override, root=root, config=config)
        return cls(
            route=contract.as_profile(),
            contract=contract,
            brain_explicit=role_explicitly_set("brain", config),
            hands_explicit=role_explicitly_set("hands", config),
            shadow_enabled=resolve_shadow_enabled(shadow),
            shadow_root=root,
        )

    def bind(self, base: ModelConfig, *, config: dict | None = None) -> tuple[ModelConfig, list[RouteChange]]:
        """Apply static route defaults under explicit overrides."""
        cfg, changes = apply_route(base, self._contract, config=config)
        self.bound_brain = cfg.brain
        self.bound_hands = cfg.hands
        return cfg, changes

    def set_phase(self, phase: str) -> RouteChange | None:
        """Update run phase; emit a route_change when phase models differ."""
        phase = (phase or "").strip().lower()
        if phase not in PHASES:
            return None
        old = self.phase
        self.phase = phase
        if old == phase:
            return None
        # Surface phase transition even if models unchanged (harness visibility).
        return RouteChange(
            reason="phase",
            role="brain",
            from_model=self.bound_brain or self._contract.brain,
            to_model=self.bound_brain or self._contract.brain,
            route=self.route.name,
            phase=phase,
        )

    def models_for_purpose(
        self,
        models: ModelConfig,
        purpose: str,
        *,
        role: str | None = None,
    ) -> tuple[ModelConfig, RouteChange | None]:
        """Return a call-scoped ModelConfig with the call-class model applied."""
        purpose = (purpose or "").strip().lower()
        resolved_role = role or _CLASS_ROLE.get(purpose, "brain")
        contract = self._contract
        # Orchestra hands-N still uses hands call-class.
        if str(resolved_role).startswith("hands"):
            resolved_role = "hands"
            if purpose not in CALL_CLASSES:
                purpose = contract.orchestra_hands_class

        target = model_for_call_class(
            contract, purpose, phase=self.phase, models=models
        )

        # Fitness bump overrides hands_step when active.
        if resolved_role == "hands" and self.fitness_hands and self.hands_bump_remaining > 0:
            target = self.fitness_hands

        # Explicit role env still wins for that role.
        if resolved_role == "brain" and self.brain_explicit:
            return models, RouteChange(
                reason="explicit_override",
                role="brain",
                from_model=models.brain,
                to_model=models.brain,
                route=self.route.name,
                purpose=purpose,
                phase=self.phase,
            )
        if resolved_role == "hands" and self.hands_explicit:
            return models, RouteChange(
                reason="explicit_override",
                role="hands",
                from_model=models.hands,
                to_model=models.hands,
                route=self.route.name,
                purpose=purpose,
                phase=self.phase,
            )

        # Skeptic env pin
        if purpose == "skeptic":
            env_skeptic = os.environ.get("RELAY_SKEPTIC_MODEL", "").strip()
            if env_skeptic:
                target = env_skeptic

        current = models.brain if resolved_role == "brain" else models.hands
        change = None
        if target != current:
            change = RouteChange(
                reason="call_class",
                role=resolved_role,
                from_model=current,
                to_model=target,
                route=self.route.name,
                purpose=purpose,
                phase=self.phase,
                provider_hint=contract.provider_sort
                if contract.provider_sort != "default"
                else None,
            )
            if resolved_role == "brain":
                models = replace(models, brain=target)
            else:
                models = replace(models, hands=target)

        if self.shadow_enabled and self.shadow_root is not None:
            eco = builtin_contract("economy") or contract
            shadow_model = model_for_call_class(eco, purpose, phase=self.phase)
            cheaper = (contract.shadow or {}).get("cheaper_class")
            if cheaper:
                shadow_model = model_for_call_class(
                    contract, str(cheaper), phase=self.phase, models=models
                )
            if shadow_model != target:
                try:
                    log_shadow_decision(
                        self.shadow_root,
                        purpose=purpose,
                        actual_model=target,
                        shadow_model=shadow_model,
                        route=self.route.name,
                        dual_call=bool((contract.shadow or {}).get("dual_call")),
                    )
                except OSError:
                    pass

        return models, change

    def note_parse_failure(self, models: ModelConfig) -> RouteChange | None:
        """Fitness gate: accumulate parse failures; bump hands when threshold hit."""
        if self.hands_explicit:
            return None
        self.parse_failures += 1
        threshold = self._contract.hands_bump_on_parse_failures
        if self.parse_failures < threshold:
            return None
        if self.bumps_frozen:
            return RouteChange(
                reason="bump_frozen",
                role="hands",
                from_model=models.hands,
                to_model=models.hands,
                route=self.route.name,
                purpose="hands_step",
            )
        bumped = next_hands_tier(self.fitness_hands or models.hands)
        if bumped is None or bumped == (self.fitness_hands or models.hands):
            return None
        self.fitness_hands = bumped
        self.hands_bump_remaining = self._contract.hands_bump_steps
        self.parse_failures = 0
        return RouteChange(
            reason="fitness_bump",
            role="hands",
            from_model=models.hands,
            to_model=bumped,
            route=self.route.name,
            purpose="hands_step",
        )

    def note_hands_success(self) -> RouteChange | None:
        """Decrement fitness bump window; clear when exhausted."""
        if self.hands_bump_remaining <= 0 or self.fitness_hands is None:
            return None
        self.hands_bump_remaining -= 1
        if self.hands_bump_remaining > 0:
            return None
        old = self.fitness_hands
        self.fitness_hands = None
        return RouteChange(
            reason="fitness_decay",
            role="hands",
            from_model=old,
            to_model=self.bound_hands or self._contract.hands,
            route=self.route.name,
            purpose="hands_step",
        )

    def envelope_freeze(self, envelope: Any | None, ledger: Any | None) -> bool:
        """Freeze bumps at ≥ freeze fraction of the cost envelope (when a ceiling exists)."""
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
        freeze_at = float(max_cost) * float(self._contract.bump_freeze_fraction)
        if float(chargeable) >= freeze_at:
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

        Explicit brain overrides never bump. Freezes at envelope freeze fraction.
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
                purpose="replan",
                phase=self.phase,
            )
            return models, change

        if self.envelope_freeze(envelope, ledger):
            change = RouteChange(
                reason="bump_frozen",
                role="brain",
                from_model=models.brain,
                to_model=models.brain,
                route=self.route.name,
                purpose="replan",
                phase=self.phase,
            )
            self.last_bump = change
            return models, change

        bumped = next_brain_tier(models.brain)
        if bumped is None:
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
            purpose="replan",
            phase=self.phase,
        )
        self.last_bump = change
        return replace(models, brain=bumped), change

    def skeptic_models(self, models: ModelConfig) -> tuple[ModelConfig, RouteChange | None]:
        """Force skeptic onto the cheap call-class (E6)."""
        return self.models_for_purpose(models, "skeptic")
