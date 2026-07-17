"""Status rail helpers for the Relay TUI cockpit (U2)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from relay.bridge import InputState
from relay.envelope import format_usd


@dataclass(frozen=True)
class StatusSnapshot:
    """Plain facts for the status rail (and the headless ``_status_text`` mirror)."""

    mode: str
    step: str = ""
    cost: str = ""
    cost_level: str = "normal"  # normal | warn | critical | pulse
    route: str = ""
    context: str = ""
    cwd: str = ""
    models: str = ""
    queued: str = ""
    hint: str = ""

    def plain(self) -> str:
        segs = [self.mode]
        for seg in (
            self.step, self.cost, self.route, self.context, self.cwd, self.models, self.queued,
        ):
            if seg:
                segs.append(seg)
        return "  ·  ".join(segs)


def mode_word(state) -> str:
    """WORKING while generating; INTERRUPTED at interrupt; else AWAITING YOU."""
    if state is InputState.INTERRUPTED:
        return "INTERRUPTED"
    if state in (InputState.PLANNING, InputState.EXECUTING):
        return "WORKING"
    return "AWAITING YOU"


def step_segment(plan_steps: list[dict]) -> str:
    """``step N/M`` from the live plan; empty if no plan."""
    total = len(plan_steps)
    if not total:
        return ""
    active = next((i for i, s in enumerate(plan_steps) if s["status"] == "active"), None)
    if active is not None:
        n = active + 1
    else:
        n = sum(1 for s in plan_steps if s["status"] in ("done", "failed"))
    return f"step {n}/{total}"


def active_instruction(plan_steps: list[dict], *, max_len: int = 40) -> str:
    """Truncated active step instruction for the rail (or '')."""
    for step in plan_steps:
        if step.get("status") == "active":
            text = " ".join(str(step.get("instruction", "")).split())
            if len(text) > max_len:
                return text[: max_len - 1] + "…"
            return text
    return ""


def cost_segment(
    *,
    goal_cost: float,
    visible: bool,
    run_in_flight: bool,
    stopping: bool,
    envelope=None,
    ledger=None,
) -> tuple[str, str]:
    """Return ``(text, level)`` for the cost slot.

    First-class cost: with an envelope ceiling, show ``$spent / $remaining left``.
    ``level`` is ``normal`` / ``warn`` / ``critical`` based on chargeable fraction.
    """
    if not visible:
        return "", "normal"
    spent = goal_cost
    if ledger is not None:
        live = ledger.total_cost()
        if live is not None:
            spent = float(live)
    remaining = None
    level = "normal"
    if envelope is not None and getattr(envelope, "max_cost", None) is not None:
        remaining = envelope.remaining_cost(ledger) if hasattr(envelope, "remaining_cost") else None
        chargeable = (
            envelope.chargeable_cost(ledger)
            if hasattr(envelope, "chargeable_cost") else spent
        )
        max_cost = float(envelope.max_cost)
        if chargeable is not None and max_cost > 0:
            fraction = float(chargeable) / max_cost
            if fraction >= 0.99 or (hasattr(envelope, "hit_cost_limit") and envelope.hit_cost_limit(ledger)):
                level = "critical"
            elif fraction >= 0.5:
                level = "warn"
        text = f"{format_usd(spent)} / {format_usd(remaining)} left"
    else:
        text = format_usd(spent)
    if run_in_flight:
        text = f"{text} · stopping..." if stopping else f"{text} · esc to stop"
    return text, level


def route_segment(router) -> str:
    """Always-on compact ``route=name`` (+ freeze* when frozen)."""
    if router is None:
        return "route=balanced"
    contract = getattr(router, "contract", None)
    name = getattr(contract, "name", None) or "balanced"
    marker = ""
    if getattr(router, "bumps_frozen", False):
        marker = "*"
    return f"route={name}{marker}"


def context_segment(models, catalog=None) -> str:
    """``ctx N%`` when a catalog window is known for brain; else ''."""
    if catalog is None or models is None:
        return ""
    try:
        from relay.context import resolve_context_window

        window, _src = resolve_context_window(
            models.brain, provider=models.brain_provider, catalog=catalog,
        )
    except Exception:  # noqa: BLE001 -- never break the rail
        return ""
    if not window or window <= 0:
        return ""
    # Without live token counts, show the window as capacity (not a real %).
    # Prefer a placeholder that still surfaces context awareness on the rail.
    try:
        override = os.environ.get("RELAY_TUI_CTX_PCT")
        if override is not None:
            return f"ctx {int(float(override))}%"
    except (TypeError, ValueError):
        pass
    return f"ctx window {window // 1000}k"


def resolve_anim_mode(explicit: str | None = None) -> str:
    """Resolve animation mode: explicit > RELAY_TUI_ANIM > default short."""
    if explicit is not None:
        return explicit
    raw = (os.environ.get("RELAY_TUI_ANIM") or "").strip().lower()
    if raw in ("0", "false", "off", "no"):
        return "off"
    if raw in ("1", "true", "on", "yes", "short"):
        return "short"
    if raw == "long":
        return "long"
    return "short"
