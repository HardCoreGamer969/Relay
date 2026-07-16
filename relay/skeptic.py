"""Adversarial reviewer (skeptic): read-only pass that tries to kill the plan.

Optional second brain (or attacker prompt) that only looks for missing tests,
destructive bash, scope creep, and silent API breaks. Unresolved objections
block continue / force replan. Cost is attributed with ``purpose="skeptic"``
on ledger :class:`~relay.telemetry.CallRecord`s.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from relay.config import ModelConfig, assumption_directive
from relay.investigation import investigate
from relay.models import call_model
from relay.planner import Plan
from relay.protocol import tag_content
from relay.store import load_config
from relay.telemetry import Ledger
from relay.tools import Tools

EventSink = Callable[[str, str, dict], None]

SKEPTIC_PURPOSE = "skeptic"

_SKEPTIC_SYSTEM = """\
You are the SKEPTIC (adversarial reviewer) for Relay, a coding agent.
Your ONLY job is to try to KILL the plan: find missing tests, destructive
bash, scope creep beyond the goal, silent API breaks, or unsafe assumptions.
You do NOT improve the plan's wording and you do NOT execute work.

You MAY investigate READ-ONLY with:
  <read path="..."/>
  <list path="..."/>
  <grep pattern="..." path="..."/>
You MUST NOT edit files or run bash.

When finished, emit EXACTLY one verdict tag:
  <verdict>clear</verdict>     -- no blocking objections
  <verdict>object</verdict>    -- blocking objections (list them)

If objecting, include one or more:
  <objection>concrete blocking concern</objection>

Be adversarial but precise. Do not invent files you did not read.
"""

_SKEPTIC_GRAMMAR = (
    "Reply format:\n"
    "<verdict>clear|object</verdict>\n"
    "<objection>...</objection>  (repeatable; required when verdict=object)\n"
    "<reason>optional short rationale</reason>"
)


@dataclass
class SkepticReview:
    """Outcome of one skeptic pass over a plan (or step)."""

    verdict: str  # clear | object
    objections: list[str] = field(default_factory=list)
    reason: str = ""
    records: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.verdict == "object" and bool(self.objections)


def resolve_skeptic(override: bool | None = None, config: dict | None = None) -> bool:
    """Resolve whether adversarial review is on: override > env > config > False.

    Env: ``RELAY_SKEPTIC=1`` / ``true``. Config: ``review.adversarial`` or
    top-level ``skeptic`` / ``adversarial``.
    """
    if override is not None:
        return bool(override)
    env = str(os.environ.get("RELAY_SKEPTIC", "")).strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    config = config if config is not None else load_config()
    if isinstance(config, dict):
        review = config.get("review")
        if isinstance(review, dict) and "adversarial" in review:
            return bool(review.get("adversarial"))
        if "skeptic" in config:
            return bool(config.get("skeptic"))
        if "adversarial" in config:
            return bool(config.get("adversarial"))
    return False


def _parse_skeptic(text: str) -> SkepticReview:
    """Parse skeptic ``<verdict>`` / ``<objection>`` reply. Fail closed → object."""
    if not (text or "").strip():
        return SkepticReview(
            verdict="object",
            objections=[
                "skeptic produced no verdict -- emit <verdict>clear|object</verdict>"
            ],
            reason="empty or budget-exhausted skeptic reply",
        )
    raw = (tag_content("verdict", text) or "").strip().lower()
    objections = [
        o.strip()
        for o in re.findall(
            r"<objection\b[^>]*>(.*?)</objection>", text, flags=re.DOTALL | re.IGNORECASE
        )
        if o.strip()
    ]
    reason = (tag_content("reason", text) or "").strip()
    if raw not in ("clear", "object"):
        return SkepticReview(
            verdict="object",
            objections=objections
            or [
                "skeptic verdict missing or unrecognized -- "
                "emit <verdict>clear|object</verdict>"
            ],
            reason=reason or f"unrecognized verdict: {raw!r}",
        )
    if raw == "object" and not objections:
        return SkepticReview(
            verdict="object",
            objections=[
                "object verdict with no <objection> tags -- state a concrete concern"
            ],
            reason=reason or "object without objections",
        )
    if raw == "clear":
        return SkepticReview(verdict="clear", objections=[], reason=reason)
    return SkepticReview(verdict="object", objections=objections, reason=reason)


def review_plan_adversarially(
    goal: str,
    plan: Plan,
    *,
    tools: Tools | None = None,
    max_skeptic_steps: int = 4,
    models: ModelConfig | None = None,
    ledger: Ledger | None = None,
    client: Any | None = None,
    brain_role: str = "brain",
    assumption_level: str = "auto",
    on_event: EventSink | None = None,
) -> SkepticReview:
    """Run a bounded read-only skeptic pass over ``plan``.

    Uses :func:`relay.investigation.investigate` (no edit/bash). Calls are
    recorded with ``purpose="skeptic"`` for separate cost attribution.
    """
    steps_block = "\n".join(
        f"{i}. {s.instruction}" for i, s in enumerate(plan.steps, 1)
    ) or "(empty plan)"
    dial = assumption_directive(assumption_level)
    seed = (
        f"{dial}\n\n"
        f"Goal: {goal}\n\n"
        f"Proposed plan:\n{steps_block}\n\n"
        f"{_SKEPTIC_GRAMMAR}\n\n"
        "Investigate read-only if useful, then emit your <verdict>."
    )
    system = _SKEPTIC_SYSTEM

    def _call(role: str, messages, *, models=None, ledger=None, client=None, **kwargs):
        return call_model(
            role,
            messages,
            models=models,
            ledger=ledger,
            client=client,
            purpose=SKEPTIC_PURPOSE,
            **kwargs,
        )

    return investigate(
        system,
        seed,
        terminators=("verdict",),
        parse_terminal=_parse_skeptic,
        safe_default=lambda: _parse_skeptic(""),
        budget=max_skeptic_steps,
        tools=tools,
        brain_role=brain_role,
        models=models,
        ledger=ledger,
        client=client,
        model_call=_call,
        emit=on_event,
        final_instruction=(
            "This is your last turn -- emit <verdict>clear|object</verdict> now."
        ),
    )


def skeptic_cost_usd(ledger: Ledger | None) -> float | None:
    """Sum known costs for calls tagged ``purpose=skeptic``."""
    if ledger is None:
        return None
    costs = [
        r.cost_usd
        for r in ledger.records
        if getattr(r, "purpose", None) == SKEPTIC_PURPOSE and r.cost_usd is not None
    ]
    return sum(costs) if costs else None
