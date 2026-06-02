"""Network-free tests for the planner (the brain).

A scripted client returns the brain's replies in order; no network is used.
"""

from __future__ import annotations

from types import SimpleNamespace

from relay.config import ModelConfig
from relay.planner import MAX_PLAN_STEPS, Plan, PlanStep, make_plan, replan
from relay.telemetry import Ledger

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")


def _resp(content):
    usage = SimpleNamespace(prompt_tokens=5, completion_tokens=5, total_tokens=10, cost=0.00001)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage)


class _Completions:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        assert self._replies, "scripted client ran out of replies"
        return _resp(self._replies.pop(0))


class ScriptedClient:
    def __init__(self, replies):
        self.chat = SimpleNamespace(completions=_Completions(replies))


def test_plan_parses_into_ordered_steps(tmp_path):
    client = ScriptedClient(["<plan><step>do A</step><step>do B</step><step>do C</step></plan>"])
    plan = make_plan("a goal", tmp_path, models=CFG, ledger=Ledger(), client=client)

    assert plan is not None
    assert [s.instruction for s in plan.steps] == ["do A", "do B", "do C"]
    assert [s.index for s in plan.steps] == [0, 1, 2]
    assert all(s.status == "pending" for s in plan.steps)


def test_brain_investigates_readonly_then_plans(tmp_path):
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    client = ScriptedClient(
        [
            '<list path="."/>',
            '<read path="main.py"/>',
            "<plan><step>add a docstring to main.py</step></plan>",
        ]
    )
    plan = make_plan("document main.py", tmp_path, models=CFG, ledger=Ledger(), client=client)

    assert plan is not None
    assert [s.instruction for s in plan.steps] == ["add a docstring to main.py"]
    assert len(client.chat.completions.calls) == 3  # two investigations + the plan


def test_brain_cannot_edit_or_bash_during_planning(tmp_path):
    client = ScriptedClient(
        [
            '<edit path="x.txt">should not be written</edit>\n<bash>touch y.txt</bash>',
            "<plan><step>do the real thing</step></plan>",
        ]
    )
    plan = make_plan("g", tmp_path, models=CFG, ledger=Ledger(), client=client)

    assert plan is not None
    # The read-only planner's write/exec attempts must not have run.
    assert not (tmp_path / "x.txt").exists()
    assert not (tmp_path / "y.txt").exists()
    # And it was told it is read-only, fed back before its next turn.
    second_turn = client.chat.completions.calls[1]["messages"]
    joined = " ".join(m["content"] for m in second_turn)
    assert "READ-ONLY" in joined


def test_planning_fails_when_no_plan_parses(tmp_path):
    client = ScriptedClient(["just chatting", "still no plan", "nope", "nope", "nope"])
    plan = make_plan("g", tmp_path, models=CFG, ledger=Ledger(), client=client, max_plan_retries=2)
    assert plan is None  # bounded retries, no infinite loop


def test_brain_abort_during_planning_returns_none(tmp_path):
    client = ScriptedClient(["<abort>this goal is incoherent</abort>"])
    plan = make_plan("g", tmp_path, models=CFG, ledger=Ledger(), client=client)
    assert plan is None


def test_plan_step_count_is_capped(tmp_path):
    many = "".join(f"<step>step {i}</step>" for i in range(30))
    client = ScriptedClient([f"<plan>{many}</plan>"])
    plan = make_plan("g", tmp_path, models=CFG, ledger=Ledger(), client=client)
    assert plan is not None
    assert len(plan.steps) == MAX_PLAN_STEPS


def test_replan_returns_revised_remaining_plan(tmp_path):
    plan = Plan.from_instructions(["a", "b"])
    plan.mark_done(plan.steps[0], "did a")
    failed = plan.steps[1]
    plan.mark_failed(failed, "blocked: ran into a wall")

    client = ScriptedClient(["<plan><step>b-prime</step><step>c</step></plan>"])
    revised = replan(
        "g", plan, failed, "blocked: ran into a wall", plan.completed_outcomes(),
        models=CFG, ledger=Ledger(), client=client,
    )

    assert revised is not None
    assert [s.instruction for s in revised.steps] == ["b-prime", "c"]


def test_replan_abort_returns_none(tmp_path):
    failed = PlanStep(index=0, instruction="a", status="failed")
    client = ScriptedClient(["<abort>cannot recover from here</abort>"])
    revised = replan("g", Plan(steps=[failed]), failed, "boom", [], models=CFG, ledger=Ledger(), client=client)
    assert revised is None
