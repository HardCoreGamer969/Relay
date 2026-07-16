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

import platform
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from relay.config import ModelConfig, load_models
from relay.context import resolve_context_window
from relay.envelope import CostEnvelope, brain_cost_since
from relay.explain import explain_events
from relay.loop import (
    MAX_CONSECUTIVE_PARSE_FAILURES,
    STATUS_COMPLETED,
    STATUS_MAX_STEPS,
    describe_action,
    guarded_execute_action,
)
from relay.memory import (
    POOL_BRAIN,
    POOL_SHARED,
    MemoryBus,
    PlanMemory,
    texts_near_duplicate,
)
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
from relay.transcript import Transcript, compact_transcript, record_decision, render_for_brain

# Terminal statuses. STATUS_COMPLETED / STATUS_MAX_STEPS are shared with the
# single-model loop; the rest are unique to the two-role run.
STATUS_PLANNING_FAILED = "planning_failed"
STATUS_ABORTED_BY_BRAIN = "aborted_by_brain"
STATUS_ESCALATION_LIMIT = "escalation_limit"
# v0.0.21: a replan re-issued a step that had ALREADY dead-ended the same way --
# the repeated-dead-end loop. Caught the moment the re-issued step would run, so
# the run fails fast instead of grinding the full escalation budget on it.
STATUS_REPEATED_STEP = "repeated_step"
STATUS_DECLINED = "declined_by_user"  # plan shown but not approved (--confirm-plan)
# A genuine product decision was needed but no user_decision callback was
# available, so the run stopped rather than guessing (the silent-wrong-build trap).
STATUS_UNRESOLVED_ESCALATION = "unresolved_escalation"
# The user asked to stop (v0.0.11 cancel_check, polled at step boundaries). A
# clean terminal status, not an error: a cancelled run still gets its result
# turn and compacted transcript.
STATUS_CANCELLED = "cancelled"
# v0.0.32: a hard cost ceiling (``--max-cost`` / ``RELAY_MAX_COST``) was
# reached. The run stops the moment ``ledger.total_cost()`` crosses the
# ceiling at the step boundary -- a real-money safety net, distinct from
# the per-step / per-run call-count ceilings. Shown to the user with the
# spent-and-limit pair so they can decide to raise and resume.
STATUS_MAX_COST = "max_cost"

_EXECUTOR_SYSTEM_PROMPT_TEMPLATE = """\
You are the EXECUTOR (the "hands") of Relay. You carry out ONE step of a larger
plan, in a narrow context. You do NOT see the whole plan -- only your current
step plus a short list of what has already been done.

__PLATFORM_LINE__

Work the current step ONE action at a time, using these EXACT tags:
  <thinking>private reasoning</thinking>   (optional)
  <read path="relative/path"/>
  <list path="relative/path"/>
  <glob pattern="**/*.py"/>                 (find files by pattern -- prefer over bash find/ls)
  <grep pattern="regex" path="relative/path"/>
  <write path="relative/path">FULL FILE CONTENTS</write>
  <edit path="relative/path">FULL NEW FILE CONTENTS</edit>
  <apply_patch>*** Begin Patch
*** Update File: relative/path
@@ context line
-old line
+new line
*** End Patch</apply_patch>
  <mkdir path="relative/path"/>             (create a directory + parents; no shell)
  <webfetch url="https://..."/>             (fetch a URL as readable text)
  <bash>command</bash>
  <question>a question you need answered to proceed</question>
  <finding>a bug, security issue, or wrong assumption to flag for the planner</finding>
  <done>one-line summary of what THIS step accomplished</done>
  <blocked>reason you cannot complete this step</blocked>

Choosing a file tool (using the WRONG one corrupts files):
- write / edit REPLACE THE WHOLE FILE with exactly what you provide -- use them ONLY to
  create a new file or deliberately rewrite a file in full, and you MUST supply the
  COMPLETE new contents. Emitting only a fragment DELETES the rest of the file.
- To MODIFY part of an existing file (change/add/remove some lines), use apply_patch --
  NOT write/edit with a fragment. It makes targeted multi-location / multi-file / rename
  / delete changes in ONE atomic patch (OpenCode envelope above: *** Add File /
  *** Update File [+ *** Move to:] / *** Delete File, with @@ anchors and -/+ lines). It
  is all-or-nothing.
- write creates parent directories automatically -- you do NOT need to create a
  directory before writing a file into it.
- glob to locate files; mkdir to create an empty directory; webfetch to read a URL's
  docs instead of guessing.

Rules:
- Paths are relative to the project root; you cannot escape it.
- Before editing an existing file, read it first; if its actual contents don't match what the step assumes, say so rather than forcing the change. This read-first rule covers edit, an existing-file write, and every apply_patch Update section (creating a new file with write or an apply_patch Add File needs no read).
- Some destructive bash commands are refused by policy ("BLOCKED by policy: ...")
  or need approval ("DENIED ..."); adapt rather than re-emitting them verbatim.
- Stay scoped to YOUR current step. Do NOT do later steps.
- If you need information to proceed, emit <question>...</question>; the planner
  will answer it (or get an answer) and you will see "ANSWER: ..." -- then continue.
- If you DISCOVER something the planner should know -- a bug, a security issue, or
  that a file is not structured the way this step assumed -- emit
  <finding>...</finding>. This does NOT end your step and you do NOT wait for a
  reply: it is noted for the planner and you keep working.
- When THIS step is complete, emit <done>...</done>. If you are genuinely stuck,
  emit <blocked>reason</blocked> early rather than thrashing.
"""


def _platform_guidance(system: str, platform_str: str) -> str:
    """One tight line telling the hands WHERE it runs, so it stops emitting Unix-isms
    into bash and prefers the dedicated tools over fragile shell."""
    if system == "Windows":
        return (
            "You are running on Windows. Use Windows-appropriate shell commands "
            "(there is no `mkdir -p`), and prefer the dedicated tools over shell where "
            'one exists -- e.g. <mkdir path="..."/> to create directories.'
        )
    label = system or "an unknown platform"
    return (
        f"You are running on {label} ({platform_str}). Prefer the dedicated tools over "
        'shell where one exists -- e.g. <mkdir path="..."/> to create directories rather '
        "than shelling out."
    )


def build_executor_system_prompt(
    *, system: str | None = None, platform_str: str | None = None
) -> str:
    """The executor system prompt with the live OS/platform line injected.

    ``system`` / ``platform_str`` default to :func:`platform.system` /
    :func:`platform.platform` (overridable for tests). The platform doesn't change
    during a run, so :data:`EXECUTOR_SYSTEM_PROMPT` is built once at import.
    """
    system = platform.system() if system is None else system
    platform_str = platform.platform() if platform_str is None else platform_str
    return _EXECUTOR_SYSTEM_PROMPT_TEMPLATE.replace(
        "__PLATFORM_LINE__", _platform_guidance(system, platform_str)
    )


EXECUTOR_SYSTEM_PROMPT = build_executor_system_prompt()


# v0.0.32 (0.2): the parse-failure nudge is now SPECIFIC, not generic.
# Instead of a single catch-all "no valid action" hint, the nudge names
# the specific shape that came closest to working -- a recognized
# tag-name with a wrong shape (e.g. ``<edit`` with no closing ``>``,
# ``<done`` with no body, an unclosed ``<bash>``) -- so the model can
# correct the next turn. The generic nudge was the v0.0.31 behavior:
# the model would re-emit the SAME malformed tag because the hint never
# named what was wrong.
def _specific_parse_failure_nudge(text: str) -> str:
    """A targeted hint for the most common malformed-tag shapes.

    Looks at the raw reply for one of the four known-failure modes and
    returns a nudge that names the problem. Falls back to the generic
    hint if none of the specific patterns match (the common case of
    "the model just emitted prose with no tag at all").
    """
    s = text or ""
    # Unclosed block tag: an opening ``<edit ...>``, ``<bash>``, etc.
    # with no matching close. Look for a ``<name`` that's not followed
    # by a ``</name>`` somewhere later.
    open_unclosed = re.search(
        r"<(edit|write|bash|apply_patch|done|blocked|question|finding|plan|abort|"
        r"thinking|step|followup|reason|verdict|headline|scope|reaction|decision|"
        r"ask|assume|record)\b[^>]*>",
        s,
        re.IGNORECASE,
    )
    has_any_close = re.search(
        r"</(edit|write|bash|apply_patch|done|blocked|question|finding|plan|abort|"
        r"thinking|step|followup|reason|verdict|headline|scope|reaction|decision|"
        r"ask|assume|record)>",
        s,
        re.IGNORECASE,
    )
    # A "self-closing" tag that didn't close: e.g. ``<read path="x"`` (no ``/>``).
    unterminated_self_close = re.search(
        r"<(read|list|grep|glob|webfetch|mkdir)\b[^/>]*$", s, re.IGNORECASE | re.DOTALL
    )
    if open_unclosed and not has_any_close:
        return (
            f"Your reply opened a ``{open_unclosed.group(0).split(' ')[0]}...`` tag "
            "but never closed it. Re-emit the SAME action with a matching closing "
            "tag (e.g. <bash>cmd</bash>). Block tags MUST be balanced: opening AND "
            "closing tag, in that order."
        )
    if unterminated_self_close:
        return (
            "Your reply has an unterminated self-closing tag (e.g. ``<read "
            'path="x"`` without a trailing ``/>``). Self-closing tags MUST end '
            "with ``/>`` (slash then greater-than) on the same line."
        )
    # Catch the case where the model used double-quotes for an attribute
    # whose value itself contains a double-quote (e.g. a path with a "
    # in it): the regex would have stopped at the first internal quote.
    if re.search(r'=\s*"[^"]*"[^"]*"', s):
        return (
            'An attribute value contains a literal double-quote -- e.g. '
            'path="with"quote". Switch to single-quotes for the value (path=\'with"quote\') '
            "or remove the embedded quote. The parser can't represent a path with a "
            'double-quote in it inside a double-quoted attribute value.'
        )
    # The fallback: the model emitted prose with no tag at all, or an
    # unrecognised tag. Name the recognized set so it can pick one.
    return _EXEC_PARSE_NUDGE


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
    memory: "MemoryBus | PlanMemory | None" = None
    revisions: int = 0
    # The effective global step ceiling for this run (None = unbounded), so a
    # surface can tell the user how to raise it when STATUS_MAX_STEPS is hit.
    max_total_steps: int | None = None
    # v0.0.32: the cost ceiling (None = unbounded), so a surface can tell
    # the user how to raise it when STATUS_MAX_COST is hit.
    max_cost: float | None = None
    # A1: full envelope state (warnings fired, wasted brain $, scope, …).
    envelope: CostEnvelope | None = None
    # A2: deterministic harness explanation (zero tokens), built at finalize.
    harness: dict | None = None
    # The continuous conversation thread (planning turns, mid-run escalations,
    # decisions, the result turn). ``transcript`` is the full live thread (source
    # of truth); ``transcript_compacted`` is its post-execution readable form.
    transcript: Transcript | None = None
    transcript_compacted: Transcript | None = None

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
    touched_paths: list[str] = field(default_factory=list)  # files actually written this step
    unresolved: bool = False   # a product-decision escalation could not be resolved
    unresolved_question: str = ""
    cancelled: bool = False    # the user cancelled between executor calls (clean stop)


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
    # Brain $ on review that did not yield an accepted step (follow-up exhaustion).
    wasted_brain_usd: float = 0.0


def _executor_step_prompt(
    goal: str,
    step: PlanStep,
    plan: Plan,
    *,
    extra_instruction: str | None = None,
    shared_context: str = "",
) -> str:
    """Build the executor's NARROW per-step context.

    Deliberately excludes the full plan, the brain's reasoning, and prior steps'
    raw transcripts -- only the current instruction plus one-line carry-over.
    ``extra_instruction`` carries a reviewer follow-up for a corrective attempt.

    ``shared_context`` (v0.0.29) is the SHARED memory slice -- the brain's
    authoritative directives + the hands' own prior findings. It is the ONLY memory
    the hands sees: never the brain's reasoning pool, and (Stage 1) not yet its own
    hands pool. So the narrow-context principle holds -- no brain reasoning leaks --
    while the hands gains the curated directives it must honor.
    """
    lines = [f"Overall goal (context only): {goal}", ""]
    carry = plan.completed_outcomes()
    if carry:
        lines.append("Already completed (one-line outcomes):")
        for _index, _instruction, outcome in carry:
            lines.append(f"- {outcome}")
        lines.append("")
    if shared_context:
        lines.append(
            "STANDING CONTEXT (directives + findings established so far -- honor "
            "these; do not re-derive or contradict them):"
        )
        lines.append(shared_context)
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
    cancel_check: Callable[[], bool] | None = None,
    reads: dict[str, str] | None = None,
    on_finding: Callable[[str], None] | None = None,
    shared_context: str = "",
) -> _StepOutcome:
    """Run the hands in a fresh, narrow context until done/blocked/budget/question.

    On a ``<question>`` the executor pauses: ``resolve_question`` is consulted
    (the brain answers or escalates); a returned answer is fed back as an
    observation and the step continues; an unresolved escalation ends the step.

    ``cancel_check`` is the fast-stop seam (v0.0.20): consulted at each executor-CALL
    boundary (before issuing the next call), so a cancel set mid-step halts after the
    current in-flight call returns -- typically within one call's latency -- instead
    of waiting for the whole step. The in-flight call is NEVER torn down (its tokens
    are already committed); we simply stop before the next one (the soonest SAFE stop,
    preserving the money-leak guard).
    """
    messages: list[dict[str, str]] = [
        {"role": "system", "content": EXECUTOR_SYSTEM_PROMPT},
        {"role": "user", "content": _executor_step_prompt(
            goal, step, plan, extra_instruction=extra_instruction, shared_context=shared_context,
        )},
    ]
    transcript: list[str] = []
    touched: list[str] = []  # files actually written this step (seed the reviewer to read them)
    consecutive_parse_failures = 0
    calls = 0
    # Read-tracking for the read-before-edit guard. ``run_planned`` passes a shared
    # run-scoped map (a read in an earlier step stays valid while the file is
    # unchanged); a direct call gets a fresh per-step map. The guard is enforced in
    # ``guarded_execute_action`` below.
    if reads is None:
        reads = {}

    for _ in range(max(max_steps, 0)):
        # Fast cancel: poll BEFORE issuing the next call. A cancel that arrived during
        # the previous in-flight call is caught here, so we stop without tearing down
        # a request mid-flight and without spending another call.
        if cancel_check is not None and cancel_check():
            return _StepOutcome(
                False, cancelled=True, failure_reason="cancelled by user",
                calls=calls, transcript=transcript,
            )
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
            messages.append({"role": "user", "content": _specific_parse_failure_nudge(reply)})
            continue
        consecutive_parse_failures = 0

        observations: list[str] = []
        # Surface findings FIRST (v0.0.29), in a pre-pass independent of where they sit
        # relative to <done>/<blocked> in the same reply: those branches return early, so
        # a "finished, and by the way I spotted a bug" turn would otherwise lose the
        # finding. A <finding> is a non-blocking note to the planner -> SHARED memory; it
        # never ends the step and carries no read guard.
        for action in parsed.actions:
            if action.kind == "finding":
                note = action.content or ""
                if on_finding is not None:
                    on_finding(note)
                if emit is not None:
                    emit("hands_finding", note, {"finding": note})
                observations.append("[finding]\nrecorded for the planner")
                transcript.append(f"[finding] {note}"[:_TRANSCRIPT_ENTRY_CAP])

        for action in parsed.actions:
            if action.kind == "finding":
                continue  # already surfaced in the pre-pass above
            if action.kind == "blocked":
                reason = action.content or "no reason given"
                # v0.0.32 (0.2): touched paths flow even on blocked -- a blocked
                # step that wrote files first (e.g. partial refactor) still
                # produced a touched-path set the reviewer should see.
                return _StepOutcome(
                    False, failure_reason=f"blocked: {reason}", calls=calls,
                    transcript=transcript, touched_paths=touched,
                )
            if action.kind == "done":
                return _StepOutcome(
                    True, summary=action.content or "step complete", calls=calls,
                    transcript=transcript, touched_paths=touched,
                )
            if action.kind == "question":
                question = action.content or ""
                if resolve_question is None:
                    return _StepOutcome(
                        False, failure_reason=f"executor asked a question but no resolver was available: {question}",
                        calls=calls, transcript=transcript, touched_paths=touched,
                    )
                resolution = resolve_question(question)
                if resolution.unresolved:
                    return _StepOutcome(
                        False, failure_reason=f"unresolved escalation: {question}", calls=calls,
                        transcript=transcript, unresolved=True, unresolved_question=question,
                        touched_paths=touched,
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
            observation = guarded_execute_action(tools, action, reads)
            if emit is not None:
                emit("exec_action", describe_action(action), {"kind": action.kind, "observation": observation})
            rendered = f"[{describe_action(action)}]\n{observation}"
            observations.append(rendered)
            transcript.append(rendered[:_TRANSCRIPT_ENTRY_CAP])
            # Track files the executor actually WROTE, so the reviewer can be seeded
            # to read what changed. Covers all three write-producing tools: edit,
            # write (both return "wrote <path> ..."), and apply_patch ("applied patch:
            # ..."). A refused ("Refused: ...") or errored ("error: ...") op is not
            # counted. Without this the agentic reviewer is blind to write/apply_patch
            # -- it only sees edit, which v0.0.26+ actively discourages for partial edits.
            if action.kind in ("edit", "write") and action.path and observation.startswith("wrote "):
                if action.path not in touched:
                    touched.append(action.path)
            elif action.kind == "apply_patch" and observation.startswith("applied patch"):
                # Extract the paths from the parsed patch sections (more reliable than
                # regexing the observation summary). Add and Delete are both "touched"
                # -- a deleted file is also something the reviewer may want to verify.
                try:
                    from relay.tools import parse_patch as _pp
                    for _sec in _pp(action.content or ""):
                        _dest = _sec.move_to or _sec.path
                        if _dest not in touched:
                            touched.append(_dest)
                except Exception:
                    pass  # parse_patch failure is already reported in the observation

        messages.append(
            {"role": "user", "content": "\n\n".join(observations) if observations else "(no output)"}
        )

    return _StepOutcome(
        False, failure_reason=f"step not completed within {max_steps} executor steps",
        calls=calls, transcript=transcript,
    )


# v0.0.21: the stuck-loop and step-ceiling terminals are the ones a beta user can
# actually act on, so they get plain-language, ACTIONABLE text (no "escalation_limit"
# jargon) -- drafted once here and reused by every surface (the transcript result
# turn -> TUI conversation + CLI --show-transcript, and the CLI status print) so the
# wording can never drift. The internal status STRINGS are unchanged (logs/tests
# still see escalation_limit / repeated_step / max_steps); only the DISPLAYED text
# becomes friendly. ASCII-safe ("--") for non-UI surfaces.
_STUCK_NEXT_STEPS = (
    "To get further you can: switch to a more capable brain model (/model or "
    "/provider), restate the goal as smaller, clearer steps, or review the partial "
    "work that did land."
)


def friendly_terminal_message(status: str, *, max_total_steps: int | None = None) -> str | None:
    """Plain, actionable text for a user-facing terminal -- or ``None`` when the
    status has no special friendly form (the caller keeps its own wording).

    Covers the terminals a user can do something about: the stuck-loop pair
    (``escalation_limit`` / ``repeated_step``), the global step ceiling
    (``max_steps``), and the cost ceiling (``max_cost``). Says, in plain
    terms, what happened and what to try next.
    """
    if status in (STATUS_ESCALATION_LIMIT, STATUS_REPEATED_STEP):
        return (
            "Relay got stuck on one step -- it kept trying the same approach without "
            f"it being accepted, so it stopped instead of looping. {_STUCK_NEXT_STEPS}"
        )
    if status == STATUS_MAX_STEPS:
        ceiling = f" ({max_total_steps} steps)" if max_total_steps else ""
        return (
            f"Relay reached its overall step ceiling{ceiling} and stopped as a safety "
            "limit. If the project is genuinely large, raise it with --max-total-steps, "
            "the RELAY_MAX_TOTAL_STEPS env var, or the config, then re-run -- or restate "
            "the goal as smaller steps and review the partial work."
        )
    if status == STATUS_MAX_COST:
        return (
            "Relay hit its cost ceiling and stopped as a safety limit (a real-money "
            "guard, not a call-count guard). To continue: raise --max-cost (or set "
            "RELAY_MAX_COST, or the max_cost field in config.json), then re-run -- or "
            "split the goal and review the partial work that already paid for itself."
        )
    return None


def _result_summary(status: str, plan: Plan, *, max_total_steps: int | None = None) -> str:
    """A plain, human-readable line for the conversation's closing result turn.

    Composed from the run outcome (NOT a model call) so it adds no brain cost and
    cannot drift from what actually happened. Reconciles the wording with the real
    done/failed counts: a ``completed`` run that recovered from a failed step (via
    replan) must NOT claim it "built everything" -- that would contradict the count.
    The stuck-loop and step-ceiling terminals use the shared, actionable
    :func:`friendly_terminal_message` so the user never sees raw jargon.
    """
    done = sum(1 for s in plan.steps if s.status == "done")
    failed = sum(1 for s in plan.steps if s.status == "failed")
    total = len(plan.steps)
    if status == STATUS_COMPLETED:
        if failed:
            reworked = f"{failed} step(s) were reworked along the way"
            return f"Done -- reached the goal ({done} of {total} steps completed; {reworked})."
        return f"Done -- built everything we agreed on. ({done}/{total} steps completed.)"
    phrasing = friendly_terminal_message(status, max_total_steps=max_total_steps) or {
        STATUS_CANCELLED: "Stopped: you cancelled the run.",
        STATUS_ABORTED_BY_BRAIN: "Stopped: I judged the goal unreachable as specified.",
        STATUS_UNRESOLVED_ESCALATION: "Paused: I needed a decision from you that I could not get.",
        STATUS_PLANNING_FAILED: "Could not produce a usable plan.",
    }.get(status, f"Ended with status: {status}.")
    return f"{phrasing} ({done}/{total} step(s) completed.)"


def _ensure_bus(memory: "MemoryBus | PlanMemory | None") -> MemoryBus:
    """Normalize the run's memory to a :class:`MemoryBus` (v0.0.29).

    ``None`` -> a fresh bus. A :class:`MemoryBus` is used as-is (the session's bus,
    threaded across steer/queue continuations). A legacy :class:`PlanMemory` (a
    direct caller / older test) is adopted AS the brain pool, so its entries and any
    brain-pool writes stay visible on that same object.
    """
    if isinstance(memory, MemoryBus):
        return memory
    if isinstance(memory, PlanMemory):
        return MemoryBus(brain=memory)
    return MemoryBus()


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
    max_review_steps: int = 4,
    max_plan_revisions: int = 5,
    max_total_steps: int | None = None,
    max_cost: float | None = None,
    envelope: CostEnvelope | None = None,
    context_window: int | None = None,
    catalog: Any | None = None,
    on_event: EventCallback | None = None,
    plan_gate: Callable[[Plan], bool] | None = None,
    committed_plan: Plan | None = None,
    assumption_level: str = "auto",
    transcript: Transcript | None = None,
    cancel_check: Callable[[], bool] | None = None,
    bash_timeout_s: float | None = 120.0,
) -> PlannedTaskResult:
    """Drive the autonomous two-role planner/executor loop.

    The brain plans up front; the hands execute each step; the brain supervises at
    step boundaries (``supervise``), answers executor ``<question>``s itself or
    escalates product decisions to ``user_decision``, learns into ``memory``, and
    evolves the remaining plan. A failed step still escalates to ``replan``
    (v0.04). All loops are bounded.

    Knobs: ``supervise`` (default on), ``max_followups_per_step``,
    ``max_review_steps`` (the agentic reviewer's own read-only investigation budget),
    ``max_plan_revisions``, ``max_escalations``, ``max_total_steps`` (overall
    executor-call budget), and ``context_window`` (override the resolved window
    used to size memory reads). ``user_decision(question) -> answer`` is the
    escalation seam; with none available, a product decision ends the run with
    status ``unresolved_escalation`` rather than guessing.

    ``committed_plan`` (from the v0.08 planning conversation) skips ``make_plan``
    and executes the already-agreed plan. ``assumption_level`` is the assumption
    dial, threaded into ``answer_or_escalate`` so the user's assume-vs-ask bias
    holds in the autonomous loop, not just the conversation.

    ``transcript`` is the ONE continuous conversation thread shared with the
    planning conversation: mid-run escalations and the user's decisions land here
    as the next turns of the same dialogue (not context-less popups). When the
    brain composes an escalation it is given a window-bounded slice of the recent
    thread, so the question reads as a continuation. After the run a readable,
    post-execution compaction pass produces ``result.transcript_compacted``.

    ``cancel_check`` is the cancel seam (a TUI/UI hands it a "did the user ask to
    stop?" probe): consulted at step boundaries AND at each executor-CALL boundary
    within a step (v0.0.20) -- before issuing the next model call, never mid-call. So
    a cancel set during a long multi-call step halts after the current in-flight call
    returns (typically within one call's latency), not at the end of the whole step;
    the in-flight request is never torn down (the money-leak guard). When it returns
    True the run ends with the clean terminal status ``cancelled`` (result turn +
    compaction still happen, like every other terminal path).

    Note: a single step's executor ceiling is ``max_executor_steps *
    (1 + max_followups_per_step)`` (each supervised follow-up re-runs the
    executor with its own budget); ``max_total_steps`` is the hard global cap.
    Review is 1..N brain calls (1 when the model verdicts immediately; up to
    ``max_review_steps`` when it investigates the touched file(s) read-only first);
    review calls do NOT count against the executor ``max_total_steps`` ceiling --
    the reviewer has its own ``max_review_steps`` budget. Answer/evolve are likewise
    brain calls and excluded from the executor budget.
    """
    ledger = ledger if ledger is not None else Ledger()
    memory = _ensure_bus(memory)  # v0.0.29: the run's memory is the three-pool bus
    transcript = transcript if transcript is not None else Transcript()
    result = PlannedTaskResult(goal=goal, ledger=ledger, memory=memory, transcript=transcript)
    result.max_total_steps = max_total_steps  # so a surface can tell the user how to raise it
    result.max_cost = max_cost  # so a surface can tell the user how to raise it (the cost ceiling)
    if envelope is None:
        envelope = CostEnvelope(max_cost=max_cost, max_steps=max_total_steps)
    else:
        # Keep result.max_* aligned with the live envelope (TUI may mutate it).
        if envelope.max_cost is not None:
            max_cost = envelope.max_cost
            result.max_cost = max_cost
        if envelope.max_steps is not None:
            max_total_steps = envelope.max_steps
            result.max_total_steps = max_total_steps
    result.envelope = envelope

    cfg = models if models is not None else load_models()
    # v0.0.29: resolve BOTH actor windows and budget each pool to its own (passing
    # provider + catalog so a catalog model gets its real window instead of the 8192
    # default). The hands reads RELAY_HANDS_CONTEXT for its manual override, never the
    # brain's. The brain-window budget (mem_budget) is what the brain calls + transcript
    # renders use; the bus holds the per-pool split internally.
    brain_window, _bsrc = resolve_context_window(
        cfg.brain, provider=cfg.brain_provider, client=client,
        override=context_window, catalog=catalog,
    )
    hands_window, _hsrc = resolve_context_window(
        cfg.hands, provider=cfg.hands_provider, client=client,
        catalog=catalog, env_override="RELAY_HANDS_CONTEXT",
    )
    memory.configure_budgets(brain_window=brain_window, hands_window=hands_window)
    mem_budget = memory.brain_budget

    def emit(kind: str, message: str, payload: dict | None = None) -> None:
        event = Event(kind, message, payload or {})
        result.events.append(event)
        if on_event is not None:
            on_event(event)

    def remember(
        kind: str, detail: str, summary: str, *, provenance: str, pool: str = POOL_BRAIN, tags=None
    ) -> None:
        # Suppressed near-duplicates return None; don't surface a memory_write for
        # an entry that was not actually stored (else the activity feed would show
        # the very restatements dedup just dropped). ``pool`` routes the write
        # (v0.0.29): brain reasoning -> POOL_BRAIN (default), authoritative directives
        # / hands findings -> POOL_SHARED.
        stored = memory.remember(kind, detail, summary, pool=pool, provenance=provenance, tags=list(tags or []))
        if stored is not None:
            emit("memory_write", f"{kind}: {summary}",
                 {"kind": kind, "summary": summary, "provenance": provenance, "pool": pool})

    def finalize(plan_for_summary: Plan | None) -> PlannedTaskResult:
        """Close the conversation thread on ANY terminal path: a deterministic
        result turn (no model call), then the readable post-execution compaction.
        Called once per run, so every outcome leaves a result turn + a compacted
        record -- not just the run-to-completion path."""
        transcript.record(
            "brain", "result",
            _result_summary(result.status, plan_for_summary or Plan(steps=[]), max_total_steps=max_total_steps),
        )
        result.transcript_compacted = compact_transcript(
            transcript, budget_tokens=mem_budget, client=client, models=models, ledger=ledger
        )
        emit("transcript_compacted", "transcript compacted to its readable form",
             {"turns": len(result.transcript_compacted.turns)})
        # A2: flight recorder from events already emitted (zero new model tokens).
        result.harness = explain_events(
            result.events,
            goal=goal,
            status=result.status,
            assumption_level=assumption_level,
            max_total_steps=max_total_steps,
            max_cost=result.max_cost,
            envelope=envelope,
        ).to_dict()
        return result

    # --- Plan phase --------------------------------------------------------
    # A committed_plan (from the planning conversation) skips make_plan.
    if committed_plan is not None:
        plan = committed_plan if committed_plan.steps else None
    else:
        plan = make_plan(
            goal, project_root, models=models, ledger=ledger, client=client,
            max_investigation_steps=max_investigation_steps, brain_role=brain_role,
            on_event=lambda kind, message, payload: emit(kind, message, payload),
        )
    if plan is None or not plan.steps:
        result.status = STATUS_PLANNING_FAILED
        emit("status", "planning failed: the brain produced no usable plan", {"status": STATUS_PLANNING_FAILED})
        return finalize(plan)

    result.plan = plan
    emit("plan_created", f"plan: {len(plan.steps)} step(s)", {"steps": [s.instruction for s in plan.steps]})

    if plan_gate is not None and not plan_gate(plan):
        result.status = STATUS_DECLINED
        emit("status", "plan declined by user before execution", {"status": STATUS_DECLINED})
        return finalize(plan)

    tools = Tools(Path(project_root), approver=approver, auto_approve=auto_approve,
                  bash_timeout_s=bash_timeout_s)
    # Run-scoped read-tracking for the read-before-edit guard (v0.0.23): which files
    # the hands has read, by content hash. Shared across every step of this run, so a
    # read stays valid while the file is unchanged (content-valid, not step-scoped).
    reads: dict[str, str] = {}

    escalations = 0
    revisions = 0
    executor_calls = 0
    # Instructions of steps that have already dead-ended this run. The repetition
    # breaker (v0.0.21) compares each newly-pending step against these: a replan
    # that re-issues a near-identical already-failed step is the loop signature.
    dead_ended_instructions: list[str] = []

    def make_question_resolver(step: PlanStep) -> Callable[[str], _QuestionResolution]:
        """Resolve an executor question: brain self-answers, or escalates to the user."""

        def resolve(question: str) -> _QuestionResolution:
            emit("executor_question", question, {"index": step.index, "question": question})
            # Give the brain a window-bounded slice of the conversation so far, so an
            # escalation reads as a continuation of the dialogue, not a fresh prompt.
            convo_ctx = render_for_brain(
                transcript, budget_tokens=mem_budget, client=client, models=models, ledger=ledger
            )
            resolution = answer_or_escalate(
                question, goal, plan, step, memory, models=models, ledger=ledger,
                client=client, memory_budget_tokens=mem_budget, brain_role=brain_role,
                assumption_level=assumption_level, conversation_context=convo_ctx,
            )
            for kind, detail, summary in resolution.records:
                remember(kind, detail, summary, provenance=f"step{step.index} brain")
            if resolution.kind == "self_answer":
                # An authoritative decision the hands must honor -> the SHARED pool, so
                # the hands (which reads shared) sees it, not just the brain.
                remember(
                    "decision", f"Q: {question} -> A: {resolution.answer}",
                    f"answered '{question}': {resolution.answer}",
                    provenance=f"step{step.index} self-answer", pool=POOL_SHARED,
                )
                emit("brain_self_answered", question,
                     {"question": question, "answer": resolution.answer, "reasoning": resolution.reasoning})
                return _QuestionResolution(answer=resolution.answer)

            # Escalation: the brain's product question is the next turn of the SAME
            # thread (a continuation), not a context-less popup.
            transcript.record("brain", "escalation", resolution.question_for_user,
                              refs=[f"step{step.index}"])
            emit("brain_escalated", resolution.question_for_user,
                 {"question": resolution.question_for_user, "reasoning": resolution.reasoning})
            if user_decision is None:
                return _QuestionResolution(answer=None, unresolved=True)
            answer = user_decision(resolution.question_for_user)
            # Transcript-first, memory-derived: the user's decision is authoritative in
            # the thread; the memory confirmation is derived + linked back to the turn.
            _turn, entry = record_decision(
                transcript, memory,
                text=answer,
                detail=f"User decided on '{resolution.question_for_user}': {answer}",
                summary=f"user: {answer}",
                kind="confirmation", phase="decision", speaker="user",
                refs=[f"step{step.index}"], pool=POOL_SHARED,
            )
            if entry is not None:
                emit("memory_write", f"confirmation: user: {answer}",
                     {"kind": "confirmation", "summary": f"user: {answer}",
                      "provenance": entry.provenance, "pool": POOL_SHARED})
            emit("user_decided", answer, {"question": resolution.question_for_user, "answer": answer})
            return _QuestionResolution(answer=answer)

        return resolve

    def run_step(step: PlanStep, step_budget: int) -> _Disposition:
        """Run the executor (with bounded supervised follow-ups) and settle the step."""
        nonlocal executor_calls
        resolver = make_question_resolver(step)
        records: list[tuple[str, str, str]] = []
        review_brain_usd = 0.0

        def record_finding(note: str) -> None:
            # The hands surfaced a bug/discovery -> the SHARED pool (kind "fact", so it
            # never collides with the brain's dead_end repetition-breaker entries), where
            # the brain reads it on its next call.
            remember("fact", f"Finding: {note}", f"finding: {note}",
                     provenance=f"step{step.index} hands-finding", pool=POOL_SHARED)

        def hands_shared() -> str:
            # The SHARED slice the hands gets (Stage 1: shared only). Rendered fresh
            # before each executor invocation so a directive/finding written during the
            # step is visible on the next (follow-up) attempt.
            return memory.hands_context(
                step.instruction, memory.hands_budget, client=client, models=models, ledger=ledger
            )

        outcome = _run_executor_step(
            step, plan, goal, tools, hands_role=hands_role, models=models, ledger=ledger,
            client=client, max_steps=step_budget, emit=emit, resolve_question=resolver,
            cancel_check=cancel_check, reads=reads, on_finding=record_finding,
            shared_context=hands_shared(),
        )
        executor_calls += outcome.calls
        followups_used = 0

        while True:
            # Fast cancel short-circuits BEFORE the failed/replan path, so a user stop
            # mid-step does not spend a brain replan call (it's a clean cancel, not a
            # failure). The outer loop then ends the run with the cancelled status.
            if outcome.cancelled:
                return _Disposition("cancelled", records=records)
            if outcome.unresolved:
                return _Disposition("unresolved", unresolved_question=outcome.unresolved_question, records=records)
            if not outcome.success:
                return _Disposition("failed", failure_reason=outcome.failure_reason, records=records)
            if not supervise:
                return _Disposition("done", summary=outcome.summary, records=records)

            before_review = len(ledger.records)
            review = review_step(
                goal, plan, step, outcome.summary, outcome.transcript, memory,
                tools=tools, touched_paths=outcome.touched_paths, max_review_steps=max_review_steps,
                models=models, ledger=ledger, client=client, memory_budget_tokens=mem_budget,
                brain_role=brain_role, on_event=emit,
            )
            review_brain_usd += brain_cost_since(ledger, before_review)
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
                    wasted_brain_usd=review_brain_usd,
                )
            followups_used += 1
            remember(
                "decision", f"Follow-up on step {step.index}: {review.followup}",
                f"reviewer follow-up: {review.followup}", provenance=f"step{step.index} review",
            )
            outcome = _run_executor_step(
                step, plan, goal, tools, hands_role=hands_role, models=models, ledger=ledger,
                client=client, max_steps=step_budget, emit=emit, resolve_question=resolver,
                extra_instruction=review.followup, cancel_check=cancel_check, reads=reads,
                on_finding=record_finding, shared_context=hands_shared(),
            )
            executor_calls += outcome.calls

    # --- Execute phase -----------------------------------------------------
    # A1: for scope=execution, planning spend is excluded from the cost ceiling.
    envelope.mark_execution_start(ledger)

    def emit_envelope_warnings() -> None:
        # Re-read live ceilings (TUI session edits may have changed them).
        nonlocal max_cost, max_total_steps
        max_cost = envelope.max_cost
        max_total_steps = envelope.max_steps
        result.max_cost = max_cost
        result.max_total_steps = max_total_steps
        for warn in envelope.drain_warnings(ledger=ledger, steps_used=executor_calls):
            emit("envelope_warn", warn["message"], warn)

    while True:
        # Coarse cancellation: polled at the step boundary, before any work or
        # model call for the next step is spent. Mid-step interruption is
        # deliberately NOT attempted (v1 is coarse by design).
        if cancel_check is not None and cancel_check():
            result.status = STATUS_CANCELLED
            emit("status", "run cancelled by user at a step boundary",
                 {"status": STATUS_CANCELLED})
            break

        step = plan.next_pending()
        if step is None:
            result.status = STATUS_COMPLETED
            emit("status", "all steps complete", {"status": STATUS_COMPLETED})
            break

        # Repetition breaker (v0.0.21): a replan/evolve just re-issued a step that
        # already dead-ended the same way -> we are looping. Stop cleanly BEFORE
        # running it again, instead of grinding the remaining escalations (each is
        # a full executor step + reviews + replan). Conservative by construction:
        # it fires only on a CLEAR re-issued dead-end (a near-identical
        # instruction), so a progressing run -- whose replan chose a genuinely
        # different approach -- is never short-circuited.
        if any(texts_near_duplicate(step.instruction, prior) for prior in dead_ended_instructions):
            result.status = STATUS_REPEATED_STEP
            emit("status",
                 "stopped: the plan kept re-issuing a step that already failed the same way",
                 {"status": STATUS_REPEATED_STEP, "index": step.index, "instruction": step.instruction})
            break

        # Overall executor-call budget guard (the "max_steps" terminal status).
        step_budget = max_executor_steps
        if max_total_steps is not None:
            remaining = max_total_steps - executor_calls
            if remaining <= 0:
                result.status = STATUS_MAX_STEPS
                emit("status", "stopped: reached the overall step ceiling (a safety limit)",
                     {"status": STATUS_MAX_STEPS, "max_total_steps": max_total_steps})
                break
            step_budget = min(max_executor_steps, remaining)

        # Soft envelope warnings (once per threshold × dimension), then hard cost stop.
        emit_envelope_warnings()

        # Cost ceiling guard: chargeable spend under envelope.scope (all|execution).
        # Checked at the step boundary so the run halts BEFORE the next step starts.
        if envelope.hit_cost_limit(ledger):
            spent = envelope.chargeable_cost(ledger)
            ceiling = envelope.max_cost
            spent_txt = f"${spent:.4f}" if spent is not None else "unknown"
            ceiling_txt = f"${ceiling:.4f}" if ceiling is not None else "unknown"
            result.status = STATUS_MAX_COST
            emit(
                "status",
                f"stopped: spent {spent_txt}, reached the cost ceiling "
                f"({ceiling_txt}, scope={envelope.scope})",
                {
                    "status": STATUS_MAX_COST,
                    "spent": spent,
                    "max_cost": envelope.max_cost,
                    "scope": envelope.scope,
                },
            )
            break

        emit("step_start", f"step {step.index}: {step.instruction}",
             {"index": step.index, "instruction": step.instruction})

        disposition = run_step(step, step_budget)
        # Persist the brain's review records (provenance: this step's review).
        for kind, detail, summary in disposition.records:
            remember(kind, detail, summary, provenance=f"step{step.index} review")

        if disposition.kind == "cancelled":
            # Fast cancel landed between executor calls within the step: end the run on
            # the existing clean terminal status (result turn + compaction still happen
            # via finalize), without marking the step or spending a replan.
            result.status = STATUS_CANCELLED
            emit("status", "run cancelled by user mid-step", {"status": STATUS_CANCELLED})
            break

        if disposition.kind == "unresolved":
            result.status = STATUS_UNRESOLVED_ESCALATION
            emit("status", "a product decision was needed but could not be obtained",
                 {"status": STATUS_UNRESOLVED_ESCALATION, "question": disposition.unresolved_question})
            break

        if disposition.kind == "done":
            plan.mark_done(step, disposition.summary)
            envelope.note_completed_step()
            remember("fact", f"Step {step.index} done: {disposition.summary}", disposition.summary,
                     provenance=f"step{step.index}")
            emit("step_done", f"step {step.index} done: {disposition.summary}",
                 {"index": step.index, "outcome": disposition.summary})
            continue

        if disposition.kind == "revise":
            # The reviewer asked to revise the remaining plan. Call evolve_plan
            # FIRST: if the brain aborts (returns None), the step must NOT be
            # marked done and the revision budget must NOT be consumed -- the run
            # ends as aborted, with the step still pending and revisions unchanged.
            if revisions >= max_plan_revisions:
                # Budget exhausted: mark the step done and proceed with the existing
                # tail (don't thrash). This path does NOT call evolve_plan, so no
                # abort is possible here.
                plan.mark_done(step, disposition.summary)
                envelope.note_completed_step()
                remember("fact", f"Step {step.index} done: {disposition.summary}", disposition.summary,
                         provenance=f"step{step.index}")
                emit("step_done", f"step {step.index} done: {disposition.summary}",
                     {"index": step.index, "outcome": disposition.summary})
                remember("decision", f"Revise remaining plan after step {step.index}: {disposition.revise_reason}",
                         f"plan revision: {disposition.revise_reason}", provenance=f"step{step.index} review")
                emit("status", f"plan-revision budget ({max_plan_revisions}) reached; keeping current plan",
                     {"status": "revision_budget"})
                continue
            revised = evolve_plan(goal, plan, disposition.revise_reason, memory, models=models,
                                  ledger=ledger, client=client, memory_budget_tokens=mem_budget, brain_role=brain_role)
            if revised is None:
                result.status = STATUS_ABORTED_BY_BRAIN
                emit("status", "brain aborted: goal deemed unreachable", {"status": STATUS_ABORTED_BY_BRAIN})
                break
            # evolve_plan succeeded: NOW mark the step done, increment revisions,
            # and adopt the revised plan.
            revisions += 1
            result.revisions = revisions
            plan.mark_done(step, disposition.summary)
            envelope.note_completed_step()
            remember("fact", f"Step {step.index} done: {disposition.summary}", disposition.summary,
                     provenance=f"step{step.index}")
            emit("step_done", f"step {step.index} done: {disposition.summary}",
                 {"index": step.index, "outcome": disposition.summary})
            remember("decision", f"Revise remaining plan after step {step.index}: {disposition.revise_reason}",
                     f"plan revision: {disposition.revise_reason}", provenance=f"step{step.index} review")
            plan = _adopt_revision(plan, revised)
            result.plan = plan
            emit("plan_revised", f"plan revised: {len(plan.remaining())} new step(s)",
                 {"steps": [s.instruction for s in plan.steps if s.status == "pending"], "reason": disposition.revise_reason})
            continue

        # disposition.kind == "failed" -> record the dead end, escalate to replan.
        plan.mark_failed(step, disposition.failure_reason)
        dead_ended_instructions.append(step.instruction)  # repetition-breaker watch list
        envelope.add_wasted_brain(disposition.wasted_brain_usd)
        remember("dead_end", f"Step {step.index} failed: {disposition.failure_reason}",
                 f"failed: {disposition.failure_reason}", provenance=f"step{step.index}")
        emit("step_failed", f"step {step.index} failed: {disposition.failure_reason}",
             {"index": step.index, "reason": disposition.failure_reason})

        if escalations >= max_escalations:
            result.status = STATUS_ESCALATION_LIMIT
            emit("status", "stopped: stuck on a step after repeated recovery attempts",
                 {"status": STATUS_ESCALATION_LIMIT, "escalations": escalations})
            break

        escalations += 1
        result.escalations = escalations
        emit("escalation", f"escalation {escalations}: replanning after step {step.index}",
             {"n": escalations, "failed_index": step.index, "reason": disposition.failure_reason})

        before_replan = len(ledger.records)
        revised = replan(
            goal, plan, step, disposition.failure_reason, plan.completed_outcomes(),
            models=models, ledger=ledger, client=client, brain_role=brain_role,
            memory=memory, memory_budget_tokens=mem_budget,
        )
        envelope.add_wasted_brain(brain_cost_since(ledger, before_replan))
        if revised is None:
            result.status = STATUS_ABORTED_BY_BRAIN
            emit("status", "brain aborted: goal deemed unreachable", {"status": STATUS_ABORTED_BY_BRAIN})
            break

        plan = _adopt_revision(plan, revised)
        result.plan = plan
        emit("replanned", f"revised plan adopted: {len(plan.remaining())} new step(s)",
             {"steps": [s.instruction for s in plan.steps if s.status == "pending"]})

    # Post-execution: a high-level brain "here's where things ended" turn (composed
    # deterministically from the outcome -- no model call), then the readable
    # auto-compaction pass that yields the durable, scroll-back record.
    return finalize(plan)
