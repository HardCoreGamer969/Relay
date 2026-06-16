"""The brain investigation loop: a generalized, read-only, text-protocol primitive.

This is the BRAIN analog of the orchestrator's ``_run_executor_step`` -- the same
shape (call the model -> parse its text into actions -> act -> feed results back ->
repeat until a terminator) but **read-only** and **consumer-parameterized**. It
exists so the family of one-shot "decide-from-a-snippet" brain calls can become
agents that *investigate the real code first*, without each one re-implementing a
bounded loop.

The CONSUMER supplies:
  - ``system``         the system prompt for this kind of decision;
  - ``seed``           the user message describing what to investigate;
  - ``terminators``    the tag name(s) whose presence ends the loop (e.g. ``verdict``);
  - ``parse_terminal`` a one-arg ``(reply) -> result`` callable (the consumer's parser); it
    MAY close over extra context the bare interface does not thread -- e.g.
    answer_or_escalate binds the executor's question via
    ``lambda reply: _parse_resolution(reply, question)`` -- so a consumer whose parser needs
    more than the reply still fits without any signature change;
  - ``safe_default``   a zero-arg ``() -> result`` factory for the result on exhaustion (it
    may likewise close over context, e.g. ``lambda: _parse_resolution("", question)``);
  - ``budget``         the max number of brain turns (small -- it verifies, not builds);
  - the brain ``brain_role`` / ``models`` / ``client`` / ``ledger`` / ``emit`` / ``tools``.

The PRIMITIVE owns (common to every consumer):
  - the loop and the terminator check;
  - the **read-only action set** -- only ``read`` / ``list`` / ``grep`` run (via the
    shared ``execute_action``); a ``edit`` / ``bash`` a model emits is REFUSED with an
    observation and never executed, so the brain can NEVER write (mirrors how
    ``make_plan`` refuses brain edit/bash). This is enforced structurally: edit/bash
    never reach ``execute_action``;
  - **budget bounding** -- at most ``budget`` brain turns; on exhaustion it returns the
    consumer's ``safe_default`` so running out never blocks progress;
  - **parse-failure handling** -- bounded nudges (the ``MAX_CONSECUTIVE_PARSE_FAILURES``
    pattern) so a malformed turn cannot spin;
  - **text protocol only** -- actions are Relay tags parsed by ``parse``; the terminator
    is a parsed tag, exactly like ``<done>`` for the executor. NEVER native tool-calling
    (the moat: every model, function-calling or not, drives Relay the same way).

Validated boundary (Part 0): the interface (prompt + seed + terminator tag(s) +
parser + budget + read-only actions, with an optional ``is_terminal`` refinement) was
checked against the four anomaly-cluster consumers. Each fits:

  - **reviewer (``review_step``)** -- system ``_REVIEW_SYSTEM`` (+ read grammar),
    terminator ``("verdict",)``, parser ``_parse_review`` -> ``StepReview``, seed = the
    step + the hands' ``<done>`` summary + the touched-file paths, budget
    ``max_review_steps``, safe_default = ``_parse_review("")`` (absent verdict -> accept).
    *WIRED in v0.0.24.*
  - **replan (``replan``)** -- system ``_REPLAN_SYSTEM``, terminators ``("plan","abort")``,
    parser = the plan parser -> ``Plan | None``, seed = goal + failed step + failure
    reason (+ value-add: read *why* it failed), budget = its own, safe_default = ``None``.
    Needs the ``is_terminal`` refinement "a NON-EMPTY ``<plan>`` or an ``<abort>``" (an
    empty ``<plan></plan>`` should keep investigating, not terminate). *Paper-validated.*
  - **evolve_plan** -- as replan, seed = reason + memory. *Paper-validated.*
  - **answer_or_escalate** -- system ``_ANSWER_SYSTEM``, terminator ``("decision",)``, seed =
    the question + goal + plan + memory (+ value-add: read the code to self-answer), budget =
    its own. Its parser, ``_parse_resolution``, takes two args (reply, question), so the
    consumer binds the question via a CLOSURE -- which is itself the one-arg callable the
    interface asks for, so NO signature change is needed:
    ``parse_terminal=lambda reply: _parse_resolution(reply, question)`` and
    ``safe_default=lambda: _parse_resolution("", question)`` -> ``Resolution`` (conservative
    escalate). This is the same shape the wired reviewer uses, where ``_parse_review`` simply
    needs no extra context. *Paper-validated.*

Only the reviewer is wired through this primitive in v0.0.24; the other three stay as
they are functionally and are proven-to-fit, ready to migrate without a re-architecture.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Sequence, TypeVar

from relay.config import ModelConfig
from relay.loop import MAX_CONSECUTIVE_PARSE_FAILURES, describe_action, execute_action
from relay.models import call_model
from relay.protocol import parse
from relay.telemetry import Ledger
from relay.tools import Tools

T = TypeVar("T")

# Callback for streaming investigation actions: (kind, message, payload).
EventSink = Callable[[str, str, dict], None]

# The read-only actions the brain may take while investigating. Anything else a model
# emits (edit/bash) is refused; the brain investigation loop can never write.
_READ_ONLY_KINDS = ("read", "list", "grep")
_WRITE_KINDS = ("edit", "bash")


def _has_terminator(reply: str, terminators: Sequence[str]) -> bool:
    """True iff any terminator tag opens in ``reply`` (``<verdict>``, ``<plan>``, ...).

    Presence-based, matching how the consumers' own parsers key off the tag: the char
    right after the name must be whitespace, ``>`` or ``/`` so ``<verdict>`` matches but
    ``<verdictish>`` does not.
    """
    for name in terminators:
        if re.search(rf"<{re.escape(name)}[\s/>]", reply or ""):
            return True
    return False


def investigate(
    system: str,
    seed: str,
    *,
    terminators: Sequence[str],
    parse_terminal: Callable[[str], T],
    safe_default: Callable[[], T],
    budget: int,
    tools: Tools | None = None,
    is_terminal: Callable[[str], bool] | None = None,
    brain_role: str = "brain",
    models: ModelConfig | None = None,
    ledger: Ledger | None = None,
    client: Any | None = None,
    model_call: Callable[..., Any] | None = None,
    emit: EventSink | None = None,
    final_instruction: str | None = None,
    max_parse_failures: int = MAX_CONSECUTIVE_PARSE_FAILURES,
) -> T:
    """Run a bounded, read-only, text-protocol investigation for one consumer.

    Each turn: call the brain -> if the reply is terminal (a terminator tag, or the
    consumer's ``is_terminal``), parse it and return; otherwise execute the read-only
    actions, append the observations, and continue. On budget/parse-failure exhaustion
    the consumer's ``safe_default`` is returned so a stuck investigation never blocks
    progress. ``model_call`` lets a caller inject its own (test-patchable) ``call_model``
    reference; it defaults to the shared seam.
    """
    call = model_call or call_model
    terminal = is_terminal or (lambda reply: _has_terminator(reply, terminators))
    term_hint = terminators[0] if terminators else "result"

    messages: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": seed},
    ]
    consecutive_parse_failures = 0
    budget = max(budget, 0)

    for turn in range(budget):
        reply = call(brain_role, messages, models=models, ledger=ledger, client=client).text
        messages.append({"role": "assistant", "content": reply})

        if terminal(reply):
            return parse_terminal(reply)

        parsed = parse(reply)
        if not parsed.actions:
            # No terminator and no action: a parse failure. Nudge, bounded.
            if ledger is not None:
                ledger.record_parse_failure()
            consecutive_parse_failures += 1
            if emit is not None:
                emit("brain_parse_failure", "no valid action",
                     {"snippet": " ".join((reply or "").split())[:200]})
            if consecutive_parse_failures >= max_parse_failures:
                break
            messages.append({
                "role": "user",
                "content": (
                    f"No valid action found. Investigate read-only "
                    f'(<read path="..."/>, <list .../>, <grep .../>) or emit '
                    f"<{term_hint}>...</{term_hint}> to finish."
                ),
            })
            continue
        consecutive_parse_failures = 0

        observations: list[str] = []
        for action in parsed.actions:
            if action.kind in _READ_ONLY_KINDS:
                if tools is None:
                    observations.append(
                        f"[{describe_action(action)}]\n(no filesystem handle available; "
                        f"cannot read -- emit <{term_hint}> with what you know.)"
                    )
                    continue
                obs = execute_action(tools, action)
                observations.append(f"[{describe_action(action)}]\n{obs}")
                if emit is not None:
                    emit("brain_action", describe_action(action), {"observation": obs})
            elif action.kind in _WRITE_KINDS:
                # The read-only-brain property: a write is refused, never executed.
                observations.append(
                    f"[{describe_action(action)}]\nrefused: this is a READ-ONLY "
                    "investigation; you cannot edit files or run bash. Investigate "
                    f"read-only or emit <{term_hint}>...</{term_hint}>."
                )
                if emit is not None:
                    emit("brain_action_refused", describe_action(action), {"kind": action.kind})
            else:
                # A non-terminator protocol tag (e.g. <done>/<question> from a confused
                # model): not actionable here -- steer back to investigate/terminate.
                observations.append(
                    f"[{action.kind}]\nnote: emit read-only actions to investigate, or "
                    f"<{term_hint}>...</{term_hint}> to finish."
                )

        tail = ""
        if budget - turn - 1 == 1:  # one turn remains after this one
            tail = "\n\n" + (
                final_instruction
                or f"This is your LAST turn: emit <{term_hint}>...</{term_hint}> now."
            )
        messages.append({"role": "user", "content": "\n\n".join(observations) + tail})

    return safe_default()
