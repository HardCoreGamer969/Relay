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


# --- v0.06: review_step / answer_or_escalate / evolve_plan ------------------

import relay.planner as planner_mod  # noqa: E402
from relay.memory import PlanMemory  # noqa: E402
from relay.planner import answer_or_escalate, evolve_plan, review_step  # noqa: E402


class _FakeBrain:
    """Records calls and returns a fixed reply as the brain's text."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def __call__(self, role, messages, **kwargs):
        self.calls.append((role, messages, kwargs))
        return SimpleNamespace(text=self.reply, record=None)


def _one_step_plan():
    return Plan.from_instructions(["do the thing"])


def test_review_accept_with_records(monkeypatch):
    monkeypatch.setattr(
        planner_mod, "call_model",
        _FakeBrain('<verdict>accept</verdict><record kind="fact">routes wired :: routes done</record>'),
    )
    plan = _one_step_plan()
    review = review_step("g", plan, plan.steps[0], "did it", ["[edit] wrote x"], PlanMemory(),
                         models=CFG, client=object())
    assert review.verdict == "accept"
    assert review.records == [("fact", "routes wired", "routes done")]


def test_review_follow_up(monkeypatch):
    monkeypatch.setattr(
        planner_mod, "call_model",
        _FakeBrain("<verdict>follow_up</verdict><followup>add the missing docstring</followup>"),
    )
    plan = _one_step_plan()
    review = review_step("g", plan, plan.steps[0], "did it", [], PlanMemory(), models=CFG, client=object())
    assert review.verdict == "follow_up"
    assert review.followup == "add the missing docstring"


def test_review_revise_plan(monkeypatch):
    monkeypatch.setattr(
        planner_mod, "call_model",
        _FakeBrain("<verdict>revise_plan</verdict><reason>discovered a config we must honor</reason>"),
    )
    plan = _one_step_plan()
    review = review_step("g", plan, plan.steps[0], "did it", [], PlanMemory(), models=CFG, client=object())
    assert review.verdict == "revise_plan"
    assert "config" in review.reason


def test_review_empty_followup_downgrades_to_accept(monkeypatch):
    monkeypatch.setattr(planner_mod, "call_model", _FakeBrain("<verdict>follow_up</verdict>"))
    plan = _one_step_plan()
    review = review_step("g", plan, plan.steps[0], "did it", [], PlanMemory(), models=CFG, client=object())
    assert review.verdict == "accept"  # unactionable follow-up -> accept


def test_review_unparseable_defaults_to_accept(monkeypatch):
    monkeypatch.setattr(planner_mod, "call_model", _FakeBrain("looks fine to me, nice work"))
    plan = _one_step_plan()
    review = review_step("g", plan, plan.steps[0], "did it", [], PlanMemory(), models=CFG, client=object())
    assert review.verdict == "accept"


def test_answer_self_answer(monkeypatch):
    monkeypatch.setattr(
        planner_mod, "call_model",
        _FakeBrain(
            "<decision>self_answer</decision><answer>reuse the existing db.py module</answer>"
            '<record kind="decision">reuse db.py :: reuse db</record>'
        ),
    )
    plan = _one_step_plan()
    res = answer_or_escalate("which db module?", "g", plan, plan.steps[0], PlanMemory(),
                             models=CFG, client=object())
    assert res.kind == "self_answer"
    assert res.answer == "reuse the existing db.py module"
    assert res.records == [("decision", "reuse db.py", "reuse db")]


def test_answer_escalate(monkeypatch):
    monkeypatch.setattr(
        planner_mod, "call_model",
        _FakeBrain("<decision>escalate</decision><ask_user>Should the app support OAuth login?</ask_user>"),
    )
    plan = _one_step_plan()
    res = answer_or_escalate("auth approach?", "g", plan, plan.steps[0], PlanMemory(),
                             models=CFG, client=object())
    assert res.kind == "escalate"
    assert res.question_for_user == "Should the app support OAuth login?"


def test_answer_unparseable_biases_to_escalate(monkeypatch):
    monkeypatch.setattr(planner_mod, "call_model", _FakeBrain("hmm, not totally sure about this"))
    plan = _one_step_plan()
    res = answer_or_escalate("the original question", "g", plan, plan.steps[0], PlanMemory(),
                             models=CFG, client=object())
    assert res.kind == "escalate"
    assert res.question_for_user == "the original question"  # falls back to the executor's question


def test_answer_self_answer_without_answer_tag_escalates(monkeypatch):
    monkeypatch.setattr(planner_mod, "call_model", _FakeBrain("<decision>self_answer</decision>"))
    plan = _one_step_plan()
    res = answer_or_escalate("q", "g", plan, plan.steps[0], PlanMemory(), models=CFG, client=object())
    assert res.kind == "escalate"  # claimed self-answer but gave none -> conservative escalate


def test_answer_reads_prior_decision_from_memory(monkeypatch):
    """Memory makes self-answers consistent: the prior decision reaches the brain."""
    mem = PlanMemory()
    mem.remember("decision", "storage backend is SQLite (chosen in step 1)", "chose SQLite",
                 provenance="step1", tags=["storage", "db"])
    captured = {}

    def fake(role, messages, **kwargs):
        captured["prompt"] = "\n".join(m["content"] for m in messages)
        return SimpleNamespace(text="<decision>self_answer</decision><answer>use SQLite</answer>", record=None)

    monkeypatch.setattr(planner_mod, "call_model", fake)
    plan = Plan.from_instructions(["build the storage layer"])
    res = answer_or_escalate("what storage backend should I use?", "build app", plan, plan.steps[0],
                             mem, models=CFG, client=object(), memory_budget_tokens=4000)
    assert "SQLite" in captured["prompt"]  # window-aware read delivered the prior decision
    assert res.kind == "self_answer" and "SQLite" in res.answer


def test_evolve_plan_returns_revised_tail(monkeypatch):
    monkeypatch.setattr(
        planner_mod, "call_model",
        _FakeBrain("<plan><step>new step A</step><step>new step B</step></plan>"),
    )
    plan = Plan.from_instructions(["done one", "old tail"])
    plan.mark_done(plan.steps[0], "did one")
    revised = evolve_plan("g", plan, "learned something", PlanMemory(), models=CFG, client=object())
    assert [s.instruction for s in revised.steps] == ["new step A", "new step B"]


def test_evolve_plan_abort_returns_none(monkeypatch):
    monkeypatch.setattr(planner_mod, "call_model", _FakeBrain("<abort>cannot continue</abort>"))
    revised = evolve_plan("g", Plan.from_instructions(["x"]), "r", PlanMemory(), models=CFG, client=object())
    assert revised is None


# --- v0.08: the assumption dial biases answer_or_escalate -------------------


class _DialHonoringBrain:
    """Simulates a model that obeys the dial: low dial -> self_answer (assume),
    high dial -> escalate (ask). Lets us prove the dial reaches the prompt AND
    changes the decision, network-free."""

    def __init__(self):
        self.prompts = []

    def __call__(self, role, messages, **kwargs):
        prompt = " ".join(m["content"] for m in messages)
        self.prompts.append(prompt)
        if "ASSUMPTION DIAL = 1" in prompt:
            text = "<decision>self_answer</decision><answer>assumed a sensible default</answer>"
        elif "ASSUMPTION DIAL = 5" in prompt:
            text = "<decision>escalate</decision><ask_user>which option do you want?</ask_user>"
        else:
            text = "<decision>self_answer</decision><answer>auto default</answer>"
        return SimpleNamespace(text=text, record=None)


def test_dial_threaded_into_answer_prompt(monkeypatch):
    brain = _DialHonoringBrain()
    monkeypatch.setattr(planner_mod, "call_model", brain)
    plan = _one_step_plan()
    answer_or_escalate("q?", "g", plan, plan.steps[0], PlanMemory(),
                       models=CFG, client=object(), assumption_level="5")
    assert "ASSUMPTION DIAL = 5" in brain.prompts[0]  # the dial reaches the prompt


def test_dial_biases_answer_decision(monkeypatch):
    monkeypatch.setattr(planner_mod, "call_model", _DialHonoringBrain())
    plan = _one_step_plan()

    low = answer_or_escalate("q?", "g", plan, plan.steps[0], PlanMemory(),
                             models=CFG, client=object(), assumption_level="1")
    high = answer_or_escalate("q?", "g", plan, plan.steps[0], PlanMemory(),
                              models=CFG, client=object(), assumption_level="5")

    assert low.kind == "self_answer"   # low dial -> the brain assumes
    assert high.kind == "escalate"     # high dial -> the brain asks the user

