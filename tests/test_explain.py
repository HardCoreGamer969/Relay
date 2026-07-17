"""Tests for the harness flight recorder (relay/explain.py)."""

from __future__ import annotations

from relay.explain import explain_events
from relay.orchestrator import Event


def test_explain_events_is_deterministic_and_token_free():
    events = [
        Event("plan_created", "plan: 2 step(s)", {"steps": ["a", "b"]}),
        Event("step_start", "step 0", {"index": 0, "instruction": "write a"}),
        Event("step_done", "done", {"index": 0, "outcome": "wrote a"}),
        Event("escalation", "escalation 1: replanning", {"n": 1}),
        Event("status", "stopped: cost", {"status": "max_cost"}),
    ]
    report = explain_events(
        events,
        goal="demo",
        status="max_cost",
        assumption_level="auto",
        max_cost=0.5,
    )
    assert report.status == "max_cost"
    assert report.last_brain_engagement and "escalation" in report.last_brain_engagement
    assert report.budgets["max_cost"] == 0.5
    assert any(s["state"] == "done" for s in report.step_summaries)
    text = report.to_text()
    assert "no new model tokens" in text.lower()
    assert "Raw model prompts" in text
    # Never dump a pretend prompt body.
    assert "<system>" not in text


def test_explain_step_filter():
    events = [
        Event("step_start", "s0", {"index": 0, "instruction": "one"}),
        Event("step_done", "d0", {"index": 0, "outcome": "ok"}),
        Event("step_start", "s1", {"index": 1, "instruction": "two"}),
        Event("step_failed", "f1", {"index": 1, "reason": "blocked"}),
    ]
    report = explain_events(events, step_filter=1)
    assert [s["index"] for s in report.step_summaries] == [1]
    assert report.step_summaries[0]["state"] == "failed"
