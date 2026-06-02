"""The planner (the "brain").

The brain plans the full ordered sequence up front and re-engages ONLY on
escalation -- it does not review every successful step (that keeps planner cost
bounded and is a deliberate future tunable). During planning the brain is
READ-ONLY: it may investigate with ``read`` / ``list`` / ``grep`` but may not
``edit`` or ``bash`` -- those write/execute tools belong to the hands. If the
brain emits an ``edit``/``bash``, it is refused with an observation so it adapts
and plans instead.

All brain calls go through ``call_model("brain", ...)`` so telemetry attributes
them to the brain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from relay.config import ModelConfig
from relay.loop import describe_action, execute_action
from relay.models import call_model
from relay.protocol import parse
from relay.telemetry import Ledger
from relay.tools import Tools

# A plan is capped so a misbehaving brain cannot emit a 10,000-step plan.
MAX_PLAN_STEPS = 20

# Directory / file names omitted from the project digest as noise.
_DIGEST_SKIP = {
    ".git", "__pycache__", ".venv", "venv", "env", "ENV", "node_modules",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".omc", "dist", "build",
}

# Callback for streaming brain investigation actions: (kind, message, payload).
EventSink = Callable[[str, str, dict], None]


@dataclass
class PlanStep:
    """One step of the brain's plan, with its execution status and outcome."""

    index: int
    instruction: str
    status: str = "pending"  # "pending" | "done" | "failed"
    outcome: str | None = None  # one-line result recorded when the step settles


@dataclass
class Plan:
    """An ordered list of :class:`PlanStep` with small navigation helpers."""

    steps: list[PlanStep] = field(default_factory=list)

    @classmethod
    def from_instructions(cls, instructions: list[str]) -> "Plan":
        return cls(steps=[PlanStep(index=i, instruction=text) for i, text in enumerate(instructions)])

    def next_pending(self) -> PlanStep | None:
        for step in self.steps:
            if step.status == "pending":
                return step
        return None

    def mark_done(self, step: PlanStep, outcome: str) -> None:
        step.status = "done"
        step.outcome = outcome

    def mark_failed(self, step: PlanStep, reason: str | None = None) -> None:
        step.status = "failed"
        if reason is not None:
            step.outcome = reason

    def remaining(self) -> list[PlanStep]:
        return [s for s in self.steps if s.status == "pending"]

    def completed_outcomes(self) -> list[tuple[int, str, str]]:
        """``(index, instruction, outcome)`` for each completed (``done``) step."""
        return [(s.index, s.instruction, s.outcome or "") for s in self.steps if s.status == "done"]


def project_digest(project_root: str | Path, *, max_depth: int = 2, max_entries: int = 200) -> str:
    """A shallow recursive listing of the project root, for the brain's context.

    Bounded in depth and entry count, with common noise directories pruned, so
    it is a cheap "map" rather than a full tree.
    """
    root = Path(project_root)
    try:
        root = root.resolve()
    except OSError:
        pass

    lines: list[str] = []

    def walk(directory: Path, depth: int) -> None:
        if depth > max_depth or len(lines) >= max_entries:
            return
        try:
            children = sorted(directory.iterdir(), key=lambda p: p.name.lower())
        except (OSError, PermissionError):
            return
        for child in children:
            if len(lines) >= max_entries:
                lines.append("... (truncated)")
                return
            if child.name in _DIGEST_SKIP or child.name.endswith(".egg-info"):
                continue
            rel = child.relative_to(root).as_posix()
            if child.is_dir():
                lines.append(rel + "/")
                walk(child, depth + 1)
            else:
                lines.append(rel)

    walk(root, 1)
    return "\n".join(lines) if lines else "(empty project)"


_PLANNER_SYSTEM = """\
You are the PLANNER (the "brain") of Relay, a coding agent. You PLAN the work;
you do NOT execute it -- a separate executor (the "hands") carries out each step.

You are given a goal and a digest of the project. You MAY investigate first,
READ-ONLY, using ONLY these tags:
  <read path="relative/path"/>
  <list path="relative/path"/>
  <grep pattern="regex" path="relative/path"/>
You are READ-ONLY: you cannot edit files or run bash. If you try, it is refused.

When ready (or immediately if no investigation is needed), output an ordered plan:
  <plan>
    <step>one concrete, executor-sized instruction</step>
    <step>...</step>
  </plan>

Each <step> must be a single concrete action an executor can carry out in a
narrow context (e.g. "Create requirements.txt listing flask and pytest", NOT
"set up the project"). Order steps so each is doable given the previous ones.
Do not bundle unrelated work into one step.

If the goal is genuinely unreachable, output <abort>reason</abort> instead.
You may investigate at most {n} time(s); then you MUST emit a <plan>.
"""

_REPLAN_SYSTEM = """\
You are the PLANNER (the "brain") of Relay. A step in your plan FAILED during
execution. Revise the plan for the REMAINING work.

You are given the goal, the plan so far with statuses, the one-line outcomes of
completed steps, and the failed step with a short reason. Output a revised plan
for the work that still remains:
  <plan>
    <step>...</step>
  </plan>
Do NOT repeat steps already completed. Account for WHY the step failed -- choose
a different approach if the previous one cannot work. If the goal is now
unreachable, output <abort>reason</abort>.
"""

_PLAN_NUDGE = (
    "No usable <plan> was found. Emit your plan now as <plan><step>...</step>...</plan> "
    "with concrete, executor-sized steps (or <abort>reason</abort>)."
)


def _render_plan(plan: Plan) -> str:
    marks = {"done": "[done]", "failed": "[FAILED]", "pending": "[pending]"}
    lines = []
    for step in plan.steps:
        line = f"{step.index}. {marks.get(step.status, step.status)} {step.instruction}"
        if step.outcome:
            line += f"  ->  {step.outcome}"
        lines.append(line)
    return "\n".join(lines) if lines else "(no steps)"


def make_plan(
    goal: str,
    project_root: str | Path,
    *,
    models: ModelConfig | None = None,
    ledger: Ledger | None = None,
    client: Any | None = None,
    max_investigation_steps: int = 5,
    max_plan_retries: int = 2,
    brain_role: str = "brain",
    on_event: EventSink | None = None,
) -> Plan | None:
    """Have the brain investigate (read-only, bounded) then produce a ``Plan``.

    Returns the parsed :class:`Plan`, or ``None`` on a planning failure (the
    brain aborted, or never produced a valid ``<plan>`` within the retry budget).
    Bounded so it cannot loop forever.
    """
    tools = Tools(Path(project_root))
    digest = project_digest(project_root)
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _PLANNER_SYSTEM.format(n=max_investigation_steps)},
        {
            "role": "user",
            "content": (
                f"Goal: {goal}\n\n"
                f"Project digest (shallow listing of the root):\n{digest}\n\n"
                "Investigate read-only if you need to, then emit your <plan>."
            ),
        },
    ]

    investigation_used = 0
    retries = 0
    max_turns = max_investigation_steps + max_plan_retries + 2  # hard cap on brain turns

    for _ in range(max_turns):
        reply = call_model(brain_role, messages, models=models, ledger=ledger, client=client).text
        messages.append({"role": "assistant", "content": reply})
        parsed = parse(reply)

        if parsed.first("abort") is not None:
            return None

        plan_action = parsed.first("plan")
        if plan_action is not None and plan_action.steps:
            return Plan.from_instructions(plan_action.steps[:MAX_PLAN_STEPS])

        # No usable plan this turn.
        if not parsed.actions or (plan_action is not None and not plan_action.steps):
            # Parse failure or an empty <plan>.
            if ledger is not None:
                ledger.record_parse_failure()
            retries += 1
            if retries > max_plan_retries:
                return None
            messages.append({"role": "user", "content": _PLAN_NUDGE})
            continue

        observations: list[str] = []
        for action in parsed.actions:
            if action.kind in ("edit", "bash"):
                observations.append(
                    f"[{describe_action(action)}]\nrefused: the planner is READ-ONLY and "
                    "cannot edit files or run bash. Investigate read-only or emit a <plan>."
                )
            elif action.kind in ("read", "list", "grep"):
                if investigation_used >= max_investigation_steps:
                    observations.append(
                        f"[{describe_action(action)}]\nrefused: investigation budget "
                        "exhausted; emit your <plan> now."
                    )
                    continue
                investigation_used += 1
                obs = execute_action(tools, action)
                observations.append(f"[{describe_action(action)}]\n{obs}")
                if on_event is not None:
                    on_event("brain_action", describe_action(action), {"observation": obs})

        tail = ""
        if investigation_used >= max_investigation_steps:
            tail = "\n\nInvestigation budget exhausted. Emit your <plan> now."
        messages.append({"role": "user", "content": "\n\n".join(observations) + tail})

    return None


def replan(
    goal: str,
    plan: Plan,
    failed_step: PlanStep,
    failure_summary: str,
    completed_outcomes: list[tuple[int, str, str]],
    *,
    models: ModelConfig | None = None,
    ledger: Ledger | None = None,
    client: Any | None = None,
    max_plan_retries: int = 2,
    brain_role: str = "brain",
) -> Plan | None:
    """Have the brain revise the remaining plan after a step failed.

    The brain receives the goal, the current plan, the failed step + a concise
    failure summary (NOT the full transcript), and the one-line outcomes of
    completed steps. Returns a :class:`Plan` for the remaining work, or ``None``
    if the brain emits ``<abort>``.
    """
    outcomes_text = (
        "\n".join(f"- [{idx}] {instr}  ->  {outcome}" for idx, instr, outcome in completed_outcomes)
        or "(none completed yet)"
    )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _REPLAN_SYSTEM},
        {
            "role": "user",
            "content": (
                f"Goal: {goal}\n\n"
                f"Plan so far:\n{_render_plan(plan)}\n\n"
                f"Completed outcomes:\n{outcomes_text}\n\n"
                f"The step that FAILED: [{failed_step.index}] {failed_step.instruction}\n"
                f"Why it failed: {failure_summary}\n\n"
                "Emit a revised <plan> for the REMAINING work (do not repeat completed "
                "steps), or <abort>reason</abort> if the goal is unreachable."
            ),
        },
    ]

    for _ in range(max_plan_retries + 1):
        reply = call_model(brain_role, messages, models=models, ledger=ledger, client=client).text
        messages.append({"role": "assistant", "content": reply})
        parsed = parse(reply)

        if parsed.first("abort") is not None:
            return None
        plan_action = parsed.first("plan")
        if plan_action is not None and plan_action.steps:
            return Plan.from_instructions(plan_action.steps[:MAX_PLAN_STEPS])

        if ledger is not None:
            ledger.record_parse_failure()
        messages.append({"role": "user", "content": _PLAN_NUDGE})

    return None
