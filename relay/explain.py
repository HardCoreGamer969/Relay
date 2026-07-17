"""Explain the harness: deterministic flight recorder from run events.

Zero model calls. Rebuilds a structured explanation from the orchestrator's
already-emitted :class:`~relay.orchestrator.Event` stream (and optional
envelope / dial metadata). Safe to paste after :func:`relay.debug.redact_secrets`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable


@dataclass
class HarnessReport:
    """Machine-readable + human-readable explanation of one run."""

    status: str = ""
    goal: str = ""
    assumption_level: str | None = None
    last_brain_engagement: str | None = None
    brain_engagements: list[str] = field(default_factory=list)
    budgets: dict[str, Any] = field(default_factory=dict)
    open_questions: list[str] = field(default_factory=list)
    escalations: list[str] = field(default_factory=list)
    envelope_warnings: list[str] = field(default_factory=list)
    route_changes: list[str] = field(default_factory=list)
    spend: str = ""
    terminal_reason: str | None = None
    step_summaries: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_text(self) -> str:
        lines = ["# Why (harness flight recorder)", ""]
        if self.goal:
            lines.append(f"Goal: {self.goal}")
        if self.status:
            lines.append(f"Status: {self.status}")
        if self.assumption_level:
            lines.append(f"Assumption dial: {self.assumption_level}")
        lines.append("")
        lines.append("## Last brain engagement")
        lines.append(self.last_brain_engagement or "(none recorded)")
        if self.brain_engagements:
            lines.append("")
            lines.append("## Brain engagements (chronological)")
            for item in self.brain_engagements:
                lines.append(f"- {item}")
        lines.append("")
        lines.append("## Budgets")
        if self.budgets:
            for key, val in self.budgets.items():
                lines.append(f"- {key}: {val}")
        else:
            lines.append("- (none)")
        if self.envelope_warnings:
            lines.append("")
            lines.append("## Envelope warnings")
            for w in self.envelope_warnings:
                lines.append(f"- {w}")
        if self.route_changes:
            lines.append("")
            lines.append("## Route changes")
            for r in self.route_changes:
                lines.append(f"- {r}")
        if self.spend:
            lines.append("")
            lines.append(self.spend)
        if self.escalations:
            lines.append("")
            lines.append("## Escalations")
            for e in self.escalations:
                lines.append(f"- {e}")
        if self.open_questions:
            lines.append("")
            lines.append("## Open questions")
            for q in self.open_questions:
                lines.append(f"- {q}")
        if self.step_summaries:
            lines.append("")
            lines.append("## Steps")
            for s in self.step_summaries:
                lines.append(
                    f"- step {s.get('index')}: {s.get('state', '?')} — {s.get('detail', '')}"
                )
        if self.terminal_reason:
            lines.append("")
            lines.append("## Terminal reason")
            lines.append(self.terminal_reason)
        if self.notes:
            lines.append("")
            lines.append("## Notes")
            for n in self.notes:
                lines.append(f"- {n}")
        lines.append("")
        lines.append("_Deterministic from the event trace — no new model tokens._")
        return "\n".join(lines)


_BRAIN_KINDS = frozenset(
    {
        "plan_created",
        "plan_proposed",
        "plan_revised",
        "replanned",
        "step_reviewed",
        "brain_self_answered",
        "brain_escalated",
        "escalation",
        "scope_assessed",
        "route_change",
    }
)


def explain_events(
    events: Iterable[Any],
    *,
    goal: str = "",
    status: str = "",
    assumption_level: str | None = None,
    max_total_steps: int | None = None,
    max_cost: float | None = None,
    envelope: Any | None = None,
    step_filter: int | None = None,
) -> HarnessReport:
    """Build a :class:`HarnessReport` from orchestrator/conversation events.

    ``events`` may be :class:`~relay.orchestrator.Event` objects or plain dicts
    with ``kind`` / ``message`` / ``payload``. Never includes raw model prompts.
    """
    report = HarnessReport(
        goal=goal,
        status=status,
        assumption_level=assumption_level,
    )
    if max_total_steps is not None:
        report.budgets["max_total_steps"] = max_total_steps
    if max_cost is not None:
        report.budgets["max_cost"] = max_cost
    if envelope is not None:
        report.budgets["envelope_scope"] = getattr(envelope, "scope", None)
        report.budgets["envelope_max_cost"] = getattr(envelope, "max_cost", None)
        report.budgets["envelope_max_steps"] = getattr(envelope, "max_steps", None)
        report.budgets["wasted_brain_usd"] = getattr(envelope, "wasted_brain_usd", None)

    steps: dict[int, dict[str, Any]] = {}
    last_brain: str | None = None
    open_q: list[str] = []

    for raw in events:
        kind, message, payload = _unpack(raw)
        if step_filter is not None:
            idx = payload.get("index")
            if idx is not None and idx != step_filter and kind.startswith("step_"):
                continue

        if kind in _BRAIN_KINDS:
            line = message or kind
            report.brain_engagements.append(f"{kind}: {line}")
            last_brain = f"{kind}: {line}"

        if kind == "executor_question":
            q = payload.get("question") or message
            qclass = payload.get("question_class") or "product"
            idx = payload.get("index")
            label = f"[step {idx}] ({qclass}) {q}" if idx is not None else f"({qclass}) {q}"
            open_q.append(label)
        if kind == "brain_escalated":
            q = payload.get("question") or message
            qclass = payload.get("question_class") or "product"
            idx = payload.get("index")
            label = f"[step {idx}] ({qclass}) {q}" if idx is not None else f"({qclass}) {q}"
            open_q.append(label)
            report.escalations.append(message or q)
        if kind == "user_decided":
            # Resolved — drop matching open questions by clearing list on decision.
            open_q.clear()
        if kind == "escalation":
            report.escalations.append(message)
        if kind == "envelope_warn":
            report.envelope_warnings.append(message)
        if kind == "route_change":
            report.route_changes.append(message or str(payload))

        if kind == "step_start":
            idx = payload.get("index")
            if idx is not None:
                steps[idx] = {
                    "index": idx,
                    "state": "started",
                    "detail": payload.get("instruction") or message,
                }
        if kind == "step_done":
            idx = payload.get("index")
            if idx is not None:
                steps[idx] = {
                    "index": idx,
                    "state": "done",
                    "detail": payload.get("outcome") or message,
                }
        if kind == "step_failed":
            idx = payload.get("index")
            if idx is not None:
                steps[idx] = {
                    "index": idx,
                    "state": "failed",
                    "detail": payload.get("reason") or message,
                }

        if kind == "status":
            st = payload.get("status") or ""
            report.terminal_reason = message
            if st and not report.status:
                report.status = st

    report.last_brain_engagement = last_brain
    report.open_questions = list(open_q)
    report.step_summaries = [steps[k] for k in sorted(steps)]
    if not report.brain_engagements:
        report.notes.append("No brain engagement events were recorded on this run.")
    report.notes.append("Raw model prompts are never included in /why exports.")
    # E5: spend timeline from route_change events (ledger optional — often absent here).
    try:
        from relay.router import explain_spend

        report.spend = explain_spend(
            [
                {"kind": "route_change", "message": m, "payload": {}}
                for m in report.route_changes
            ],
            None,
        )
    except Exception:  # noqa: BLE001 — /why must never fail
        report.spend = ""
    return report


def _unpack(raw: Any) -> tuple[str, str, dict]:
    if isinstance(raw, dict):
        return (
            str(raw.get("kind") or ""),
            str(raw.get("message") or ""),
            dict(raw.get("payload") or {}),
        )
    kind = getattr(raw, "kind", "") or ""
    message = getattr(raw, "message", "") or ""
    payload = getattr(raw, "payload", None) or {}
    return str(kind), str(message), dict(payload)
