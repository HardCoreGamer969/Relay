"""Conversational planning: a multi-turn dialogue that reaches a committed plan.

Today the brain plans in one shot and the user can only approve/decline. This
module makes planning a *conversation* (a PRE-EXECUTION phase): the brain assesses
the goal's scope, proposes or asks per posture + the assumption dial, the user
reacts in plain language, the brain revises, and only on commit does it hand the
finalized :class:`~relay.planner.Plan` to ``run_planned``.

Two rules hold this together:

1. **The assumption dial is a global bias.** ``assumption_level`` (from
   ``relay.config``) modulates how much the brain assumes vs. asks here, exactly
   as it does in the autonomous loop's ``answer_or_escalate``. Level 1 -> assume
   freely (propose-fast, ask almost nothing); level 5 -> follow the letter
   (elicit first, ask freely); ``auto`` -> the brain judges.

2. **Dual-fidelity is DERIVED, never parallel.** The brain writes ONE precise
   executor plan; the plain-language version the user sees is *derived from those
   exact steps* (:func:`_derive_plain`) -- not a second, independent generation.
   If they were produced separately they would drift, and the user would approve
   a friendly sentence while a different reality got built. The consequential
   ADDITIONS the brain made compiling vague intent into a precise spec (e.g.
   "make login simple" -> "Google sign-in only, no passwords") are surfaced as
   assumptions for confirmation.

Scope guards (this is prompt A of B): the conversation is a pre-execution phase
only -- no persistent transcript, no mid-run continuity, no TUI. Those are next.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from relay.config import ModelConfig, assumption_directive
from relay.context import DEFAULT_CONTEXT_WINDOW
from relay.memory import MemoryBus, PlanMemory, memory_budget
from relay.models import call_model
from relay.planner import MAX_PLAN_STEPS, Plan, project_digest
from relay.protocol import parse
from relay.telemetry import Ledger
from relay.transcript import Transcript, record_decision

DEFAULT_MAX_ROUNDS = 6
MAX_ELICITATION_QUESTIONS = 3

# Scope size -> posture, modulated by the dial. Level 1 always proposes fast
# (assume freely, even on big/ambiguous goals -- "build it, I'll react"); level 5
# always elicits first (ask, even on small goals); 2-4/auto follow the scope with
# graded nudges. This is the dial sliding the threshold, in code, so its effect on
# posture is deterministic and testable.
_POSTURE_BY_LEVEL = {
    "1": {"small": "propose_fast", "large": "propose_fast", "ambiguous": "propose_fast"},
    "2": {"small": "propose_fast", "large": "propose_fast", "ambiguous": "scoping_question"},
    "3": {"small": "propose_fast", "large": "elicit_first", "ambiguous": "scoping_question"},
    "auto": {"small": "propose_fast", "large": "elicit_first", "ambiguous": "scoping_question"},
    "4": {"small": "elicit_first", "large": "elicit_first", "ambiguous": "scoping_question"},
    "5": {"small": "elicit_first", "large": "elicit_first", "ambiguous": "elicit_first"},
}

EventSink = Callable[[str, str, dict], None]

# How the brain may classify the user's reaction to a proposed plan. The category
# comes from the MODEL understanding the reply, never a keyword match (see
# _classify_reaction): there is a language model right here that understands
# intent, so interpreting the human's natural-language reply is its judgment.
_REACTION_CATEGORIES = {"approve", "approve_changes", "revise", "reject", "unclear"}

# The fallback clarifying question when the brain classifies a reply as unclear but
# offers no question of its own. Commit spends money and edits files, so an
# ambiguous reply is NEVER guessed as approve -- Relay asks instead.
_DEFAULT_CLARIFY = (
    "Just to confirm -- should I go ahead and build this, or do you want changes first?"
)

_SCOPE_SYSTEM = """\
You are the PLANNER (the "brain") of Relay. Before planning, assess the goal's
SCOPE: how much here is both consequential AND undetermined by the goal as stated?
- "small": self-contained, one obvious shape (e.g. a Python game, a static page).
- "large": forking, many consequential undetermined choices (e.g. a full app).
- "ambiguous": you genuinely cannot tell which it is from the goal alone
  (e.g. "build a website" -- a landing page or an e-commerce store?).
Then list the FEW highest-stakes questions that are genuinely undetermined AND
that a non-expert user could actually judge as an outcome. Do NOT ask trivia the
user cannot weigh (e.g. "retry 3 times or 5?") -- decide those yourself later.
"""

_SCOPE_GRAMMAR = (
    "Reply using ONLY these tags:\n"
    "<scope>small|large|ambiguous</scope>\n"
    "<reason>one sentence</reason>\n"
    "<ask>a high-stakes, genuinely-undetermined, user-judgable question</ask>"
    "  (zero or more; omit for a clearly small goal)"
)

_PROPOSE_SYSTEM = """\
You are the PLANNER (the "brain") of Relay. Produce ONE precise, executor-ready
plan as ordered <step> tags -- each a concrete action an executor can carry out.

FOUNDING RULE: the plain-language plan the user will see is DERIVED from these
exact steps -- do NOT write a separate friendly description. Write the steps so
they read clearly AND are precise enough to execute.

When you turn vague intent into a precise choice, SURFACE that choice as an
<assume> (plain language) so the user can confirm or correct it -- e.g.
"make login simple" -> assume "Google sign-in only, no passwords". These surfaced
assumptions are the consequential additions, not restatements of the goal.

ALSO emit a <headline>: ONE or TWO plain sentences a non-coder can follow, saying
what you'll build at a high level -- e.g. "A single-file Python todo CLI with
add/list/done/delete and JSON storage in the project directory." The headline is
the SAME plan at a plainer fidelity (it must describe THESE steps, not something
else); keep it free of file names, code, and step-by-step detail. It is what the
ongoing conversation record shows; the full steps stay in the plan itself.
"""

_PROPOSE_GRAMMAR = (
    "Reply using ONLY these tags:\n"
    "<plan><step>precise executor-sized step</step>...</plan>\n"
    "<headline>one or two plain sentences describing the plan at a high level</headline>\n"
    "<assume>a consequential choice you made, in plain language</assume>  (zero or more)"
)

_PLAN_NUDGE = "Emit a <plan> with <step>...</step> children (and any <assume> tags)."

# The brain interprets the user's plain-language reaction to a proposed plan. The
# point is that the CATEGORY is the model's judgment of intent -- not a keyword
# match -- mirroring how a mature agent reads "yes that looks perfect!" the same
# way it reads "ok". Parsed deterministically from the <reaction> tag below.
_REACTION_SYSTEM = """\
You are the PLANNER (the "brain") of Relay. You proposed a plan and the user has
replied in plain language. INTERPRET their reply the way a person would -- do NOT
keyword-match -- and classify what they want so Relay can react:

- approve: they are satisfied and want you to proceed AS PROPOSED. "ok", "yes that
  looks perfect!", "go for it", "ship it", "lgtm" all mean approve.
- approve_changes: they approve the direction BUT ask for a modification in the
  same breath (e.g. "looks good, but make step 2 use Postgres"). Capture the exact
  change so it can be folded in before proceeding -- do not drop it.
- revise: they want changes or are not satisfied and expect a NEW proposal (e.g.
  "change the approach", "no, do X instead"). Capture the feedback.
- reject: they want to STOP entirely (e.g. "no", "cancel", "scrap it").
- unclear: the reply is genuinely ambiguous -- you cannot confidently tell which of
  the above it is (e.g. "hmm, maybe"). Committing spends money and edits files, so
  do NOT guess approve: ask ONE brief clarifying question instead.
"""

_REACTION_GRAMMAR = (
    "Reply using ONLY these tags:\n"
    "<reaction>approve|approve_changes|revise|reject|unclear</reaction>\n"
    "<change>the exact change or feedback to fold in</change>"
    "  (REQUIRED for approve_changes and revise; omit otherwise)\n"
    "<clarify>one brief clarifying question</clarify>"
    "  (REQUIRED for unclear; omit otherwise)"
)


@dataclass
class ScopeAssessment:
    """The brain's scope read for a goal (a visible, logged step)."""

    size: str  # "small" | "large" | "ambiguous"
    reason: str = ""
    questions: list[str] = field(default_factory=list)


@dataclass
class ConversationResult:
    """The outcome of :func:`plan_conversationally`."""

    goal: str
    scope: str = ""
    scope_reason: str = ""
    posture: str = ""  # "propose_fast" | "elicit_first" | "scoping_question"
    plan: Plan | None = None
    plain_plan: str = ""  # the plain rendering shown to the user (DERIVED from plan)
    assumptions: list[str] = field(default_factory=list)
    committed: bool = False
    rounds: int = 0
    questions_asked: int = 0  # scoping + elicitation questions put to the user
    events: list[dict] = field(default_factory=list)
    # The continuous conversation thread (this planning dialogue's turns). The same
    # object is handed to ``run_planned`` so a mid-run escalation continues it.
    transcript: Transcript | None = None


# --- small parse helpers ----------------------------------------------------


def _tag(name: str, text: str) -> str | None:
    match = re.search(rf"<{name}>(.*?)</{name}>", text or "", re.DOTALL)
    return match.group(1).strip() if match else None


def _tags(name: str, text: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(rf"<{name}>(.*?)</{name}>", text or "", re.DOTALL) if m.group(1).strip()]


def _posture(scope: str, level: str) -> str:
    table = _POSTURE_BY_LEVEL.get(level, _POSTURE_BY_LEVEL["auto"])
    return table.get(scope, "propose_fast")


def _derive_plain(plan: Plan, assumptions: list[str]) -> str:
    """Render the plain-language plan FROM the precise steps (the single source).

    This is a pure function of ``plan`` + ``assumptions`` -- the plain text cannot
    diverge from the executor spec because it is computed from it.
    """
    lines = ["Here is the plan:"]
    for i, step in enumerate(plan.steps, 1):
        lines.append(f"  {i}. {step.instruction}")
    if assumptions:
        lines.append("")
        lines.append("Assumptions I'm making (tell me if any are wrong):")
        for assumption in assumptions:
            lines.append(f"  - {assumption}")
    lines.append("")
    lines.append(
        "React in plain language -- e.g. \"make it dark mode\" to change it, or "
        "\"looks good\" to build it. (I read your reply, so say it however you like.)"
    )
    return "\n".join(lines)


# --- brain calls ------------------------------------------------------------


def _memory_slice(memory, goal, *, client, models, ledger) -> str:
    # v0.0.29: with a MemoryBus the planning conversation is a brain reader, so it
    # sees brain UNION shared, budgeted to the bus's configured brain window (this
    # also fixes the old hardcoded default-window budget for the bus case). A plain
    # PlanMemory keeps the single-pool, default-window behavior unchanged.
    if isinstance(memory, MemoryBus):
        return memory.brain_context(
            goal, memory.brain_budget, client=client, models=models, ledger=ledger
        )
    if memory is None or not getattr(memory, "entries", None):
        return ""
    return memory.compacted_context(
        goal, budget_tokens=memory_budget(DEFAULT_CONTEXT_WINDOW), client=client, models=models, ledger=ledger
    )


def _assess_scope(
    goal, digest, level, mem_ctx, *, models, ledger, client, brain_role
) -> ScopeAssessment:
    system = f"{_SCOPE_SYSTEM}\n{assumption_directive(level)}"
    memory_block = f"Relevant memory:\n{mem_ctx}\n\n" if mem_ctx else ""
    user = (
        f"Goal: {goal}\n\n"
        f"Project digest (shallow listing of the root):\n{digest}\n\n"
        f"{memory_block}"
        f"{_SCOPE_GRAMMAR}"
    )
    reply = call_model(
        brain_role,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        models=models, ledger=ledger, client=client,
    ).text
    size = (_tag("scope", reply) or "small").lower()
    if size not in ("small", "large", "ambiguous"):
        size = "small"
    return ScopeAssessment(size=size, reason=_tag("reason", reply) or "", questions=_tags("ask", reply))


def _derive_headline(plan: Plan, headline: str) -> str:
    """The plain, human-facing headline for a proposal turn.

    Prefers the brain's ``<headline>`` (emitted in the SAME generation as the plan,
    so it costs no extra call and reflects the same intent). Falls back to a
    deterministic line computed FROM the plan when the brain omits it, so the
    transcript turn is always a short headline -- never the full executor spec.
    """
    headline = " ".join((headline or "").split())
    if headline:
        return headline
    steps = plan.steps
    if not steps:
        return "Proposed a plan (no steps)."
    first = " ".join(steps[0].instruction.split())
    if len(first) > 100:
        first = first[:97] + "..."
    return f"Proposed a {len(steps)}-step plan; it begins by: {first}"


def _propose(
    goal, digest, level, answers, prior_plan, reaction, mem_ctx,
    *, models, ledger, client, brain_role, max_retries=1,
) -> tuple[Plan, list[str], str]:
    system = f"{_PROPOSE_SYSTEM}\n{assumption_directive(level)}"
    answers_block = ""
    if answers:
        answers_block = "What the user told you (use these):\n" + "\n".join(
            f"- Q: {q}\n  A: {a}" for q, a in answers
        ) + "\n\n"
    prior_block = ""
    if prior_plan is not None and reaction:
        prior_block = (
            "Current plan steps:\n"
            + "\n".join(f"- {s.instruction}" for s in prior_plan.steps)
            + f"\n\nThe user reacted: {reaction}\n"
            "Fold this reaction into a revised plan (keep what still applies).\n\n"
        )
    memory_block = f"Relevant memory:\n{mem_ctx}\n\n" if mem_ctx else ""
    base_user = (
        f"Goal: {goal}\n\n"
        f"Project digest (shallow listing of the root):\n{digest}\n\n"
        f"{answers_block}{prior_block}{memory_block}{_PROPOSE_GRAMMAR}"
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": base_user}]

    for _ in range(max_retries + 1):
        reply = call_model(brain_role, messages, models=models, ledger=ledger, client=client).text
        parsed = parse(reply)
        plan_action = parsed.first("plan")
        if plan_action is not None and plan_action.steps:
            plan = Plan.from_instructions(plan_action.steps[:MAX_PLAN_STEPS])
            # Headline is parsed from the SAME reply -- no extra brain call.
            return plan, _tags("assume", reply), _derive_headline(plan, _tag("headline", reply) or "")
        if ledger is not None:
            ledger.record_parse_failure()
        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": _PLAN_NUDGE})

    return Plan(steps=[]), [], ""  # bounded: give up to an empty plan rather than loop


@dataclass
class ReactionClassification:
    """The brain's read of one user reaction to a proposed plan.

    ``category`` is one of :data:`_REACTION_CATEGORIES`; ``change`` carries the
    requested modification/feedback (for approve_changes / revise) and ``clarify``
    the brain's question (for unclear). This is the COMMIT GATE: a plan commits
    only when the brain interprets the reply as approve (or approve_changes, after
    folding the change) -- there is no affirmative-keyword check anywhere.
    """

    category: str
    change: str = ""
    clarify: str = ""


def _classify_reaction(
    goal, plan, reaction, *, models, ledger, client, brain_role
) -> ReactionClassification:
    """Have the brain INTERPRET the user's reply to the proposed plan.

    Reuses the ``call_model("brain", ...)`` path and the text-tag protocol (no
    native tool-calling): the brain emits ``<reaction>...</reaction>`` (+ ``<change>``
    / ``<clarify>`` where relevant), parsed deterministically here. An unparseable
    or unknown classification is treated as ``unclear`` -- NEVER an implicit approve
    -- so a confused model can't trigger a spend-and-edit commit.
    """
    steps_block = "\n".join(f"- {s.instruction}" for s in plan.steps) or "(no steps)"
    user = (
        f"Goal: {goal}\n\n"
        f"The plan you proposed:\n{steps_block}\n\n"
        f"The user's reply:\n{reaction}\n\n"
        f"{_REACTION_GRAMMAR}"
    )
    reply = call_model(
        brain_role,
        [{"role": "system", "content": _REACTION_SYSTEM}, {"role": "user", "content": user}],
        models=models, ledger=ledger, client=client,
    ).text
    category = (_tag("reaction", reply) or "").lower().strip()
    if category not in _REACTION_CATEGORIES:
        category = "unclear"
    return ReactionClassification(
        category=category, change=_tag("change", reply) or "", clarify=_tag("clarify", reply) or ""
    )


def plan_conversationally(
    goal: str,
    project_root: str | Path,
    *,
    models: ModelConfig | None = None,
    ledger: Ledger | None = None,
    client: Any | None = None,
    assumption_level: str = "auto",
    user_turn: Callable[[str], str],
    memory: PlanMemory | None = None,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    brain_role: str = "brain",
    on_event: EventSink | None = None,
    transcript: Transcript | None = None,
) -> ConversationResult:
    """Run the pre-execution planning conversation, returning a committed plan.

    ``user_turn(prompt) -> reply`` is the seam that gets the user's next reply
    (the CLI wires it to an interactive prompt; tests script it). Flow: assess
    scope (logged) -> ask per posture + dial -> propose -> the user reacts in
    free-form natural language -> fold + re-render -> repeat until commit or
    ``max_rounds`` (never loops forever). On commit, ``result.plan`` is the
    finalized :class:`~relay.planner.Plan` ready for ``run_planned``.

    ``transcript`` is the ONE continuous conversation thread. The planning turns
    (proposal, the user's reactions, the commit) are appended here; the SAME object
    is then handed to ``run_planned`` so a mid-run escalation is the next turn of
    this dialogue, not a separate popup. A commit is recorded transcript-first via
    :func:`~relay.transcript.record_decision` (memory, if any, is derived from it).
    """
    transcript = transcript if transcript is not None else Transcript()
    result = ConversationResult(goal=goal, transcript=transcript)

    def emit(kind: str, message: str, payload: dict | None = None) -> None:
        event = {"kind": kind, "message": message, "payload": payload or {}}
        result.events.append(event)
        if on_event is not None:
            on_event(kind, message, payload or {})

    digest = project_digest(project_root)
    mem_ctx = _memory_slice(memory, goal, client=client, models=models, ledger=ledger)

    # 1. Scope assessment (visible + logged), then posture from scope + dial.
    assessment = _assess_scope(
        goal, digest, assumption_level, mem_ctx, models=models, ledger=ledger, client=client, brain_role=brain_role
    )
    result.scope = assessment.size
    result.scope_reason = assessment.reason
    posture = _posture(assessment.size, assumption_level)
    result.posture = posture
    emit("scope_assessed", f"scope: {assessment.size} -> posture: {posture}",
         {"scope": assessment.size, "reason": assessment.reason, "posture": posture})

    # 2. Ask per posture (propose_fast asks nothing -- it assumes).
    answers: list[tuple[str, str]] = []
    if posture == "scoping_question":
        question = assessment.questions[0] if assessment.questions else (
            f"To scope this, what kind of thing do you have in mind for: {goal}?"
        )
        emit("scoping_question", question, {"question": question})
        transcript.record("brain", "planning", question)
        reply = user_turn(question)
        transcript.record("user", "planning", reply)
        answers.append((question, reply))
        result.questions_asked += 1
    elif posture == "elicit_first":
        for question in assessment.questions[:MAX_ELICITATION_QUESTIONS]:
            emit("elicitation", question, {"question": question})
            transcript.record("brain", "planning", question)
            reply = user_turn(question)
            transcript.record("user", "planning", reply)
            answers.append((question, reply))
            result.questions_asked += 1

    # 3. Propose the initial plan (precise spec + surfaced assumptions).
    plan, assumptions, headline = _propose(
        goal, digest, assumption_level, answers, None, None, mem_ctx,
        models=models, ledger=ledger, client=client, brain_role=brain_role,
    )
    result.plan = plan
    result.assumptions = assumptions
    result.plain_plan = _derive_plain(plan, assumptions)
    emit("plan_proposed", "plan proposed",
         {"plain": result.plain_plan, "steps": [s.instruction for s in plan.steps], "assumptions": assumptions})
    # The transcript (human-facing scroll-back) carries the plain HEADLINE, not the
    # full executor spec; the steps live in the Plan (linked via refs). The live
    # display (result.plain_plan, shown by user_turn) keeps the full detail.
    transcript.record("brain", "proposal", headline, refs=[f"step{s.index}" for s in plan.steps])

    def commit(reaction_text: str) -> ConversationResult:
        """Finalize: commit ``result.plan``. A user-facing decision, so it is
        recorded transcript-first, then derived into memory (provenance links the
        entry to this commit turn)."""
        steps = result.plan.steps
        record_decision(
            transcript, memory,
            text=reaction_text or "(approved as proposed)",
            detail=f"User committed the plan ({len(steps)} step(s)): "
                   + "; ".join(s.instruction for s in steps),
            summary="user committed the plan",
            kind="decision", phase="commit", speaker="user",
            refs=[f"step{s.index}" for s in steps],
        )
        result.committed = True
        emit("committed", "plan committed", {"steps": [s.instruction for s in steps]})
        return result

    def revise_plan(feedback: str) -> None:
        """Fold ``feedback`` into a revised plan and re-render (no commit here)."""
        nonlocal plan, assumptions, headline
        plan, assumptions, headline = _propose(
            goal, digest, assumption_level, answers, result.plan, feedback, mem_ctx,
            models=models, ledger=ledger, client=client, brain_role=brain_role,
        )
        result.plan = plan
        result.assumptions = assumptions
        result.plain_plan = _derive_plain(plan, assumptions)
        emit("plan_revised", "plan revised",
             {"plain": result.plain_plan, "steps": [s.instruction for s in plan.steps], "assumptions": assumptions})
        transcript.record("brain", "proposal", headline, refs=[f"step{s.index}" for s in plan.steps])

    # 4. Reaction loop: the user reacts in plain language; the BRAIN interprets the
    # reply (no keyword match) and we react to the category, bounded by max_rounds.
    prompt = result.plain_plan
    while result.rounds < max_rounds:
        reaction = user_turn(prompt)
        result.rounds += 1
        classification = _classify_reaction(
            goal, result.plan, reaction,
            models=models, ledger=ledger, client=client, brain_role=brain_role,
        )
        category = classification.category

        if category == "approve":
            return commit(reaction)

        # Every other category records the user's reaction turn for the thread.
        emit("user_reacted", reaction, {"reaction": reaction, "reaction_class": category})
        transcript.record("user", "reaction", reaction)

        if category == "reject":
            emit("rejected", "user rejected the plan", {"reaction": reaction})
            return result  # aborts cleanly: not committed

        if category == "unclear":
            # Ambiguous reply: ASK rather than guess approve (commit spends money +
            # edits files). The clarifying question is the next prompt shown.
            question = classification.clarify or _DEFAULT_CLARIFY
            emit("clarify", question, {"question": question})
            transcript.record("brain", "planning", question)
            result.questions_asked += 1
            prompt = question
            continue

        if category == "approve_changes":
            # The user approved the direction but asked for a tweak in the same
            # breath: fold it in, then PROCEED with the revised plan (never commit
            # the un-modified plan and lose the change).
            revise_plan(classification.change or reaction)
            return commit(reaction)

        # revise: the user wants a new proposal -> re-plan with their feedback and
        # re-present next round (if a round remains to show it).
        if result.rounds >= max_rounds:
            break  # no further round to show a revision; stop without committing
        revise_plan(classification.change or reaction)
        prompt = result.plain_plan

    emit("not_committed", f"no commit within {max_rounds} round(s)", {})
    return result
