"""The orchestrator: the two-role brain/hands loop (the v0.04 milestone).

The brain plans the full ordered sequence up front; the hands execute each step
in a NARROW context (the current step instruction + one-line outcomes of
completed steps -- NOT the full plan, NOT the brain's reasoning, NOT prior
steps' raw transcripts). The brain re-engages ONLY when a step escalates, which
keeps planner cost bounded. All loops are bounded so a weak model cannot burn
money in a spiral.

This reuses the single-model loop's shared mechanics (``execute_action`` /
``describe_action``) and leaves ``relay.loop.run_task`` intact for ``--solo``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from relay.config import ModelConfig
from relay.loop import (
    MAX_CONSECUTIVE_PARSE_FAILURES,
    STATUS_COMPLETED,
    STATUS_MAX_STEPS,
    describe_action,
    execute_action,
)
from relay.models import call_model
from relay.planner import Plan, PlanStep, make_plan, replan
from relay.protocol import parse
from relay.telemetry import Ledger
from relay.tools import Tools

# Terminal statuses for a planned run. STATUS_COMPLETED / STATUS_MAX_STEPS are
# shared with the single-model loop; these three are unique to the two-role run.
STATUS_PLANNING_FAILED = "planning_failed"
STATUS_ABORTED_BY_BRAIN = "aborted_by_brain"
STATUS_ESCALATION_LIMIT = "escalation_limit"
STATUS_DECLINED = "declined_by_user"  # plan shown but not approved (--confirm-plan)

EXECUTOR_SYSTEM_PROMPT = """\
You are the EXECUTOR (the "hands") of Relay. You carry out ONE step of a larger
plan, in a narrow context. You do NOT see the whole plan -- only your current
step plus a short list of what has already been done.

Work the current step ONE action at a time, using these EXACT tags:
  <thinking>private reasoning</thinking>   (optional)
  <read path="relative/path"/>
  <list path="relative/path"/>
  <grep pattern="regex" path="relative/path"/>
  <edit path="relative/path">FULL NEW FILE CONTENTS</edit>
  <bash>command</bash>
  <done>one-line summary of what THIS step accomplished</done>
  <blocked>reason you cannot complete this step</blocked>

Rules:
- Paths are relative to the project root; you cannot escape it.
- Some destructive bash commands are refused by policy ("BLOCKED by policy: ...")
  or need approval ("DENIED ..."); adapt rather than re-emitting them verbatim.
- Stay scoped to YOUR current step. Do NOT do later steps.
- When THIS step is complete, emit <done>...</done>. If you are genuinely stuck,
  emit <blocked>reason</blocked> early rather than thrashing.
"""

_EXEC_PARSE_NUDGE = (
    "No valid action was found. Emit exactly one protocol tag, e.g. "
    '<read path="..."/>, <edit path="...">...</edit>, <bash>...</bash>, '
    "<done>...</done>, or <blocked>...</blocked>."
)

# Callback the CLI uses to stream the run; receives whole Events.
EventCallback = Callable[["Event"], None]


@dataclass
class Event:
    """One streamed orchestration event (plan creation, step start/result, ...)."""

    kind: str
    message: str  # ready-to-print, ASCII-safe summary
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlannedTaskResult:
    """The outcome of a :func:`run_planned` run."""

    goal: str
    plan: Plan | None = None
    status: str = STATUS_MAX_STEPS
    escalations: int = 0
    ledger: Ledger | None = None
    events: list[Event] = field(default_factory=list)

    @property
    def done(self) -> bool:
        """True iff the plan was exhausted with every step completed."""
        return self.status == STATUS_COMPLETED


@dataclass
class _StepOutcome:
    """Internal result of one executor mini-loop."""

    success: bool
    summary: str = ""          # <done> content on success
    failure_reason: str = ""   # concise reason on failure (fed to replan)
    calls: int = 0             # executor model-calls consumed


def _executor_step_prompt(goal: str, step: PlanStep, plan: Plan) -> str:
    """Build the executor's NARROW per-step context.

    Deliberately excludes the full plan, the brain's reasoning, and prior steps'
    raw transcripts -- only the current instruction plus one-line carry-over.
    """
    lines = [f"Overall goal (context only): {goal}", ""]
    carry = plan.completed_outcomes()
    if carry:
        lines.append("Already completed (one-line outcomes):")
        for _index, _instruction, outcome in carry:
            lines.append(f"- {outcome}")
        lines.append("")
    lines.append(f"YOUR CURRENT STEP: {step.instruction}")
    lines.append("")
    lines.append(
        "Do ONLY this step, one action at a time. Emit <done>one-line result</done> "
        "when THIS step is complete, or <blocked>reason</blocked> if you are stuck. Begin."
    )
    return "\n".join(lines)


def _run_executor_step(
    step: PlanStep,
    plan: Plan,
    goal: str,
    tools: Tools,
    *,
    hands_role: str,
    models: ModelConfig | None,
    ledger: Ledger | None,
    client: Any | None,
    max_steps: int,
    emit: Callable[[str, str, dict], None] | None = None,
) -> _StepOutcome:
    """Run the hands in a fresh, narrow context until ``<done>``/``<blocked>``/budget."""
    messages: list[dict[str, str]] = [
        {"role": "system", "content": EXECUTOR_SYSTEM_PROMPT},
        {"role": "user", "content": _executor_step_prompt(goal, step, plan)},
    ]
    consecutive_parse_failures = 0
    calls = 0

    for _ in range(max(max_steps, 0)):
        reply = call_model(hands_role, messages, models=models, ledger=ledger, client=client).text
        calls += 1
        messages.append({"role": "assistant", "content": reply})
        parsed = parse(reply)

        if parsed.is_parse_failure:
            if ledger is not None:
                ledger.record_parse_failure()
            consecutive_parse_failures += 1
            if emit is not None:
                emit("exec_parse_failure", "no valid action", {"snippet": " ".join(reply.split())[:200]})
            if consecutive_parse_failures >= MAX_CONSECUTIVE_PARSE_FAILURES:
                return _StepOutcome(
                    False, failure_reason="executor produced no valid actions (parse-failure abort)", calls=calls
                )
            messages.append({"role": "user", "content": _EXEC_PARSE_NUDGE})
            continue
        consecutive_parse_failures = 0

        observations: list[str] = []
        for action in parsed.actions:
            if action.kind == "blocked":
                reason = action.content or "no reason given"
                return _StepOutcome(False, failure_reason=f"blocked: {reason}", calls=calls)
            if action.kind == "done":
                return _StepOutcome(True, summary=action.content or "step complete", calls=calls)
            if action.kind in ("plan", "abort"):
                observations.append(
                    f"[{action.kind}]\nnote: you are the executor. Emit <done> when this step "
                    "is complete, or <blocked>reason</blocked> if stuck -- do not plan."
                )
                continue
            observation = execute_action(tools, action)
            if emit is not None:
                emit("exec_action", describe_action(action), {"kind": action.kind, "observation": observation})
            observations.append(f"[{describe_action(action)}]\n{observation}")

        messages.append(
            {"role": "user", "content": "\n\n".join(observations) if observations else "(no output)"}
        )

    return _StepOutcome(False, failure_reason=f"step not completed within {max_steps} executor steps", calls=calls)


def _adopt_revision(plan: Plan, revised: Plan) -> Plan:
    """Splice a revision into the plan: keep done/failed steps, replace the tail.

    Forward-only (no snapshot/branch): completed and failed steps are kept as a
    record; the pending tail is dropped and the revised steps are appended as new
    pending steps with continued indices.
    """
    kept = [s for s in plan.steps if s.status in ("done", "failed")]
    next_index = max((s.index for s in kept), default=-1) + 1
    new_steps = list(kept)
    for offset, revised_step in enumerate(revised.steps):
        new_steps.append(PlanStep(index=next_index + offset, instruction=revised_step.instruction))
    return Plan(steps=new_steps)


def run_planned(
    goal: str,
    project_root: str | Path,
    *,
    brain_role: str = "brain",
    hands_role: str = "hands",
    models: ModelConfig | None = None,
    ledger: Ledger | None = None,
    client: Any | None = None,
    approver: Callable[[str, str], bool] | None = None,
    auto_approve: bool = False,
    max_investigation_steps: int = 5,
    max_executor_steps: int = 12,
    max_escalations: int = 3,
    max_total_steps: int | None = None,
    on_event: EventCallback | None = None,
    plan_gate: Callable[[Plan], bool] | None = None,
) -> PlannedTaskResult:
    """Drive the two-role planner/executor loop.

    The brain plans up front (``make_plan``); the hands execute each step in a
    narrow context; a failed step escalates to the brain (``replan``), which can
    revise the remaining tail or ``<abort>``. Bounded by ``max_executor_steps``
    per step, ``max_escalations`` replans, and an optional ``max_total_steps``
    overall executor-call budget (the ``max_steps`` terminal status).

    Args mirror ``run_task`` (roles, models, ledger, client, approver,
    auto_approve) plus the planner/escalation budgets. ``on_event`` streams
    :class:`Event`s (plan creation, each step start/result, escalations, replans)
    so the CLI can render the run live. ``plan_gate`` (used by ``--confirm-plan``)
    is called once with the freshly-created plan; returning False stops the run
    before any execution with status ``declined_by_user``.
    """
    ledger = ledger if ledger is not None else Ledger()
    result = PlannedTaskResult(goal=goal, ledger=ledger)

    def emit(kind: str, message: str, payload: dict | None = None) -> None:
        event = Event(kind, message, payload or {})
        result.events.append(event)
        if on_event is not None:
            on_event(event)

    # --- Plan phase --------------------------------------------------------
    plan = make_plan(
        goal,
        project_root,
        models=models,
        ledger=ledger,
        client=client,
        max_investigation_steps=max_investigation_steps,
        brain_role=brain_role,
        on_event=lambda kind, message, payload: emit(kind, message, payload),
    )
    if plan is None:
        result.status = STATUS_PLANNING_FAILED
        emit("status", "planning failed: the brain produced no usable plan", {"status": STATUS_PLANNING_FAILED})
        return result

    result.plan = plan
    emit("plan_created", f"plan: {len(plan.steps)} step(s)", {"steps": [s.instruction for s in plan.steps]})

    if plan_gate is not None and not plan_gate(plan):
        result.status = STATUS_DECLINED
        emit("status", "plan declined by user before execution", {"status": STATUS_DECLINED})
        return result

    tools = Tools(Path(project_root), approver=approver, auto_approve=auto_approve)

    escalations = 0
    executor_calls = 0

    # --- Execute phase -----------------------------------------------------
    while True:
        step = plan.next_pending()
        if step is None:
            result.status = STATUS_COMPLETED
            emit("status", "all steps complete", {"status": STATUS_COMPLETED})
            break

        # Overall executor-call budget guard (the "max_steps" terminal status).
        step_budget = max_executor_steps
        if max_total_steps is not None:
            remaining = max_total_steps - executor_calls
            if remaining <= 0:
                result.status = STATUS_MAX_STEPS
                emit("status", "overall executor-step budget exhausted", {"status": STATUS_MAX_STEPS})
                break
            step_budget = min(max_executor_steps, remaining)

        emit("step_start", f"step {step.index}: {step.instruction}", {"index": step.index, "instruction": step.instruction})

        outcome = _run_executor_step(
            step,
            plan,
            goal,
            tools,
            hands_role=hands_role,
            models=models,
            ledger=ledger,
            client=client,
            max_steps=step_budget,
            emit=lambda kind, message, payload: emit(kind, message, payload),
        )
        executor_calls += outcome.calls

        if outcome.success:
            plan.mark_done(step, outcome.summary)
            emit("step_done", f"step {step.index} done: {outcome.summary}", {"index": step.index, "outcome": outcome.summary})
            continue

        # Step failed -> escalate to the brain.
        plan.mark_failed(step, outcome.failure_reason)
        emit("step_failed", f"step {step.index} failed: {outcome.failure_reason}", {"index": step.index, "reason": outcome.failure_reason})

        if escalations >= max_escalations:
            result.status = STATUS_ESCALATION_LIMIT
            emit("status", f"escalation limit ({max_escalations}) reached", {"status": STATUS_ESCALATION_LIMIT})
            break

        escalations += 1
        result.escalations = escalations
        emit(
            "escalation",
            f"escalation {escalations}: replanning after step {step.index}",
            {"n": escalations, "failed_index": step.index, "reason": outcome.failure_reason},
        )

        revised = replan(
            goal,
            plan,
            step,
            outcome.failure_reason,
            plan.completed_outcomes(),
            models=models,
            ledger=ledger,
            client=client,
            brain_role=brain_role,
        )
        if revised is None:
            result.status = STATUS_ABORTED_BY_BRAIN
            emit("status", "brain aborted: goal deemed unreachable", {"status": STATUS_ABORTED_BY_BRAIN})
            break

        plan = _adopt_revision(plan, revised)
        result.plan = plan
        emit(
            "replanned",
            f"revised plan adopted: {len(plan.remaining())} new step(s)",
            {"steps": [s.instruction for s in plan.steps if s.status == "pending"]},
        )

    return result
