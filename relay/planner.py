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

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from relay.config import ModelConfig, assumption_directive
from relay.context import DEFAULT_CONTEXT_WINDOW
from relay.investigation import investigate
from relay.loop import describe_action, execute_action
from relay.memory import PlanMemory, memory_budget
from relay.models import call_model
from relay.protocol import parse
from relay.telemetry import Ledger
from relay.tools import Tools

# A plan is capped so a misbehaving brain cannot emit a 10,000-step plan.
MAX_PLAN_STEPS = 20

# Fallback memory-read budget when a caller doesn't pass one (sized to the
# conservative default window). Real runs pass the resolved window's budget.
_DEFAULT_MEMORY_BUDGET = memory_budget(DEFAULT_CONTEXT_WINDOW)

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
    memory: PlanMemory | None = None,
    memory_budget_tokens: int = _DEFAULT_MEMORY_BUDGET,
    steer: str | None = None,
) -> Plan | None:
    """Have the brain revise the remaining plan -- after a step failed, OR after the
    user interrupted to redirect (``steer``).

    The brain receives the goal, the current plan, and the one-line outcomes of
    completed steps. For a FAILURE replan it also gets the failed step + a concise
    failure summary; for a STEER it gets the user's new direction instead and is
    asked to fold it into the remaining work. When ``memory`` is given, a
    window-aware (budget-bounded) slice of what has been learned is included so the
    brain stays consistent. Returns a :class:`Plan` for the remaining work, or
    ``None`` on ``<abort>``. Completed steps are never repeated.
    """
    outcomes_text = (
        "\n".join(f"- [{idx}] {instr}  ->  {outcome}" for idx, instr, outcome in completed_outcomes)
        or "(none completed yet)"
    )
    mem_focus = f"{goal} {steer}" if steer else f"{goal} {failure_summary}"
    mem_ctx = _memory_context(
        memory, mem_focus, memory_budget_tokens,
        client=client, models=models, ledger=ledger,
    )
    memory_block = f"What has been learned (memory):\n{mem_ctx}\n\n" if mem_ctx else ""
    if steer:
        # A steer is a redirection, not a failure: the user interrupted to change
        # course. Replan the remainder to incorporate the new direction.
        directive = (
            f"The user INTERRUPTED and gave new direction: {steer}\n\n"
            "Replan the REMAINING work to incorporate this new direction. Keep the "
            "completed steps (do not repeat them); emit a revised <plan> for what is "
            "left, or <abort>reason</abort> if the new direction makes the goal "
            "unreachable."
        )
    else:
        directive = (
            f"The step that FAILED: [{failed_step.index}] {failed_step.instruction}\n"
            f"Why it failed: {failure_summary}\n\n"
            "Emit a revised <plan> for the REMAINING work (do not repeat completed "
            "steps), or <abort>reason</abort> if the goal is unreachable."
        )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _REPLAN_SYSTEM},
        {
            "role": "user",
            "content": (
                f"Goal: {goal}\n\n"
                f"Plan so far:\n{_render_plan(plan)}\n\n"
                f"Completed outcomes:\n{outcomes_text}\n\n"
                f"{memory_block}"
                f"{directive}"
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


# ===========================================================================
# v0.06 (2 of 2): autonomous brain behaviors -- supervise, answer-or-escalate,
# evolve. Each reads memory through the window-aware budget machinery (never the
# whole store) and emits a small tag grammar parsed locally below.
# ===========================================================================


@dataclass
class StepReview:
    """The brain's verdict on a finished step (at the step boundary)."""

    verdict: str  # "accept" | "follow_up" | "revise_plan"
    followup: str = ""  # concrete corrective instruction (only for follow_up)
    reason: str = ""  # what was learned / why revise
    records: list[tuple[str, str, str]] = field(default_factory=list)  # (kind, detail, summary)


@dataclass
class Resolution:
    """The brain's answer-vs-escalate decision for an executor question."""

    kind: str  # "self_answer" | "escalate"
    answer: str = ""  # the answer to hand the executor (self_answer)
    question_for_user: str = ""  # the precise product question to escalate
    reasoning: str = ""  # why this is self-answerable or a product decision (logged)
    records: list[tuple[str, str, str]] = field(default_factory=list)


_REVIEW_SYSTEM = """\
You are the PLANNER (the "brain") supervising an EXECUTOR (the "hands"). At a step
boundary you judge whether the executor's work met the step's intent, given the
goal, the plan, what the executor did, and relevant prior memory. Be concise.
Bias toward accept when the work is adequate; ask for a follow-up only when there
is a concrete, correctable gap; choose revise_plan only when what was learned
changes the REMAINING work.

You MAY investigate READ-ONLY before judging, using ONLY these tags:
  <read path="relative/path"/>
  <list path="relative/path"/>
  <grep pattern="regex" path="relative/path"/>
You cannot edit files or run bash. The executor's summary can be wrong or thin --
reading the file(s) it actually changed is the surest way to judge, so prefer
reading what changed over trusting the summary. When you have seen enough (or
immediately, if no reading is needed), emit your <verdict>.
"""

_REVIEW_GRAMMAR = (
    "Reply using ONLY these tags:\n"
    "<verdict>accept|follow_up|revise_plan</verdict>\n"
    "<followup>concrete corrective instruction for the executor</followup>  (only if follow_up)\n"
    "<reason>what changed / why</reason>  (especially for revise_plan)\n"
    '<record kind="fact|decision|dead_end">precise detail :: short human summary</record>'
    "  (zero or more facts worth remembering)"
)

_ANSWER_SYSTEM = """\
You are the PLANNER (the "brain"). The EXECUTOR asked a question mid-step. Decide
whether you can answer it YOURSELF -- a TECHNICAL question decidable from the
goal, the plan, the code, and established memory -- or whether it is a genuine
PRODUCT DECISION only the user can make (what to build, scope, a user-visible
preference). Answer it yourself when you legitimately can. When GENUINELY UNSURE,
ESCALATE: a needless escalation is a mild annoyance, but answering a product
decision yourself silently builds the wrong thing.

If a "conversation so far" is provided, you are continuing ONE ongoing dialogue
with the user. Phrase any escalation as a natural continuation of it -- you may
reference what was already discussed (e.g. "earlier you said you wanted this
simple, so...") -- not a fresh, context-less prompt.
"""

_ANSWER_GRAMMAR = (
    "Reply using ONLY these tags:\n"
    "<decision>self_answer|escalate</decision>\n"
    "<answer>the answer to hand the executor</answer>  (only if self_answer)\n"
    "<ask_user>the precise question to put to the user</ask_user>  (only if escalate)\n"
    "<reason>why this is self-answerable or a product decision</reason>\n"
    '<record kind="decision">precise detail :: short human summary</record>'
    "  (what to remember, especially for self_answer)"
)

_EVOLVE_SYSTEM = """\
You are the PLANNER (the "brain"). What the executor learned changes the remaining
work. Revise the plan for the REMAINING steps only, as a projection over the goal
and what has been learned (memory). Preserve completed steps; do not repeat them.
Output <plan><step>...</step>...</plan>, or <abort>reason</abort> if the goal is
now unreachable.
"""

_TAG_CACHE: dict[str, re.Pattern[str]] = {}
_RECORD_RE = re.compile(r'<record\s+kind="([^"]+)"\s*>(.*?)</record>', re.DOTALL)


def _tag(name: str, text: str) -> str | None:
    pattern = _TAG_CACHE.get(name)
    if pattern is None:
        pattern = re.compile(rf"<{name}>(.*?)</{name}>", re.DOTALL)
        _TAG_CACHE[name] = pattern
    match = pattern.search(text or "")
    return match.group(1).strip() if match else None


def _parse_records(text: str) -> list[tuple[str, str, str]]:
    """Parse ``<record kind="...">detail :: summary</record>`` entries."""
    records: list[tuple[str, str, str]] = []
    for match in _RECORD_RE.finditer(text or ""):
        kind = match.group(1).strip()
        body = match.group(2).strip()
        if "::" in body:
            detail, summary = (part.strip() for part in body.split("::", 1))
        else:
            detail = summary = body
        if detail:
            records.append((kind, detail, summary or detail))
    return records


def _bounded_text(text: str, max_chars: int) -> str:
    text = text or ""
    return text if len(text) <= max_chars else text[:max_chars] + " ...(truncated)"


def _memory_context(
    memory: PlanMemory | None,
    query: str,
    budget_tokens: int,
    *,
    client: Any | None,
    models: ModelConfig | None,
    ledger: Ledger | None,
) -> str:
    """Window-aware (budget-bounded) memory slice -- never the whole store."""
    if memory is None or not memory.entries or budget_tokens <= 0:
        return ""
    return memory.compacted_context(
        query, budget_tokens=budget_tokens, client=client, models=models, ledger=ledger
    )


def review_step(
    goal: str,
    plan: Plan,
    step: PlanStep,
    executor_summary: str,
    observations: list[str] | None,
    memory: PlanMemory | None,
    *,
    tools: Tools | None = None,
    touched_paths: Sequence[str] = (),
    max_review_steps: int = 4,
    models: ModelConfig | None = None,
    ledger: Ledger | None = None,
    client: Any | None = None,
    memory_budget_tokens: int = _DEFAULT_MEMORY_BUDGET,
    brain_role: str = "brain",
    on_event: EventSink | None = None,
) -> StepReview:
    """Agentically review a finished step: accept / follow_up / revise_plan.

    The reviewer is an agent, not a one-shot blind call: seeded with the file(s) the
    executor changed (``touched_paths``), it may ``read`` / ``list`` / ``grep`` to see
    the ACTUAL work before judging -- the ground truth the old one-shot reviewer lacked
    (it saw only a path label + byte-count). It runs through the shared read-only
    :func:`relay.investigation.investigate` primitive: bounded by ``max_review_steps``
    brain turns, read-only (it can never edit/bash), and on budget exhaustion it returns
    the safe ``accept`` default (running out of investigation budget must not block
    progress). It is 1 brain call when the model verdicts immediately, up to
    ``max_review_steps`` when it investigates first; review calls are excluded from the
    executor ``max_total_steps`` ceiling (they have their own budget).

    Phase (a): the seed is the touched file(s) -- the minimum that fixes the documented
    blind-reviewer loop. Phase (b) (widening to related files / reusing command output
    already in the transcript) is later a prompt/seed/budget change, not a re-architecture.
    """
    mem_ctx = _memory_context(
        memory, step.instruction, memory_budget_tokens, client=client, models=models, ledger=ledger
    )
    transcript = _bounded_text("\n".join(observations or []), 4000)
    memory_block = f"Relevant memory:\n{mem_ctx}\n\n" if mem_ctx else ""
    touched_block = ""
    if touched_paths:
        listing = "\n".join(f"- {p}" for p in touched_paths)
        touched_block = (
            "Files the executor changed this step (READ them to verify the real "
            f"contents before judging):\n{listing}\n\n"
        )
    seed = (
        f"Goal: {goal}\n\n"
        f"Plan so far:\n{_render_plan(plan)}\n\n"
        f"Step under review: [{step.index}] {step.instruction}\n"
        f"Executor reported done: {executor_summary}\n\n"
        f"What the executor did this step:\n{transcript or '(no observations)'}\n\n"
        f"{touched_block}"
        f"{memory_block}"
        f"{_REVIEW_GRAMMAR}\n\n"
        "Investigate read-only if useful, then emit your <verdict>."
    )
    return investigate(
        _REVIEW_SYSTEM,
        seed,
        terminators=("verdict",),
        parse_terminal=_parse_review,
        safe_default=lambda: _parse_review(""),  # absent verdict -> accept (never blocks)
        budget=max_review_steps,
        tools=tools,
        brain_role=brain_role,
        models=models,
        ledger=ledger,
        client=client,
        model_call=call_model,  # planner's (test-patchable) call_model reference
        emit=on_event,
        final_instruction=(
            "This is your last turn -- emit <verdict>accept|follow_up|revise_plan</verdict> now."
        ),
    )


def _parse_review(text: str) -> StepReview:
    verdict = (_tag("verdict", text) or "accept").lower()
    if verdict not in ("accept", "follow_up", "revise_plan"):
        verdict = "accept"  # unparseable verdict -> don't block progress
    followup = _tag("followup", text) or ""
    reason = _tag("reason", text) or ""
    if verdict == "follow_up" and not followup.strip():
        verdict = "accept"  # a follow-up with no instruction is unactionable
    return StepReview(
        verdict=verdict, followup=followup.strip(), reason=reason.strip(), records=_parse_records(text)
    )


def answer_or_escalate(
    question: str,
    goal: str,
    plan: Plan,
    step: PlanStep,
    memory: PlanMemory | None,
    *,
    models: ModelConfig | None = None,
    ledger: Ledger | None = None,
    client: Any | None = None,
    memory_budget_tokens: int = _DEFAULT_MEMORY_BUDGET,
    brain_role: str = "brain",
    assumption_level: str = "auto",
    conversation_context: str = "",
) -> Resolution:
    """Classify an executor question: self_answer (technical) or escalate (product).

    Reads a window-aware memory slice so self-answers stay consistent with earlier
    decisions. The **assumption dial** (``assumption_level``) biases the
    self-answer-vs-escalate threshold globally: a low dial assumes more (escalates
    rarely), a high dial asks more, ``auto`` is the brain's normal-mode judgment.
    Still biased to ``escalate`` when the reply is unparseable/ambiguous, because a
    wrong self-answer silently builds the wrong thing.

    ``conversation_context`` is a window-bounded slice of the continuous transcript
    (the dialogue so far). When present, the brain phrases an escalation as the next
    turn of that conversation -- a continuation, not a context-less popup.
    """
    mem_ctx = _memory_context(
        memory, question, memory_budget_tokens, client=client, models=models, ledger=ledger
    )
    memory_block = (
        f"Relevant memory (facts/decisions already established):\n{mem_ctx}\n\n" if mem_ctx else ""
    )
    convo_block = (
        f"The conversation so far (continue it; reference it when escalating):\n"
        f"{conversation_context}\n\n" if conversation_context else ""
    )
    system = f"{_ANSWER_SYSTEM}\n{assumption_directive(assumption_level)}"
    user = (
        f"Goal: {goal}\n\n"
        f"Current step: [{step.index}] {step.instruction}\n\n"
        f"{convo_block}"
        f"The executor asks: {question}\n\n"
        f"{memory_block}"
        f"{_ANSWER_GRAMMAR}"
    )
    reply = call_model(
        brain_role,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        models=models,
        ledger=ledger,
        client=client,
    ).text
    return _parse_resolution(reply, question)


def _parse_resolution(text: str, question: str) -> Resolution:
    decision = (_tag("decision", text) or "").lower()
    answer = (_tag("answer", text) or "").strip()
    ask = (_tag("ask_user", text) or "").strip()
    reasoning = (_tag("reason", text) or "").strip()
    records = _parse_records(text)
    if decision == "self_answer" and answer:
        return Resolution(kind="self_answer", answer=answer, reasoning=reasoning, records=records)
    # Conservative default: anything unclear -> escalate (lean to the user).
    return Resolution(
        kind="escalate", question_for_user=(ask or question), reasoning=reasoning, records=records
    )


def evolve_plan(
    goal: str,
    plan: Plan,
    reason: str,
    memory: PlanMemory | None,
    *,
    models: ModelConfig | None = None,
    ledger: Ledger | None = None,
    client: Any | None = None,
    memory_budget_tokens: int = _DEFAULT_MEMORY_BUDGET,
    max_plan_retries: int = 2,
    brain_role: str = "brain",
) -> Plan | None:
    """Revise the remaining plan tail as a projection over memory + goal.

    Triggered by a ``revise_plan`` review verdict (vs :func:`replan`, triggered by
    failure). Preserves completed steps; returns a revised tail or ``None`` on
    ``<abort>``.
    """
    outcomes_text = (
        "\n".join(
            f"- [{idx}] {instr}  ->  {outcome}" for idx, instr, outcome in plan.completed_outcomes()
        )
        or "(none completed yet)"
    )
    mem_ctx = _memory_context(
        memory, f"{goal} {reason}", memory_budget_tokens, client=client, models=models, ledger=ledger
    )
    memory_block = f"What has been learned (memory):\n{mem_ctx}\n\n" if mem_ctx else ""
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _EVOLVE_SYSTEM},
        {
            "role": "user",
            "content": (
                f"Goal: {goal}\n\n"
                f"Plan so far:\n{_render_plan(plan)}\n\n"
                f"Completed outcomes:\n{outcomes_text}\n\n"
                f"{memory_block}"
                f"Why the remaining plan should change: {reason}\n\n"
                "Emit a revised <plan> for the REMAINING work only (do not repeat completed "
                "steps), or <abort>reason</abort>."
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
