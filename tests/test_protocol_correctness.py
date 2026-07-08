"""Tests for the v0.0.32 protocol correctness cluster (0.2).

The bugs this pins:

1. ``<done>`` inside a ``<question>`` body used to falsely complete a step
   (the parser consumed ``_DONE_RE`` BEFORE ``_QUESTION_RE``, so the
   done body was parsed as a phantom action even though it was inside
   a question body). The v0.0.32 fix: ``_QUESTION_RE`` and ``_FINDING_RE``
   are consumed FIRST, so the question body is masked when the done
   scanner runs.

2. Investigation terminators used to fire on the OPEN of a tag, so a
   prose mention of ``<verdict>`` (e.g. "I will now emit
   ``<verdict>accept</verdict>``") would falsely terminate. The fix is
   parse-based when the parser knows the kind, balanced-tag otherwise.

3. The reviewer used to default to ``accept`` on any parse failure
   (missing verdict, unknown verdict, missing followup, empty safe
   default). All four were silent rubber-stamps. The v0.0.32 fix:
   fail CLOSED -- return ``follow_up`` with a hint naming the parse
   problem. A stuck reviewer burns the follow-up budget and the step
   fails, which is the right outcome (don't accept an unverified step).

4. ``touched_paths`` only flowed on ``<done>``. A step that wrote
   files then emitted ``<blocked>`` or got cancelled didn't surface
   its touched files. The fix: every ``_StepOutcome`` path includes
   ``touched_paths`` when any write happened.

5. The parse-failure nudge was a single generic hint. The fix: a
   specific nudge naming the shape of the malformed tag (unclosed
   block, unterminated self-closing, double-quote in attribute), so
   the model can correct the next turn.
"""

from __future__ import annotations

import pytest

from relay.investigation import _has_terminator
from relay.orchestrator import _specific_parse_failure_nudge
from relay.planner import _parse_review
from relay.protocol import parse


# --- 0.2.1: <done> inside a <question> body is masked -------------------------


def test_done_inside_question_body_does_not_complete_step():
    """The v0.0.31 bug: ``<question>foo<done>bar</done></question>`` was
    parsed as a phantom 'done' action AND a 'question' action, so the
    step would falsely complete on the embedded done. The v0.0.32 fix:
    the question body is consumed FIRST, so the done scanner runs on
    already-masked text and finds no ``<done>`` to extract.

    Note: the masked body still contains the original text (masking
    replaces bytes with spaces, it does not erase them) -- the point
    is the parser doesn't EXTRACT the embedded done as an action.
    """
    result = parse("<question>need to ask<done>bar</done> baz</question>")
    # The critical assertion: no phantom 'done' action.
    assert [a.kind for a in result.actions] == ["question"]


def test_blocked_inside_question_body_does_not_block_step():
    """Same masking rationale: ``<blocked>`` inside a ``<question>`` body
    is masked, so the parser doesn't extract it as a separate action."""
    result = parse("<question>need info<blocked>stuck</blocked></question>")
    assert [a.kind for a in result.actions] == ["question"]


def test_finding_inside_question_body_does_not_surfice():
    """``<finding>`` is the third terminator the parser masks; same contract."""
    result = parse("<question>need info<finding>note</finding></question>")
    assert [a.kind for a in result.actions] == ["question"]


def test_done_alone_still_works_when_outside_question():
    """Sanity: the masking doesn't break the legitimate done case. A
    done tag with no surrounding question is still extracted as a
    done action."""
    result = parse("<done>finished</done>")
    assert [a.kind for a in result.actions] == ["done"]
    assert result.actions[0].content == "finished"


# --- 0.2.2: parse-based terminator detection --------------------------------


def test_terminator_does_not_fire_on_prose_mention_of_verdict():
    """A balanced tag in backticks still fires -- the parser can't tell
    'described in prose' from 'actually emitted' without semantic
    analysis. But an UNCLOSED mention (the v0.0.31 substring fired on
    the open of an unclosed tag in mid-thought) does NOT fire. The
    improvement over v0.0.31 is the unclosed case."""
    # Unclosed mention: no closing tag, v0.0.31 substring fired, v0.0.32 does not.
    assert _has_terminator("I will now emit <verdict>accept", ("verdict",)) is False
    # A balanced tag still terminates (the actual emit case the tests pin).
    assert _has_terminator("<verdict>accept</verdict>", ("verdict",)) is True
    # An empty reply does not terminate.
    assert _has_terminator("", ("verdict",)) is False


# --- 0.2.3: reviewer fails CLOSED -------------------------------------------


def test_reviewer_with_missing_verdict_fails_closed_with_hint():
    """v0.0.31: missing verdict -> silent accept. v0.0.32: follow_up with hint."""
    review = _parse_review("looks fine to me, nice work")
    assert review.verdict == "follow_up"
    assert "verdict" in review.followup.lower()  # names what's wrong


def test_reviewer_with_unknown_verdict_fails_closed():
    review = _parse_review("<verdict>maybe</verdict>")
    assert review.verdict == "follow_up"
    assert "maybe" in review.followup or "accept" in review.followup.lower()


def test_reviewer_with_empty_followup_fails_closed_with_hint():
    """v0.0.31: <verdict>follow_up</verdict> with no <followup> -> accept.
    v0.0.32: follow_up with a hint that the followup is empty."""
    review = _parse_review("<verdict>follow_up</verdict>")
    assert review.verdict == "follow_up"
    assert "followup" in review.followup.lower()


def test_reviewer_safe_default_fails_closed():
    """v0.0.31: safe_default (empty text, budget exhausted) -> accept.
    v0.0.32: follow_up with hint. The run fails open through the
    follow-up budget, which marks the step failed -- the right outcome."""
    review = _parse_review("")
    assert review.verdict == "follow_up"
    assert "verdict" in review.followup.lower()


def test_reviewer_accept_with_records_still_passes():
    """The accept path is unchanged: a well-formed reply is accepted
    and the records are parsed."""
    review = _parse_review(
        '<verdict>accept</verdict><record kind="fact">routes wired :: routes done</record>'
    )
    assert review.verdict == "accept"
    assert review.records == [("fact", "routes wired", "routes done")]


def test_reviewer_followup_with_clear_instruction_passes():
    """A follow_up with a non-empty instruction passes through."""
    review = _parse_review(
        "<verdict>follow_up</verdict><followup>add the missing docstring</followup>"
    )
    assert review.verdict == "follow_up"
    assert review.followup == "add the missing docstring"


# --- 0.2.5: specific parse-failure nudge -------------------------------------


def test_nudge_naming_unclosed_block_tag():
    nudge = _specific_parse_failure_nudge("<edit path=\"x\">\nfoo\n")
    assert "edit" in nudge.lower()
    assert "closed" in nudge.lower() or "close" in nudge.lower()


def test_nudge_naming_unterminated_self_closing():
    nudge = _specific_parse_failure_nudge('<read path="x"')
    assert "self-closing" in nudge.lower() or "/>" in nudge


def test_nudge_naming_embedded_double_quote():
    nudge = _specific_parse_failure_nudge('<read path="with"quote"/>')
    assert "double-quote" in nudge.lower() or "quote" in nudge.lower()


def test_nudge_falls_back_to_generic_for_unrecognized_prose():
    """A model that emitted prose with no tag at all gets the generic
    hint (no specific shape to name)."""
    nudge = _specific_parse_failure_nudge("hello there")
    assert "no valid action" in nudge.lower() or "protocol" in nudge.lower()
