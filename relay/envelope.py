"""Cost envelope contracts: budgets, warnings, preflight, and receipts.

Productizes Relay's existing ``max_cost`` / step ceilings into an explicit
contract with threshold warnings and an end-of-run receipt. The cost
``scope`` knob is **cost-only** — it does not change step-ceiling semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from relay.config import (
    DEFAULT_ENVELOPE_SCOPE,
    DEFAULT_ENVELOPE_WARN,
)
from relay.telemetry import Ledger

# Re-export for callers that import thresholds from this module.
DEFAULT_WARN_THRESHOLDS = DEFAULT_ENVELOPE_WARN

ENVELOPE_SCOPES = ("all", "execution")

# Event kind emitted when a soft threshold is crossed (once per threshold×dimension).
EVENT_ENVELOPE_WARN = "envelope_warn"


def format_usd(amount: float | None) -> str:
    """Compact dollar formatting for UI/receipt lines."""
    if amount is None:
        return "unknown"
    return f"${amount:.4f}"


def format_warn_pct(thresholds: tuple[float, ...] | list[float]) -> str:
    """Human list like ``50%/80%/90%/99%``."""
    parts = []
    for t in thresholds:
        pct = t * 100.0
        parts.append(f"{pct:.0f}%" if abs(pct - round(pct)) < 1e-9 else f"{pct:.1f}%")
    return "/".join(parts)


@dataclass
class CostEnvelope:
    """Mutable per-run envelope state (ceilings, scope, warnings, wasted brain $).

    Session/TUI edits may mutate ``max_cost``, ``max_steps``, and
    ``warn_thresholds`` in place; they must not be written back to config.
    """

    max_cost: float | None = None
    max_steps: int | None = None
    scope: str = DEFAULT_ENVELOPE_SCOPE  # cost-only: "all" | "execution"
    warn_thresholds: tuple[float, ...] = DEFAULT_WARN_THRESHOLDS
    # When scope == "execution", planning spend before this baseline is excluded.
    cost_baseline: float = 0.0
    wasted_brain_usd: float = 0.0
    completed_steps: int = 0
    _fired: set[tuple[str, float]] = field(default_factory=set)

    def mark_execution_start(self, ledger: Ledger | None) -> None:
        """Anchor the execution-only cost baseline (no-op for scope ``all``)."""
        if self.scope != "execution" or ledger is None:
            return
        total = ledger.total_cost()
        self.cost_baseline = float(total) if total is not None else 0.0

    def chargeable_cost(self, ledger: Ledger | None) -> float | None:
        """Cost that counts against ``max_cost`` under the current scope."""
        if ledger is None:
            return None
        total = ledger.total_cost()
        if total is None:
            return None
        if self.scope == "execution":
            return max(0.0, float(total) - self.cost_baseline)
        return float(total)

    def hit_cost_limit(self, ledger: Ledger | None) -> bool:
        if self.max_cost is None:
            return False
        spent = self.chargeable_cost(ledger)
        return spent is not None and spent >= self.max_cost

    def add_wasted_brain(self, amount: float | None) -> None:
        if amount is not None and amount > 0:
            self.wasted_brain_usd += float(amount)

    def note_completed_step(self) -> None:
        self.completed_steps += 1

    def drain_warnings(
        self,
        *,
        ledger: Ledger | None,
        steps_used: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return newly crossed soft-threshold events (once each per run).

        Each item: ``{"dimension": "cost"|"steps", "threshold": float,
        "fraction": float, "message": str}``.
        """
        out: list[dict[str, Any]] = []
        if self.max_cost is not None:
            spent = self.chargeable_cost(ledger)
            if spent is not None and self.max_cost > 0:
                fraction = spent / self.max_cost
                out.extend(
                    self._fire_dimension(
                        "cost",
                        fraction,
                        f"envelope warn: cost at {fraction:.0%} of "
                        f"{format_usd(self.max_cost)} "
                        f"(spent {format_usd(spent)})",
                    )
                )
        if self.max_steps is not None and steps_used is not None and self.max_steps > 0:
            fraction = steps_used / self.max_steps
            out.extend(
                self._fire_dimension(
                    "steps",
                    fraction,
                    f"envelope warn: steps at {fraction:.0%} of "
                    f"{self.max_steps} (used {steps_used})",
                )
            )
        return out

    def _fire_dimension(
        self, dimension: str, fraction: float, message: str
    ) -> list[dict[str, Any]]:
        fired: list[dict[str, Any]] = []
        for threshold in self.warn_thresholds:
            key = (dimension, threshold)
            if key in self._fired:
                continue
            if fraction + 1e-12 >= threshold:
                self._fired.add(key)
                fired.append(
                    {
                        "dimension": dimension,
                        "threshold": threshold,
                        "fraction": fraction,
                        "message": message,
                    }
                )
        return fired

    def preflight_text(self) -> str:
        """One-line (or short multi-clause) contract for run-start panels."""
        cost_part = (
            f"cost ≤ {format_usd(self.max_cost)} (scope={self.scope})"
            if self.max_cost is not None
            else "cost: unbounded"
        )
        steps_part = (
            f"steps ≤ {self.max_steps}"
            if self.max_steps is not None
            else "steps: unbounded"
        )
        warn_part = f"warn @ {format_warn_pct(self.warn_thresholds)}"
        return f"envelope: {cost_part} · {steps_part} · {warn_part}"

    def post_plan_snapshot(self, ledger: Ledger | None) -> str:
        """Short spent/remaining line after plan commit (planned mode)."""
        total = ledger.total_cost() if ledger is not None else None
        if self.max_cost is None:
            return f"post-plan: spent {format_usd(total)} · cost envelope unbounded"
        chargeable = self.chargeable_cost(ledger)
        # For scope=execution, planning spend is outside the ceiling — show both.
        if self.scope == "execution":
            remaining = None if chargeable is None else max(0.0, self.max_cost - chargeable)
            return (
                f"post-plan: planning spent {format_usd(total)} "
                f"(excluded from cost ceiling) · "
                f"execution budget {format_usd(self.max_cost)} remaining {format_usd(remaining)}"
            )
        remaining = None if chargeable is None else max(0.0, self.max_cost - chargeable)
        return (
            f"post-plan: spent {format_usd(chargeable)} · "
            f"remaining {format_usd(remaining)} of {format_usd(self.max_cost)}"
        )

    def remaining_cost(self, ledger: Ledger | None) -> float | None:
        if self.max_cost is None:
            return None
        spent = self.chargeable_cost(ledger)
        if spent is None:
            return None
        return max(0.0, self.max_cost - spent)

    def outcome_label(self, status: str) -> str:
        if status == "max_cost":
            return "hit_cost"
        if status in ("max_steps",):
            return "hit_steps"
        return "within"

    def receipt_lines(
        self,
        ledger: Ledger | None,
        *,
        status: str,
        steps_completed: int | None = None,
    ) -> list[str]:
        """Human receipt lines (brain/hands split + wasted + $/step + outcome)."""
        lines: list[str] = ["Receipt"]
        summaries = ledger.by_role() if ledger is not None else {}
        for role in ("brain", "hands"):
            s = summaries.get(role)
            if s is None:
                lines.append(f"  {role}: —")
            else:
                lines.append(
                    f"  {role}: {format_usd(s.cost_usd)} · "
                    f"{s.total_tokens} tok · {s.calls} call(s)"
                )
        total = ledger.total_cost() if ledger is not None else None
        chargeable = self.chargeable_cost(ledger)
        lines.append(f"  total: {format_usd(total)}")
        if self.max_cost is not None:
            lines.append(
                f"  chargeable ({self.scope}): {format_usd(chargeable)} / "
                f"{format_usd(self.max_cost)}"
            )
        completed = (
            steps_completed if steps_completed is not None else self.completed_steps
        )
        if total is not None and completed > 0:
            lines.append(f"  $/completed-step: {format_usd(total / completed)}")
        lines.append(
            f"  wasted brain (replan/review on incomplete steps): "
            f"{format_usd(self.wasted_brain_usd)}"
        )
        lines.append(f"  envelope outcome: {self.outcome_label(status)}")
        return lines


def brain_cost_since(ledger: Ledger | None, start_index: int) -> float:
    """Sum brain ``cost_usd`` for records at/after ``start_index`` (0 if unknown)."""
    if ledger is None:
        return 0.0
    total = 0.0
    for record in ledger.records[start_index:]:
        if record.role == "brain" and record.cost_usd is not None:
            total += float(record.cost_usd)
    return total
