"""The orchestrator: the autonomous two-role brain/hands loop.

The brain plans the full ordered sequence up front; the hands execute each step
in a NARROW context (the current step instruction + one-line outcomes of
completed steps -- NOT the full plan, NOT the brain's reasoning, NOT prior
steps' raw transcripts).

v0.06 (2 of 2) removes the human from the middle of the loop. The brain now:
  - SUPERVISES at step boundaries (one review call per step; default-on knob),
    judging the executor's work -> accept / follow_up / revise_plan;
  - ANSWERS the executor's mid-step <question>s ITSELF when they are technical
    (decidable from code + memory), ESCALATING to the user only for genuine
    product decisions (logged, biased conservative);
  - LEARNS into a within-run PlanMemory and EVOLVES the remaining plan from it.

Every brain<->executor exchange is a first-class Event. Memory reads are
window-aware (budget-bounded, never the whole store) via the v0.06 part-1
substrate. All loops are bounded so a weak model cannot burn money in a spiral.
``relay.loop.run_task`` (single-model ``--solo``) is left untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from relay.config import ModelConfig, load_models
from relay.context import resolve_context_window
from relay.loop import (
    MAX_CONSECUTIVE_PARSE_FAILURES,
    STATUS_COMPLETED,
    STATUS_MAX_STEPS,
    describe_action,
    execute_action,
)
from relay.memory import PlanMemory, memory_budget
from relay.models import call_model
from relay.planner import (
    Plan,
    PlanStep,
    answer_or_escalate,
    evolve_plan,
    make_plan,
    replan,
    review_step,
)
from relay.protocol import parse
from relay.telemetry import Ledger
from relay.tools import Tools

# Terminal statuses. STATUS_COMPLETED / STATUS_MAX_STEPS are shared with the
# single-model loop; the rest are unique to the two-role run.
STATUS_PLANNING_FAILED = "planning_failed"
STATUS_ABORTED_BY_BRAIN = "aborted_by_brain"
STATUS_ESCALATION_LIMIT = "escalation_limit"
STATUS_DECLINED = "declined_by_user"  # plan shown but not approved (--confirm-plan)
# A genuine product decision was needed but no user_decision callback was
# available, so the run stopped rather than guessing (the silent-wrong-build trap).
STATUS_UNRESOLVED_ESCALATION = "unresolved_escalation"

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
  <question>a question you need answered to proceed</question>
  <done>one-line summary of what THIS step accomplished</done>
  <blocked>reason you cannot complete this step</blocked>

Rules:
- Paths are relative to the project root; you cannot escape it.
- Some destructive bash commands are refused by policy ("BLOCKED by policy: ...")
  or need approval ("DENIED ..."); adapt rather than re-emitting them verbatim.
- Stay scoped to YOUR current step. Do NOT do later steps.
- If you need information to proceed, emit <question>...</question>; the planner
  will answer it (or get an answer) and you will see "ANSWER: ..." -- then continue.
- When THIS step is complete, emit <done>...</done>. If you are genuinely stuck,
  emit <blocked>reason</blocked> early rather than thrashing.
"""

_EXEC_PARSE_NUDGE = (
    "No valid action was found. Emit exactly one protocol tag, e.g. "
    '<read path="..."/>, <edit path="...">...</edit>, <bash>...</bash>, '
    "<question>...</question>, <done>...</done>, or <blocked>...</blocked>."
)

# Per-entry cap for the reviewer-facing transcript copy, so one huge file read or
# bash output cannot bloat memory or the review prompt. (The executor's own
# message context keeps the full observation -- it needs it.)
_TRANSCRIPT_ENTRY_CAP = 2000

# Callback the CLI uses to stream the run; receives whole Events.
EventCallback = Callable[["Event"], None]


@dataclass
class Event:
    """One streamed orchestration event (plan, step, question, review, ...)."""

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
    memory: PlanMemory | None = None
    revisions: int = 0

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
    transcript: list[str] = field(default_factory=list)  # what the executor did
    unresolved: bool = False   # a product-decision escalation could not be resolved
    unresolved_question: str = ""


@dataclass
class _QuestionResolution:
    """How an executor question was resolved (returned to the executor loop)."""

    answer: str | None
    unresolved: bool = False


@dataclass
class _Disposition:
    """How a (supervised) step settled, for the outer loop to act on."""

    kind: str  # "done" | "revise" | "failed" | "unresolved"
    summary: str = ""
    revise_reason: str = ""
    failure_reason: str = ""
    unresolved_question: str = ""
    records: list[tuple[str, str, str]] = field(default_factory=list)
    calls: int = 0


def _executor_step_prompt(
    goal: str, step: PlanStep, plan: Plan, *, extra_instruction: str | None = None
) -> str:
    """Build the executor's NARROW per-step context.

    Deliberately excludes the full plan, the brain's reasoning, and prior steps'
    raw transcripts -- only the current instruction plus one-line carry-over.
    ``extra_instruction`` carries a reviewer follow-up for a corrective attempt.
    """
    lines = [f"Overall goal (context only): {goal}", ""]
    carry = plan.completed_outcomes()
    if carry:
        lines.append("Already completed (one-line outcomes):")
        for _index, _instruction, outcome in carry:
            lines.append(f"- {outcome}")
        lines.append("")
    lines.append(f"YOUR CURRENT STEP: {step.instruction}")
    if extra_instruction:
        lines.append("")
        lines.append(f"REVIEWER FEEDBACK (address this, then re-emit <done>): {extra_instruction}")
    lines.append("")
    lines.append(
        "Do ONLY this step, one action at a time. Emit <done>one-line result</done> "
        "when THIS step is complete, <question>...</question> if you need info, or "
        "<blocked>reason</blocked> if you are stuck. Begin."
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
    resolve_question: Callable[[str], _QuestionResolution] | None = None,
    extra_instruction: str | None = None,
) -> _StepOutcome:
    """Run the hands in a fresh, narrow context until done/blocked/budget/question.

    On a ``<question>`` the executor pauses: ``resolve_question`` is consulted
    (the brain answers or escalates); a returned answer is fed back as an
    observation and the step continues; an unresolved escalation ends the step.
    """
    messages: list[dict[str, str]] = [
        {"role": "system", "content": EXECUTOR_SYSTEM_PROMPT},
        {"role": "user", "content": _executor_step_prompt(goal, step, plan, extra_instruction=extra_instruction)},
    ]
    transcript: list[str] = []
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
                    False, failure_reason="executor produced no valid actions (parse-failure abort)",
                    calls=calls, transcript=transcript,
                )
            messages.append({"role": "user", "content": _EXEC_PARSE_NUDGE})
            continue
        consecutive_parse_failures = 0

        observations: list[str] = []
        for action in parsed.actions:
            if action.kind == "blocked":
                reason = action.content or "no reason given"
                return _StepOutcome(False, failure_reason=f"blocked: {reason}", calls=calls, transcript=transcript)
            if action.kind == "done":
                return _StepOutcome(True, summary=action.content or "step complete", calls=calls, transcript=transcript)
            if action.kind == "question":
                question = action.content or ""
                if resolve_question is None:
                    return _StepOutcome(
                        False, failure_reason=f"executor asked a question but no resolver was available: {question}",
                        calls=calls, transcript=transcript,
                    )
                resolution = resolve_question(question)
                if resolution.unresolved:
                    return _StepOutcome(
                        False, failure_reason=f"unresolved escalation: {question}", calls=calls,
                        transcript=transcript, unresolved=True, unresolved_question=question,
                    )
                answer_obs = f"[question]\nANSWER: {resolution.answer}"
                observations.append(answer_obs)
                transcript.append(answer_obs[:_TRANSCRIPT_ENTRY_CAP])
                break  # stop this turn; feed the answer back so the executor continues
            if action.kind in ("plan", "abort"):
                observations.append(
                    f"[{action.kind}]\nnote: you are the executor. Emit <done> when this step "
                    "is complete, <question> if you need info, or <blocked> if stuck -- do not plan."
                )
                continue
            observation = execute_action(tools, action)
            if emit is not None:
                emit("exec_action", describe_action(action), {"kind": action.kind, "observation": observation})
            rendered = f"[{describe_action(action)}]\n{observation}"
            observations.append(rendered)
            transcript.append(rendered[:_TRANSCRIPT_ENTRY_CAP])

        messages.append(
            {"role": "user", "content": "\n\n".join(observations) if observations else "(no output)"}
        )

    return _StepOutcome(
        False, failure_reason=f"step not completed within {max_steps} executor steps",
        calls=calls, transcript=transcript,
    )


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
    memory: PlanMemory | None = None,
    supervise: bool = True,
    user_decision: Callable[[str], str] | None = None,
    max_investigation_steps: int = 5,
    max_executor_steps: int = 12,
    max_escalations: int = 3,
    max_followups_per_step: int = 2,
    max_plan_revisions: int = 5,
    max_total_steps: int | None = None,
    context_window: int | None = None,
    on_event: EventCallback | None = None,
    plan_gate: Callable[[Plan], bool] | None = None,
) -> PlannedTaskResult:
    """Drive the autonomous two-role planner/executor loop.

    The brain plans up front; the hands execute each step; the brain supervises at
    step boundaries (``supervise``), answers executor ``<question>``s itself or
    escalates product decisions to ``user_decision``, learns into ``memory``, and
    evolves the remaining plan. A failed step still escalates to ``replan``
    (v0.04). All loops are bounded.

    Knobs: ``supervise`` (default on), ``max_followups_per_step``,
    ``max_plan_revisions``, ``max_escalations``, ``max_total_steps`` (overall
    executor-call budget), and ``context_window`` (override the resolved window
    used to size memory reads). ``user_decision(question) -> answer`` is the
    escalation seam; with none available, a product decision ends the run with
    status ``unresolved_escalation`` rather than guessing.

    Note: a single step's executor ceiling is ``max_executor_steps *
    (1 + max_followups_per_step)`` (each supervised follow-up re-runs the
    executor with its own budget); ``max_total_steps`` is the hard global cap.
    Review/answer/evolve are brain calls and do NOT count against the executor
    budget.
    """
    ledger = ledger if ledger is not None else Ledger()
    memory = memory if memory is not None else PlanMemory()
    result = PlannedTaskResult(goal=goal, ledger=ledger, memory=memory)

    cfg = models if models is not None else load_models()
    window, _source = resolve_context_window(cfg.brain, client=client, override=context_window)
    mem_budget = memory_budget(window)

    def emit(kind: str, message: str, payload: dict | None = None) -> None:
        event = Event(kind, message, payload or {})
        result.events.append(event)
        if on_event is not None:
            on_event(event)

    def remember(kind: str, detail: str, summary: str, *, provenance: str, tags=None) -> None:
        memory.remember(kind, detail, summary, provenance=provenance, tags=list(tags or []))
        emit("memory_write", f"{kind}: {summary}", {"kind": kind, "summary": summary, "provenance": provenance})

    # --- Plan phase --------------------------------------------------------
    plan = make_plan(
        goal, project_root, models=models, ledger=ledger, client=client,
        max_investigation_steps=max_investigation_steps, brain_role=brain_role,
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
    revisions = 0
    executor_calls = 0

    def make_question_resolver(step: PlanStep) -> Callable[[str], _QuestionResolution]:
        """Resolve an executor question: brain self-answers, or escalates to the user."""

        def resolve(question: str) -> _QuestionResolution:
            emit("executor_question", question, {"index": step.index, "question": question})
            resolution = answer_or_escalate(
                question, goal, plan, step, memory, models=models, ledger=ledger,
                client=client, memory_budget_tokens=mem_budget, brain_role=brain_role,
            )
            for kind, detail, summary in resolution.records:
                remember(kind, detail, summary, provenance=f"step{step.index} brain")
            if resolution.kind == "self_answer":
                remember(
                    "decision", f"Q: {question} -> A: {resolution.answer}",
                    f"answered '{question}': {resolution.answer}", provenance=f"step{step.index} self-answer",
                )
                emit("brain_self_answered", question,
                     {"question": question, "answer": resolution.answer, "reasoning": resolution.reasoning})
                return _QuestionResolution(answer=resolution.answer)

            emit("brain_escalated", resolution.question_for_user,
                 {"question": resolution.question_for_user, "reasoning": resolution.reasoning})
            if user_decision is None:
                return _QuestionResolution(answer=None, unresolved=True)
            answer = user_decision(resolution.question_for_user)
            remember(
                "confirmation", f"User decided on '{resolution.question_for_user}': {answer}",
                f"user: {answer}", provenance="user",
            )
            emit("user_decided", answer, {"question": resolution.question_for_user, "answer": answer})
            return _QuestionResolution(answer=answer)

        return resolve

    def run_step(step: PlanStep, step_budget: int) -> _Disposition:
        """Run the executor (with bounded supervised follow-ups) and settle the step."""
        nonlocal executor_calls
        resolver = make_question_resolver(step)
        records: list[tuple[str, str, str]] = []

        outcome = _run_executor_step(
            step, plan, goal, tools, hands_role=hands_role, models=models, ledger=ledger,
            client=client, max_steps=step_budget, emit=emit, resolve_question=resolver,
        )
        executor_calls += outcome.calls
        followups_used = 0

        while True:
            if outcome.unresolved:
                return _Disposition("unresolved", unresolved_question=outcome.unresolved_question, records=records)
            if not outcome.success:
                return _Disposition("failed", failure_reason=outcome.failure_reason, records=records)
            if not supervise:
                return _Disposition("done", summary=outcome.summary, records=records)

            review = review_step(
                goal, plan, step, outcome.summary, outcome.transcript, memory, models=models,
                ledger=ledger, client=client, memory_budget_tokens=mem_budget, brain_role=brain_role,
            )
            emit("step_reviewed", f"step {step.index} review: {review.verdict}",
                 {"index": step.index, "verdict": review.verdict, "followup": review.followup, "reason": review.reason})
            records.extend(review.records)

            if review.verdict == "accept":
                return _Disposition("done", summary=outcome.summary, records=records)

            if review.verdict == "revise_plan":
                return _Disposition("revise", summary=outcome.summary, revise_reason=review.reason or "review", records=records)

            # follow_up
            if followups_used >= max_followups_per_step:
                return _Disposition(
                    "failed",
                    failure_reason=f"step not accepted after {max_followups_per_step} follow-up(s)",
                    records=records,
                )
            followups_used += 1
            remember(
                "decision", f"Follow-up on step {step.index}: {review.followup}",
                f"reviewer follow-up: {review.followup}", provenance=f"step{step.index} review",
            )
            outcome = _run_executor_step(
                step, plan, goal, tools, hands_role=hands_role, models=models, ledger=ledger,
                client=client, max_steps=step_budget, emit=emit, resolve_question=resolver,
                extra_instruction=review.followup,
            )
            executor_calls += outcome.calls

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

        emit("step_start", f"step {step.index}: {step.instruction}",
             {"index": step.index, "instruction": step.instruction})

        disposition = run_step(step, step_budget)
        # Persist the brain's review records (provenance: this step's review).
        for kind, detail, summary in disposition.records:
            remember(kind, detail, summary, provenance=f"step{step.index} review")

        if disposition.kind == "unresolved":
            result.status = STATUS_UNRESOLVED_ESCALATION
            emit("status", "a product decision was needed but could not be obtained",
                 {"status": STATUS_UNRESOLVED_ESCALATION, "question": disposition.unresolved_question})
            break

        if disposition.kind == "done":
            plan.mark_done(step, disposition.summary)
            remember("fact", f"Step {step.index} done: {disposition.summary}", disposition.summary,
                     provenance=f"step{step.index}")
            emit("step_done", f"step {step.index} done: {disposition.summary}",
                 {"index": step.index, "outcome": disposition.summary})
            continue

        if disposition.kind == "revise":
            plan.mark_done(step, disposition.summary)
            remember("fact", f"Step {step.index} done: {disposition.summary}", disposition.summary,
                     provenance=f"step{step.index}")
            emit("step_done", f"step {step.index} done: {disposition.summary}",
                 {"index": step.index, "outcome": disposition.summary})
            remember("decision", f"Revise remaining plan after step {step.index}: {disposition.revise_reason}",
                     f"plan revision: {disposition.revise_reason}", provenance=f"step{step.index} review")
            if revisions >= max_plan_revisions:
                emit("status", f"plan-revision budget ({max_plan_revisions}) reached; keeping current plan",
                     {"status": "revision_budget"})
                continue  # don't thrash; proceed with the existing tail
            revisions += 1
            result.revisions = revisions
            revised = evolve_plan(goal, plan, disposition.revise_reason, memory, models=models,
                                  ledger=ledger, client=client, memory_budget_tokens=mem_budget, brain_role=brain_role)
            if revised is None:
                result.status = STATUS_ABORTED_BY_BRAIN
                emit("status", "brain aborted: goal deemed unreachable", {"status": STATUS_ABORTED_BY_BRAIN})
                break
            plan = _adopt_revision(plan, revised)
            result.plan = plan
            emit("plan_revised", f"plan revised: {len(plan.remaining())} new step(s)",
                 {"steps": [s.instruction for s in plan.steps if s.status == "pending"], "reason": disposition.revise_reason})
            continue

        # disposition.kind == "failed" -> record the dead end, escalate to replan.
        plan.mark_failed(step, disposition.failure_reason)
        remember("dead_end", f"Step {step.index} failed: {disposition.failure_reason}",
                 f"failed: {disposition.failure_reason}", provenance=f"step{step.index}")
        emit("step_failed", f"step {step.index} failed: {disposition.failure_reason}",
             {"index": step.index, "reason": disposition.failure_reason})

        if escalations >= max_escalations:
            result.status = STATUS_ESCALATION_LIMIT
            emit("status", f"escalation limit ({max_escalations}) reached", {"status": STATUS_ESCALATION_LIMIT})
            break

        escalations += 1
        result.escalations = escalations
        emit("escalation", f"escalation {escalations}: replanning after step {step.index}",
             {"n": escalations, "failed_index": step.index, "reason": disposition.failure_reason})

        revised = replan(
            goal, plan, step, disposition.failure_reason, plan.completed_outcomes(),
            models=models, ledger=ledger, client=client, brain_role=brain_role,
            memory=memory, memory_budget_tokens=mem_budget,
        )
        if revised is None:
            result.status = STATUS_ABORTED_BY_BRAIN
            emit("status", "brain aborted: goal deemed unreachable", {"status": STATUS_ABORTED_BY_BRAIN})
            break

        plan = _adopt_revision(plan, revised)
        result.plan = plan
        emit("replanned", f"revised plan adopted: {len(plan.remaining())} new step(s)",
             {"steps": [s.instruction for s in plan.steps if s.status == "pending"]})

    return result
