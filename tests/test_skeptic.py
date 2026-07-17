"""Hermetic tests for adversarial skeptic (D1)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from relay.config import ModelConfig
from relay.loop import STATUS_COMPLETED
from relay.orchestrator import STATUS_SKEPTIC_BLOCKED, run_planned
from relay.planner import Plan
from relay.skeptic import (
    SKEPTIC_PURPOSE,
    _parse_skeptic,
    resolve_skeptic,
    review_plan_adversarially,
    skeptic_cost_usd,
)
from relay.telemetry import CallRecord, Ledger

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")


def _resp(content, cost=0.00003):
    usage = SimpleNamespace(
        prompt_tokens=6, completion_tokens=4, total_tokens=10, cost=cost
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=usage,
    )


class _Completions:
    def __init__(self, brain, hands):
        self.brain = list(brain)
        self.hands = list(hands)
        self.calls: list[dict] = []

    def create(self, *, model, **kwargs):
        self.calls.append({"model": model, **kwargs})
        queue = self.brain if model == "vendor/brain" else self.hands
        role = "brain" if model == "vendor/brain" else "hands"
        assert queue, f"ran out of {role} replies"
        return _resp(queue.pop(0))


class RoutedClient:
    def __init__(self, brain=(), hands=()):
        self.chat = SimpleNamespace(completions=_Completions(brain, hands))


def test_parse_skeptic_clear():
    r = _parse_skeptic("<verdict>clear</verdict><reason>looks fine</reason>")
    assert r.verdict == "clear"
    assert not r.blocked


def test_parse_skeptic_object():
    r = _parse_skeptic(
        "<verdict>object</verdict>"
        "<objection>no tests</objection>"
        "<objection>rm -rf in plan</objection>"
    )
    assert r.blocked
    assert len(r.objections) == 2


def test_parse_skeptic_fail_closed():
    r = _parse_skeptic("I think it's fine")
    assert r.verdict == "object"
    assert r.blocked


def test_resolve_skeptic_sources(monkeypatch, tmp_path):
    monkeypatch.delenv("RELAY_SKEPTIC", raising=False)
    assert resolve_skeptic(None, config={}) is False
    assert resolve_skeptic(True) is True
    assert resolve_skeptic(False, config={"review": {"adversarial": True}}) is False
    monkeypatch.setenv("RELAY_SKEPTIC", "1")
    assert resolve_skeptic(None, config={}) is True


def test_skeptic_blocks_unresolved(tmp_path):
    """Scripted objection with no dismiss / no replan budget → skeptic_blocked."""
    client = RoutedClient(
        brain=[
            "<plan><step>create a.txt</step></plan>",
            "<verdict>object</verdict><objection>missing tests for a.txt</objection>",
        ],
        hands=[],  # must not execute
    )
    result = run_planned(
        "create a.txt",
        tmp_path,
        models=CFG,
        client=client,
        skeptic=True,
        max_plan_revisions=0,  # skip forced replan
        supervise=False,
    )
    assert result.status == STATUS_SKEPTIC_BLOCKED
    assert not (tmp_path / "a.txt").exists()
    assert any(e.kind == "skeptic_review" for e in result.events)
    assert skeptic_cost_usd(result.ledger) is not None
    assert any(
        getattr(r, "purpose", None) == SKEPTIC_PURPOSE for r in result.ledger.records
    )


def test_skeptic_user_dismiss(tmp_path):
    client = RoutedClient(
        brain=[
            "<plan><step>create a.txt with A</step></plan>",
            "<verdict>object</verdict><objection>no tests</objection>",
        ],
        hands=['<edit path="a.txt">A</edit>\n<done>wrote</done>'],
    )
    result = run_planned(
        "create a.txt",
        tmp_path,
        models=CFG,
        client=client,
        skeptic=True,
        max_plan_revisions=0,
        supervise=False,
        user_decision=lambda q: "dismiss",
    )
    assert result.status == STATUS_COMPLETED
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "A"
    assert any(e.kind == "skeptic_dismissed" for e in result.events)


def test_skeptic_replan_then_clear(tmp_path):
    client = RoutedClient(
        brain=[
            "<plan><step>create a.txt with A</step></plan>",
            "<verdict>object</verdict><objection>add a test step</objection>",
            # evolve_plan reply
            "<plan><step>create a.txt with A</step><step>create a_test.txt with TEST</step></plan>",
            # second skeptic
            "<verdict>clear</verdict>",
        ],
        hands=[
            '<edit path="a.txt">A</edit>\n<done>wrote a</done>',
            '<edit path="a_test.txt">TEST</edit>\n<done>wrote test</done>',
        ],
    )
    result = run_planned(
        "create a.txt",
        tmp_path,
        models=CFG,
        client=client,
        skeptic=True,
        max_plan_revisions=2,
        supervise=False,
    )
    assert result.status == STATUS_COMPLETED
    assert result.revisions >= 1
    assert any(e.kind == "skeptic_replan" for e in result.events)
    assert (tmp_path / "a_test.txt").exists()


def test_skeptic_clear_allows_run(tmp_path):
    client = RoutedClient(
        brain=[
            "<plan><step>create b.txt with B</step></plan>",
            "<verdict>clear</verdict>",
        ],
        hands=['<edit path="b.txt">B</edit>\n<done>ok</done>'],
    )
    result = run_planned(
        "create b.txt",
        tmp_path,
        models=CFG,
        client=client,
        skeptic=True,
        supervise=False,
    )
    assert result.status == STATUS_COMPLETED
    kinds = [e.kind for e in result.events]
    assert "skeptic_review" in kinds
    assert "skeptic_blocked" not in str(result.status)


def test_review_plan_adversarially_read_only(tmp_path):
    """Skeptic that tries to edit is refused by investigate; still verdicts."""
    client = RoutedClient(
        brain=[
            '<edit path="evil.txt">nope</edit>',
            "<verdict>clear</verdict>",
        ],
        hands=[],
    )
    ledger = Ledger()
    plan = Plan.from_instructions(["touch x"])
    review = review_plan_adversarially(
        "goal",
        plan,
        models=CFG,
        client=client,
        ledger=ledger,
        tools=None,  # no FS — edit refused either way
    )
    assert review.verdict == "clear"
    assert all(r.purpose == SKEPTIC_PURPOSE for r in ledger.records)


def test_skeptic_cost_helper():
    ledger = Ledger()
    ledger.add(
        CallRecord(
            role="brain", model="m", prompt_tokens=1, completion_tokens=1,
            latency_s=0.1, cost_usd=0.01, purpose=SKEPTIC_PURPOSE,
        )
    )
    ledger.add(
        CallRecord(
            role="brain", model="m", prompt_tokens=1, completion_tokens=1,
            latency_s=0.1, cost_usd=0.02, purpose=None,
        )
    )
    assert skeptic_cost_usd(ledger) == pytest.approx(0.01)
