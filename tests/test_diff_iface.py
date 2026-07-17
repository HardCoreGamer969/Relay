"""Hermetic tests for diff-as-interface (D3)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from relay.config import ModelConfig
from relay.diff_iface import (
    parse_diff_decision,
    resolve_confirm_diff,
    rewind_step_files,
    step_unified_diff,
    unified_diff_for_path,
)
from relay.orchestrator import STATUS_COMPLETED, run_planned
from relay.plan_fork import save_checkpoint
from relay.planner import Plan, PlanStep

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")


def _resp(content, cost=0.00001):
    usage = SimpleNamespace(
        prompt_tokens=4, completion_tokens=2, total_tokens=6, cost=cost
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=usage,
    )


class _Completions:
    def __init__(self, brain, hands):
        self.brain = list(brain)
        self.hands = list(hands)

    def create(self, *, model, **kwargs):
        queue = self.brain if model == "vendor/brain" else self.hands
        assert queue, f"ran out of replies for {model}"
        return _resp(queue.pop(0))


class RoutedClient:
    def __init__(self, brain=(), hands=()):
        self.chat = SimpleNamespace(completions=_Completions(brain, hands))


def test_parse_diff_decision():
    assert parse_diff_decision("y") == "accept"
    assert parse_diff_decision("accept") == "accept"
    assert parse_diff_decision("") == "accept"
    assert parse_diff_decision("reject") == "reject"
    assert parse_diff_decision("n") == "reject"


def test_resolve_confirm_diff(monkeypatch):
    monkeypatch.delenv("RELAY_CONFIRM_DIFF", raising=False)
    assert resolve_confirm_diff(None, config={}) is False
    assert resolve_confirm_diff(True) is True
    monkeypatch.setenv("RELAY_CONFIRM_DIFF", "1")
    assert resolve_confirm_diff(None, config={}) is True


def test_unified_diff_new_file():
    diff = unified_diff_for_path("a.txt", None, "hello\n")
    assert "a.txt" in diff or "/dev/null" in diff
    assert "+hello" in diff


def test_confirm_diff_accept(tmp_path):
    plan = Plan.from_instructions(["create a.txt with hello"])
    decisions = iter(["accept"])

    client = RoutedClient(
        brain=["<verdict>accept</verdict><reason>ok</reason>"],
        hands=['<write path="a.txt">hello</write>\n<done>wrote</done>'],
    )
    result = run_planned(
        "a",
        tmp_path,
        models=CFG,
        client=client,
        committed_plan=plan,
        supervise=True,
        confirm_diff=True,
        user_decision=lambda prompt: next(decisions),
        auto_checkpoint=False,
    )
    assert result.status == STATUS_COMPLETED
    assert (tmp_path / "a.txt").read_text() == "hello"
    assert any(e.kind == "diff_confirm" and e.payload.get("accepted") for e in result.events)


def test_confirm_diff_reject_replans(tmp_path):
    """Reject → failed step → replan → new step accepted."""
    plan = Plan.from_instructions(["create a.txt with bad"])
    n = {"i": 0}

    def decide(prompt):
        n["i"] += 1
        return "reject" if n["i"] == 1 else "accept"

    client = RoutedClient(
        brain=[
            "<verdict>accept</verdict><reason>ok</reason>",
            "<plan><step>create a.txt with good</step></plan>",
            "<verdict>accept</verdict><reason>ok</reason>",
        ],
        hands=[
            '<write path="a.txt">bad</write>\n<done>wrote bad</done>',
            '<read path="a.txt"/><write path="a.txt">good</write>\n<done>wrote good</done>',
        ],
    )
    result = run_planned(
        "a",
        tmp_path,
        models=CFG,
        client=client,
        committed_plan=plan,
        supervise=True,
        confirm_diff=True,
        user_decision=decide,
        auto_checkpoint=False,
        max_escalations=3,
    )
    assert result.status == STATUS_COMPLETED
    assert (tmp_path / "a.txt").read_text() == "good"
    assert any(
        e.kind == "diff_confirm" and e.payload.get("accepted") is False
        for e in result.events
    )


def test_step_unified_diff_from_snapshots(tmp_path):
    (tmp_path / "f.txt").write_text("old\n", encoding="utf-8")
    (tmp_path / "f.txt").write_text("new\n", encoding="utf-8")
    diff = step_unified_diff(tmp_path, ["f.txt"], {"f.txt": "old\n"})
    assert "-old" in diff
    assert "+new" in diff


def test_rewind_requires_git(tmp_path):
    plan = Plan(steps=[
        PlanStep(0, "write a.txt", status="done", outcome="ok"),
    ])
    save_checkpoint(tmp_path, plan, goal="g", step_touches={"0": ["a.txt"]})
    (tmp_path / "a.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(RuntimeError, match="git"):
        rewind_step_files(tmp_path, "step-0")
