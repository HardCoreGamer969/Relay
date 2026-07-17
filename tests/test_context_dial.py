"""Tests for the hands context dial (B3)."""

from __future__ import annotations

from relay.config import (
    DEFAULT_HANDS_CONTEXT_MODE,
    resolve_hands_context_mode,
)
from relay.orchestrator import _executor_step_prompt, _prior_step_summaries
from relay.planner import Plan
from relay.transcript import Transcript


def test_default_is_needle(monkeypatch):
    monkeypatch.delenv("RELAY_HANDS_CONTEXT_MODE", raising=False)
    assert resolve_hands_context_mode(config={}) == DEFAULT_HANDS_CONTEXT_MODE
    assert DEFAULT_HANDS_CONTEXT_MODE == "needle"


def test_resolve_override_beats_env(monkeypatch):
    monkeypatch.setenv("RELAY_HANDS_CONTEXT_MODE", "wide")
    assert resolve_hands_context_mode("findings", config={}) == "findings"


def test_modes_change_prompt_contents():
    plan = Plan.from_instructions(["first", "second"])
    plan.mark_done(plan.steps[0], "did first")
    step = plan.steps[1]
    shared = "DIRECTIVE: use SQLite"
    summaries = _prior_step_summaries(plan)
    wide = "[hands/execution] wrote db.py"

    needle = _executor_step_prompt(
        "build app", step, plan, shared_context=shared, hands_context_mode="needle",
    )
    findings = _executor_step_prompt(
        "build app", step, plan, shared_context=shared, hands_context_mode="findings",
    )
    summary = _executor_step_prompt(
        "build app", step, plan, shared_context=shared, hands_context_mode="summary",
        step_summaries=summaries,
    )
    wide_prompt = _executor_step_prompt(
        "build app", step, plan, shared_context=shared, hands_context_mode="wide",
        step_summaries=summaries, wide_transcript=wide,
    )

    assert "YOUR CURRENT STEP: second" in needle
    assert "did first" in needle
    assert "STANDING CONTEXT" not in needle
    assert "SQLite" not in needle

    assert "STANDING CONTEXT" in findings
    assert "SQLite" in findings
    assert "PRIOR STEP SUMMARIES" not in findings

    assert "PRIOR STEP SUMMARIES" in summary
    assert "first" in summary
    assert "WIDE CONTEXT" not in summary

    assert "WIDE CONTEXT" in wide_prompt
    assert "wrote db.py" in wide_prompt
    # Hard invariant: never frame brain reasoning as hands context.
    assert "brain reasoning" in wide_prompt.lower() or "never brain" in wide_prompt.lower()


def test_wide_helper_skips_brain_planning():
    from relay.orchestrator import _wide_hands_transcript

    tx = Transcript()
    tx.record("brain", "planning", "SECRET BRAIN DELIBERATION about architecture")
    tx.record("user", "decision", "use sqlite")
    tx.record("hands", "execution", "created schema")
    out = _wide_hands_transcript(tx)
    assert "SECRET BRAIN" not in out
    assert "use sqlite" in out
    assert "created schema" in out
